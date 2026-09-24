"""
Carga xG a nivel de partido desde Understat (via soccerdata) y lo une
con los datos de football-data.co.uk para conservar las cuotas.

Requiere:  python -m pip install soccerdata

Uso:
    python xg_loader.py --liga premier --temporadas 2324 2425 2526
    python xg_loader.py --todas --temporadas 2425 --refrescar

Deja un CSV normalizado en cache/xg_<liga>_<temporada>.csv, asi que
despues de la primera corrida no vuelve a necesitar soccerdata ni red.
"""

import argparse
import csv
import difflib
import json
import os
import threading
from datetime import datetime

from data_loader import LIGAS, CACHE_DIR, descargar_csv, leer_partidos, parsear_fecha
from logging_setup import get_logger
from red_understat import blindar
from validacion import validar_lote

logger = get_logger("xg_loader")

# Reintentos con backoff exponencial para la llamada de red a Understat.
# soccerdata a veces devuelve un error transitorio (rate limit, timeout)
# en la llamada de resumen por equipo, igual que le pasa a bajar_jugadores.
RED_REINTENTOS = 4
RED_ESPERA_BASE = 5.0  # segundos: 5, 10, 20, 40...

# Una sola descarga de Understat a la vez por proceso. En la web conviven
# el hilo de actualizacion en segundo plano y las cargas que dispara un
# usuario: sin esto ambos podrian scrapear lo mismo en paralelo (doble
# trafico contra el rate limit) y pisarse el CSV del cache.
_LOCK_DESCARGA = threading.Lock()

# ALIAS puede extenderse sin tocar codigo: si existe este archivo junto al
# script, sus entradas se mezclan con el diccionario hardcodeado de abajo
# (y lo pisan en caso de choque). Asi un equipo recien ascendido se agrega
# editando un JSON en vez de esperar un deploy de xg_loader.py.
RUTA_ALIAS_EXTRA = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "alias_equipos.json")

# Ligas de Understat <-> claves nuestras
LIGAS_UNDERSTAT = {
    "premier": "ENG-Premier League",
    "laliga": "ESP-La Liga",
    "bundesliga": "GER-Bundesliga",
    "ligue1": "FRA-Ligue 1",
}

# ----------------------------------------------------------------------
# MAPEO DE NOMBRES  (Understat -> football-data.co.uk)
# ----------------------------------------------------------------------
# Este es el team_source_map del que depende todo. Si un equipo asciende
# y no esta aqui, el codigo FALLA en vez de descartar partidos en silencio.
ALIAS = {
    # Premier League
    "Manchester City": "Man City",
    "Manchester United": "Man United",
    "Newcastle United": "Newcastle",
    "Nottingham Forest": "Nott'm Forest",
    "Wolverhampton Wanderers": "Wolves",
    # LaLiga
    "Athletic Club": "Ath Bilbao",
    "Atletico Madrid": "Ath Madrid",
    "Celta Vigo": "Celta",
    "Espanyol": "Espanol",
    "Rayo Vallecano": "Vallecano",
    "Real Betis": "Betis",
    "Real Oviedo": "Oviedo",
    "Real Sociedad": "Sociedad",
    "Real Valladolid": "Valladolid",
    # Bundesliga
    "Bayer Leverkusen": "Leverkusen",
    "Borussia Dortmund": "Dortmund",
    "Borussia M.Gladbach": "M'gladbach",
    "Eintracht Frankfurt": "Ein Frankfurt",
    "FC Cologne": "FC Koln",
    "FC Heidenheim": "Heidenheim",
    "Hamburger SV": "Hamburg",
    "Mainz 05": "Mainz",
    "RasenBallsport Leipzig": "RB Leipzig",
    "St. Pauli": "St Pauli",
    "VfB Stuttgart": "Stuttgart",
}


_alias_cache = None


def _alias_completo():
    """ALIAS hardcodeado + alias_equipos.json (si existe), este ultimo pisa
    al primero en caso de choque.

    Cachea en memoria de proceso: el JSON se lee una sola vez por corrida,
    no una vez por equipo. Un ascenso nuevo se resuelve agregando una
    linea a alias_equipos.json, sin tocar xg_loader.py ni redeployar.
    """
    global _alias_cache
    if _alias_cache is not None:
        return _alias_cache

    combinado = dict(ALIAS)
    if os.path.exists(RUTA_ALIAS_EXTRA):
        try:
            with open(RUTA_ALIAS_EXTRA, encoding="utf-8") as f:
                extra = json.load(f)
            combinado.update(extra)
            logger.info("cargados %s alias extra desde %s", len(extra), RUTA_ALIAS_EXTRA)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("no se pudo leer %s (%s); se ignora", RUTA_ALIAS_EXTRA, e)

    _alias_cache = combinado
    return combinado


def normalizar(nombre, conocidos):
    """Understat -> nombre canonico de football-data.

    Orden: alias explicito (ALIAS + alias_equipos.json) -> coincidencia
    exacta -> error. No hacemos fuzzy matching AUTOMATICO: preferimos
    fallar ruidosamente antes que unir 'Leganes' con 'Leverkusen' por
    parecido. Pero el mensaje de error SI sugiere el nombre mas parecido
    (difflib), para que agregar el alias correcto a alias_equipos.json
    sea cosa de copiar/pegar en vez de adivinar.
    """
    nombre = nombre.strip()
    alias = _alias_completo()
    if nombre in alias:
        return alias[nombre]
    if nombre in conocidos:
        return nombre

    sugerencias = difflib.get_close_matches(nombre, conocidos, n=3, cutoff=0.5)
    sugerencia_txt = (
        "\n  Los mas parecidos en football-data: {}".format(", ".join(sugerencias))
        if sugerencias else "")
    raise SystemExit(
        "  [error] equipo sin mapear: '{}'\n"
        "  Agregalo a alias_equipos.json (o al diccionario ALIAS en xg_loader.py):\n"
        '    {{"...": "..."}}  ej: {{"{}" : "{}"}}\n'
        "  Nombres disponibles en football-data: {}{}".format(
            nombre, nombre, sugerencias[0] if sugerencias else "???",
            ", ".join(sorted(conocidos)), sugerencia_txt))


def diagnosticar_alias(ligas, temporadas):
    """Compara nombres Understat vs football-data ANTES de procesar, y
    reporta que equipos no tienen alias, con la sugerencia mas parecida.

    Pensado para correr proactivamente (--diagnostico) antes de una
    temporada nueva, en vez de descubrir el equipo sin mapear a mitad
    de una descarga de --jugadores que tarda minutos.
    """
    alias = _alias_completo()
    problemas = []
    for liga in ligas:
        _, nombres_fd = indexar_football_data(liga, temporadas)
        filas_us = bajar_understat([liga], temporadas)
        vistos = set()
        for f in filas_us:
            for campo in ("local_us", "visita_us"):
                nombre = f[campo].strip()
                if nombre in vistos:
                    continue
                vistos.add(nombre)
                if nombre in alias or nombre in nombres_fd:
                    continue
                sugerencias = difflib.get_close_matches(nombre, nombres_fd, n=1, cutoff=0.5)
                problemas.append((liga, nombre, sugerencias[0] if sugerencias else None))

    if not problemas:
        print("  [ok] todos los equipos de Understat tienen mapeo a football-data")
        return problemas

    print("  {} equipos sin mapear:".format(len(problemas)))
    for liga, nombre, sugerencia in problemas:
        if sugerencia:
            print("    [{}] '{}'  ->  sugerido: '{}'".format(liga, nombre, sugerencia))
        else:
            print("    [{}] '{}'  ->  sin sugerencia clara".format(liga, nombre))
    print("\n  Agrega las que correspondan a alias_equipos.json, ej:")
    print("    {{ \"{}\": \"{}\" }}".format(
        problemas[0][1], problemas[0][2] or "NOMBRE_EN_FOOTBALL_DATA"))
    return problemas


# ----------------------------------------------------------------------
# DESCARGA DESDE UNDERSTAT
# ----------------------------------------------------------------------

def _importar_soccerdata():
    """import soccerdata, con su log callado.

    soccerdata configura el logger RAIZ al importarse y escupe decenas de
    lineas por llamada. Subimos el nivel del raiz en vez de usar
    logging.disable(): disable() apaga TODOS los loggers del proceso, y en
    la app web (proceso de larga vida) eso silenciaba para siempre los
    logs propios tras la primera descarga. Los loggers de logging_setup no
    propagan al raiz, asi que no les afecta.
    """
    import logging
    import warnings
    warnings.filterwarnings("ignore")
    try:
        import soccerdata as sd
    except ImportError:
        raise SystemExit("  [error] falta soccerdata.\n"
                         "  Instalalo con: python -m pip install soccerdata")
    logging.getLogger().setLevel(logging.CRITICAL)
    return sd


def bajar_understat(ligas, temporadas):
    """Trae xG por partido. Devuelve {(liga, temporada): [filas]}.

    soccerdata cachea en ~/soccerdata/, asi que la segunda llamada
    no vuelve a scrapear. Reintenta con backoff exponencial ante fallos
    de red: la misma disciplina que bajar_jugadores(), porque esta
    llamada tambien puede toparse con un rate-limit o timeout puntual
    de Understat.
    """
    import time
    sd = _importar_soccerdata()

    nombres_us = [LIGAS_UNDERSTAT[l] for l in ligas]
    print("  [understat] descargando {} | {}".format(
        ", ".join(ligas), ", ".join(temporadas)))

    us = blindar(sd.Understat(leagues=nombres_us, seasons=list(temporadas)))

    df = None
    ultimo_error = None
    for intento in range(RED_REINTENTOS):
        try:
            df = us.read_team_match_stats().reset_index()
            break
        except Exception as e:
            ultimo_error = e
            if intento < RED_REINTENTOS - 1:
                espera = RED_ESPERA_BASE * (2 ** intento)
                logger.warning("fallo al leer Understat (%s); reintento en %.0fs",
                               str(e)[:100], espera)
                time.sleep(espera)
    if df is None:
        raise SystemExit("  [error] no se pudo leer Understat tras {} intentos\n  {}".format(
            RED_REINTENTOS, ultimo_error))

    def num(valor, por_defecto=0.0):
        """pandas devuelve NAType en celdas vacias; float() revienta con eso."""
        try:
            v = float(valor)
        except (TypeError, ValueError):
            return por_defecto
        return por_defecto if v != v else v  # NaN no es igual a si mismo

    inverso = {v: k for k, v in LIGAS_UNDERSTAT.items()}
    filas, descartados = [], 0
    for r in df.itertuples(index=False):
        # Sin goles o sin xG el partido no sirve: probablemente no se jugo
        if num(r.home_goals, -1) < 0 or num(r.home_xg, -1) < 0:
            descartados += 1
            continue
        filas.append({
            "liga": inverso[r.league],
            "temporada": str(r.season),
            "game_id": int(r.game_id),
            "fecha": r.date.to_pydatetime() if hasattr(r.date, "to_pydatetime") else r.date,
            "local_us": r.home_team,
            "visita_us": r.away_team,
            "goles_local": int(num(r.home_goals)),
            "goles_visita": int(num(r.away_goals)),
            "xg_local": num(r.home_xg),
            "xg_visita": num(r.away_xg),
            "npxg_local": num(r.home_np_xg),
            "npxg_visita": num(r.away_np_xg),
            "ppda_local": num(r.home_ppda),
            "ppda_visita": num(r.away_ppda),
            "deep_local": int(num(r.home_deep_completions)),
            "deep_visita": int(num(r.away_deep_completions)),
        })
    if descartados:
        print("  [aviso] {} filas de Understat sin resultado, descartadas".format(descartados))
    return filas


# ----------------------------------------------------------------------
# UNION CON football-data.co.uk (para conservar cuotas y tiros)
# ----------------------------------------------------------------------

def indexar_football_data(liga, temporadas):
    """{(fecha_dia, equipo_local): partido} desde los CSV de football-data."""
    codigo, _ = LIGAS[liga]
    indice, nombres = {}, set()
    for temp in temporadas:
        ruta = descargar_csv(codigo, temp)
        for p in leer_partidos(ruta):
            indice[(p["fecha"].date(), p["local"])] = p
            nombres.add(p["local"])
            nombres.add(p["visita"])
        _cargar_cuotas(ruta, indice)
    return indice, nombres


def _cargar_cuotas(ruta, indice):
    """Pega B365H/D/A a cada partido ya indexado."""
    with open(ruta, "rb") as f:
        crudo = f.read()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            texto = crudo.decode(enc)
            break
        except UnicodeDecodeError:
            continue

    for fila in csv.DictReader(texto.splitlines()):
        fecha = parsear_fecha(fila.get("Date") or "")
        local = (fila.get("HomeTeam") or "").strip()
        if not fecha or not local:
            continue
        clave = (fecha.date(), local)
        if clave not in indice:
            continue
        try:
            indice[clave]["cuotas"] = (float(fila["B365H"]), float(fila["B365D"]),
                                       float(fila["B365A"]))
        except (KeyError, ValueError, TypeError):
            indice[clave].setdefault("cuotas", None)


def unir(filas_us, liga, temporadas):
    """Une Understat con football-data por (fecha, equipo local).

    Understat y football-data a veces difieren un dia en la fecha
    (husos horarios, partidos de medianoche), asi que probamos -1/+1.
    """
    from datetime import timedelta

    indice, nombres_fd = indexar_football_data(liga, temporadas)
    unidos, huerfanos = [], []

    for f in filas_us:
        if f["liga"] != liga:
            continue
        local = normalizar(f["local_us"], nombres_fd)
        visita = normalizar(f["visita_us"], nombres_fd)

        base = f["fecha"].date()
        fd = None
        for delta in (0, -1, 1):
            fd = indice.get((base + timedelta(days=delta), local))
            if fd:
                break

        if fd is None:
            huerfanos.append("{} {} vs {}".format(base, local, visita))
            continue

        unidos.append({
            "fecha": fd["fecha"],
            "game_id": f["game_id"],
            "liga": liga,
            "local": local,
            "visita": visita,
            "goles_local": f["goles_local"],
            "goles_visita": f["goles_visita"],
            "xg_local": f["xg_local"],
            "xg_visita": f["xg_visita"],
            "npxg_local": f["npxg_local"],
            "npxg_visita": f["npxg_visita"],
            "tiros_local": fd.get("tiros_local", 0),
            "tiros_visita": fd.get("tiros_visita", 0),
            "sot_local": fd.get("sot_local", 0),
            "sot_visita": fd.get("sot_visita", 0),
            "cuotas": fd.get("cuotas"),
        })

    if huerfanos:
        print("  [aviso] {} partidos de Understat sin pareja en football-data".format(
            len(huerfanos)))
        for h in huerfanos[:5]:
            print("          {}".format(h))

    validados, reporte = validar_lote(unidos, logger=logger)
    if reporte["descartados"] or reporte["duplicados"]:
        logger.warning("union Understat+football-data (%s): %s descartados, "
                       "%s duplicados de %s", liga, reporte["descartados"],
                       reporte["duplicados"], reporte["total"])
    return validados


# ----------------------------------------------------------------------
# CACHE LOCAL
# ----------------------------------------------------------------------

CAMPOS = ["fecha", "game_id", "liga", "local", "visita", "goles_local", "goles_visita",
          "xg_local", "xg_visita", "npxg_local", "npxg_visita",
          "tiros_local", "tiros_visita", "sot_local", "sot_visita",
          "cuota_h", "cuota_d", "cuota_a"]


def ruta_cache(liga, temporadas):
    return os.path.join(CACHE_DIR, "xg_{}_{}.csv".format(liga, "-".join(sorted(temporadas))))


def guardar(partidos, ruta):
    os.makedirs(CACHE_DIR, exist_ok=True)
    # Se escribe a un temporal y se renombra: la app puede estar leyendo
    # este CSV mientras el hilo de actualizacion lo regenera, y nunca
    # debe ver un archivo a medio escribir.
    tmp = ruta + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS)
        w.writeheader()
        for p in partidos:
            fila = {c: p.get(c) for c in CAMPOS if c not in ("cuota_h", "cuota_d", "cuota_a")}
            fila["fecha"] = p["fecha"].strftime("%Y-%m-%d")
            c = p.get("cuotas")
            fila["cuota_h"], fila["cuota_d"], fila["cuota_a"] = c if c else ("", "", "")
            w.writerow(fila)
    os.replace(tmp, ruta)
    print("  [guardado] {} ({} partidos)".format(ruta, len(partidos)))


def leer_cache(ruta):
    partidos = []
    with open(ruta, encoding="utf-8") as f:
        for fila in csv.DictReader(f):
            try:
                cuotas = (float(fila["cuota_h"]), float(fila["cuota_d"]), float(fila["cuota_a"]))
            except (ValueError, KeyError):
                cuotas = None
            partidos.append({
                "fecha": datetime.strptime(fila["fecha"], "%Y-%m-%d"),
                "game_id": int(fila["game_id"]),
                "liga": fila["liga"],
                "local": fila["local"],
                "visita": fila["visita"],
                "goles_local": int(fila["goles_local"]),
                "goles_visita": int(fila["goles_visita"]),
                "xg_local": float(fila["xg_local"]),
                "xg_visita": float(fila["xg_visita"]),
                "npxg_local": float(fila["npxg_local"]),
                "npxg_visita": float(fila["npxg_visita"]),
                "tiros_local": int(fila["tiros_local"] or 0),
                "tiros_visita": int(fila["tiros_visita"] or 0),
                "sot_local": int(fila["sot_local"] or 0),
                "sot_visita": int(fila["sot_visita"] or 0),
                "cuotas": cuotas,
            })
    return partidos


# ----------------------------------------------------------------------
# API PRINCIPAL
# ----------------------------------------------------------------------

def cargar_xg(ligas, temporadas, refrescar=False):
    """Devuelve la lista de partidos con xG, ordenada cronologicamente.

    Lee del cache local salvo que pidas --refrescar. Solo llama a
    soccerdata cuando falta algun archivo.
    """
    def faltantes():
        return [l for l in ligas
                if refrescar or not os.path.exists(ruta_cache(l, temporadas))]

    if faltantes():
        with _LOCK_DESCARGA:
            # Se recalcula dentro del lock: si otro hilo acaba de bajar
            # esta misma liga mientras esperabamos, no se repite.
            pendientes = faltantes()
            if pendientes:
                filas_us = bajar_understat(pendientes, temporadas)
                for liga in pendientes:
                    guardar(unir(filas_us, liga, temporadas), ruta_cache(liga, temporadas))

    todos = []
    for liga in ligas:
        todos.extend(leer_cache(ruta_cache(liga, temporadas)))
    todos.sort(key=lambda p: (p["fecha"], p["local"]))
    return todos


# ----------------------------------------------------------------------
# JUGADORES: quien jugo cada partido, con sus minutos y su xG
# ----------------------------------------------------------------------

CAMPOS_JUG = ["game_id", "liga", "equipo", "jugador", "posicion",
              "minutos", "xg", "xa"]


def ruta_cache_jug(liga, temporadas):
    return os.path.join(CACHE_DIR, "jug_{}_{}.csv".format(
        liga, "-".join(sorted(temporadas))))


def _ya_descargados(ruta):
    """game_ids que ya estan en el CSV parcial, para poder reanudar."""
    if not os.path.exists(ruta):
        return set()
    with open(ruta, encoding="utf-8") as f:
        return {int(fila["game_id"]) for fila in csv.DictReader(f)}


def bajar_jugadores(liga, temporadas, ruta, lote=10, pausa=6.0, reintentos=6):
    """Descarga las stats por jugador y partido, de forma resistente.

    Understat sirve un partido por request y corta la conexion si lo
    aprietas: hace falta paciencia y reintentos. Tres defensas:

      1. Guardado INCREMENTAL en disco tras cada lote. Si se cae, no se
         pierde lo bajado.
      2. Reanudacion: al relanzar salta los game_id que ya estan en el CSV.
      3. Backoff exponencial ante error de red (5s a 160s).

    Understat limita agresivamente pasados ~150 partidos seguidos.
    Por eso el ritmo por defecto es lento a proposito (lotes de 10
    con 6s de pausa): ir suave sale mas rapido que chocar contra el
    limite y comerse backoffs de 160s.

    Ademas soccerdata cachea cada partido en ~/soccerdata/, asi que un
    reintento sobre algo ya bajado no vuelve a salir a la red.
    """
    import time
    sd = _importar_soccerdata()

    os.makedirs(CACHE_DIR, exist_ok=True)
    us = blindar(sd.Understat(leagues=[LIGAS_UNDERSTAT[liga]], seasons=list(temporadas)))
    ids = sorted(set(us.read_schedule().reset_index().game_id.dropna().astype(int)))

    hechos = _ya_descargados(ruta)
    pendientes = [i for i in ids if i not in hechos]
    print("  [jugadores] {}: {} partidos ({} ya en cache, {} pendientes)".format(
        liga, len(ids), len(hechos), len(pendientes)), flush=True)

    nuevo = not os.path.exists(ruta)
    f = open(ruta, "a", newline="", encoding="utf-8")
    escritor = csv.DictWriter(f, fieldnames=CAMPOS_JUG)
    if nuevo:
        escritor.writeheader()

    fallidos = 0
    try:
        for i in range(0, len(pendientes), lote):
            trozo = pendientes[i:i + lote]
            df = None
            for intento in range(reintentos):
                try:
                    df = us.read_player_match_stats(match_id=trozo).reset_index()
                    break
                except Exception as e:
                    espera = 5 * (2 ** intento)
                    print("    [red] {} | reintento en {}s".format(
                        str(e)[:70], espera), flush=True)
                    time.sleep(espera)

            if df is None:
                fallidos += len(trozo)
                print("    [salto] lote de {} partidos sin bajar".format(len(trozo)),
                      flush=True)
                continue

            # El blindaje de red salta (en vez de abortar el lote) los
            # partidos que agotan reintentos: se cuentan para el aviso final
            # y, al no quedar en el CSV, la proxima corrida los reintenta.
            bajados = set(df.game_id.astype(int)) if "game_id" in df.columns else set()
            fallidos += len(set(trozo) - bajados)
            for r in df.itertuples(index=False):
                escritor.writerow({
                    "game_id": int(r.game_id),
                    "liga": liga,
                    "equipo": r.team,
                    "jugador": r.player,
                    "posicion": str(r.position),
                    "minutos": int(r.minutes) if r.minutes == r.minutes else 0,
                    "xg": float(r.xg) if r.xg == r.xg else 0.0,
                    "xa": float(r.xa) if r.xa == r.xa else 0.0,
                })
            f.flush()
            print("    {}/{} pendientes".format(min(i + lote, len(pendientes)),
                                                len(pendientes)), flush=True)
            time.sleep(pausa)
    finally:
        f.close()

    if fallidos:
        print("  [aviso] {} partidos no bajados. Relanza el comando para "
              "reintentarlos.".format(fallidos), flush=True)
    print("  [guardado] {}".format(ruta), flush=True)


def cargar_jugadores(ligas, temporadas, refrescar=False):
    """Devuelve {game_id: {equipo_canonico: [jugadores]}}.

    Cada jugador es un dict con jugador/posicion/minutos/xg/xa.
    Los nombres de equipo ya vienen normalizados a football-data.
    """
    por_partido = {}
    for liga in ligas:
        ruta = ruta_cache_jug(liga, temporadas)
        if refrescar or not os.path.exists(ruta):
            bajar_jugadores(liga, temporadas, ruta)

        _, nombres_fd = indexar_football_data(liga, temporadas)
        with open(ruta, encoding="utf-8") as f:
            for fila in csv.DictReader(f):
                gid = int(fila["game_id"])
                equipo = normalizar(fila["equipo"], nombres_fd)
                por_partido.setdefault(gid, {}).setdefault(equipo, []).append({
                    "jugador": fila["jugador"],
                    "posicion": fila["posicion"],
                    "minutos": int(fila["minutos"]),
                    "xg": float(fila["xg"]),
                    "xa": float(fila["xa"]),
                })
    return por_partido


# ----------------------------------------------------------------------
# CLI / DIAGNOSTICO
# ----------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Carga xG de Understat")
    ap.add_argument("--liga", default="premier", choices=list(LIGAS))
    ap.add_argument("--todas", action="store_true")
    ap.add_argument("--temporadas", nargs="+", default=["2324", "2425", "2526"])
    ap.add_argument("--refrescar", action="store_true", help="ignora el cache local")
    ap.add_argument("--jugadores", action="store_true",
                    help="descarga tambien las stats por jugador (tarda minutos)")
    ap.add_argument("--diagnostico", action="store_true",
                    help="lista equipos de Understat sin alias a football-data y sale, "
                         "sin descargar nada mas. Correr antes de una temporada nueva.")
    args = ap.parse_args()

    ligas = list(LIGAS) if args.todas else [args.liga]

    if args.diagnostico:
        diagnosticar_alias(ligas, args.temporadas)
        return

    if args.jugadores:
        for liga in ligas:
            ruta = ruta_cache_jug(liga, args.temporadas)
            if args.refrescar and os.path.exists(ruta):
                os.remove(ruta)
            # Siempre llamamos: si ya esta completo no baja nada, y si
            # quedaron partidos sueltos de una corrida anterior, los reintenta.
            bajar_jugadores(liga, args.temporadas, ruta)
        return
    partidos = cargar_xg(ligas, args.temporadas, refrescar=args.refrescar)

    print("\n" + "=" * 70)
    print("  {} partidos | {} | {}".format(
        len(partidos), "+".join(ligas), " ".join(args.temporadas)))
    print("=" * 70)

    n = len(partidos)
    gl = sum(p["goles_local"] for p in partidos) / n
    gv = sum(p["goles_visita"] for p in partidos) / n
    xl = sum(p["xg_local"] for p in partidos) / n
    xv = sum(p["xg_visita"] for p in partidos) / n
    con_cuota = sum(1 for p in partidos if p["cuotas"])

    print("  {:<22} {:>8} {:>8}".format("", "LOCAL", "VISITA"))
    print("  {:<22} {:>8.3f} {:>8.3f}".format("goles por partido", gl, gv))
    print("  {:<22} {:>8.3f} {:>8.3f}".format("xG por partido", xl, xv))
    print("  {:<22} {:>8.3f} {:>8.3f}".format("ratio goles/xG", gl / xl, gv / xv))
    print("\n  partidos con cuotas de Bet365: {}/{}".format(con_cuota, n))

    print("\n  ULTIMOS 5 PARTIDOS")
    for p in partidos[-5:]:
        print("  {}  {:<16} {}-{}  {:<16}   xG {:.2f}-{:.2f}".format(
            p["fecha"].strftime("%Y-%m-%d"), p["local"][:16],
            p["goles_local"], p["goles_visita"], p["visita"][:16],
            p["xg_local"], p["xg_visita"]))


if __name__ == "__main__":
    main()

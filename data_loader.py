"""
Carga datos reales de football-data.co.uk y los convierte en objetos
Liga / Equipo listos para poisson_model.py

Uso rapido:
    python data_loader.py --liga premier --local Arsenal --visita Chelsea
    python data_loader.py --liga laliga --tabla

Sin dependencias externas: solo stdlib.
"""

import argparse
import csv
import os
import time
import urllib.request
from datetime import datetime
from math import exp, log

from poisson_model import (Liga, Equipo, predecir, imprimir,
                           K_SHRINK, K_SHRINK_XG, RHO)
from logging_setup import get_logger
from validacion import validar_lote, validar_esquema
import config_modelo

logger = get_logger("data_loader")

BASE_URL = "https://www.football-data.co.uk/mmz4281/{temporada}/{codigo}.csv"
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")

# Reintentos con backoff exponencial ante fallos de red al descargar.
# football-data.co.uk no tiene SLA: un timeout puntual no deberia tirar
# abajo todo el pipeline si un segundo intento alcanza.
DESCARGA_REINTENTOS = 4
DESCARGA_ESPERA_BASE = 2.0  # segundos: 2, 4, 8, 16...

# Velocidad del decaimiento temporal, por dia.
# peso = exp(-XI * dias_de_antiguedad)
# 0.001 -> un partido pierde la mitad de su peso cada ~693 dias
# 0     -> sin decaimiento (todos los partidos pesan igual)
# Valor calibrado con backtest.py. La superficie es plana entre 0 y 0.002:
# con goles crudos conviene poco decaimiento, porque la senal es ruidosa
# y compensa acumular mas muestra.
XI = 0.001

# Correccion Dixon-Coles por liga. El sesgo de empates no es igual en
# las tres: la Bundesliga, mas goleadora, es la que mas lo sufre (-4.5%
# sin corregir) y LaLiga la que menos (-1.0%).
# Son estimaciones sobre ~950 partidos por liga: la direccion es solida,
# el valor exacto no. Si dudas, usa el agrupado (RHO = -0.12).
# ligue1 todavia no tiene backtest --dixon-coles propio: usa el agrupado
# (RHO) hasta que se calibre, en vez de inventar un numero.
RHO_LIGA = {"premier": -0.12, "laliga": -0.06, "bundesliga": -0.18}

# Codigos de division de football-data.co.uk para nuestras ligas
LIGAS = {
    "premier": ("E0", "Premier League"),
    "laliga": ("SP1", "LaLiga"),
    "bundesliga": ("D1", "Bundesliga"),
    "ligue1": ("F1", "Ligue 1"),
}


# ----------------------------------------------------------------------
# 1. DESCARGA (con cache en disco)
# ----------------------------------------------------------------------

def temporada_por_defecto():
    """Devuelve la ultima temporada COMPLETA en formato 'YYYY' -> '2526'.

    Las ligas europeas arrancan en agosto. Si estamos en o despues de
    agosto, la temporada en curso es la actual y la ultima completa es
    la anterior.
    """
    hoy = datetime.now()
    anio_inicio = hoy.year - 1 if hoy.month >= 8 else hoy.year - 2
    return "{:02d}{:02d}".format(anio_inicio % 100, (anio_inicio + 1) % 100)


def descargar_csv(codigo, temporada, forzar=False):
    """Baja el CSV y lo cachea. Devuelve la ruta local.

    El cache evita martillar el servidor mientras desarrollas: una vez
    bajado, las siguientes ejecuciones leen del disco.

    Reintenta con backoff exponencial ante fallos de red (timeout, 5xx):
    un problema transitorio del servidor no deberia abortar todo el
    pipeline si el segundo o tercer intento pasa.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    destino = os.path.join(CACHE_DIR, "{}_{}.csv".format(codigo, temporada))

    if os.path.exists(destino) and not forzar:
        return destino

    url = BASE_URL.format(temporada=temporada, codigo=codigo)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})

    ultimo_error = None
    for intento in range(DESCARGA_REINTENTOS):
        try:
            logger.info("descargando %s (intento %s/%s)", url, intento + 1,
                     DESCARGA_REINTENTOS)
            datos = urllib.request.urlopen(req, timeout=30).read()
            # Temporal + rename: la app puede estar leyendo este CSV
            # mientras el hilo de actualizacion lo vuelve a bajar.
            with open(destino + ".tmp", "wb") as f:
                f.write(datos)
            os.replace(destino + ".tmp", destino)
            return destino
        except Exception as e:
            ultimo_error = e
            if intento < DESCARGA_REINTENTOS - 1:
                espera = DESCARGA_ESPERA_BASE * (2 ** intento)
                logger.warning("fallo al descargar %s (%s); reintento en %.0fs",
                           url, e, espera)
                time.sleep(espera)

    raise SystemExit("  [error] no se pudo descargar {} tras {} intentos\n  {}".format(
        url, DESCARGA_REINTENTOS, ultimo_error))


# ----------------------------------------------------------------------
# 2. PARSEO
# ----------------------------------------------------------------------

_CACHE_PARTIDOS = {}  # ruta -> (mtime, partidos) : evita re-parsear el mismo CSV


def leer_partidos(ruta, usar_cache=True):
    """Lee el CSV y devuelve solo los partidos ya jugados y validados.

    Columnas que usamos:
      HomeTeam, AwayTeam  -> nombres
      FTHG, FTAG          -> goles finales (Full Time Home/Away Goals)
      HS, AS / HST, AST   -> tiros y tiros a puerta (para la fase de xSoT)

    Filtramos filas vacias (el archivo trae lineas en blanco al final)
    y partidos sin resultado (aun no jugados). Antes de parsear, valida
    que las columnas criticas existan (detecta si football-data cambio
    de formato) y, ya parseados, pasa los partidos por validacion.py
    para descartar filas corruptas (goles negativos, fechas imposibles,
    duplicados) en vez de dejarlas contaminar las stats en silencio.

    Cachea en memoria por ruta+mtime: si el archivo no cambio desde la
    ultima lectura, evita re-parsear el CSV entero (relevante cuando
    cargar() se llama varias veces en la misma sesion, ej. backtest
    reprocesando la misma temporada para distintos xi).
    """
    mtime = os.path.getmtime(ruta)
    if usar_cache:
        cacheado = _CACHE_PARTIDOS.get(ruta)
        if cacheado is not None and cacheado[0] == mtime:
            return cacheado[1]

    # football-data.co.uk mezcla encodings segun el anio: probamos en orden
    texto = None
    with open(ruta, "rb") as f:
        crudo = f.read()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            texto = crudo.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if texto is None:
        raise SystemExit("  [error] no se pudo decodificar {}".format(ruta))

    lector = csv.DictReader(texto.splitlines())
    validar_esquema(lector.fieldnames, ruta=ruta, logger=logger)

    crudos = []
    for fila in lector:
        local = (fila.get("HomeTeam") or "").strip()
        visita = (fila.get("AwayTeam") or "").strip()
        gl = (fila.get("FTHG") or "").strip()
        gv = (fila.get("FTAG") or "").strip()

        if not local or not visita or not gl or not gv:
            continue  # fila vacia o partido no jugado

        fecha = parsear_fecha(fila.get("Date") or "")
        if fecha is None:
            continue  # sin fecha no podemos ponderar el partido

        def entero(clave):
            valor = (fila.get(clave) or "").strip()
            return int(valor) if valor.isdigit() else 0

        # FTHG/FTAG no siempre son enteros validos si el CSV viene corrupto
        # (celda con texto, separador corrido); mejor descartar la fila que
        # reventar todo el pipeline con un ValueError.
        if not gl.lstrip("-").isdigit() or not gv.lstrip("-").isdigit():
            logger.warning("%s: fila con goles no numericos descartada (%s vs %s: '%s'-'%s')",
                       ruta, local, visita, gl, gv)
            continue

        crudos.append({
            "fecha": fecha,
            "local": local,
            "visita": visita,
            "goles_local": int(gl),
            "goles_visita": int(gv),
            "tiros_local": entero("HS"),
            "tiros_visita": entero("AS"),
            "sot_local": entero("HST"),
            "sot_visita": entero("AST"),
        })

    partidos, reporte = validar_lote(crudos, logger=logger)
    if reporte["descartados"] or reporte["duplicados"]:
        logger.warning("%s: %s partidos descartados, %s duplicados de %s totales",
                   ruta, reporte["descartados"], reporte["duplicados"], reporte["total"])

    _CACHE_PARTIDOS[ruta] = (mtime, partidos)
    return partidos


def parsear_fecha(texto):
    """El CSV usa dd/mm/yyyy en archivos recientes y dd/mm/yy en los viejos."""
    texto = texto.strip()
    for formato in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(texto, formato)
        except ValueError:
            continue
    return None


# ----------------------------------------------------------------------
# 2b. DECAIMIENTO TEMPORAL
# ----------------------------------------------------------------------

def calcular_pesos(partidos, xi=XI):
    """Asigna a cada partido un peso entre 0 y 1 segun su antiguedad.

        peso = exp(-xi * dias_desde_el_partido_mas_reciente)

    La referencia es el ultimo partido del dataset, no la fecha de hoy:
    asi el codigo funciona igual para backtests historicos.
    Con xi=0 todos los pesos valen 1 (equivale a desactivarlo).
    """
    if not partidos:
        return []
    referencia = max(p["fecha"] for p in partidos)
    return [exp(-xi * (referencia - p["fecha"]).days) for p in partidos]


# ----------------------------------------------------------------------
# 3. CONSTRUCCION DE Liga Y Equipo
# ----------------------------------------------------------------------

def valor(p, lado, w_mix):
    """Senal con la que se mide la fuerza: mezcla de goles y xG.

    w_mix=1 -> solo goles | w_mix=0 -> solo xG | 0.5 -> mitad y mitad
    Si el partido no trae xG (fuente CSV), cae a goles automaticamente.
    """
    g = p["goles_" + lado]
    x = p.get("xg_" + lado, g)
    return w_mix * g + (1.0 - w_mix) * x


def construir_liga(partidos, pesos, nombre, w_mix=1.0):
    """Calcula los promedios de la liga, ponderados por antiguedad.

    Reemplaza los valores quemados (1.55 / 1.30) del modelo: ahora
    salen de los datos reales, dando mas peso a lo reciente.

    Devuelve dos pares de promedios: los goles fijan la escala de lambda,
    la senal (goles/xG/mezcla) sirve para medir las fuerzas.
    """
    if not partidos:
        raise SystemExit("  [error] no hay partidos jugados en este archivo")

    t = sum(pesos)
    pares = zip(partidos, pesos)
    acc = {"gl": 0.0, "gv": 0.0, "vl": 0.0, "vv": 0.0}
    for p, w in pares:
        acc["gl"] += p["goles_local"] * w
        acc["gv"] += p["goles_visita"] * w
        acc["vl"] += valor(p, "local", w_mix) * w
        acc["vv"] += valor(p, "visita", w_mix) * w

    return Liga(nombre=nombre,
                media_goles_local=acc["gl"] / t,
                media_goles_visita=acc["gv"] / t,
                media_valor_local=acc["vl"] / t,
                media_valor_visita=acc["vv"] / t)


class EquiposIndexados(dict):
    """dict {nombre: Equipo} con un indice de busqueda por nombre.lower()
    precomputado una sola vez en el constructor.

    buscar_equipo() antes reconstruia listas por comprension en CADA
    llamada (coincidencia exacta case-insensitive + parcial). Con 20-30
    equipos por liga el costo es marginal, pero en backtest.py o en una
    sesion interactiva que busca equipos repetidamente sobre la misma
    tabla, precomputar el indice evita ese trabajo redundante.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._indice_lower = {n.lower(): e for n, e in self.items()}

    def indice_lower(self):
        return self._indice_lower


def construir_equipos(partidos, pesos, min_partidos=3, w_mix=1.0):
    """Acumula GF/GC de cada equipo, casa y fuera, ponderando por antiguedad.

    Los campos del Equipo pasan a ser sumas ponderadas:
      pj_local = suma de pesos      -> "partidos efectivos"
      gf_local = suma de goles*peso
    El cociente gf_local/pj_local sigue siendo goles por partido, pero
    ahora es un promedio ponderado. El modelo no necesita cambios.

    Devuelve: (EquiposIndexados, {nombre: (pj_crudo_local, pj_crudo_visita)})
    """
    acc = {}

    def nuevo():
        return {"pj_l": 0.0, "gf_l": 0.0, "gc_l": 0.0,
                "pj_v": 0.0, "gf_v": 0.0, "gc_v": 0.0,
                "n_l": 0, "n_v": 0}

    for p, w in zip(partidos, pesos):
        loc, vis = p["local"], p["visita"]
        acc.setdefault(loc, nuevo())
        acc.setdefault(vis, nuevo())
        v_loc = valor(p, "local", w_mix)
        v_vis = valor(p, "visita", w_mix)

        # El equipo local: suma a sus stats de casa
        acc[loc]["pj_l"] += w
        acc[loc]["gf_l"] += v_loc * w
        acc[loc]["gc_l"] += v_vis * w
        acc[loc]["n_l"] += 1

        # El visitante: suma a sus stats de fuera
        acc[vis]["pj_v"] += w
        acc[vis]["gf_v"] += v_vis * w
        acc[vis]["gc_v"] += v_loc * w
        acc[vis]["n_v"] += 1

    equipos_crudo, crudos = {}, {}
    for nombre, a in acc.items():
        # El filtro usa el conteo crudo, no el ponderado
        if a["n_l"] < min_partidos or a["n_v"] < min_partidos:
            continue
        equipos_crudo[nombre] = Equipo(
            nombre=nombre,
            pj_local=a["pj_l"], gf_local=a["gf_l"], gc_local=a["gc_l"],
            pj_visita=a["pj_v"], gf_visita=a["gf_v"], gc_visita=a["gc_v"],
        )
        crudos[nombre] = (a["n_l"], a["n_v"])
    return EquiposIndexados(equipos_crudo), crudos


def cargar(liga_key, temporadas, xi=XI, w_mix=1.0, fuente="csv", forzar=False):
    """Punto de entrada: descarga, parsea, pondera y arma todo.

    fuente="csv"        -> football-data.co.uk, solo goles.
    fuente="understat"  -> xG real via soccerdata (necesita xg_loader).

    La descarga/parseo por fuente vive en fuentes.py (FuenteDatos): anadir
    un proveedor nuevo no requiere tocar esta funcion, solo registrarlo
    alli con registrar_fuente().

    Acepta varias temporadas y las concatena. Con el decaimiento activo
    ya no hay penalizacion por sumar temporadas viejas: pesan poco solas.

    Devuelve: (Liga, EquiposIndexados, {nombre: pj_crudos}, n_partidos)
    """
    if liga_key not in LIGAS:
        raise SystemExit("  [error] liga desconocida: {}. Opciones: {}".format(
            liga_key, ", ".join(LIGAS)))

    _, nombre_liga = LIGAS[liga_key]

    from fuentes import obtener_fuente
    proveedor = obtener_fuente(fuente)
    partidos = proveedor.partidos(liga_key, temporadas, forzar=forzar)
    sufijo = " [xG]" if fuente == "understat" else ""
    etiqueta = "{} ({}){}".format(nombre_liga, ", ".join(temporadas), sufijo)

    pesos = calcular_pesos(partidos, xi)
    liga = construir_liga(partidos, pesos, etiqueta, w_mix)
    equipos, crudos = construir_equipos(partidos, pesos, w_mix=w_mix)
    return liga, equipos, crudos, len(partidos)


# ----------------------------------------------------------------------
# 4. BUSQUEDA TOLERANTE DE NOMBRES
# ----------------------------------------------------------------------

def buscar_equipo(equipos, texto):
    """Encuentra un equipo sin exigir el nombre exacto del CSV.

    El CSV usa nombres cortos ('Man City', 'Ath Madrid', 'Ein Frankfurt').
    Aceptamos coincidencia exacta, sin mayusculas, o por subcadena.

    Si `equipos` es un EquiposIndexados (lo que devuelve construir_equipos),
    la coincidencia case-insensitive es O(1) via el indice precomputado en
    vez de reconstruir una lista por comprension en cada llamada.
    """
    if texto in equipos:
        return equipos[texto]

    objetivo = texto.lower().strip()
    if isinstance(equipos, EquiposIndexados):
        exacto = equipos.indice_lower().get(objetivo)
        if exacto is not None:
            return exacto
    else:
        exactos = [e for n, e in equipos.items() if n.lower() == objetivo]
        if exactos:
            return exactos[0]

    parciales = [e for n, e in equipos.items() if objetivo in n.lower()]
    if len(parciales) == 1:
        return parciales[0]
    if len(parciales) > 1:
        raise SystemExit("  [error] '{}' es ambiguo: {}".format(
            texto, ", ".join(e.nombre for e in parciales)))

    raise SystemExit("  [error] no encontre '{}'. Usa --tabla para ver los nombres.".format(texto))


# ----------------------------------------------------------------------
# 5. TABLA RESUMEN
# ----------------------------------------------------------------------

def imprimir_tabla(liga, equipos, crudos, n_partidos, xi, k, w_mix=1.0, rho=0.0):
    """Lista los equipos con sus fuerzas, ordenados por diferencia de gol."""
    from poisson_model import (fuerza_ataque_local, fuerza_defensa_local,
                               fuerza_ataque_visita, fuerza_defensa_visita)

    vida_media = "{:.0f} dias".format(log(2) / xi) if xi > 0 else "desactivado"

    print("=" * 76)
    print("  {}".format(liga.nombre))
    print("  {} partidos | media local {:.2f} | media visita {:.2f}".format(
        n_partidos, liga.media_goles_local, liga.media_goles_visita))
    senal = "{:.0f}% goles / {:.0f}% xG".format(w_mix * 100, (1 - w_mix) * 100)
    print("  senal: {} | decaimiento xi={} (vida media {}) | shrinkage k={}".format(
        senal, xi, vida_media, k))
    print("  Dixon-Coles rho={}".format(rho))
    print("=" * 76)
    print("  {:<18} {:>3} {:>6} {:>4} {:>4} {:>5}   {:>5} {:>5} {:>5} {:>5}".format(
        "EQUIPO", "PJ", "PJ-EF", "GF", "GC", "DIF", "ATK-L", "DEF-L", "ATK-V", "DEF-V"))
    print("  " + "-" * 72)

    orden = sorted(
        equipos.values(),
        key=lambda e: (e.gf_local + e.gf_visita) - (e.gc_local + e.gc_visita),
        reverse=True,
    )
    for e in orden:
        pj_crudo = sum(crudos[e.nombre])
        pj_efec = e.pj_local + e.pj_visita          # suma de pesos
        gf = e.gf_local + e.gf_visita               # goles ponderados
        gc = e.gc_local + e.gc_visita
        print("  {:<18} {:>3} {:>6.1f} {:>4.0f} {:>4.0f} {:>+5.0f}   "
              "{:>5.2f} {:>5.2f} {:>5.2f} {:>5.2f}".format(
                  e.nombre[:18], pj_crudo, pj_efec, gf, gc, gf - gc,
                  fuerza_ataque_local(e, liga, k), fuerza_defensa_local(e, liga, k),
                  fuerza_ataque_visita(e, liga, k), fuerza_defensa_visita(e, liga, k)))
    print("=" * 76)
    print("  PJ-EF = partidos efectivos tras el decaimiento | GF/GC ponderados")
    print("  ATK: mas alto = mejor ataque | DEF: mas bajo = mejor defensa")


# ----------------------------------------------------------------------
# 6. CLI
# ----------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Predictor Poisson con datos reales")
    ap.add_argument("--liga", default="premier", choices=list(LIGAS),
                    help="premier | laliga | bundesliga | ligue1")
    ap.add_argument("--temporadas", nargs="+", default=None,
                    help="codigos de temporada, ej: 2526 2425 (por defecto la ultima completa)")
    ap.add_argument("--local", help="equipo local")
    ap.add_argument("--visita", help="equipo visitante")
    ap.add_argument("--tabla", action="store_true", help="lista equipos y fuerzas")
    ap.add_argument("--forzar-descarga", action="store_true", help="ignora el cache")
    ap.add_argument("--fuente", default="understat", choices=["csv", "understat"],
                    help="understat = con xG (mejor) | csv = solo goles")
    ap.add_argument("--xi", type=float, default=None,
                    help="decaimiento temporal por dia (0 = desactivado)")
    ap.add_argument("--k", type=float, default=None,
                    help="fuerza del shrinkage en partidos equivalentes (0 = desactivado)")
    ap.add_argument("--w", type=float, default=None,
                    help="mezcla goles/xG: 1 = solo goles, 0 = solo xG")
    ap.add_argument("--rho", type=float, default=None,
                    help="correccion Dixon-Coles (0 = Poisson pura)")
    ap.add_argument("--info-calibracion", action="store_true",
                    help="muestra el estado de calibracion guardado y sale")
    args = ap.parse_args()

    if args.info_calibracion:
        print(config_modelo.resumen())
        return

    # Defaults: primero lo calibrado por backtest.py --guardar-config (si
    # existe y no esta vencido), si no las constantes hardcodeadas de
    # siempre. Dependen de la fuente: con xG la senal es menos ruidosa y
    # hace falta menos shrinkage.
    con_xg = args.fuente == "understat"
    defaults_principal = {
        "xi": XI, "k": (K_SHRINK_XG if con_xg else K_SHRINK), "w": (0.0 if con_xg else 1.0),
        "rho": RHO_LIGA.get(args.liga, RHO),
    }
    calibrados, info_calib = config_modelo.cargar_config("principal", defaults_principal)
    if info_calib["fuente"] == "calibrado":
        logger.info("usando parametros calibrados (bloque 'principal', hace %s dias)",
                 info_calib["antiguedad_dias"])

    xi = calibrados["xi"] if args.xi is None else args.xi
    k = calibrados["k"] if args.k is None else args.k
    w = calibrados["w"] if args.w is None else args.w
    rho = calibrados["rho"] if args.rho is None else args.rho

    temporadas = args.temporadas or [temporada_por_defecto()]
    liga, equipos, crudos, n = cargar(args.liga, temporadas, xi=xi, w_mix=w,
                                      fuente=args.fuente, forzar=args.forzar_descarga)

    if args.tabla or not (args.local and args.visita):
        imprimir_tabla(liga, equipos, crudos, n, xi, k, w, rho)
        if not (args.local and args.visita):
            print("\n  Para predecir:  python data_loader.py --liga {} --local \"X\" --visita \"Y\"".format(args.liga))
        return

    local = buscar_equipo(equipos, args.local)
    visita = buscar_equipo(equipos, args.visita)
    imprimir(predecir(local=local, visita=visita, liga=liga, k=k, rho=rho))


if __name__ == "__main__":
    main()

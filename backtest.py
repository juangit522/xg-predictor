"""
Backtest walk-forward para calibrar xi (decaimiento) y k (shrinkage).

Regla de oro: para predecir un partido solo se usan partidos ANTERIORES.
Nunca se entrena con datos del futuro.

Uso:
    python backtest.py --liga premier
    python backtest.py --todas --temporadas 2324 2425 2526
    python backtest.py --liga laliga --guardar grid_laliga.csv

Sin dependencias externas: solo stdlib.
"""

import argparse
import csv
import os
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from math import exp, log

from poisson_model import Liga, Equipo, calcular_xg, matriz_marcadores, MAX_GOLES
from data_loader import LIGAS, descargar_csv, leer_partidos, parsear_fecha
from disponibilidad import Plantilla, factores, aplicar, es_titular
from elo import RatingElo, elo_a_probabilidades
from logging_setup import get_logger
import config_modelo

logger = get_logger("backtest")

# Rejillas de busqueda. Se recorren todas las combinaciones.
XI_GRID = [0.0, 0.001, 0.002, 0.003, 0.004, 0.006, 0.008, 0.012]
K_GRID = [0.0, 2.0, 4.0, 6.0, 8.0, 12.0, 16.0, 24.0]

# w = peso de los GOLES frente al xG al medir la fuerza de un equipo.
#   w=1.0 -> solo goles (modelo clasico)
#   w=0.0 -> solo xG
#   w=0.3 -> 30% goles, 70% xG
W_GRID = [0.0, 0.25, 0.5, 0.75, 1.0]

# Elasticidades de la capa de disponibilidad. 0 = capa desactivada.
GA_GRID = [0.0, 0.25, 0.5, 0.75, 1.0, 1.5]   # ataque
GD_GRID = [0.0, 0.25, 0.5, 0.75, 1.0]        # defensa
MODOS = ["ninguno", "previo", "alineacion"]

# Correccion Dixon-Coles. Valores tipicos en la literatura: -0.03 a -0.15.
RHO_GRID = [0.0, -0.03, -0.06, -0.09, -0.12, -0.15, -0.18]

# Optimos ya calibrados en la fase anterior. Al barrer disponibilidad
# los fijamos para no explotar la rejilla a cinco dimensiones.
XI_OPT, K_OPT, W_OPT = 0.002, 2.0, 0.0

MIN_HIST = 4  # partidos minimos en casa y fuera para que un equipo sea predecible


# ----------------------------------------------------------------------
# 1. METRICAS
# ----------------------------------------------------------------------

def rps(p_h, p_d, p_a, real):
    """Ranked Probability Score. Metrica estandar para el 1X2.

    A diferencia del log-loss, respeta el ORDEN de los resultados:
    predecir 'local' cuando gana el visitante penaliza mas que
    predecir 'empate'. Mas bajo es mejor. Rango [0, 1].

        RPS = 0.5 * [ (pH - oH)^2 + (pH + pD - oH - oD)^2 ]
    """
    o_h = 1.0 if real == "H" else 0.0
    o_d = 1.0 if real == "D" else 0.0
    return 0.5 * ((p_h - o_h) ** 2 + (p_h + p_d - o_h - o_d) ** 2)


def log_loss(p_h, p_d, p_a, real):
    """Penaliza con dureza la confianza equivocada. Mas bajo es mejor."""
    p = {"H": p_h, "D": p_d, "A": p_a}[real]
    return -log(max(p, 1e-15))


def brier(p_h, p_d, p_a, real):
    """Error cuadratico multiclase. Mas bajo es mejor."""
    o = {"H": (1, 0, 0), "D": (0, 1, 0), "A": (0, 0, 1)}[real]
    return (p_h - o[0]) ** 2 + (p_d - o[1]) ** 2 + (p_a - o[2]) ** 2


class Marcador:
    """Acumula metricas partido a partido."""

    def __init__(self, etiqueta):
        self.etiqueta = etiqueta
        self.rps = self.ll = self.br = 0.0
        self.aciertos = self.n = 0
        # Calibracion del empate: cuanto empate predecimos vs cuanto hay.
        # Es el diagnostico directo del sesgo que corrige Dixon-Coles.
        self.pd_suma = 0.0
        self.empates = 0

    def anotar(self, p_h, p_d, p_a, real):
        self.rps += rps(p_h, p_d, p_a, real)
        self.ll += log_loss(p_h, p_d, p_a, real)
        self.br += brier(p_h, p_d, p_a, real)
        predicho = max((p_h, "H"), (p_d, "D"), (p_a, "A"))[1]
        self.aciertos += 1 if predicho == real else 0
        self.pd_suma += p_d
        self.empates += 1 if real == "D" else 0
        self.n += 1

    def resumen(self):
        if self.n == 0:
            return {"rps": float("nan"), "ll": float("nan"), "brier": float("nan"),
                    "acc": float("nan"), "emp_pred": float("nan"),
                    "emp_real": float("nan"), "n": 0}
        return {"rps": self.rps / self.n, "ll": self.ll / self.n,
                "brier": self.br / self.n, "acc": self.aciertos / self.n,
                "emp_pred": self.pd_suma / self.n,
                "emp_real": self.empates / self.n, "n": self.n}


# ----------------------------------------------------------------------
# 2. ACUMULADOR INCREMENTAL
# ----------------------------------------------------------------------

class Historial:
    """Mantiene las stats ponderadas de todos los partidos vistos hasta ahora.

    Truco que hace esto rapido: el peso de un partido es
    exp(-xi * (t_ref - t_i)), y t_ref cambia en cada prediccion. Pero
    ese exponencial se factoriza:

        exp(-xi*(t_ref - t_i)) = exp(-xi*t_ref) * exp(xi*t_i)

    El factor exp(-xi*t_ref) es comun a numerador y denominador de
    gf/pj, asi que SE CANCELA. Por eso podemos acumular con el peso
    creciente exp(xi*(t_i - t_0)) y no recalcular nada nunca.

    Guardamos goles y xG en acumuladores SEPARADOS. Como la mezcla
    w*goles + (1-w)*xG es lineal, cualquier w se arma en el momento de
    predecir sin repetir la pasada:

        sum(peso * (w*g + (1-w)*x)) = w*sum(peso*g) + (1-w)*sum(peso*x)
    """

    CAMPOS = ("pj_l", "gf_l_g", "gf_l_x", "gc_l_g", "gc_l_x",
              "pj_v", "gf_v_g", "gf_v_x", "gc_v_g", "gc_v_x")

    def __init__(self, xi, fecha_base):
        self.xi = xi
        self.base = fecha_base
        self.equipos = {}
        self.w_total = 0.0
        self.tot = {"gl_g": 0.0, "gl_x": 0.0, "gv_g": 0.0, "gv_x": 0.0}

    def _peso(self, fecha):
        return exp(self.xi * (fecha - self.base).days)

    def agregar(self, p):
        w = self._peso(p["fecha"])
        loc, vis = p["local"], p["visita"]
        gl, gv = p["goles_local"], p["goles_visita"]
        xl, xv = p["xg_local"], p["xg_visita"]

        for nombre in (loc, vis):
            if nombre not in self.equipos:
                d = {c: 0.0 for c in self.CAMPOS}
                d["n_l"] = d["n_v"] = 0
                self.equipos[nombre] = d

        a = self.equipos[loc]
        a["pj_l"] += w
        a["gf_l_g"] += gl * w
        a["gf_l_x"] += xl * w
        a["gc_l_g"] += gv * w
        a["gc_l_x"] += xv * w
        a["n_l"] += 1

        b = self.equipos[vis]
        b["pj_v"] += w
        b["gf_v_g"] += gv * w
        b["gf_v_x"] += xv * w
        b["gc_v_g"] += gl * w
        b["gc_v_x"] += xl * w
        b["n_v"] += 1

        self.w_total += w
        self.tot["gl_g"] += gl * w
        self.tot["gl_x"] += xl * w
        self.tot["gv_g"] += gv * w
        self.tot["gv_x"] += xv * w

    def liga(self, w_mix, nombre="backtest"):
        """Promedios de liga con lo visto hasta ahora.

        media_goles_* fija la escala de lambda (siempre goles reales).
        media_valor_* es la senal con la que se miden las fuerzas.
        """
        T = self.w_total
        mg_l = self.tot["gl_g"] / T
        mg_v = self.tot["gv_g"] / T
        return Liga(nombre=nombre,
                    media_goles_local=mg_l,
                    media_goles_visita=mg_v,
                    media_valor_local=w_mix * mg_l + (1 - w_mix) * self.tot["gl_x"] / T,
                    media_valor_visita=w_mix * mg_v + (1 - w_mix) * self.tot["gv_x"] / T)

    def equipo(self, nombre, w_mix):
        a = self.equipos[nombre]
        u = 1.0 - w_mix
        return Equipo(nombre=nombre,
                      pj_local=a["pj_l"],
                      gf_local=w_mix * a["gf_l_g"] + u * a["gf_l_x"],
                      gc_local=w_mix * a["gc_l_g"] + u * a["gc_l_x"],
                      pj_visita=a["pj_v"],
                      gf_visita=w_mix * a["gf_v_g"] + u * a["gf_v_x"],
                      gc_visita=w_mix * a["gc_v_g"] + u * a["gc_v_x"])

    def tiene_historia(self, nombre, minimo=MIN_HIST):
        a = self.equipos.get(nombre)
        return a is not None and a["n_l"] >= minimo and a["n_v"] >= minimo


# ----------------------------------------------------------------------
# 3. PREDICCION 1X2
# ----------------------------------------------------------------------

def probs_1x2(local, visita, liga, k):
    """Devuelve (p_local, p_empate, p_visita) para un k dado."""
    return probs_desde_lambda(*calcular_xg(local, visita, liga, k))


def probs_desde_lambda(xg_l, xg_v, rho=0.0):
    """Igual que probs_1x2 pero partiendo de lambdas ya calculados.

    Lo usan las capas que corrigen los lambda antes de construir la
    matriz (disponibilidad) o que solo varian rho (Dixon-Coles), y que
    no quieren rehacer las fuerzas en cada combinacion.
    """
    m = matriz_marcadores(xg_l, xg_v, rho)

    p_l = p_e = p_v = 0.0
    for i in range(MAX_GOLES + 1):
        for j in range(MAX_GOLES + 1):
            if i > j:
                p_l += m[i][j]
            elif i == j:
                p_e += m[i][j]
            else:
                p_v += m[i][j]
    return p_l, p_e, p_v


def resultado_real(p):
    if p["goles_local"] > p["goles_visita"]:
        return "H"
    if p["goles_local"] == p["goles_visita"]:
        return "D"
    return "A"


# ----------------------------------------------------------------------
# 4. CARGA Y AGRUPACION POR DIA
# ----------------------------------------------------------------------

def cargar_partidos(ligas, temporadas, fuente="csv", forzar=False):
    """Carga y ordena cronologicamente. Trae tambien las cuotas de Bet365.

    fuente="csv"        -> football-data.co.uk. Solo goles: xg = goles,
                           asi que la dimension w no cambia nada.
    fuente="understat"  -> xG real de Understat via soccerdata.

    Asigna un indice entero estable ("_idx") a cada partido. Antes el
    conjunto de partidos "evaluables" se identificaba con id(p) (la
    identidad del objeto Python en memoria): funciona mientras todo
    corre en el mismo proceso, pero es fragil (dos ejecuciones pueden
    reciclar el mismo id, y no sobrevive a serializar partidos para
    otro proceso). Un indice de posicion es estable y ademas es lo que
    hace falta para paralelizar el barrido por xi con ProcessPoolExecutor.
    """
    if fuente == "understat":
        from xg_loader import cargar_xg
        todos = cargar_xg(ligas, temporadas, refrescar=forzar)
    else:
        todos = []
        for liga_key in ligas:
            codigo, _ = LIGAS[liga_key]
            for temp in temporadas:
                ruta = descargar_csv(codigo, temp, forzar=forzar)
                for p in leer_partidos(ruta):
                    p["liga"] = liga_key
                    # Sin xG real, la senal es el gol: w queda sin efecto
                    p["xg_local"] = float(p["goles_local"])
                    p["xg_visita"] = float(p["goles_visita"])
                    todos.append(p)
                _anexar_cuotas(ruta, todos)
        todos.sort(key=lambda p: (p["fecha"], p["local"]))

    for i, p in enumerate(todos):
        p["_idx"] = i
    return todos


def _anexar_cuotas(ruta, partidos):
    """Lee B365H/B365D/B365A del mismo CSV y las pega por (fecha, local)."""
    with open(ruta, "rb") as f:
        crudo = f.read()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            texto = crudo.decode(enc)
            break
        except UnicodeDecodeError:
            continue

    indice = {}
    for fila in csv.DictReader(texto.splitlines()):
        fecha = parsear_fecha(fila.get("Date") or "")
        local = (fila.get("HomeTeam") or "").strip()
        if not fecha or not local:
            continue
        try:
            cuotas = (float(fila["B365H"]), float(fila["B365D"]), float(fila["B365A"]))
        except (KeyError, ValueError, TypeError):
            cuotas = None
        indice[(fecha, local)] = cuotas

    for p in partidos:
        if "cuotas" not in p:
            p["cuotas"] = indice.get((p["fecha"], p["local"]))


def agrupar_por_dia(partidos):
    """Agrupa por fecha. Predecimos toda la jornada ANTES de incorporarla,
    para que dos partidos del mismo dia no se informen entre si."""
    grupos, actual, fecha_actual = [], [], None
    for p in partidos:
        if p["fecha"] != fecha_actual:
            if actual:
                grupos.append(actual)
            actual, fecha_actual = [], p["fecha"]
        actual.append(p)
    if actual:
        grupos.append(actual)
    return grupos


# ----------------------------------------------------------------------
# 5. BASELINES
# ----------------------------------------------------------------------

def correr_baselines(grupos):
    """Evalua las referencias y marca que partidos son predecibles.

    El conjunto evaluable NO depende de xi ni de k (solo del historial
    crudo), asi que lo fijamos una vez y todas las combinaciones se
    miden exactamente sobre los mismos partidos.

    Incluye Elo como baseline: resume la fuerza general de cada equipo
    en un solo numero (ver elo.py), sin las fuerzas separadas ataque/
    defensa ni el xG del modelo Poisson. Si el Poisson no le gana a
    Elo en RPS, es una senal de que las fuerzas ataque/defensa no estan
    aportando nada sobre "quien es mejor equipo en general".

    Devuelve: (marcadores, set de ids evaluables)
    """
    uniforme = Marcador("Uniforme (1/3)")
    tasa_base = Marcador("Tasa base historica")
    mercado = Marcador("Cuotas Bet365")
    elo_marcador = Marcador("Elo")

    vistos = {}   # equipo -> [n_local, n_visita]
    conteo = {"H": 0, "D": 0, "A": 0}
    evaluables = set()
    elo = RatingElo()

    for grupo in grupos:
        for p in grupo:
            loc, vis = p["local"], p["visita"]
            ok = (vistos.get(loc, [0, 0])[0] >= MIN_HIST
                  and vistos.get(loc, [0, 0])[1] >= MIN_HIST
                  and vistos.get(vis, [0, 0])[0] >= MIN_HIST
                  and vistos.get(vis, [0, 0])[1] >= MIN_HIST
                  and sum(conteo.values()) > 0)
            if not ok:
                continue

            evaluables.add(p["_idx"])
            real = resultado_real(p)

            uniforme.anotar(1 / 3, 1 / 3, 1 / 3, real)

            tot = sum(conteo.values())
            tasa_base.anotar(conteo["H"] / tot, conteo["D"] / tot, conteo["A"] / tot, real)

            if p.get("cuotas"):
                ch, cd, ca = p["cuotas"]
                inv = (1 / ch, 1 / cd, 1 / ca)
                s = sum(inv)  # quitamos el margen de la casa
                mercado.anotar(inv[0] / s, inv[1] / s, inv[2] / s, real)

            p_l, p_d, p_v = elo_a_probabilidades(elo.rating(loc), elo.rating(vis))
            elo_marcador.anotar(p_l, p_d, p_v, real)

        for p in grupo:
            for nombre, idx in ((p["local"], 0), (p["visita"], 1)):
                vistos.setdefault(nombre, [0, 0])[idx] += 1
            conteo[resultado_real(p)] += 1
            elo.actualizar(p["local"], p["visita"], p["goles_local"], p["goles_visita"])

    return [uniforme, tasa_base, mercado, elo_marcador], evaluables


# ----------------------------------------------------------------------
# 6. BARRIDO WALK-FORWARD
# ----------------------------------------------------------------------

def _barrer_un_xi(args):
    """Cuerpo de una iteracion de xi, extraido a nivel de modulo para poder
    correrlo en un proceso aparte (ProcessPoolExecutor solo puede pickle-ar
    funciones definidas en el modulo, no closures). Recibe una tupla porque
    executor.map() pasa un unico argumento por tarea.
    """
    xi, grupos, evaluables, k_grid, w_grid = args
    fecha_base = grupos[0][0]["fecha"]
    hist = Historial(xi, fecha_base)
    marcadores = {(k, w): Marcador("xi={} k={} w={}".format(xi, k, w))
                  for k in k_grid for w in w_grid}

    for grupo in grupos:
        # 1) Predecir con el historial actual (solo pasado)
        for p in grupo:
            if p["_idx"] not in evaluables:
                continue
            if not (hist.tiene_historia(p["local"]) and hist.tiene_historia(p["visita"])):
                continue

            real = resultado_real(p)
            for w in w_grid:
                liga = hist.liga(w)
                local = hist.equipo(p["local"], w)
                visita = hist.equipo(p["visita"], w)
                for k in k_grid:
                    p_l, p_e, p_v = probs_1x2(local, visita, liga, k)
                    marcadores[(k, w)].anotar(p_l, p_e, p_v, real)

        # 2) Recien ahora incorporamos la jornada al historial
        for p in grupo:
            hist.agregar(p)

    return xi, {(xi,) + clave: m.resumen() for clave, m in marcadores.items()}


def barrer(grupos, evaluables, xi_grid, k_grid, w_grid, paralelo=True, workers=None):
    """Una pasada por cada xi; dentro, todas las combinaciones de k y w.

    Solo xi obliga a rehacer la acumulacion. k se aplica al predecir y w
    se arma por combinacion lineal, asi que ambos son gratis.

    Cada valor de xi es independiente (arranca su propio Historial desde
    cero), asi que el barrido se paraleliza con un proceso por valor de
    xi: en una grilla tipica de 8 xi x 8 k x 5 w, eso reparte el trabajo
    entre nucleos casi linealmente en vez de correr todo en serie.
    Con paralelo=False, o un solo xi en la grilla, corre secuencial (evita
    el overhead de levantar procesos quando no compensa).

    Devuelve {(xi, k, w): resumen_de_metricas}
    """
    resultados = {}

    if paralelo and len(xi_grid) > 1:
        tareas = [(xi, grupos, evaluables, k_grid, w_grid) for xi in xi_grid]
        n_workers = workers or min(len(xi_grid), os.cpu_count() or 1)
        logger.info("barrido paralelo: %s valores de xi en %s workers", len(xi_grid), n_workers)
        with ProcessPoolExecutor(max_workers=n_workers) as ex:
            for xi, parcial in ex.map(_barrer_un_xi, tareas):
                resultados.update(parcial)
                print("  xi={:<7} listo".format(xi))
    else:
        for xi in xi_grid:
            _, parcial = _barrer_un_xi((xi, grupos, evaluables, k_grid, w_grid))
            resultados.update(parcial)
            print("  xi={:<7} listo".format(xi))

    return resultados


# ----------------------------------------------------------------------
# 6b. BARRIDO DE LA CAPA DE DISPONIBILIDAD
# ----------------------------------------------------------------------

def titulares_de(jug_equipo):
    return {j["jugador"] for j in jug_equipo if es_titular(j["posicion"])}


def barrer_disponibilidad(grupos, evaluables, jugadores, xi, k, w,
                          modos, ga_grid, gd_grid):
    """Barre (modo, g_atk, g_def) con xi/k/w fijos en su optimo.

    Una sola Plantilla sirve para los tres modos: la acumulacion es
    identica, lo unico que cambia es a QUIEN se le pregunta que juega.

    Devuelve {(modo, g_atk, g_def): resumen}
    """
    fecha_base = grupos[0][0]["fecha"]
    hist = Historial(xi, fecha_base)
    plantilla = Plantilla(xi, fecha_base)
    marcadores = {(m, ga, gd): Marcador("{} ga={} gd={}".format(m, ga, gd))
                  for m in modos for ga in ga_grid for gd in gd_grid}
    sin_datos = 0

    for grupo in grupos:
        # 1) Predecir con el historial actual (solo pasado)
        for p in grupo:
            if p["_idx"] not in evaluables:
                continue
            if not (hist.tiene_historia(p["local"]) and hist.tiene_historia(p["visita"])):
                continue

            liga = hist.liga(w)
            local = hist.equipo(p["local"], w)
            visita = hist.equipo(p["visita"], w)
            xg_l, xg_v = calcular_xg(local, visita, liga, k)
            real = resultado_real(p)

            jug = jugadores.get(p.get("game_id"), {})
            if not jug:
                sin_datos += 1

            for m in modos:
                aj = factores(plantilla, p["local"], p["visita"], jug, m)
                for ga in ga_grid:
                    for gd in gd_grid:
                        lam_l, lam_v = aplicar(xg_l, xg_v, aj, ga, gd)
                        p_l, p_e, p_v = probs_desde_lambda(lam_l, lam_v)
                        marcadores[(m, ga, gd)].anotar(p_l, p_e, p_v, real)

        # 2) Recien ahora incorporamos la jornada
        for p in grupo:
            hist.agregar(p)
            jug = jugadores.get(p.get("game_id"), {})
            for equipo in (p["local"], p["visita"]):
                lista = jug.get(equipo, [])
                if not lista:
                    continue
                plantilla.anotar_cobertura(equipo, titulares_de(lista))
                plantilla.agregar(equipo, lista, p["fecha"])

    if sin_datos:
        print("  [aviso] {} partidos evaluados sin datos de jugadores".format(sin_datos))
    return {c: m.resumen() for c, m in marcadores.items()}


def imprimir_reporte_disp(resultados, baselines, modos, ga_grid, gd_grid, etiqueta):
    mejor = min(resultados.items(), key=lambda kv: kv[1]["rps"])
    (m_opt, ga_opt, gd_opt), r_opt = mejor
    base = resultados[(modos[0], 0.0, 0.0)]

    print("\n" + "=" * 78)
    print("  CAPA DE DISPONIBILIDAD  ({})".format(etiqueta))
    print("  xi={} k={} w={} fijos | {} partidos".format(
        XI_OPT, K_OPT, W_OPT, r_opt["n"]))
    print("=" * 78)
    print("  {:<14} {:>7} {:>7} {:>9} {:>9} {:>9}".format(
        "MODO", "g_atk", "g_def", "RPS", "vs BASE", "ACIERTO"))
    print("  " + "-" * 74)
    for m in modos:
        sub = {c: r for c, r in resultados.items() if c[0] == m}
        (_, ga, gd), r = min(sub.items(), key=lambda kv: kv[1]["rps"])
        marca = "  <--" if (m, ga, gd) == (m_opt, ga_opt, gd_opt) else ""
        print("  {:<14} {:>7} {:>7} {:>9.4f} {:>+9.4f} {:>8.1%}{}".format(
            m, ga, gd, r["rps"], r["rps"] - base["rps"], r["acc"], marca))
    print("  " + "-" * 74)
    print("  vs BASE = diferencia contra el modelo sin capa (negativo es mejor)")

    print("\n" + "=" * 78)
    print("  REJILLA modo={}  | filas: g_atk | columnas: g_def".format(m_opt))
    print("=" * 78)
    print("  {:>9}".format("atk \\ def") + "".join("{:>8}".format(gd) for gd in gd_grid))
    print("  " + "-" * 74)
    for ga in ga_grid:
        fila = "  {:>9}".format(ga)
        for gd in gd_grid:
            v = resultados[(m_opt, ga, gd)]["rps"]
            fila += "{:>7.4f}{}".format(v, "*" if (ga, gd) == (ga_opt, gd_opt) else " ")
        print(fila)
    print("  " + "-" * 74)

    print("\n" + "=" * 78)
    print("  COMPARATIVA")
    print("=" * 78)
    filas = [(b.etiqueta, b.resumen()) for b in baselines if b.resumen()["n"] > 0]
    filas.append(("Poisson xG sin disponibilidad", base))
    filas.append(("Poisson xG + disp ({})".format(m_opt), r_opt))
    filas.sort(key=lambda f: f[1]["rps"])
    print("  {:<32} {:>8} {:>9} {:>8} {:>8}".format(
        "MODELO", "RPS", "LOGLOSS", "BRIER", "ACIERTO"))
    print("  " + "-" * 74)
    for nombre, r in filas:
        print("  {:<32} {:>8.4f} {:>9.4f} {:>8.4f} {:>7.1%}".format(
            nombre, r["rps"], r["ll"], r["brier"], r["acc"]))
    print("=" * 78)

    mercado = next((b.resumen() for b in baselines if "Bet365" in b.etiqueta), None)
    if mercado and mercado["n"] > 0:
        print("\n" + "  Distancia al mercado")
        print("    sin disponibilidad : {:+.4f}".format(base["rps"] - mercado["rps"]))
        print("    con disponibilidad : {:+.4f}".format(r_opt["rps"] - mercado["rps"]))

    print("\n" + "  OPTIMO -> --modo {} --g-atk {} --g-def {}".format(m_opt, ga_opt, gd_opt))


# ----------------------------------------------------------------------
# 6c. BARRIDO DIXON-COLES
# ----------------------------------------------------------------------

def barrer_dixon_coles(grupos, evaluables, xi, k_grid, w, rho_grid):
    """Barre (k, rho) con xi y w fijos en su optimo.

    rho solo afecta a la construccion de la matriz, no a la acumulacion
    de fuerzas, asi que una sola pasada cubre toda la rejilla.

    Se barre k junto a rho porque interactuan: Dixon-Coles sube la
    probabilidad de empate, y el shrinkage tambien (acerca los equipos
    entre si). Calibrar uno sin el otro sobreajusta.

    Devuelve {(k, rho): resumen}
    """
    fecha_base = grupos[0][0]["fecha"]
    hist = Historial(xi, fecha_base)
    marcadores = {(k, r): Marcador("k={} rho={}".format(k, r))
                  for k in k_grid for r in rho_grid}

    for grupo in grupos:
        for p in grupo:
            if p["_idx"] not in evaluables:
                continue
            if not (hist.tiene_historia(p["local"]) and hist.tiene_historia(p["visita"])):
                continue

            liga = hist.liga(w)
            local = hist.equipo(p["local"], w)
            visita = hist.equipo(p["visita"], w)
            real = resultado_real(p)

            for k in k_grid:
                lam_l, lam_v = calcular_xg(local, visita, liga, k)
                for r in rho_grid:
                    p_l, p_e, p_v = probs_desde_lambda(lam_l, lam_v, r)
                    marcadores[(k, r)].anotar(p_l, p_e, p_v, real)

        for p in grupo:
            hist.agregar(p)

    return {c: m.resumen() for c, m in marcadores.items()}


def imprimir_reporte_dc(resultados, baselines, k_grid, rho_grid, xi, w, etiqueta):
    mejor = min(resultados.items(), key=lambda kv: kv[1]["rps"])
    (k_opt, rho_opt), r_opt = mejor
    # Referencia: mismo k optimo pero sin correccion
    base = resultados[(k_opt, 0.0)]

    print("\n" + "=" * 78)
    print("  DIXON-COLES  ({})".format(etiqueta))
    print("  xi={} w={} fijos | {} partidos".format(xi, w, r_opt["n"]))
    print("=" * 78)
    print("  {:>9}".format("k \\ rho") + "".join("{:>8}".format(r) for r in rho_grid))
    print("  " + "-" * 74)
    for k in k_grid:
        fila = "  {:>9}".format(k)
        for r in rho_grid:
            v = resultados[(k, r)]["rps"]
            fila += "{:>7.4f}{}".format(v, "*" if (k, r) == (k_opt, rho_opt) else " ")
        print(fila)
    print("  " + "-" * 74)
    print("  (*) optimo")

    # --- Calibracion del empate: el diagnostico que justifica la correccion ---
    print("\n" + "=" * 78)
    print("  CALIBRACION DEL EMPATE  (k={})".format(k_opt))
    print("  cuanto empate predice el modelo vs cuanto hubo de verdad")
    print("=" * 78)
    print("  {:>8} {:>12} {:>12} {:>10} {:>10}".format(
        "rho", "X PREDICHO", "X REAL", "SESGO", "RPS"))
    print("  " + "-" * 74)
    for r in rho_grid:
        d = resultados[(k_opt, r)]
        sesgo = d["emp_pred"] - d["emp_real"]
        marca = "  <--" if r == rho_opt else ""
        print("  {:>8} {:>11.1%} {:>12.1%} {:>+10.1%} {:>10.4f}{}".format(
            r, d["emp_pred"], d["emp_real"], sesgo, d["rps"], marca))
    print("  " + "-" * 74)
    print("  SESGO negativo = el modelo predice MENOS empates de los que hay")

    # --- Comparativa ---
    print("\n" + "=" * 78)
    print("  COMPARATIVA")
    print("=" * 78)
    filas = [(b.etiqueta, b.resumen()) for b in baselines if b.resumen()["n"] > 0]
    filas.append(("Poisson xG sin Dixon-Coles", base))
    filas.append(("Poisson xG + DC (rho={})".format(rho_opt), r_opt))
    filas.sort(key=lambda f: f[1]["rps"])
    print("  {:<32} {:>8} {:>9} {:>8} {:>8}".format(
        "MODELO", "RPS", "LOGLOSS", "BRIER", "ACIERTO"))
    print("  " + "-" * 74)
    for nombre, r in filas:
        print("  {:<32} {:>8.4f} {:>9.4f} {:>8.4f} {:>7.1%}".format(
            nombre, r["rps"], r["ll"], r["brier"], r["acc"]))
    print("=" * 78)

    mercado = next((b.resumen() for b in baselines if "Bet365" in b.etiqueta), None)
    if mercado and mercado["n"] > 0:
        print("\n" + "  Distancia al mercado")
        print("    sin Dixon-Coles : {:+.4f}".format(base["rps"] - mercado["rps"]))
        print("    con Dixon-Coles : {:+.4f}".format(r_opt["rps"] - mercado["rps"]))

    print("\n" + "  OPTIMO -> --k {} --rho {}".format(k_opt, rho_opt))


# ----------------------------------------------------------------------
# 7. REPORTE
# ----------------------------------------------------------------------

def imprimir_reporte(resultados, baselines, xi_grid, k_grid, w_grid, etiqueta):
    mejor = min(resultados.items(), key=lambda kv: kv[1]["rps"])
    (xi_opt, k_opt, w_opt), m_opt = mejor

    # --- 1) Efecto del xG: mejor combinacion para cada valor de w ---
    if len(w_grid) > 1:
        print("\n" + "=" * 78)
        print("  EFECTO DEL xG  ({})".format(etiqueta))
        print("  w = peso de los goles. w=1 solo goles, w=0 solo xG")
        print("=" * 78)
        print("  {:>6}  {:<22} {:>8} {:>9} {:>9}".format(
            "w", "SENAL", "RPS", "LOGLOSS", "ACIERTO"))
        print("  " + "-" * 74)
        for w in sorted(w_grid, reverse=True):
            sub = {c: r for c, r in resultados.items() if c[2] == w}
            (xi_b, k_b, _), r = min(sub.items(), key=lambda kv: kv[1]["rps"])
            senal = "{:.0f}% goles / {:.0f}% xG".format(w * 100, (1 - w) * 100)
            marca = "  <-- optimo" if w == w_opt else ""
            print("  {:>6.2f}  {:<22} {:>8.4f} {:>9.4f} {:>8.1%}{}".format(
                w, senal, r["rps"], r["ll"], r["acc"], marca))
        print("  " + "-" * 74)
        print("  (mejor xi/k elegido por separado para cada w)")

    # --- 2) Rejilla xi x k en el w optimo ---
    print("\n" + "=" * 78)
    print("  REJILLA DE RPS con w={}".format(w_opt))
    print("  mas bajo = mejor | filas: xi (decaimiento) | columnas: k (shrinkage)")
    print("=" * 78)
    print("  {:>8}".format("xi \\ k") + "".join("{:>8.0f}".format(k) for k in k_grid))
    print("  " + "-" * 74)
    for xi in xi_grid:
        fila = "  {:>8}".format(xi)
        for k in k_grid:
            v = resultados[(xi, k, w_opt)]["rps"]
            marca = "*" if (xi, k) == (xi_opt, k_opt) else " "
            fila += "{:>7.4f}{}".format(v, marca)
        print(fila)
    print("  " + "-" * 74)
    print("  (*) optimo")

    # --- 3) Comparativa contra baselines ---
    print("\n" + "=" * 78)
    print("  COMPARATIVA  ({} partidos evaluados)".format(m_opt["n"]))
    print("=" * 78)
    print("  {:<30} {:>8} {:>9} {:>8} {:>8}".format(
        "MODELO", "RPS", "LOGLOSS", "BRIER", "ACIERTO"))
    print("  " + "-" * 74)

    filas = [(b.etiqueta, b.resumen()) for b in baselines if b.resumen()["n"] > 0]
    filas.append(("Poisson xi={} k={} w={}".format(xi_opt, k_opt, w_opt), m_opt))

    # Referencia: el mejor modelo que solo usa goles
    solo_goles = {c: r for c, r in resultados.items() if c[2] == 1.0}
    if solo_goles and w_opt != 1.0:
        (xi_g, k_g, _), r_g = min(solo_goles.items(), key=lambda kv: kv[1]["rps"])
        filas.append(("Poisson solo goles (w=1)", r_g))

    filas.sort(key=lambda f: f[1]["rps"])
    for nombre, r in filas:
        print("  {:<30} {:>8.4f} {:>9.4f} {:>8.4f} {:>7.1%}".format(
            nombre, r["rps"], r["ll"], r["brier"], r["acc"]))
    print("=" * 78)

    mercado = next((b.resumen() for b in baselines if "Bet365" in b.etiqueta), None)
    if mercado and mercado["n"] > 0:
        print("\n  Distancia al mercado: {:+.4f} RPS".format(m_opt["rps"] - mercado["rps"]))
        if solo_goles and w_opt != 1.0:
            print("  Antes (solo goles)  : {:+.4f} RPS".format(r_g["rps"] - mercado["rps"]))

    print("\n  PARAMETROS OPTIMOS -> --xi {} --k {} --w {}".format(xi_opt, k_opt, w_opt))
    if xi_opt > 0:
        print("  (vida media del decaimiento: {:.0f} dias)".format(log(2) / xi_opt))
    return xi_opt, k_opt, w_opt


def guardar_csv(resultados, ruta):
    with open(ruta, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        # Las claves son (xi, k, w) o (modo, g_atk, g_def) segun el barrido
        primera = next(iter(resultados))
        if isinstance(primera[0], str):
            cols = ["modo", "g_atk", "g_def"]
        elif len(primera) == 2:
            cols = ["k", "rho"]
        else:
            cols = ["xi", "k", "w"]
        w.writerow(cols + ["rps", "logloss", "brier", "acierto", "n"])
        for clave, r in sorted(resultados.items(), key=lambda kv: str(kv[0])):
            w.writerow(list(clave) + [round(r["rps"], 6), round(r["ll"], 6),
                                      round(r["brier"], 6), round(r["acc"], 6), r["n"]])
    print("  [guardado] {}".format(ruta))


# ----------------------------------------------------------------------
# 8. CLI
# ----------------------------------------------------------------------

def temporadas_por_defecto(n=3):
    """Las n ultimas temporadas completas, formato '2526'."""
    hoy = datetime.now()
    ultimo = hoy.year - 1 if hoy.month >= 8 else hoy.year - 2
    return ["{:02d}{:02d}".format((ultimo - i) % 100, (ultimo - i + 1) % 100)
            for i in range(n - 1, -1, -1)]


def main():
    ap = argparse.ArgumentParser(description="Backtest walk-forward del modelo Poisson")
    ap.add_argument("--liga", default="premier", choices=list(LIGAS))
    ap.add_argument("--todas", action="store_true", help="usa las 3 ligas juntas")
    ap.add_argument("--temporadas", nargs="+", default=None)
    ap.add_argument("--xi-grid", nargs="+", type=float, default=XI_GRID)
    ap.add_argument("--k-grid", nargs="+", type=float, default=K_GRID)
    ap.add_argument("--w-grid", nargs="+", type=float, default=None,
                    help="mezcla goles/xG: 1=solo goles, 0=solo xG")
    ap.add_argument("--fuente", default="csv", choices=["csv", "understat"],
                    help="csv = football-data (solo goles) | understat = con xG")
    ap.add_argument("--guardar", help="ruta CSV para la rejilla completa")
    ap.add_argument("--forzar-descarga", action="store_true")
    ap.add_argument("--disponibilidad", action="store_true",
                    help="barre la capa de disponibilidad en vez de xi/k/w")
    ap.add_argument("--modos", nargs="+", default=MODOS, choices=MODOS)
    ap.add_argument("--ga-grid", nargs="+", type=float, default=GA_GRID)
    ap.add_argument("--gd-grid", nargs="+", type=float, default=GD_GRID)
    ap.add_argument("--dixon-coles", action="store_true",
                    help="barre la correccion rho de Dixon-Coles")
    ap.add_argument("--rho-grid", nargs="+", type=float, default=RHO_GRID)
    ap.add_argument("--sin-paralelo", action="store_true",
                    help="fuerza el barrido xi/k/w secuencial (por defecto usa "
                         "un proceso por cada valor de xi)")
    ap.add_argument("--workers", type=int, default=None,
                    help="procesos a usar en el barrido paralelo (por defecto: min(len(xi_grid), cpus))")
    ap.add_argument("--guardar-config", action="store_true",
                    help="persiste los parametros optimos encontrados en modelo_config.json, "
                         "con fecha y metadata de la calibracion, para que data_loader.py "
                         "los use por defecto y para poder detectar cuando queden viejos")
    args = ap.parse_args()

    ligas = list(LIGAS) if args.todas else [args.liga]
    temporadas = args.temporadas or temporadas_por_defecto(3)
    etiqueta = "{} | {}".format("+".join(ligas), " ".join(temporadas))

    # Ambos barridos especializados trabajan sobre el modelo de xG
    if args.disponibilidad or args.dixon_coles:
        args.fuente = "understat"

    # Sin xG real la dimension w no aporta nada: la colapsamos
    w_grid = args.w_grid or (W_GRID if args.fuente == "understat" else [1.0])

    print("=" * 78)
    print("  BACKTEST WALK-FORWARD")
    print("  ligas      : {}".format(", ".join(ligas)))
    print("  temporadas : {}".format(", ".join(temporadas)))
    print("  fuente     : {}".format(
        "Understat (xG real)" if args.fuente == "understat" else "football-data (goles)"))
    if args.disponibilidad:
        print("  rejilla    : {} modos x {} g_atk x {} g_def = {} combinaciones".format(
            len(args.modos), len(args.ga_grid), len(args.gd_grid),
            len(args.modos) * len(args.ga_grid) * len(args.gd_grid)))
    elif args.dixon_coles:
        print("  rejilla    : {} k x {} rho = {} combinaciones".format(
            len(args.k_grid), len(args.rho_grid),
            len(args.k_grid) * len(args.rho_grid)))
    else:
        print("  rejilla    : {} xi x {} k x {} w = {} combinaciones".format(
            len(args.xi_grid), len(args.k_grid), len(w_grid),
            len(args.xi_grid) * len(args.k_grid) * len(w_grid)))
    print("=" * 78)

    partidos = cargar_partidos(ligas, temporadas, fuente=args.fuente,
                               forzar=args.forzar_descarga)
    grupos = agrupar_por_dia(partidos)
    print("  {} partidos en {} jornadas\n".format(len(partidos), len(grupos)))

    baselines, evaluables = correr_baselines(grupos)
    print("  {} partidos evaluables (el resto es burn-in)\n".format(len(evaluables)))

    if args.disponibilidad:
        from xg_loader import cargar_jugadores
        jugadores = cargar_jugadores(ligas, temporadas)
        print("  datos de jugadores para {} partidos\n".format(len(jugadores)))
        resultados = barrer_disponibilidad(
            grupos, evaluables, jugadores, XI_OPT, K_OPT, W_OPT,
            args.modos, args.ga_grid, args.gd_grid)
        imprimir_reporte_disp(resultados, baselines, args.modos,
                              args.ga_grid, args.gd_grid, etiqueta)
        if args.guardar_config:
            (m_opt, ga_opt, gd_opt), r_opt = min(
                resultados.items(), key=lambda kv: kv[1]["rps"])
            config_modelo.guardar_config(
                "disponibilidad",
                {"modo": m_opt, "g_atk": ga_opt, "g_def": gd_opt},
                {"ligas": ligas, "temporadas": temporadas, "n_partidos": r_opt["n"],
                 "rps": round(r_opt["rps"], 6)})
    elif args.dixon_coles:
        resultados = barrer_dixon_coles(grupos, evaluables, XI_OPT,
                                        args.k_grid, W_OPT, args.rho_grid)
        imprimir_reporte_dc(resultados, baselines, args.k_grid, args.rho_grid,
                            XI_OPT, W_OPT, etiqueta)
        if args.guardar_config:
            (k_opt, rho_opt), r_opt = min(resultados.items(), key=lambda kv: kv[1]["rps"])
            config_modelo.guardar_config(
                "dixon_coles",
                {"k": k_opt, "rho": rho_opt},
                {"ligas": ligas, "temporadas": temporadas, "n_partidos": r_opt["n"],
                 "rps": round(r_opt["rps"], 6)})
    else:
        resultados = barrer(grupos, evaluables, args.xi_grid, args.k_grid, w_grid,
                            paralelo=not args.sin_paralelo, workers=args.workers)
        xi_opt, k_opt, w_opt = imprimir_reporte(resultados, baselines, args.xi_grid,
                                                args.k_grid, w_grid, etiqueta)
        if args.guardar_config:
            r_opt = resultados[(xi_opt, k_opt, w_opt)]
            config_modelo.guardar_config(
                "principal",
                {"xi": xi_opt, "k": k_opt, "w": w_opt},
                {"ligas": ligas, "temporadas": temporadas, "n_partidos": r_opt["n"],
                 "rps": round(r_opt["rps"], 6)})

    if args.guardar:
        guardar_csv(resultados, args.guardar)


if __name__ == "__main__":
    main()

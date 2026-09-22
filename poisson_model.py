"""
Predictor de partidos con Distribucion de Poisson.
Ligas soportadas: Premier League, LaLiga, Bundesliga.
Sin dependencias externas: solo stdlib de Python 3.9+.
"""

from dataclasses import dataclass
from functools import lru_cache
from math import exp, factorial

MAX_GOLES = 8  # truncamos la matriz en 8 goles: arriba de eso la probabilidad es ~0

# Fuerza del shrinkage, en "partidos equivalentes".
# Es el peso que le damos al promedio de la liga frente a lo observado.
# k=0  -> sin shrinkage (el modelo cree ciegamente en la muestra)
# k=6  -> un equipo con 6 partidos queda a mitad de camino entre su dato y 1.00
# Valor calibrado con backtest.py sobre PL+LaLiga+Bundesliga, 2324-2526:
# k=6 es el optimo en las 3 ligas por separado y juntas.
K_SHRINK = 6.0

# Con xG el optimo baja a 2: el xG es una senal mucho menos ruidosa que
# el gol, asi que hay que desconfiar menos de la muestra observada.
K_SHRINK_XG = 2.0

# Correccion Dixon-Coles. La Poisson independiente subestima de forma
# sistematica los marcadores bajos empatados (0-0 y 1-1): en el futbol
# los goles de ambos equipos NO son independientes.
#   rho = 0     -> Poisson pura
#   rho < 0     -> sube 0-0 y 1-1, baja 1-0 y 0-1
# Valor calibrado con backtest.py --dixon-coles sobre las 3 ligas juntas.
# Con rho=-0.12 el empate queda perfectamente calibrado: 25.4% predicho
# contra 25.4% real (sin correccion predecia 22.9%).
RHO = -0.12


# ----------------------------------------------------------------------
# 1. ESTRUCTURAS DE DATOS
# ----------------------------------------------------------------------

@dataclass
class Liga:
    """Promedios de la liga. Son el ancla contra la que se mide cada equipo.

    Hay DOS pares de promedios y la distincion importa:

    - media_goles_*  : la ESCALA en la que vive lambda. Siempre goles
                       reales, porque la Poisson modela goles.
    - media_valor_*  : la SENAL con la que se miden las fuerzas. Puede
                       ser goles, xG, o una mezcla de ambos.

    Separarlos permite calcular las fuerzas con xG (mejor senal, menos
    ruido) sin heredar su sesgo de escala: el xG de Understat sobreestima
    los goles de local en ~9%, y si multiplicaramos por la media de xG
    todos los lambda saldrian inflados.

    Si no pasas media_valor_*, se usan los goles (comportamiento clasico).
    """
    nombre: str
    media_goles_local: float   # goles que marca el equipo local, por partido
    media_goles_visita: float  # goles que marca el equipo visitante, por partido
    media_valor_local: float = None
    media_valor_visita: float = None

    def __post_init__(self):
        if self.media_valor_local is None:
            self.media_valor_local = self.media_goles_local
        if self.media_valor_visita is None:
            self.media_valor_visita = self.media_goles_visita


@dataclass
class Equipo:
    """Stats acumuladas del equipo, separadas en casa y fuera.

    La separacion local/visita NO es opcional: un equipo puede ser
    dominante en casa y mediocre fuera, y mezclarlo borra esa senal.

    Los campos gf_/gc_ guardan la SENAL elegida (goles, xG o mezcla),
    no necesariamente goles. El modelo solo necesita que sean cantidades
    de tipo conteo y que liga.media_valor_* este en la misma unidad.
    """
    nombre: str
    pj_local: float   # partidos jugados como local (float: pueden venir ponderados)
    gf_local: float   # valor a favor como local (goles o xG segun la fuente)
    gc_local: float   # goles en contra como local
    pj_visita: float
    gf_visita: float
    gc_visita: float


# ----------------------------------------------------------------------
# 2. FUERZAS DE ATAQUE Y DEFENSA (con shrinkage)
# ----------------------------------------------------------------------

def _shrink(valor_obs, n, k):
    """Empuja el valor observado hacia 1.00 (el promedio de la liga).

        resultado = (n * observado + k * 1.00) / (n + k)

    Con pocos partidos casi no creemos en el dato y nos quedamos cerca
    del promedio. Con muchos, el dato manda. Esto evita que un equipo
    con una racha corta produzca fuerzas absurdas.
    """
    if n <= 0:
        return 1.0
    return (n * valor_obs + k * 1.0) / (n + k)


def fuerza_ataque_local(eq, liga, k=K_SHRINK):
    """Cuanto marca en casa vs. lo que marca un local promedio.
    1.00 = exactamente el promedio de la liga. 1.30 = marca un 30% mas."""
    if eq.pj_local <= 0:
        return 1.0
    bruto = (eq.gf_local / eq.pj_local) / liga.media_valor_local
    return _shrink(bruto, eq.pj_local, k)


def fuerza_defensa_local(eq, liga, k=K_SHRINK):
    """Cuanto encaja en casa vs. lo que encaja un local promedio.
    Aqui MENOS ES MEJOR: 0.70 = encaja un 30% menos que el promedio."""
    if eq.pj_local <= 0:
        return 1.0
    bruto = (eq.gc_local / eq.pj_local) / liga.media_valor_visita
    return _shrink(bruto, eq.pj_local, k)


def fuerza_ataque_visita(eq, liga, k=K_SHRINK):
    """Idem ataque, pero jugando fuera."""
    if eq.pj_visita <= 0:
        return 1.0
    bruto = (eq.gf_visita / eq.pj_visita) / liga.media_valor_visita
    return _shrink(bruto, eq.pj_visita, k)


def fuerza_defensa_visita(eq, liga, k=K_SHRINK):
    """Idem defensa, pero jugando fuera. Menos es mejor."""
    if eq.pj_visita <= 0:
        return 1.0
    bruto = (eq.gc_visita / eq.pj_visita) / liga.media_valor_local
    return _shrink(bruto, eq.pj_visita, k)


# ----------------------------------------------------------------------
# 3. CALCULO DE xG (los lambdas de Poisson)
# ----------------------------------------------------------------------

def calcular_xg(local, visita, liga, k=K_SHRINK):
    """Formula base del modelo:

        xG_local  = ataque_casa(A)  x  defensa_fuera(B)  x  media_goles_local
        xG_visita = ataque_fuera(B) x  defensa_casa(A)   x  media_goles_visita

    Las fuerzas son adimensionales (ratios contra el promedio de liga),
    asi que pueden venir de xG mientras lambda queda en escala de goles.

    La ventaja de local ya esta incorporada, porque media_goles_local
    es mayor que media_goles_visita (en las 3 ligas el local marca ~20% mas).

    Devuelve: (xg_local, xg_visita)
    """
    xg_local = (
        fuerza_ataque_local(local, liga, k)
        * fuerza_defensa_visita(visita, liga, k)
        * liga.media_goles_local
    )
    xg_visita = (
        fuerza_ataque_visita(visita, liga, k)
        * fuerza_defensa_local(local, liga, k)
        * liga.media_goles_visita
    )
    # Cota de seguridad: evita lambdas absurdos con muestras chicas (jornada 1-3)
    xg_local = max(0.15, min(xg_local, 5.0))
    xg_visita = max(0.15, min(xg_visita, 5.0))
    return xg_local, xg_visita


# ----------------------------------------------------------------------
# 4. DISTRIBUCION DE POISSON
# ----------------------------------------------------------------------

_FACT = [float(factorial(i)) for i in range(MAX_GOLES + 1)]


def poisson_pmf(k, lam):
    """P(X = k) con media lam.    P = (lam^k * e^-lam) / k!

    Responde: si un equipo espera lam goles, que probabilidad hay
    de que marque exactamente k.
    """
    return (lam ** k) * exp(-lam) / _FACT[k]


def tau(x, y, lam_l, lam_v, rho):
    """Factor de correccion Dixon-Coles.

    Solo toca las cuatro celdas de marcador bajo, que son justo donde la
    Poisson independiente falla:

        tau(0,0) = 1 - lam_l*lam_v*rho     0-0  (sube si rho<0)
        tau(0,1) = 1 + lam_l*rho           0-1  (baja si rho<0)
        tau(1,0) = 1 + lam_v*rho           1-0  (baja si rho<0)
        tau(1,1) = 1 - rho                 1-1  (sube si rho<0)
        resto    = 1

    Con rho negativo se redistribuye masa desde 1-0 y 0-1 hacia 0-0 y
    1-1, que es exactamente el sesgo que hay que corregir: el modelo
    base predice pocos empates.
    """
    if rho == 0.0:
        return 1.0
    if x == 0:
        if y == 0:
            t = 1.0 - lam_l * lam_v * rho
        elif y == 1:
            t = 1.0 + lam_l * rho
        else:
            return 1.0
    elif x == 1:
        if y == 0:
            t = 1.0 + lam_v * rho
        elif y == 1:
            t = 1.0 - rho
        else:
            return 1.0
    else:
        return 1.0
    # Guarda de validez: con lambdas altos y rho muy negativo tau podria
    # volverse negativo y romper la distribucion.
    return max(t, 1e-6)


@lru_cache(maxsize=8192)
def _matriz_marcadores_cacheada(xg_local, xg_visita, rho):
    """Cuerpo real de matriz_marcadores(), memoizado.

    backtest.py evalua la misma combinacion de (lambda_local, lambda_visita,
    rho) miles de veces durante un grid search (muchos partidos tempranos
    comparten el mismo lambda por el shrinkage fuerte con poca muestra).
    Cachear evita rehacer 81 exp() + la correccion Dixon-Coles + la
    renormalizacion cada vez. Las claves ya vienen redondeadas por el
    wrapper publico para que el ruido de punto flotante no tire el cache.
    """
    pl = [poisson_pmf(i, xg_local) for i in range(MAX_GOLES + 1)]
    pv = [poisson_pmf(j, xg_visita) for j in range(MAX_GOLES + 1)]

    m = [[pl[i] * pv[j] for j in range(MAX_GOLES + 1)] for i in range(MAX_GOLES + 1)]

    if rho != 0.0:
        for x in (0, 1):
            for y in (0, 1):
                m[x][y] *= tau(x, y, xg_local, xg_visita, rho)

    # Renormalizamos: al truncar en 8 goles, y al aplicar tau, la masa
    # total deja de sumar exactamente 1
    total = sum(sum(fila) for fila in m)
    return tuple(tuple(valor / total for valor in fila) for fila in m)


def matriz_marcadores(xg_local, xg_visita, rho=RHO):
    """Matriz 9x9 donde M[i][j] = P(local marca i, visita marca j).

    Con rho=0 asume independencia entre ambos marcadores (Poisson pura).
    Con rho!=0 aplica la correccion Dixon-Coles a los marcadores bajos.

    Devuelve listas (no la tupla cacheada) para que el caller pueda tratar
    el resultado como antes sin arriesgarse a mutar el valor compartido
    que vive en el cache.
    """
    cacheada = _matriz_marcadores_cacheada(round(xg_local, 4), round(xg_visita, 4),
                                           round(rho, 4))
    return [list(fila) for fila in cacheada]


# ----------------------------------------------------------------------
# 5. MERCADOS DERIVADOS (todos se leen de la misma matriz)
# ----------------------------------------------------------------------

def calcular_mercados(m):
    """Suma regiones de la matriz de marcadores.

    1  -> triangulo inferior (i > j)
    X  -> diagonal           (i == j)
    2  -> triangulo superior (i < j)

    Como todo sale de la MISMA distribucion, las probabilidades son
    coherentes entre si por construccion.
    """
    p_local = p_empate = p_visita = 0.0
    p_over25 = p_btts = 0.0

    for i in range(MAX_GOLES + 1):
        for j in range(MAX_GOLES + 1):
            p = m[i][j]
            if i > j:
                p_local += p
            elif i == j:
                p_empate += p
            else:
                p_visita += p

            if i + j > 2.5:
                p_over25 += p
            if i >= 1 and j >= 1:
                p_btts += p

    return {
        "local": p_local,
        "empate": p_empate,
        "visita": p_visita,
        "over_2_5": p_over25,
        "under_2_5": 1 - p_over25,
        "btts_si": p_btts,
    }


def top_marcadores(m, n=5):
    """Los n marcadores exactos mas probables, ordenados."""
    todos = [
        ("{}-{}".format(i, j), m[i][j])
        for i in range(MAX_GOLES + 1)
        for j in range(MAX_GOLES + 1)
    ]
    todos.sort(key=lambda x: x[1], reverse=True)
    return todos[:n]


def prob_a_cuota(p):
    """Probabilidad -> cuota decimal justa (sin margen de la casa)."""
    return float("inf") if p <= 0 else round(1 / p, 2)


# ----------------------------------------------------------------------
# 6. FUNCION PRINCIPAL
# ----------------------------------------------------------------------

def predecir(local, visita, liga, k=K_SHRINK, rho=RHO):
    """Orquesta el pipeline completo: stats -> fuerzas -> xG -> matriz -> mercados."""
    xg_l, xg_v = calcular_xg(local, visita, liga, k)
    m = matriz_marcadores(xg_l, xg_v, rho)

    return {
        "partido": "{} vs {}".format(local.nombre, visita.nombre),
        "liga": liga.nombre,
        "xg_local": round(xg_l, 2),
        "xg_visita": round(xg_v, 2),
        "xg_total": round(xg_l + xg_v, 2),
        "mercados": calcular_mercados(m),
        "top_marcadores": top_marcadores(m),
    }


def imprimir(r):
    """Salida formateada en consola."""
    mk = r["mercados"]
    ancho = 60
    print("=" * ancho)
    print("  " + r["partido"])
    print("  " + r["liga"])
    print("=" * ancho)

    print("\n  GOLES ESPERADOS (xG)")
    print("    Local   : {:.2f}".format(r["xg_local"]))
    print("    Visita  : {:.2f}".format(r["xg_visita"]))
    print("    Total   : {:.2f}".format(r["xg_total"]))

    print("\n  RESULTADO 1X2           PROB    CUOTA")
    filas = (
        ("1  Victoria local ", "local"),
        ("X  Empate         ", "empate"),
        ("2  Victoria visita", "visita"),
    )
    for etiqueta, clave in filas:
        p = mk[clave]
        barra = "#" * int(p * 30)
        print("    {}  {:6.1%}  {:>6.2f}  {}".format(etiqueta, p, prob_a_cuota(p), barra))

    print("\n  OTROS MERCADOS")
    print("    Mas de 2.5 goles   : {:6.1%}   (cuota {})".format(
        mk["over_2_5"], prob_a_cuota(mk["over_2_5"])))
    print("    Menos de 2.5 goles : {:6.1%}   (cuota {})".format(
        mk["under_2_5"], prob_a_cuota(mk["under_2_5"])))
    print("    Ambos marcan (SI)  : {:6.1%}   (cuota {})".format(
        mk["btts_si"], prob_a_cuota(mk["btts_si"])))

    print("\n  MARCADORES MAS PROBABLES")
    for marcador, p in r["top_marcadores"]:
        print("    {}  ->  {:5.2%}".format(marcador, p))

    suma = mk["local"] + mk["empate"] + mk["visita"]
    print("\n  [check] Suma 1X2 = {:.4f}  (debe dar 1.0000)".format(suma))
    print("=" * ancho)


# ----------------------------------------------------------------------
# 7. DATOS DE EJEMPLO (MOCK) + EJECUCION
# ----------------------------------------------------------------------

# Promedios de liga. Son los unicos numeros que debes actualizar por temporada.
PREMIER = Liga("Premier League 2024/25", media_goles_local=1.55, media_goles_visita=1.30)
LALIGA = Liga("LaLiga 2024/25", media_goles_local=1.42, media_goles_visita=1.15)
BUNDESLIGA = Liga("Bundesliga 2024/25", media_goles_local=1.72, media_goles_visita=1.40)

ARSENAL = Equipo(
    nombre="Arsenal",
    pj_local=19, gf_local=38, gc_local=17,      # fuerte en casa
    pj_visita=19, gf_visita=31, gc_visita=22,
)

CHELSEA = Equipo(
    nombre="Chelsea",
    pj_local=19, gf_local=33, gc_local=22,
    pj_visita=19, gf_visita=25, gc_visita=24,   # discreto como visitante
)


if __name__ == "__main__":
    resultado = predecir(local=ARSENAL, visita=CHELSEA, liga=PREMIER)
    imprimir(resultado)

    # Desglose de fuerzas: util para depurar de donde salen los xG
    print("\n  DESGLOSE DE FUERZAS (1.00 = promedio de la liga)")
    print("    Arsenal ataque en casa  : {:.3f}".format(fuerza_ataque_local(ARSENAL, PREMIER)))
    print("    Arsenal defensa en casa : {:.3f}   (menos = mejor)".format(fuerza_defensa_local(ARSENAL, PREMIER)))
    print("    Chelsea ataque fuera    : {:.3f}".format(fuerza_ataque_visita(CHELSEA, PREMIER)))
    print("    Chelsea defensa fuera   : {:.3f}   (menos = mejor)".format(fuerza_defensa_visita(CHELSEA, PREMIER)))

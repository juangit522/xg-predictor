"""
Rating Elo por equipo: una senal complementaria a las fuerzas ataque/defensa
del modelo Poisson.

El modelo Poisson mide CUANTO marca/encaja un equipo, pero no CONTRA QUE
NIVEL de rival lo hizo: un 3-0 al ultimo de la tabla no dice lo mismo que
un 3-0 a un candidato al titulo. El shrinkage hacia la media de liga
amortigua las muestras chicas, pero no corrige esto.

Elo resume la fuerza GENERAL de un equipo en un solo numero, actualizado
partido a partido (ganarle a un equipo fuerte suma mas que ganarle a uno
debil). Sirve como:

  1. Diagnostico: la tabla por Elo puede diferir de la tabla por puntos
     y eso senala equipos sobre/infravalorados por las fuerzas Poisson.
  2. Prior para equipos nuevos/ascendidos: mientras el modelo Poisson
     necesita MIN_HIST partidos para ser fiable, Elo puede arrancar cada
     equipo ascendido mas abajo que el default en vez de a ciegas.
  3. Probabilidades 1X2 independientes (elo_a_probabilidades), utiles
     como baseline adicional en backtest.py junto a Uniforme/Tasa
     base/Cuotas: si el Poisson no le gana a Elo, algo anda mal.

No reemplaza al modelo Poisson: backtest.py decide si mezclarla evaluando
su RPS por separado, igual que hace con las demas baselines.
"""

ELO_INICIAL = 1500.0
K_FACTOR = 20.0          # velocidad de ajuste tras cada partido
VENTAJA_LOCAL = 60.0     # puntos Elo que vale jugar en casa (~55-100 tipico)
DIVISOR = 400.0          # escala estandar de Elo


class RatingElo:
    """Mantiene el rating de cada equipo y lo actualiza walk-forward.

    Misma disciplina que el resto del proyecto: actualizar() solo se
    llama con partidos YA JUGADOS, despues de haber leido rating()/
    prob_esperada() para predecir el partido que toca.
    """

    def __init__(self, k=K_FACTOR, ventaja_local=VENTAJA_LOCAL, inicial=ELO_INICIAL):
        self.k = k
        self.ventaja_local = ventaja_local
        self.inicial = inicial
        self.ratings = {}

    def rating(self, equipo):
        return self.ratings.get(equipo, self.inicial)

    def establecer_inicial(self, equipo, valor):
        """Fija un rating de arranque distinto del default.

        Pensado para equipos recien ascendidos: en vez de arrancar a
        1500 a ciegas, se les puede dar un valor heredado (ej. Elo de
        la categoria inferior menos una penalizacion tipica de ~60-100
        puntos por el salto de nivel).
        """
        self.ratings[equipo] = valor

    def prob_esperada(self, local, visita):
        """P(el local suma mas puntos que el visita), formula Elo estandar."""
        diff = (self.rating(local) + self.ventaja_local) - self.rating(visita)
        return 1.0 / (1.0 + 10 ** (-diff / DIVISOR))

    def actualizar(self, local, visita, goles_local, goles_visita):
        """Incorpora el resultado de un partido ya jugado.

        Multiplicador por margen de gol (goal-difference), version
        simplificada de la que usa el World Football Elo Ratings: gana
        mas cuanto mayor la diferencia, pero con retornos decrecientes
        para que una goleada no distorsione el rating de un solo golpe.
        Devuelve el delta aplicado (positivo = subio el local).
        """
        if goles_local > goles_visita:
            resultado = 1.0
        elif goles_local < goles_visita:
            resultado = 0.0
        else:
            resultado = 0.5

        esperado = self.prob_esperada(local, visita)
        dif_goles = abs(goles_local - goles_visita)
        if dif_goles <= 1:
            multiplicador = 1.0
        elif dif_goles == 2:
            multiplicador = 1.5
        else:
            multiplicador = 1.75 + (dif_goles - 3) / 8.0

        delta = self.k * multiplicador * (resultado - esperado)
        self.ratings[local] = self.rating(local) + delta
        self.ratings[visita] = self.rating(visita) - delta
        return delta

    def tabla(self):
        """Equipos ordenados de mayor a menor rating."""
        return sorted(self.ratings.items(), key=lambda kv: kv[1], reverse=True)


def elo_a_probabilidades(elo_local, elo_visita, ventaja_local=VENTAJA_LOCAL,
                         factor_empate=0.5):
    """Convierte dos ratings Elo en (p_local, p_empate, p_visita).

    Elo puro solo da P(no perder) via la formula logistica; para partir
    esa masa en victoria/empate se usa una heuristica estandar (Hvattum
    & Arntzen 2010): la probabilidad de empate es maxima cuando los
    equipos estan parejos y decae con la distancia entre ratings.
    """
    diff = (elo_local + ventaja_local) - elo_visita
    p_no_pierde_local = 1.0 / (1.0 + 10 ** (-diff / DIVISOR))

    p_empate = factor_empate * (1.0 - abs(2 * p_no_pierde_local - 1.0))
    p_empate = max(0.05, min(p_empate, 0.40))

    p_local = max(0.0, p_no_pierde_local - p_empate / 2.0)
    p_visita = max(0.0, 1.0 - p_local - p_empate)
    total = p_local + p_empate + p_visita
    return p_local / total, p_empate / total, p_visita / total


def elo_desde_partidos(partidos, k=K_FACTOR, ventaja_local=VENTAJA_LOCAL, inicial=ELO_INICIAL):
    """Construye un RatingElo recorriendo partidos YA ORDENADOS cronologicamente
    (tal como los devuelven data_loader.cargar()/leer_partidos() o
    backtest.cargar_partidos()). Util para obtener la tabla Elo de una liga
    sin duplicar el bucle de actualizacion en cada caller.
    """
    elo = RatingElo(k=k, ventaja_local=ventaja_local, inicial=inicial)
    for p in partidos:
        elo.actualizar(p["local"], p["visita"], p["goles_local"], p["goles_visita"])
    return elo


# ----------------------------------------------------------------------
# CLI: tabla Elo de una liga, calculada sobre datos reales
# ----------------------------------------------------------------------

def main():
    import argparse

    from data_loader import LIGAS, temporada_por_defecto
    from fuentes import obtener_fuente

    ap = argparse.ArgumentParser(
        description="Tabla Elo de una liga (diagnostico: compara contra la "
                    "tabla de fuerzas ataque/defensa de data_loader.py --tabla)")
    ap.add_argument("--liga", default="premier", choices=list(LIGAS))
    ap.add_argument("--temporadas", nargs="+", default=None)
    ap.add_argument("--fuente", default="csv", choices=["csv", "understat"])
    args = ap.parse_args()

    temporadas = args.temporadas or [temporada_por_defecto()]
    proveedor = obtener_fuente(args.fuente)
    partidos = proveedor.partidos(args.liga, temporadas)
    partidos.sort(key=lambda p: (p["fecha"], p["local"]))

    _, nombre_liga = LIGAS[args.liga]
    elo = elo_desde_partidos(partidos)

    print("=" * 50)
    print("  TABLA ELO -- {} ({})".format(nombre_liga, ", ".join(temporadas)))
    print("  {} partidos procesados".format(len(partidos)))
    print("=" * 50)
    print("  {:<4} {:<20} {:>8}".format("#", "EQUIPO", "ELO"))
    print("  " + "-" * 34)
    for i, (equipo, rating) in enumerate(elo.tabla(), 1):
        print("  {:<4} {:<20} {:>8.1f}".format(i, equipo[:20], rating))
    print("=" * 50)


if __name__ == "__main__":
    main()

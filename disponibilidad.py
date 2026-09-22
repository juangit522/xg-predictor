"""
Capa de disponibilidad: ajusta lambda segun quien juega.

La idea: si falta un delantero que aporta el 30% del xG del equipo, el
modelo base no se entera. Aqui medimos que porcentaje de la produccion
habitual del equipo esta sobre el campo y corregimos lambda.

CUANDO SABES QUIEN JUEGA (esto decide todo el diseno):

  modo "alineacion" -> usas los titulares del partido que predices.
                       Disponible a T-60min, cuando salen las alineaciones.
                       Mide el techo de la senal.

  modo "previo"     -> usas los titulares del partido ANTERIOR del equipo
                       como proxy. Disponible siempre, incluso a T-24h.
                       Es lo que puedes usar sin feed de lesiones.

  modo "ninguno"    -> sin ajuste. Es la referencia contra la que medir.

Ninguno de los tres mira el resultado del partido que se predice.
"""

from math import exp

# Posiciones de Understat que contamos como defensivas
# (GK, DC, DL, DR, DMC, DML, DMR -> todas empiezan por D, o son GK)
def es_defensiva(pos):
    return pos == "GK" or pos.startswith("D")


def es_titular(pos):
    """Understat marca a los suplentes con la posicion literal 'Sub'."""
    return pos != "Sub"


# Limites del ajuste. Sin esto, un equipo con historial raro puede
# generar factores absurdos en las primeras jornadas.
AJUSTE_MIN = 0.70
AJUSTE_MAX = 1.30

# Shrinkage de la cobertura tipica de un equipo hacia la media global.
K_COBERTURA = 5.0


class Plantilla:
    """Contribucion rodante de cada jugador, por equipo.

    Se actualiza partido a partido con el mismo decaimiento exponencial
    que el resto del modelo, y con la misma disciplina walk-forward:
    solo se incorpora un partido DESPUES de haberlo predicho.
    """

    def __init__(self, xi, fecha_base):
        self.xi = xi
        self.base = fecha_base
        # equipo -> jugador -> {"atk": float, "def": float}
        self.equipos = {}
        # equipo -> titulares del ultimo partido (para el modo "previo")
        self.ultimos = {}
        # Referencia de cobertura: cuanto suele cubrir cada equipo
        self.cob = {}          # equipo -> tipo -> (suma, n)
        self.cob_global = {"atk": [0.0, 0], "def": [0.0, 0]}
        # Cobertura observada justo ANTES de incorporar cada partido.
        # Es de instancia, no de clase: si fuera de clase la compartirian
        # todas las pasadas del barrido.
        self._cob_previa = {}

    def _peso(self, fecha):
        return exp(self.xi * (fecha - self.base).days)

    # ------------------------------------------------------------------
    # LECTURA (antes del partido)
    # ------------------------------------------------------------------

    def _cobertura(self, equipo, titulares, tipo):
        """Fraccion de la produccion rodante del equipo que esta en el campo.

        tipo="atk" -> se pondera por xG+xA acumulado del jugador
        tipo="def" -> se pondera por minutos en posicion defensiva

        Devuelve None si no hay historial suficiente.
        """
        d = self.equipos.get(equipo)
        if not d:
            return None
        total = sum(v[tipo] for v in d.values())
        if total <= 0:
            return None
        cubierto = sum(d[n][tipo] for n in titulares if n in d)
        return cubierto / total

    def _referencia(self, equipo, tipo):
        """Cobertura TIPICA de este equipo, encogida hacia la media global.

        Es la clave del diseno: un equipo que rota mucho tiene cobertura
        baja siempre, y eso ya esta dentro de su lambda base. Lo que nos
        interesa es la DESVIACION respecto a su propia costumbre.
        """
        g_suma, g_n = self.cob_global[tipo]
        global_media = g_suma / g_n if g_n else 0.60

        suma, n = self.cob.get(equipo, {}).get(tipo, (0.0, 0))
        if n == 0:
            return global_media
        propia = suma / n
        return (n * propia + K_COBERTURA * global_media) / (n + K_COBERTURA)

    def ajuste(self, equipo, titulares, tipo):
        """Factor multiplicativo: >1 alineacion mas fuerte de lo normal.

        1.0 significa "sin informacion" o "alineacion habitual", que es
        el comportamiento neutro correcto en ambos casos.
        """
        if not titulares:
            return 1.0
        cob = self._cobertura(equipo, titulares, tipo)
        if cob is None:
            return 1.0
        ref = self._referencia(equipo, tipo)
        if ref <= 0:
            return 1.0
        return max(AJUSTE_MIN, min(cob / ref, AJUSTE_MAX))

    def titulares_previos(self, equipo):
        """Titulares del ultimo partido del equipo. Proxy para el modo previo."""
        return self.ultimos.get(equipo, set())

    # ------------------------------------------------------------------
    # ESCRITURA (despues del partido)
    # ------------------------------------------------------------------

    def agregar(self, equipo, jugadores, fecha):
        """Incorpora un partido ya jugado al historial del equipo."""
        w = self._peso(fecha)
        d = self.equipos.setdefault(equipo, {})
        titulares = set()

        for j in jugadores:
            nombre, pos = j["jugador"], j["posicion"]
            if es_titular(pos):
                titulares.add(nombre)

            reg = d.setdefault(nombre, {"atk": 0.0, "def": 0.0})
            reg["atk"] += (j["xg"] + j["xa"]) * w
            if es_defensiva(pos):
                reg["def"] += j["minutos"] * w

        if titulares:
            self.ultimos[equipo] = titulares

        # Actualizamos la referencia con la cobertura que tuvo ESTE partido,
        # medida ANTES de incorporarlo (si no, se mediria contra si mismo).
        # Consumimos solo las entradas de este equipo: el rival se procesa
        # en su propia llamada a agregar().
        for tipo in ("atk", "def"):
            cob = self._cob_previa.pop((equipo, tipo), None)
            if cob is None:
                continue
            suma, n = self.cob.setdefault(equipo, {}).get(tipo, (0.0, 0))
            self.cob[equipo][tipo] = (suma + cob, n + 1)
            g = self.cob_global[tipo]
            g[0] += cob
            g[1] += 1

    def anotar_cobertura(self, equipo, titulares):
        """Guarda la cobertura observada antes de incorporar el partido."""
        for tipo in ("atk", "def"):
            cob = self._cobertura(equipo, titulares, tipo)
            if cob is not None:
                self._cob_previa[(equipo, tipo)] = cob


# ----------------------------------------------------------------------
# APLICACION A LAMBDA
# ----------------------------------------------------------------------

def aplicar(xg_local, xg_visita, aj, g_atk, g_def):
    """Corrige ambos lambda con los cuatro factores de disponibilidad.

    aj = (atk_local, def_local, atk_visita, def_visita)

        lambda_local  *= atk_local^g_atk  *  def_visita^(-g_def)
        lambda_visita *= atk_visita^g_atk *  def_local^(-g_def)

    El exponente de la defensa es NEGATIVO: si la defensa rival esta
    debilitada (factor < 1), el equipo marca MAS, no menos.

    g_atk = g_def = 0 desactiva la capa por completo.
    """
    atk_l, def_l, atk_v, def_v = aj
    xg_local = xg_local * (atk_l ** g_atk) * (def_v ** -g_def)
    xg_visita = xg_visita * (atk_v ** g_atk) * (def_l ** -g_def)
    return max(0.15, min(xg_local, 5.0)), max(0.15, min(xg_visita, 5.0))


def factores(plantilla, local, visita, jugadores_partido, modo):
    """Calcula los cuatro factores segun el modo elegido.

    jugadores_partido = {equipo: [jugadores]} del partido a predecir.
    Solo se usa en modo "alineacion".
    """
    if modo == "ninguno":
        return (1.0, 1.0, 1.0, 1.0)

    if modo == "alineacion":
        tit = {}
        for equipo in (local, visita):
            lista = (jugadores_partido or {}).get(equipo, [])
            tit[equipo] = {j["jugador"] for j in lista if es_titular(j["posicion"])}
    elif modo == "previo":
        tit = {equipo: plantilla.titulares_previos(equipo) for equipo in (local, visita)}
    else:
        raise ValueError("modo desconocido: {}".format(modo))

    return (plantilla.ajuste(local, tit[local], "atk"),
            plantilla.ajuste(local, tit[local], "def"),
            plantilla.ajuste(visita, tit[visita], "atk"),
            plantilla.ajuste(visita, tit[visita], "def"))

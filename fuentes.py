"""
Abstraccion de fuentes de datos: separa "de donde vienen los partidos"
de "que se hace con ellos".

Antes, cargar() en data_loader.py tenia un if/else hardcodeado entre
"csv" y "understat" repetido tambien en backtest.py (cargar_partidos).
Anadir una tercera fuente (otro proveedor de xG, un feed en vivo, datos
propios) implicaba tocar ambas funciones. Con esta interfaz, una fuente
nueva es una clase mas que implementa FuenteDatos.partidos() y se
registra en FUENTES; nada mas cambia.
"""

from abc import ABC, abstractmethod


class FuenteDatos(ABC):
    """Contrato minimo: dado (liga, temporadas), devuelve una lista de
    partidos ya parseados, cada uno con al menos fecha/local/visita/
    goles_local/goles_visita. El orden cronologico lo garantiza el
    llamador (cargar_partidos ya ordena al final), no la fuente.
    """

    nombre = "abstracta"

    @abstractmethod
    def partidos(self, liga_key, temporadas, forzar=False):
        raise NotImplementedError


class FuenteCSV(FuenteDatos):
    """football-data.co.uk: goles, tiros y cuotas. Sin xG."""

    nombre = "csv"

    def partidos(self, liga_key, temporadas, forzar=False):
        from data_loader import LIGAS, descargar_csv, leer_partidos
        codigo, _ = LIGAS[liga_key]
        partidos = []
        for temp in temporadas:
            partidos.extend(leer_partidos(descargar_csv(codigo, temp, forzar=forzar)))
        return partidos


class FuenteUnderstat(FuenteDatos):
    """Understat via soccerdata: xG real, unido con football-data por fecha."""

    nombre = "understat"

    def partidos(self, liga_key, temporadas, forzar=False):
        from xg_loader import cargar_xg
        return cargar_xg([liga_key], temporadas, refrescar=forzar)


class FuenteSelecciones(FuenteDatos):
    """Partidos de selecciones nacionales (amistosos/clasificatorios en
    fechas FIFA), filtrados a UEFA-UEFA / CONMEBOL-CONMEBOL / UEFA-CONMEBOL
    con confederaciones.filtrar_partidos_fifa() (ver ese modulo para la
    regla exacta).

    NO CONECTADA A NINGUNA FUENTE DE DATOS TODAVIA. Se investigo el stack
    ya integrado en el proyecto (soccerdata: Understat, FBref, ESPN,
    MatchHistory) y NINGUNO cubre amistosos/clasificatorios de selecciones:
      - Understat / ESPN / MatchHistory: solo las "Big 5" ligas de club.
      - FBref: si tiene "INT-World Cup" e "INT-European Championship",
        pero son los TORNEOS FINALES, no las ventanas FIFA de amistosos
        y clasificatorios que se juegan durante el resto del anio (y
        ninguno de los dos trae xG).

    En vez de simular datos o devolver una lista vacia en silencio (lo
    que en un pipeline de apuestas es peor que fallar: parece que "no
    hay partidos" cuando en realidad es que la fuente no existe), esto
    lanza un error explicando el hueco y que hace falta conectar un
    proveedor real (ver el parametro `obtener_partidos_crudos`).

    Para activarla: pasar una funcion `obtener_partidos_crudos(temporadas)
    -> [partidos]` (cada partido con fecha/local/visita/goles_local/
    goles_visita como nombre COMPLETO de pais en ingles, igual que en
    confederaciones.py) que traiga los datos desde el proveedor que se
    elija, y registrarla con registrar_fuente_selecciones().
    """

    nombre = "selecciones"

    def __init__(self, obtener_partidos_crudos=None):
        self._obtener_partidos_crudos = obtener_partidos_crudos

    def partidos(self, liga_key, temporadas, forzar=False):
        if self._obtener_partidos_crudos is None:
            raise NotImplementedError(
                "FuenteSelecciones no tiene un proveedor de datos conectado.\n"
                "Ninguna fuente ya integrada (Understat/FBref/ESPN/MatchHistory "
                "via soccerdata) cubre amistosos/clasificatorios de selecciones "
                "en fechas FIFA -- ver el docstring de esta clase.\n"
                "Conecta un proveedor real con registrar_fuente_selecciones() "
                "antes de usar --fuente selecciones.")

        from logging_setup import get_logger
        from confederaciones import filtrar_partidos_fifa
        from validacion import validar_lote
        logger = get_logger("fuentes.selecciones")

        crudos = self._obtener_partidos_crudos(temporadas)
        # Mismo filtro de calidad que football-data/Understat (goles
        # negativos, fechas imposibles, duplicados): un feed nuevo es
        # justo el caso donde mas conviene no confiar a ciegas.
        validados, reporte = validar_lote(crudos, logger=logger)
        if reporte["descartados"] or reporte["duplicados"]:
            logger.warning("selecciones: %s descartados, %s duplicados de %s totales",
                           reporte["descartados"], reporte["duplicados"], reporte["total"])

        validos, descartados_confed = filtrar_partidos_fifa(validados, logger=logger)
        if descartados_confed:
            logger.info("%s partidos excluidos por confederacion (no UEFA/CONMEBOL)",
                       descartados_confed)
        return validos


FUENTES = {
    "csv": FuenteCSV(),
    "understat": FuenteUnderstat(),
    "selecciones": FuenteSelecciones(),
}


def registrar_fuente_selecciones(obtener_partidos_crudos):
    """Conecta un proveedor real de partidos de selecciones a la fuente
    "selecciones". `obtener_partidos_crudos(temporadas)` debe devolver
    partidos SIN filtrar por confederacion (el filtro lo aplica
    FuenteSelecciones.partidos() automaticamente).
    """
    FUENTES["selecciones"] = FuenteSelecciones(obtener_partidos_crudos)


def obtener_fuente(nombre):
    if nombre not in FUENTES:
        raise ValueError("fuente desconocida: '{}'. Opciones: {}".format(
            nombre, ", ".join(FUENTES)))
    return FUENTES[nombre]


def registrar_fuente(nombre, instancia):
    """Anade una fuente nueva sin tocar este archivo desde otro modulo.

    Uso: registrar_fuente("mi_feed", MiFuente()); luego --fuente mi_feed
    funciona en cualquier CLI que use obtener_fuente().
    """
    FUENTES[nombre] = instancia

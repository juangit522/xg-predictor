"""
Mapeo de selecciones nacionales a confederacion FIFA, y el filtro de
partidos internacionales que pide el pipeline de "fechas FIFA".

Regla de negocio (fijada por el usuario): de los partidos de selecciones
durante parones FIFA, SOLO se procesan aquellos donde:
  - ambas selecciones son UEFA, o
  - ambas selecciones son CONMEBOL, o
  - una es UEFA y la otra CONMEBOL (cruce intercontinental).
Se excluye cualquier partido que involucre a una seleccion de CONCACAF,
CAF, AFC u OFC.

Los nombres de pais aqui son los 55 miembros UEFA y los 10 miembros
CONMEBOL segun el padron de FIFA/UEFA/CONMEBOL (lista estable: los
cambios de afiliacion de confederacion son rarisimos, a diferencia de
un roster de club). No se listan CONCACAF/CAF/AFC/OFC completos porque
el filtro no los necesita: cualquier seleccion que no este en UEFA ni
en CONMEBOL cae automaticamente fuera, sea cual sea su confederacion
real.

IMPORTANTE: este mapeo por si solo NO trae datos de partidos. Es la
capa de filtrado que se aplica DESPUES de obtener los partidos de
alguna fuente (ver fuentes.py::FuenteSelecciones). Sin una fuente real
conectada, este modulo no tiene nada que filtrar.
"""

UEFA = {
    "Albania", "Andorra", "Armenia", "Austria", "Azerbaijan", "Belarus",
    "Belgium", "Bosnia and Herzegovina", "Bulgaria", "Croatia", "Cyprus",
    "Czech Republic", "Denmark", "England", "Estonia", "Faroe Islands",
    "Finland", "France", "Georgia", "Germany", "Gibraltar", "Greece",
    "Hungary", "Iceland", "Israel", "Italy", "Kazakhstan", "Kosovo",
    "Latvia", "Liechtenstein", "Lithuania", "Luxembourg", "Malta",
    "Moldova", "Montenegro", "Netherlands", "North Macedonia",
    "Northern Ireland", "Norway", "Poland", "Portugal", "Republic of Ireland",
    "Romania", "Russia", "San Marino", "Scotland", "Serbia", "Slovakia",
    "Slovenia", "Spain", "Sweden", "Switzerland", "Turkey", "Ukraine",
    "Wales",
}

CONMEBOL = {
    "Argentina", "Bolivia", "Brazil", "Chile", "Colombia", "Ecuador",
    "Paraguay", "Peru", "Uruguay", "Venezuela",
}

CONFEDERACIONES_INCLUIDAS = {"UEFA": UEFA, "CONMEBOL": CONMEBOL}


def confederacion(seleccion):
    """UEFA, CONMEBOL, u OTRA (agrupa Concacaf/CAF/AFC/OFC/desconocido:
    el filtro no necesita distinguirlas entre si, solo excluirlas)."""
    if seleccion in UEFA:
        return "UEFA"
    if seleccion in CONMEBOL:
        return "CONMEBOL"
    return "OTRA"


def es_partido_valido(local, visita):
    """True solo si el partido es UEFA-UEFA, CONMEBOL-CONMEBOL, o el
    cruce UEFA-CONMEBOL en cualquier orden. Cualquier otra combinacion
    (incluida una sola seleccion de Concacaf/CAF/AFC/OFC) se excluye.
    """
    c_local = confederacion(local)
    c_visita = confederacion(visita)
    permitidas = ({"UEFA"}, {"CONMEBOL"}, {"UEFA", "CONMEBOL"})
    return {c_local, c_visita} in permitidas and "OTRA" not in (c_local, c_visita)


def filtrar_partidos_fifa(partidos, logger=None):
    """Filtra una lista de partidos de selecciones, dejando solo los que
    cumplen es_partido_valido(). partidos: dicts con local/visita.

    Devuelve (validos, descartados_por_confederacion).
    """
    validos, descartados = [], 0
    for p in partidos:
        if es_partido_valido(p["local"], p["visita"]):
            validos.append(p)
        else:
            descartados += 1
            if logger:
                logger.info("partido excluido por confederacion: %s (%s) vs %s (%s)",
                           p["local"], confederacion(p["local"]),
                           p["visita"], confederacion(p["visita"]))
    return validos, descartados

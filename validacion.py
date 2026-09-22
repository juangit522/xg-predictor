"""
Validacion de datos: detecta partidos corruptos o inconsistentes antes
de que entren al pipeline y contaminen las fuerzas de los equipos.

Un partido corrupto (goles negativos, fecha invalida, equipo vacio) que
pasa desapercibido no falla ruidosamente: se cuela en gf_/gc_ de algun
equipo y sesga su fuerza en silencio durante toda la temporada. Esto
valida ANTES de acumular, no despues.

validar_partido() devuelve una lista de problemas (texto). Lista vacia
= sin problemas. Algunos problemas son DESCARTABLES (el partido no se
usa: datos imposibles) y otros son SOSPECHOSOS (el partido se conserva
pero se loguea: son raros pero posibles, ej. un 7-0 real).
"""

from datetime import datetime, timedelta

GOLES_MAX_RAZONABLE = 15
XG_MAX_RAZONABLE = 8.0

# Cualquier problema cuyo texto contenga una de estas subcadenas hace
# que el partido se DESCARTE en vez de solo avisar.
_CLAVES_DESCARTABLES = (
    "vacio", "mismo equipo", "sin fecha", "fecha en el futuro",
    "fecha inverosimil", "ausente", "negativo",
)


def validar_partido(p, hoy=None):
    """Valida un partido ya parseado (dict con fecha/local/visita/goles_*).

    No modifica el partido. Devuelve la lista de problemas encontrados.
    """
    problemas = []
    hoy = hoy or datetime.now()

    # --- Identidad ---
    if not p.get("local") or not p.get("visita"):
        problemas.append("equipo local o visita vacio")
    elif p["local"] == p["visita"]:
        problemas.append("local y visita son el mismo equipo ({})".format(p["local"]))

    # --- Fecha ---
    fecha = p.get("fecha")
    if fecha is None:
        problemas.append("sin fecha")
    else:
        if fecha > hoy + timedelta(days=1):
            problemas.append("fecha en el futuro ({})".format(fecha.date()))
        if fecha.year < 2000:
            problemas.append("fecha inverosimil ({})".format(fecha.date()))

    # --- Goles ---
    for etiqueta in ("local", "visita"):
        g = p.get("goles_" + etiqueta)
        if g is None:
            problemas.append("goles_{} ausente".format(etiqueta))
        elif g < 0:
            problemas.append("goles_{} negativo ({})".format(etiqueta, g))
        elif g > GOLES_MAX_RAZONABLE:
            problemas.append("goles_{} inusualmente alto ({})".format(etiqueta, g))

    # --- Tiros vs tiros a puerta (si vienen en el partido) ---
    for etiqueta in ("local", "visita"):
        tiros = p.get("tiros_" + etiqueta)
        sot = p.get("sot_" + etiqueta)
        if tiros is not None and sot is not None and sot > tiros:
            problemas.append("sot_{0} ({1}) > tiros_{0} ({2})".format(etiqueta, sot, tiros))

        goles = p.get("goles_" + etiqueta)
        # Mas goles que tiros a puerta + margen: sospechoso (autogoles y
        # penales lo explican a veces, pero un margen grande no es normal).
        if sot is not None and goles is not None and goles > sot + 3:
            problemas.append("goles_{0} ({1}) muy por encima de sot_{0} ({2})".format(
                etiqueta, goles, sot))

    # --- xG (si viene en el partido) ---
    for etiqueta in ("local", "visita"):
        x = p.get("xg_" + etiqueta)
        if x is not None:
            if x < 0:
                problemas.append("xg_{} negativo ({:.2f})".format(etiqueta, x))
            elif x > XG_MAX_RAZONABLE:
                problemas.append("xg_{} inusualmente alto ({:.2f})".format(etiqueta, x))

    return problemas


def es_descartable(problemas):
    """True si algun problema es grave (datos imposibles, no solo raros)."""
    return any(clave in texto for texto in problemas for clave in _CLAVES_DESCARTABLES)


def validar_lote(partidos, hoy=None, logger=None):
    """Filtra una lista de partidos: descarta invalidos, avisa de sospechosos
    y elimina duplicados exactos (misma fecha + local + visita).

    Devuelve (partidos_validos, reporte). reporte tiene conteos:
    total / validos / descartados / sospechosos / duplicados.
    """
    validos = []
    vistos = set()
    descartados = sospechosos = duplicados = 0

    for p in partidos:
        clave = (p.get("fecha"), p.get("local"), p.get("visita"))
        if clave in vistos and clave != (None, None, None):
            duplicados += 1
            if logger:
                logger.warning("partido duplicado descartado: %s %s vs %s",
                               p.get("fecha"), p.get("local"), p.get("visita"))
            continue

        problemas = validar_partido(p, hoy=hoy)
        if not problemas:
            vistos.add(clave)
            validos.append(p)
            continue

        if es_descartable(problemas):
            descartados += 1
            if logger:
                logger.warning("partido descartado (%s vs %s): %s",
                               p.get("local"), p.get("visita"), "; ".join(problemas))
            continue

        sospechosos += 1
        if logger:
            logger.info("partido sospechoso pero conservado (%s vs %s): %s",
                        p.get("local"), p.get("visita"), "; ".join(problemas))
        vistos.add(clave)
        validos.append(p)

    reporte = {
        "total": len(partidos),
        "validos": len(validos),
        "descartados": descartados,
        "sospechosos": sospechosos,
        "duplicados": duplicados,
    }
    return validos, reporte


# ----------------------------------------------------------------------
# DETECCION DE CAMBIOS DE FORMATO EN EL CSV DE ORIGEN
# ----------------------------------------------------------------------

# Columnas de las que dependemos. Si football-data.co.uk renombra o quita
# alguna, preferimos fallar ruidosamente en el momento de leer el CSV en
# vez de que leer_partidos() la trate como ausente y siga en silencio.
COLUMNAS_CRITICAS = ("Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG")
COLUMNAS_OPCIONALES = ("HS", "AS", "HST", "AST", "B365H", "B365D", "B365A")


def validar_esquema(cabecera, ruta="", logger=None):
    """Verifica que el CSV tenga las columnas de las que depende el parseo.

    cabecera -> lista de nombres de columna (csv.DictReader.fieldnames).
    Lanza SystemExit si falta una columna CRITICA (sin ella leer_partidos
    fallaria en silencio, devolviendo partidos vacios o corruptos).
    Solo avisa (no falla) si falta una OPCIONAL.
    """
    presentes = set(cabecera or [])
    faltantes_criticas = [c for c in COLUMNAS_CRITICAS if c not in presentes]
    if faltantes_criticas:
        raise SystemExit(
            "  [error] el CSV {} no tiene las columnas esperadas: {}\n"
            "  Esto indica que football-data.co.uk cambio su formato.\n"
            "  Columnas encontradas: {}".format(
                ruta, ", ".join(faltantes_criticas), ", ".join(sorted(presentes))))

    faltantes_opcionales = [c for c in COLUMNAS_OPCIONALES if c not in presentes]
    if faltantes_opcionales and logger:
        logger.warning("%s: columnas opcionales ausentes %s (tiros/cuotas "
                       "no disponibles para este archivo)",
                       ruta, ", ".join(faltantes_opcionales))

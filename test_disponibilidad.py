"""
Test de humo de la capa de disponibilidad, con datos inventados.

Verifica las tres propiedades que tienen que cumplirse:
  1. Alineacion habitual -> ajuste neutro (1.00)
  2. Falta el goleador    -> ajuste < 1, lambda baja
  3. El equipo rota mucho -> su rotacion NORMAL no dispara ajuste

Correr con:  python test_disponibilidad.py
"""

from datetime import datetime, timedelta

from disponibilidad import Plantilla, factores, aplicar

HOY = datetime(2026, 1, 1)


def jugador(nombre, pos, minutos, xg, xa=0.0):
    return {"jugador": nombre, "posicion": pos, "minutos": minutos, "xg": xg, "xa": xa}


def plantel(titulares, suplentes=()):
    """Arma la lista de un partido: titulares + suplentes."""
    filas = list(titulares)
    for s in suplentes:
        filas.append(jugador(s, "Sub", 10, 0.0))
    return filas


def alinear(estrella_juega=True, xg_estrella=0.8):
    """Once tipo: 1 estrella, 3 del medio, 4 defensas, 1 portero."""
    t = []
    if estrella_juega:
        t.append(jugador("Estrella", "FW", 90, xg_estrella, 0.2))
    t += [jugador("Medio{}".format(i), "MC", 90, 0.10, 0.10) for i in range(3)]
    t += [jugador("Def{}".format(i), "DC", 90, 0.02) for i in range(4)]
    t.append(jugador("Portero", "GK", 90, 0.0))
    return t


def entrenar(pl, equipo, n=12, estrella=True):
    """Alimenta n partidos al historial del equipo."""
    for i in range(n):
        fecha = HOY + timedelta(days=7 * i)
        lista = plantel(alinear(estrella_juega=estrella), ["Suplente1", "Suplente2"])
        pl.anotar_cobertura(equipo, {j["jugador"] for j in lista if j["posicion"] != "Sub"})
        pl.agregar(equipo, lista, fecha)


def aprox(a, b, tol=0.02):
    return abs(a - b) <= tol


fallos = []


def check(nombre, condicion, detalle=""):
    estado = "OK  " if condicion else "FALLA"
    print("  [{}] {}{}".format(estado, nombre, "  " + detalle if detalle else ""))
    if not condicion:
        fallos.append(nombre)


print("=" * 66)
print("  TEST DE LA CAPA DE DISPONIBILIDAD")
print("=" * 66)

# ---------------------------------------------------------------
# 1. Alineacion habitual -> neutro
# ---------------------------------------------------------------
pl = Plantilla(xi=0.002, fecha_base=HOY)
entrenar(pl, "AtletiFicticio", n=12)
entrenar(pl, "RivalFicticio", n=12)

once_normal = plantel(alinear(True), ["Suplente1", "Suplente2"])
jug = {"AtletiFicticio": once_normal, "RivalFicticio": plantel(alinear(True))}

aj = factores(pl, "AtletiFicticio", "RivalFicticio", jug, "alineacion")
check("alineacion habitual da ajuste neutro",
      all(aprox(x, 1.0) for x in aj),
      "ajustes={}".format(tuple(round(x, 3) for x in aj)))

lam_l, lam_v = aplicar(1.60, 1.10, aj, g_atk=0.5, g_def=0.5)
check("lambda no se mueve sin bajas",
      aprox(lam_l, 1.60) and aprox(lam_v, 1.10),
      "lambda={:.3f} / {:.3f}".format(lam_l, lam_v))

# ---------------------------------------------------------------
# 2. Falta el goleador -> ataque baja
# ---------------------------------------------------------------
sin_estrella = plantel(alinear(estrella_juega=False), ["Suplente1", "Suplente2"])
jug_baja = {"AtletiFicticio": sin_estrella, "RivalFicticio": plantel(alinear(True))}

aj_baja = factores(pl, "AtletiFicticio", "RivalFicticio", jug_baja, "alineacion")
check("sin el goleador el ataque local cae",
      aj_baja[0] < 0.95,
      "atk_local={:.3f}".format(aj_baja[0]))
check("la defensa local no se ve afectada",
      aprox(aj_baja[1], 1.0),
      "def_local={:.3f}".format(aj_baja[1]))

lam_l2, lam_v2 = aplicar(1.60, 1.10, aj_baja, g_atk=0.5, g_def=0.5)
check("lambda local baja al faltar el goleador",
      lam_l2 < 1.60,
      "{:.3f} -> {:.3f}".format(1.60, lam_l2))
check("lambda visitante no sube por una baja ofensiva rival",
      aprox(lam_v2, 1.10),
      "lambda_visita={:.3f}".format(lam_v2))

# ---------------------------------------------------------------
# 3. Un equipo que SIEMPRE rota no debe recibir castigo
# ---------------------------------------------------------------
pl_rot = Plantilla(xi=0.002, fecha_base=HOY)
entrenar(pl_rot, "RotadorFicticio", n=12, estrella=False)  # nunca usa la estrella
entrenar(pl_rot, "RivalFicticio", n=12)

jug_rot = {"RotadorFicticio": plantel(alinear(estrella_juega=False)),
           "RivalFicticio": plantel(alinear(True))}
aj_rot = factores(pl_rot, "RotadorFicticio", "RivalFicticio", jug_rot, "alineacion")
check("la rotacion habitual no genera ajuste",
      aprox(aj_rot[0], 1.0),
      "atk={:.3f}".format(aj_rot[0]))

# ---------------------------------------------------------------
# 4. Defensa debilitada -> el rival marca MAS
# ---------------------------------------------------------------
sin_defensas = plantel([j for j in alinear(True) if not j["posicion"].startswith("D")])
jug_def = {"AtletiFicticio": sin_defensas, "RivalFicticio": plantel(alinear(True))}
aj_def = factores(pl, "AtletiFicticio", "RivalFicticio", jug_def, "alineacion")
lam_l3, lam_v3 = aplicar(1.60, 1.10, aj_def, g_atk=0.0, g_def=0.5)
check("con la defensa local rota, el visitante marca mas",
      lam_v3 > 1.10,
      "def_local={:.3f} -> lambda_visita {:.3f}".format(aj_def[1], lam_v3))

# ---------------------------------------------------------------
# 5. g_atk = g_def = 0 desactiva la capa por completo
# ---------------------------------------------------------------
lam_l4, lam_v4 = aplicar(1.60, 1.10, aj_baja, g_atk=0.0, g_def=0.0)
check("con gamma=0 la capa no hace nada",
      aprox(lam_l4, 1.60, 1e-9) and aprox(lam_v4, 1.10, 1e-9))

# ---------------------------------------------------------------
# 6. Modo "previo" usa la ultima alineacion conocida
# ---------------------------------------------------------------
aj_prev = factores(pl, "AtletiFicticio", "RivalFicticio", jug_baja, "previo")
check("modo previo ignora la alineacion de hoy",
      aprox(aj_prev[0], 1.0),
      "atk={:.3f} (el ultimo partido si tuvo estrella)".format(aj_prev[0]))

# ---------------------------------------------------------------
# 7. Equipo sin historial -> neutro, no explota
# ---------------------------------------------------------------
pl_vacia = Plantilla(xi=0.002, fecha_base=HOY)
aj_vacio = factores(pl_vacia, "Nuevo", "Otro", {}, "alineacion")
check("equipo sin historial devuelve neutro",
      all(x == 1.0 for x in aj_vacio))

print("=" * 66)
if fallos:
    print("  {} FALLOS: {}".format(len(fallos), ", ".join(fallos)))
    raise SystemExit(1)
print("  Todo OK")

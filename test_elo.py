"""
Test de humo del rating Elo, con resultados inventados.

Verifica las propiedades que tienen que cumplirse:
  1. Dos equipos nuevos arrancan parejos (salvo la ventaja de local).
  2. Ganar sube el rating, perder lo baja, con conservacion de la suma
     (lo que gana uno lo pierde el otro: Elo es de suma cero).
  3. Ganarle a un rival mas fuerte da mas puntos que ganarle a uno debil.
  4. Una goleada mueve mas el rating que un resultado ajustado.
  5. elo_a_probabilidades() da una terna valida que suma 1 y favorece
     al equipo con mejor rating.

Correr con:  python test_elo.py
"""

from elo import RatingElo, elo_a_probabilidades, ELO_INICIAL

fallos = []


def check(nombre, condicion, detalle=""):
    estado = "OK  " if condicion else "FALLA"
    print("  [{}] {}{}".format(estado, nombre, "  " + detalle if detalle else ""))
    if not condicion:
        fallos.append(nombre)


def aprox(a, b, tol=0.01):
    return abs(a - b) <= tol


print("=" * 66)
print("  TEST DE RATING ELO")
print("=" * 66)

# ---------------------------------------------------------------
# 1. Equipos nuevos arrancan en el rating inicial
# ---------------------------------------------------------------
elo = RatingElo()
check("equipo sin historial usa el rating inicial",
      elo.rating("Nuevo") == ELO_INICIAL)

# La ventaja de local hace que el favorito sea el local si estan parejos
p_esperada = elo.prob_esperada("Local", "Visita")
check("con ratings iguales, el local es favorito por la ventaja de jugar en casa",
      p_esperada > 0.5, "p_esperada={:.3f}".format(p_esperada))

# ---------------------------------------------------------------
# 2. Ganar sube, perder baja, la suma de deltas es cero (suma constante)
# ---------------------------------------------------------------
antes_a = elo.rating("A")
antes_b = elo.rating("B")
delta = elo.actualizar("A", "B", goles_local=2, goles_visita=0)
check("el ganador sube de rating", elo.rating("A") > antes_a,
      "{:.1f} -> {:.1f}".format(antes_a, elo.rating("A")))
check("el perdedor baja de rating", elo.rating("B") < antes_b,
      "{:.1f} -> {:.1f}".format(antes_b, elo.rating("B")))
check("Elo es de suma cero (lo que gana uno lo pierde el otro)",
      aprox((elo.rating("A") - antes_a), -(elo.rating("B") - antes_b)),
      "delta_A={:.4f} delta_B={:.4f}".format(elo.rating("A") - antes_a,
                                             elo.rating("B") - antes_b))
check("actualizar() devuelve el delta aplicado al local",
      aprox(delta, elo.rating("A") - antes_a))

# ---------------------------------------------------------------
# 3. Ganarle a un rival fuerte vale mas que ganarle a uno debil
# ---------------------------------------------------------------
elo2 = RatingElo()
elo2.establecer_inicial("Grande", 1800.0)
elo2.establecer_inicial("Chico", 1200.0)
elo2.establecer_inicial("Mediano1", 1500.0)
elo2.establecer_inicial("Mediano2", 1500.0)

delta_vs_grande = elo2.actualizar("Retador", "Grande", 1, 0)
elo3 = RatingElo()
elo3.establecer_inicial("Retador", ELO_INICIAL)
elo3.establecer_inicial("Chico", 1200.0)
delta_vs_chico = elo3.actualizar("Retador", "Chico", 1, 0)

check("ganarle a un rival fuerte da mas puntos que ganarle a uno debil",
      delta_vs_grande > delta_vs_chico,
      "vs_grande={:.2f} vs_chico={:.2f}".format(delta_vs_grande, delta_vs_chico))

# ---------------------------------------------------------------
# 4. El margen de gol amplifica el ajuste
# ---------------------------------------------------------------
elo_ajustado = RatingElo()
elo_ajustado.establecer_inicial("X", 1500.0)
elo_ajustado.establecer_inicial("Y", 1500.0)
delta_1_0 = elo_ajustado.actualizar("X", "Y", 1, 0)

elo_goleada = RatingElo()
elo_goleada.establecer_inicial("X", 1500.0)
elo_goleada.establecer_inicial("Y", 1500.0)
delta_5_0 = elo_goleada.actualizar("X", "Y", 5, 0)

check("una goleada mueve mas el rating que un resultado ajustado",
      delta_5_0 > delta_1_0,
      "1-0: {:.2f}  5-0: {:.2f}".format(delta_1_0, delta_5_0))

# ---------------------------------------------------------------
# 5. tabla() ordena de mayor a menor
# ---------------------------------------------------------------
orden = elo2.tabla()
check("tabla() esta ordenada de mayor a menor rating",
      all(orden[i][1] >= orden[i + 1][1] for i in range(len(orden) - 1)),
      str(orden))

# ---------------------------------------------------------------
# 6. elo_a_probabilidades(): terna valida, favorece al mejor rating
# ---------------------------------------------------------------
p_l, p_d, p_v = elo_a_probabilidades(1700, 1500)
check("las probabilidades suman 1", aprox(p_l + p_d + p_v, 1.0),
      "suma={:.4f}".format(p_l + p_d + p_v))
check("el equipo con mejor rating es favorito", p_l > p_v,
      "p_local={:.3f} p_visita={:.3f}".format(p_l, p_v))
check("todas las probabilidades son no negativas",
      p_l >= 0 and p_d >= 0 and p_v >= 0)

p_l2, p_d2, p_v2 = elo_a_probabilidades(1500, 1500)
check("con ratings identicos (sin ventaja de local) el empate es mas probable "
      "que con equipos dispares",
      p_d2 > p_d, "empate_parejo={:.3f} empate_dispar={:.3f}".format(p_d2, p_d))

print("=" * 66)
if fallos:
    print("  {} FALLOS: {}".format(len(fallos), ", ".join(fallos)))
    raise SystemExit(1)
print("  Todo OK")

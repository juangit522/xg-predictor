# 📖 Manual de uso — xG Predictor

Esta guía te explica, paso a paso y sin tecnicismos, cómo usar la aplicación de predicción de partidos. No hace falta saber programar ni entender de estadística: con seguir estos pasos alcanza.

---

## 1. Cómo iniciar la aplicación

1. Andá a la carpeta del proyecto (`xg-predictor`).
2. Hacé **doble clic en `iniciar_app.bat`**.
3. Se va a abrir una ventana negra (la consola) que hace el trabajo de fondo, y a los pocos segundos se abre sola una pestaña en tu navegador con la aplicación.
4. **No cierres la ventana negra** mientras estés usando la app: es la que la mantiene funcionando. Si la cerrás, la app deja de funcionar en el navegador.
5. Para cerrar la aplicación cuando termines, simplemente cerrá esa ventana negra.

### ¿Qué hago si aparece una pantalla "Welcome to Streamlit" pidiendo un Email?

Es normal la primera vez que se instala en una computadora nueva. Es una pantalla de bienvenida del programa que hace funcionar la app por dentro (Streamlit); **no tiene nada que ver con tu cuenta, tus datos ni con internet**.

- Si aparece en la ventana negra un texto que dice `Email:`, simplemente **dejá el campo vacío y apretá la tecla Enter**.
- La aplicación va a seguir abriéndose normalmente después de eso.
- Esto solo debería pasar la primera vez. Las siguientes veces que abras `iniciar_app.bat`, la app arranca directo sin preguntar nada.

---

## 2. Cómo cambiar entre Ligas y Modos de datos

Todos estos controles están en la **barra lateral izquierda** (si no la ves, hacé clic en la flechita `«` en la esquina superior izquierda para desplegarla).

### Cambiar de competición

1. En la barra lateral, buscá el menú desplegable **"Competición"**.
2. Hacé clic y elegí una de las 4 opciones: **Premier League**, **LaLiga**, **Bundesliga** o **Ligue 1**.
3. La aplicación va a tardar unos segundos en cargar los datos de esa liga (vas a ver un mensaje de "Cargando..."). Una vez lista, el título de la pantalla principal va a cambiar, por ejemplo a "Ligue 1 — 917 partidos cargados".

### Elegir la fuente de datos: "xG real" vs "Solo goles"

Debajo del selector de liga vas a ver dos opciones:

- **"xG real (Understat)"** ⭐ *(recomendada)*: usa una métrica llamada **xG** (goles esperados), que mide qué tan buenas fueron las ocasiones de gol de cada equipo, no solo cuántos goles metió. Es una señal más precisa porque no depende de la suerte de un partido puntual (un tiro que pegó en el palo, un rebote raro, etc.).
- **"Solo goles (football-data)"**: usa únicamente el resultado final de los partidos (cuántos goles metió cada equipo), sin mirar qué tan merecido fue el resultado.

**En la práctica:** dejá la opción "xG real" seleccionada salvo que quieras comparar cómo cambia la predicción si solo se mira el resultado final.

### Elegir cuántas temporadas usar

El control deslizante **"Temporadas a usar"** define cuántos años hacia atrás mira el modelo (1, 2 o 3 temporadas). Usar más temporadas le da más historial al modelo, pero también incluye partidos más viejos que pesan menos en el cálculo. Dejarlo en 3 (el valor por defecto) funciona bien en la mayoría de los casos.

La temporada que se está jugando **siempre se incluye** (aparece marcada como "en curso" debajo del control), a partir de septiembre. Sus partidos son los más recientes, así que son los que más pesan. Los datos se actualizan solos varias veces al día. Con **1 temporada** a principio de campaña hay pocos partidos por equipo, así que las predicciones son menos estables: conviene usar 2 o 3.

---

## 3. Cómo interpretar una predicción

1. Andá a la pestaña **"🎯 Predicciones del Día"** (arriba de todo).
2. Elegí el **equipo local** y el **equipo visitante** en los dos menúes desplegables.
3. Hacé clic en el botón rojo **"🔮 Predecir partido"**.

### Las tarjetas de porcentaje (1X2)

Vas a ver tres tarjetas: una para el equipo local, una para el empate y otra para el visitante. Cada una muestra:

- **Un porcentaje (%)**: la probabilidad que calcula el modelo de que ese resultado ocurra. Por ejemplo, "Arsenal 64.3%" significa que el modelo cree que Arsenal tiene un 64.3% de chances de ganar ese partido.
- **"Cuota justa"**: es la cuota que *debería* pagar una casa de apuestas si no ganara ningún margen (si solo se basara en la probabilidad real). Sirve como referencia para comparar contra las cuotas que ofrecen las casas de apuestas reales (ver más abajo).

Los tres porcentajes siempre suman 100%: entre victoria local, empate y victoria visitante se reparte toda la probabilidad del partido.

### ¿Qué son los Goles Esperados (xG)?

Debajo de las tarjetas de 1X2 vas a ver "xG [equipo local]" y "xG [equipo visitante]". Es la cantidad de goles que el modelo espera que meta cada equipo en ese partido, tomando en cuenta qué tan bien ataca ese equipo y qué tan bien defiende el rival.

**Pensalo así:** no es una predicción exacta de "van a hacer 2 goles", sino un promedio esperado. Un equipo con xG 2.1 va a marcar distinto número de goles en distintos partidos, pero en promedio (si jugara ese mismo partido muchas veces) metería alrededor de 2 goles.

También vas a ver dos datos extra:
- **"+2.5 goles"**: la probabilidad de que entre los dos equipos metan 3 goles o más en total.
- **"Ambos marcan"**: la probabilidad de que los dos equipos anoten al menos un gol cada uno.

### La calculadora de cuotas y la "Cuota de Valor" (edge)

Si tenés a mano las cuotas que te ofrece tu casa de apuestas para ese partido, podés cargarlas para ver si el modelo detecta una ventaja:

1. Antes de predecir, abrí el desplegable **"Cuotas de la casa de apuestas (opcional, para detectar valor)"**.
2. Cargá la cuota decimal que viste en tu casa de apuestas para Local (1), Empate (X) y Visitante (2). Podés cargar solo una, dos o las tres — no hace falta completar todas.
3. Hacé clic en **"Predecir partido"**.
4. Debajo de los resultados va a aparecer la sección **"Alerta de valor"**, con un mensaje de color para cada cuota que cargaste:

   - 🟢 **Verde ("posible value bet")**: el modelo calcula que esa cuota paga *más* de lo que debería según su propia probabilidad. Es una señal de que esa apuesta podría tener valor a favor.
   - 🟡 **Amarillo/naranja**: la cuota está *por debajo* de lo que el modelo considera justo — el modelo cree que esa apuesta no conviene tanto como parece.
   - 🔵 **Azul (neutral)**: la cuota está alineada con lo que calcula el modelo, sin ventaja clara para ningún lado.

**Importante:** esto es un cálculo informativo basado en un modelo estadístico, **no es una garantía ni una recomendación de apuesta**. El modelo puede equivocarse, y ninguna predicción reemplaza tu propio criterio. Usalo como una herramienta más de análisis, no como la última palabra.

### Para ver más detalle

Debajo de todo hay dos secciones plegables que podés abrir haciendo clic:

- **"Desglose de fuerzas"**: muestra qué tan bueno es cada equipo atacando y defendiendo, tanto de local como de visitante (1.00 es el promedio de la liga; para el ataque más alto es mejor, para la defensa más bajo es mejor).
- **"Marcadores más probables"**: lista los resultados exactos (2-1, 1-1, etc.) más probables según el modelo.

---

## 4. Cómo ver la aplicación desde el celular

Podés abrir la misma aplicación desde el celular mientras la computadora esté prendida y la app abierta, sin instalar nada. El único requisito es que **el celular y la computadora estén conectados al mismo Wi-Fi** (el de tu casa).

### Pasos:

1. En la computadora, abrí la app como siempre con `iniciar_app.bat` y esperá a que abra en el navegador.
2. Mirá la **ventana negra (consola)** que se abrió junto con el navegador. Ahí va a aparecer un texto parecido a esto:

   ```
   Local URL: http://localhost:8501
   Network URL: http://192.168.1.XX:8501
   ```

3. Anotá o copiá la dirección que aparece al lado de **"Network URL"** (la que empieza con `http://192.168...`). Esa es la dirección de tu computadora dentro de tu red de Wi-Fi.
4. Puede aparecer una ventana de **Windows Defender Firewall** preguntando si querés permitir el acceso. Tildá la opción de **"Redes privadas"** y hacé clic en **"Permitir acceso"** (es seguro: solo permite conexiones desde dispositivos de tu propia casa).
5. En el celular, conectate al mismo Wi-Fi que la computadora.
6. Abrí el navegador del celular (Chrome, Safari, etc.) y escribí (o pegá) esa dirección "Network URL" completa, tal cual aparece (incluyendo el `http://` y los números al final).
7. La aplicación se va a abrir en el celular, funcionando exactamente igual que en la computadora.

### Cosas para tener en cuenta:

- Esto **solo funciona mientras la computadora esté prendida** y la ventana negra de la app siga abierta.
- Esto **solo funciona dentro del mismo Wi-Fi**: si el celular usa datos móviles (4G/5G) en vez del Wi-Fi de casa, no va a poder conectarse.
- La dirección "Network URL" puede cambiar si reiniciás el router o cambiás de red, así que fijate cada vez en la ventana negra cuál es la actual.

---

## ¿Dónde encuentro esto de nuevo?

Este mismo manual está disponible sin salir de la aplicación: buscá la pestaña **"📖 Manual"** arriba de todo, al lado de "Predicciones del Día", "Tabla de Posiciones / Datos" y "Estadísticas del Modelo".

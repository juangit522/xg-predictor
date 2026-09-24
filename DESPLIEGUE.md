# Despliegue

## Cómo se actualizan los datos

Hay dos mecanismos, que se complementan:

| Mecanismo | Dónde corre | Qué hace |
|---|---|---|
| `actualizador.py` (hilo en segundo plano) | Dentro de la app | Arranca con la primera visita. Cada 6 h revisa el cache: los datos de la temporada en curso se vuelven a descargar si tienen más de 12 h, los de temporadas terminadas si tienen más de 7 días. También atiende el botón **Actualizar datos**. La web nunca espera a la red. |
| `.github/workflows/actualizar_datos.yml` | GitHub Actions | Cada día a las 06:00 UTC (o a mano desde la pestaña *Actions*) vuelve a descargar todo, commitea `cache/` si cambió, y eso redespliega la app. |

Hacen falta los dos porque Streamlit Community Cloud **duerme las apps sin
visitas** y **borra los archivos escritos en ejecución al reiniciar**. El
hilo mantiene frescos los datos mientras la app está despierta; el workflow
garantiza que cada arranque parte de un `cache/` reciente.

Variables de entorno opcionales (en Streamlit Cloud: *Settings → Secrets*,
como `XG_INTERVALO_HORAS = "3"`):

- `XG_ACTUALIZACION_AUTO=0`: desactiva el barrido periódico. El botón sigue funcionando.
- `XG_INTERVALO_HORAS` (6): cada cuánto se revisa el cache.
- `XG_EDAD_MAX_EN_CURSO_HORAS` (12): antigüedad máxima de los datos que incluyen la temporada en curso.
- `XG_EDAD_MAX_HORAS` (168): antigüedad máxima de las temporadas ya terminadas.

## Streamlit Community Cloud

1. Subí el repo a GitHub (`git push`).
2. En https://share.streamlit.io → **Create app** → elegí el repo, rama `main`,
   archivo `app.py`.
3. En **Advanced settings** elegí **Python 3.13** (pandas 3 necesita ≥ 3.11).
4. Deploy.
5. En GitHub: **Settings → Actions → General → Workflow permissions →
   Read and write permissions**, para que el workflow pueda commitear el cache.
   Después probalo una vez con **Actions → Actualizar datos → Run workflow**.

## Docker (Render, Railway, Fly.io, VPS…)

Conviene si querés que el hilo de actualización corra siempre, sin que la
app se duerma:

```bash
docker build -t xg-predictor .
docker run -p 8501:8501 -v xg-cache:/app/cache xg-predictor
```

El volumen `xg-cache` conserva lo descargado entre reinicios. El contenedor
respeta la variable `PORT` que inyectan la mayoría de estas plataformas.

## Temporada en curso y equipos ascendidos

La app usa las últimas 1–3 temporadas **incluida la que está en curso**
(desde septiembre; en agosto sigue usando las ya terminadas porque las
fuentes todavía no publicaron partidos). Los scripts de calibración
(`backtest.py`) siguen usando solo temporadas completas.

Cada temporada nueva trae equipos ascendidos que Understat y
football-data nombran distinto. Antes de septiembre corré:

```bash
python xg_loader.py --todas --temporadas 2728 --diagnostico
```

y agregá a `alias_equipos.json` los que falten. Si no, la descarga de xG
de esa liga falla y la app muestra el error con el nombre sugerido.

## Actualizar a mano

```bash
python actualizador.py            # solo lo que falte o esté viejo
python actualizador.py --forzar   # todo de nuevo
```

## Errores de red con Understat

Los `TLS Client Error ... status 0` son cortes transitorios de conexión.
`red_understat.py` los reintenta con sesión nueva, backoff exponencial y,
si el cliente TLS sigue fallando, cambia a `requests`. En los logs se ven
como `WARNING red_understat ... reintento 1/5`. Solo es un problema si
aparece `sin respuesta tras 6 intentos` de forma repetida: en ese caso
Understat está bloqueando la IP (pasa a veces en IPs de datacenter). La
app sigue funcionando con el cache commiteado, y el workflow de GitHub
Actions sale desde otra IP.

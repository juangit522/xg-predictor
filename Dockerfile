# Alternativa a Streamlit Community Cloud para un host con contenedor
# siempre encendido (Render, Railway, Fly.io, un VPS...). Ahi el hilo de
# actualizacion en segundo plano corre sin que la app se duerma.
#
#   docker build -t xg-predictor .
#   docker run -p 8501:8501 -v xg-cache:/app/cache xg-predictor
#
# El volumen en /app/cache conserva lo descargado entre reinicios.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
# En Linux el cliente TLS de soccerdata baja su binario Go de GitHub la
# primera vez que se importa: se hace aqui para que quede en la imagen y
# no dependa de GitHub al arrancar.
RUN python -c "import soccerdata" > /dev/null

COPY . .

# El puerto lo fija el host en muchas plataformas (variable PORT).
EXPOSE 8501
CMD ["sh", "-c", "streamlit run app.py --server.port=${PORT:-8501} --server.address=0.0.0.0"]

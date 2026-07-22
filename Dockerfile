# Imagem de DEV/CI (spec 06). Nao e imagem de producao: a automacao Selenium/Chrome
# roda no HOST Windows (certificado RS + unidade de rede Z:), fora do container.
# Base 3.12-slim para bater com a versao do CI (setup-python 3.12).
FROM python:3.12-slim

# Nao gerar .pyc e nao bufferizar stdout (logs aparecem na hora no docker logs).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FLASK_APP=run.py

WORKDIR /app

# build-essential cobre qualquer dependencia que precise compilar (defensivo: a
# maioria tem wheel). Removido o cache do apt para manter a camada enxuta.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

# Instala deps primeiro (camada cacheada enquanto requirements.txt nao muda).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Codigo da app (no compose de dev o diretorio e montado como volume por cima).
COPY . .

EXPOSE 5000

# flask run --host=0.0.0.0 para o servidor ser alcancavel do host (app.run() do
# run.py liga so em 127.0.0.1). Dev server: sem gunicorn (fora de escopo, spec 06).
CMD ["flask", "run", "--host=0.0.0.0", "--port=5000"]

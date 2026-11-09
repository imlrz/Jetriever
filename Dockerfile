FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/data/jetriever.sqlite3 \
    RETENTION_DAYS=30

WORKDIR /service

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --system jetriever \
    && useradd --system --gid jetriever --home-dir /service jetriever \
    && mkdir -p /data /logs \
    && chown -R jetriever:jetriever /service /data /logs

COPY --chown=jetriever:jetriever app ./app

USER jetriever
EXPOSE 8000
VOLUME ["/data", "/logs"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

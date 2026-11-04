FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt asyncpg redis

COPY botfactory ./botfactory

RUN useradd --create-home --uid 1000 bot && mkdir -p /app/data && chown -R bot /app
USER bot
VOLUME ["/app/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import os,urllib.request,sys; sys.exit(0) if not os.getenv('BASE_URL') and os.getenv('MODE','polling')=='polling' else urllib.request.urlopen('http://127.0.0.1:'+os.getenv('WEB_PORT','8080')+'/healthz', timeout=3)"

CMD ["python", "-m", "botfactory"]

# Agent memory server. Build context: repository root.
FROM python:3.12-slim@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

WORKDIR /app
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --home-dir /app app

COPY servidor/requirements.txt /tmp/requirements.txt
RUN python -m pip install -r /tmp/requirements.txt

COPY redaccion.py fragmentar.py /app/
COPY servidor/ /app/servidor/
COPY config/ /app/config/
COPY pruebas/ /app/pruebas/

USER app
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=4 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/v1/salud', timeout=4).status == 200 else 1)"
CMD ["uvicorn", "servidor.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]

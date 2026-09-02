FROM python:3.13.3-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    ZHIPI_DOCKER=1 \
    ZHIPI_ENV=production \
    ZHIPI_HOST=0.0.0.0 \
    ZHIPI_PORT=8010 \
    ZHIPI_DATA_DIR=/data

WORKDIR /app

COPY requirements.lock.txt /app/requirements.lock.txt
RUN python -m pip install --no-cache-dir -r /app/requirements.lock.txt

COPY app.py /app/app.py
COPY zhipi/ /app/zhipi/
COPY static/ /app/static/
COPY data/ /app/data/

RUN useradd --create-home --shell /usr/sbin/nologin zhipi \
 && mkdir -p /data \
 && chown -R zhipi:zhipi /app /data
USER zhipi

EXPOSE 8010

HEALTHCHECK --interval=30s --timeout=4s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8010/healthz', timeout=3)" || exit 1

CMD ["sh", "-c", "exec python -m uvicorn app:app --host 0.0.0.0 --port 8010 --workers ${ZHIPI_WORKERS:-2}"]

FROM python:3.12-slim-bookworm

RUN useradd --create-home --uid 1000 servarr
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh \
    && mkdir -p /data && chown -R servarr:servarr /data

# The entrypoint starts as root only to make /data writable, then drops to
# PUID:PGID (default 1000) before starting the app. Set `user:` in compose
# to skip that step entirely.

ENV DATA_DIR=/data
EXPOSE 8888

# app/serve.py runs uvicorn on 0.0.0.0:8888 unless SERVARR_HOST / SERVARR_PORT
# say otherwise (e.g. SERVARR_HOST=127.0.0.1 with host networking). Its
# --check asks /health wherever that is.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-m", "app.serve", "--check"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "-m", "app.serve"]

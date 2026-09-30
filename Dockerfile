FROM python:3.12-slim-bookworm

RUN useradd --create-home --uid 1000 servarr
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static

RUN mkdir -p /data && chown -R servarr:servarr /app /data
USER servarr

ENV DATA_DIR=/data
EXPOSE 8888

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8888"]

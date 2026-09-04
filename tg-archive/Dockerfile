FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY archiver ./archiver

ENV TG_ARCHIVE_DATA=/data \
    TG_ARCHIVE_CONFIG=/app/config.yaml \
    PYTHONUNBUFFERED=1

CMD ["python", "-m", "archiver.main"]

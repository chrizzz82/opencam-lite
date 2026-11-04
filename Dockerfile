FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Berlin

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    tzdata \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY server.py /app/server.py
COPY index.html /app/index.html

RUN mkdir -p /config /recordings /tmp/streams

EXPOSE 7777

CMD ["python3", "/app/server.py"]
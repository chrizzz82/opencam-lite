FROM python:3.11-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg tzdata && \
    rm -rf /var/lib/apt/lists/*

ENV TZ=Europe/Berlin
WORKDIR /app

COPY server.py /app/server.py
COPY index.html /app/index.html

EXPOSE 7777

CMD ["python3", "-u", "/app/server.py"]
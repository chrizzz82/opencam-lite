FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# HTML-Dateien aus dem Hauptordner immer nach templates/ kopieren (ueberschreibt alte Kopien)
RUN mkdir -p /app/templates /config /recordings && \
    if [ -f /app/login.html ]; then cp /app/login.html /app/templates/login.html; fi && \
    if [ -f /app/index.html ]; then cp /app/index.html /app/templates/index.html; fi

ENV PYTHONUNBUFFERED=1
ENV CONFIG_DIR=/config
ENV RECORDINGS_DIR=/recordings
ENV PORT=7777

EXPOSE 7777

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '7777'), timeout=4)"

CMD ["python", "server.py"]
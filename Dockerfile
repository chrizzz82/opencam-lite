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

# HTML-Dateien automatisch auch in templates/ bereitstellen, falls sie im Hauptordner liegen
RUN mkdir -p /app/templates /config /recordings && \
    if [ -f /app/login.html ] && [ ! -f /app/templates/login.html ]; then cp /app/login.html /app/templates/login.html; fi && \
    if [ -f /app/index.html ] && [ ! -f /app/templates/index.html ]; then cp /app/index.html /app/templates/index.html; fi

ENV PYTHONUNBUFFERED=1
ENV CONFIG_DIR=/config
ENV RECORDINGS_DIR=/recordings
ENV PORT=7777

EXPOSE 7777

CMD ["python", "server.py"]
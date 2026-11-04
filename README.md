# 📹 OpenCam Lite

**OpenCam Lite** is a lightweight, local-first NVR (Network Video Recorder) built for Docker, CasaOS, and ZimaOS. It is optimized for Tapo and ONVIF-compatible cameras, utilizing a single RTSP stream for both low-latency live viewing and continuous 24/7 recording.

---

## 🚀 Key Features

* **Single-RTSP Architecture:** Pulls one stream per camera and multiplexes it into an in-memory HLS live stream and daily MP4 recording files simultaneously.
* **Daily Recording Folders (`YYYY-MM-DD`):** Automatically structures recordings into daily subfolders (`/recordings/<Camera>/YYYY-MM-DD/`) with pre-emptive directory creation to prevent midnight crashes.
* **Watchdog & Auto-Reconnect:** Continuously monitors stream health, detects frozen connections or nightly camera reboots, and automatically reconnects after a short pause.
* **Advanced Archive Search:** Integrated filters for daily folders, cameras, and precise **Time Ranges (From / To)**, plus single-file and full-day ZIP exports.
* **Signed Tapo PTZ Support:** Built-in pan and tilt controls with ONVIF WS-Security Digest Authentication directly in the card footer.
* **Zone Editor:** Draw privacy blackout masks and green motion detection zones directly over the live video canvas.
* **Multilingual UI (i18n):** Fully available in German, English, French, and Spanish.
* **Push Notifications:** Optional alerts via `ntfy.sh` or Home Assistant webhooks.

---

## 🛠️ Quick Start (Docker Compose)

```yaml
name: opencam-lite
services:
  opencam-lite:
    image: ghcr.io/chrizz82/opencam-lite:latest
    container_name: opencam-lite
    restart: unless-stopped
    network_mode: host
    environment:
      - TZ=Europe/Berlin
    volumes:
      - ./config:/config
      - ./recordings:/recordings

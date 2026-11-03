# 🛡️ OpenCam Lite

**OpenCam Lite** is a lightweight, self-hosted, local-first NVR (Network Video Recorder) built for ZimaOS, CasaOS, and Docker environments. Designed as an easy-to-use alternative to bloated video management systems, it focuses on low resource usage, instant setup, and full local control.

![Status](https://img.shields.io/badge/version-v0.1.0-brightgreen) ![Docker](https://img.shields.io/badge/docker-ready-blue) ![License](https://img.shields.io/badge/license-MIT-yellow)

---

## ✨ Key Features

* **Single RTSP Connection Architecture:** Uses only one RTSP stream per camera simultaneously for live viewing and 24/7 recording—preventing camera-side stream lockouts (especially on TP-Link Tapo cameras).
* **ONVIF Auto-Discovery & Manual Setup:** Scan your local network for ONVIF/RTSP cameras with a single click or configure cameras manually using built-in brand presets.
* **Customizable Live Grid:** Freely resize camera tiles via drag-and-drop (persisted in `localStorage`) with smooth digital zoom and pan.
* **Visual Zone Editor:** Draw Privacy Zones (permanent blackout in stream and recordings) and Motion Zones directly over the live video feed.
* **Flexible 24/7 Recording:** Choose recording file lengths in 15-minute increments (15 to 120 minutes) with automatic retention cleanup and ZIP time-range export.
* **Instant Snapshots:** Zero-delay snapshots from the active stream without opening additional RTSP connections.
* **Multi-Language UI:** English (default), German, French, and Spanish with server-side persistence.
* **ONVIF PTZ & Alerts:** Built-in pan/tilt controls and push notifications via `ntfy.sh` or Home Assistant webhooks.

---

## 🚀 Quick Start (Docker Compose)

1. Clone this repository or copy the files into `/DATA/AppData/opencam-lite`.
2. Run the container:

```bash
docker compose up -d --build

3. Open http://<your-server-ip>:7777 in your browser.

        Default Login: admin / admin

📹 Supported Camera Presets

    TP-Link Tapo

    Reolink

    Hikvision

    Dahua / Amcrest

    Foscam

    Uniview (UNV)

    Axis

    Generic ONVIF / Custom RTSP Path

🔒 Privacy First

OpenCam Lite runs 100% locally. No cloud accounts, no subscriptions, and no external telemetry.

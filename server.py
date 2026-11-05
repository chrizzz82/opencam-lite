import os
import io
import re
import cv2
import time
import json
import uuid
import base64
import socket
import shutil
import zipfile
import hashlib
import threading
import datetime
import requests
import numpy as np
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import wraps
from flask import (
    Flask, Response, render_template, request, jsonify,
    session, redirect, url_for, send_file, abort
)
from werkzeug.security import generate_password_hash, check_password_hash

APP_VERSION = "v1.1.0"

# RTSP über TCP erzwingen und Timeout setzen (verhindert Hänger bei Tapo/Hikvision)
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|stimeout;5000000"

TEMPLATE_DIR = "templates" if os.path.isdir("templates") and os.path.exists(os.path.join("templates", "index.html")) else "."
app = Flask(__name__, template_folder=TEMPLATE_DIR)
app.secret_key = os.environ.get("SECRET_KEY", "opencam_lite_secret_key_2026")

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
RECORDINGS_DIR = os.environ.get("RECORDINGS_DIR", "/recordings")

os.makedirs(CONFIG_DIR, exist_ok=True)
os.makedirs(RECORDINGS_DIR, exist_ok=True)

USERS_FILE = os.path.join(CONFIG_DIR, "users.json")
CAMERAS_FILE = os.path.join(CONFIG_DIR, "cameras.json")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")

config_lock = threading.Lock()
workers_lock = threading.Lock()
camera_workers = {}


# ==============================================================================
# KONFIGURATION & PERSISTENZ
# ==============================================================================

def load_json(path, default):
    with config_lock:
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(default, f, indent=4, ensure_ascii=False)
            return default
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default


def save_json(path, data):
    with config_lock:
        temp_path = path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        os.replace(temp_path, path)


def init_configs():
    if not os.path.exists(USERS_FILE):
        default_users = {
            "admin": {
                "password_hash": generate_password_hash("admin"),
                "role": "admin",
                "created_at": datetime.datetime.now().isoformat()
            }
        }
        save_json(USERS_FILE, default_users)

    if not os.path.exists(CAMERAS_FILE):
        save_json(CAMERAS_FILE, [])

    default_settings = {
        "language": "en",
        "recording_interval": 15,  # 15 bis 120 Minuten
        "retention_days": 7,
        "motion_post_buffer": 15,  # Sekunden Nachlauf bei Bewegung
        "ntfy_url": "",
        "ha_webhook_url": "",
        "alert_cooldown": 60
    }
    current_settings = load_json(SETTINGS_FILE, default_settings)
    for k, v in default_settings.items():
        if k not in current_settings:
            current_settings[k] = v
    save_json(SETTINGS_FILE, current_settings)


init_configs()


def ensure_daily_folders():
    """Erstellt die YYYY-MM-DD Ordner für heute und morgen vorab (verhindert Mitternachts-Fehler)."""
    now = datetime.datetime.now()
    for delta in (0, 1):
        day_str = (now + datetime.timedelta(days=delta)).strftime("%Y-%m-%d")
        day_path = os.path.join(RECORDINGS_DIR, day_str)
        os.makedirs(day_path, exist_ok=True)


def cleanup_old_recordings():
    """Löscht Tagesordner, die älter als retention_days sind."""
    settings = load_json(SETTINGS_FILE, {})
    retention_days = int(settings.get("retention_days", 7))
    if retention_days <= 0:
        return
    cutoff = datetime.date.today() - datetime.timedelta(days=retention_days)
    try:
        for entry in os.listdir(RECORDINGS_DIR):
            entry_path = os.path.join(RECORDINGS_DIR, entry)
            if os.path.isdir(entry_path) and re.match(r"^\d{4}-\d{2}-\d{2}$", entry):
                try:
                    folder_date = datetime.datetime.strptime(entry, "%Y-%m-%d").date()
                    if folder_date < cutoff:
                        shutil.rmtree(entry_path, ignore_errors=True)
                except ValueError:
                    pass
    except Exception as e:
        print(f"[Cleanup] Fehler: {e}")


# ==============================================================================
# BENACHRICHTIGUNGEN (NTFY.SH & HOME ASSISTANT WEBHOOK)
# ==============================================================================

def trigger_motion_alert(cam_name, cam_id):
    settings = load_json(SETTINGS_FILE, {})
    ntfy_url = settings.get("ntfy_url", "").strip()
    ha_url = settings.get("ha_webhook_url", "").strip()
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if ntfy_url:
        try:
            requests.post(
                ntfy_url,
                data=f"Bewegung erkannt: {cam_name} ({timestamp})".encode("utf-8"),
                headers={"Title": f"OpenCam Alert: {cam_name}", "Priority": "high"},
                timeout=4
            )
        except Exception as e:
            print(f"[Alert] ntfy Fehler: {e}")

    if ha_url:
        try:
            requests.post(
                ha_url,
                json={"camera_id": cam_id, "camera_name": cam_name, "event": "motion", "timestamp": timestamp},
                timeout=4
            )
        except Exception as e:
            print(f"[Alert] Home Assistant Webhook Fehler: {e}")


# ==============================================================================
# SINGLE RTSP CONNECTION WORKER (LIVE + MOTION + 24/7 + WATCHDOG)
# ==============================================================================

class CameraStreamWorker:
    def __init__(self, cam_config):
        self.config = cam_config
        self.cam_id = cam_config["id"]
        self.running = True
        self.lock = threading.Lock()
        self.latest_jpeg = None
        self.latest_raw_shape = (720, 1280)
        self.last_frame_time = time.time()
        self.connected = False
        self.motion_detected = False
        self.last_motion_time = 0
        self.last_alert_time = 0

        # VideoWriter Status
        self.writer = None
        self.current_rec_start = 0
        self.current_rec_date = ""
        self.current_rec_mode = ""

        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

    def update_config(self, new_config):
        with self.lock:
            rtsp_changed = self.config.get("rtsp_url") != new_config.get("rtsp_url")
            self.config = new_config
            if rtsp_changed:
                self.last_frame_time = 0

    def stop(self):
        self.running = False
        self._close_writer()

    def get_jpeg(self):
        with self.lock:
            if self.latest_jpeg is not None:
                return self.latest_jpeg
        return self._generate_placeholder_jpeg("Verbinde Kamera...")

    def _generate_placeholder_jpeg(self, text):
        img = np.zeros((480, 854, 3), dtype=np.uint8)
        img[:] = (22, 24, 29)
        cv2.putText(img, self.config.get("name", "Camera"), (30, 60),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, (220, 220, 220), 2)
        cv2.putText(img, text, (30, 240),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (140, 150, 165), 2)
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cv2.putText(img, ts, (30, 440),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (100, 110, 120), 1)
        ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        return buf.tobytes() if ok else b""

    def _apply_privacy_zones(self, frame, privacy_zones):
        if not privacy_zones:
            return
        h, w = frame.shape[:2]
        for z in privacy_zones:
            try:
                x1 = max(0, int(float(z["x"]) * w))
                y1 = max(0, int(float(z["y"]) * h))
                x2 = min(w, int((float(z["x"]) + float(z["w"])) * w))
                y2 = min(h, int((float(z["y"]) + float(z["h"])) * h))
                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 0, 0), -1)
            except Exception:
                pass

    def _detect_motion(self, prev_gray, curr_gray, motion_zones, sensitivity):
        if prev_gray is None or prev_gray.shape != curr_gray.shape:
            return False
        diff = cv2.absdiff(prev_gray, curr_gray)
        _, thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)

        h, w = thresh.shape[:2]
        if motion_zones:
            mask = np.zeros((h, w), dtype=np.uint8)
            for z in motion_zones:
                try:
                    x1 = max(0, int(float(z["x"]) * w))
                    y1 = max(0, int(float(z["y"]) * h))
                    x2 = min(w, int((float(z["x"]) + float(z["w"])) * w))
                    y2 = min(h, int((float(z["y"]) + float(z["h"])) * h))
                    cv2.rectangle(mask, (x1, y1), (x2, y2), 255, -1)
                except Exception:
                    pass
            thresh = cv2.bitwise_and(thresh, mask)

        changed_pixels = cv2.countNonZero(thresh)
        total_pixels = float(h * w)
        sens = max(1, min(100, int(sensitivity or 50)))
        required_ratio = 0.05 * ((101 - sens) / 100.0) * 0.5 + 0.0005
        return (changed_pixels / total_pixels) >= required_ratio

    def _close_writer(self):
        if self.writer is not None:
            try:
                self.writer.release()
            except Exception:
                pass
            self.writer = None

    def _handle_recording(self, frame, fps):
        settings = load_json(SETTINGS_FILE, {})
        interval_min = int(settings.get("recording_interval", 15))
        interval_min = max(15, min(120, interval_min))
        interval_sec = interval_min * 60
        post_buffer = int(settings.get("motion_post_buffer", 15))

        mode = self.config.get("recording_mode", "live")
        now = time.time()
        dt_now = datetime.datetime.now()
        today_str = dt_now.strftime("%Y-%m-%d")

        should_record = False
        if mode == "24/7":
            should_record = True
        elif mode == "motion":
            if self.motion_detected or (now - self.last_motion_time <= post_buffer):
                should_record = True

        if not should_record:
            self._close_writer()
            return

        if (
            self.writer is None
            or self.current_rec_date != today_str
            or (now - self.current_rec_start) >= interval_sec
        ):
            self._close_writer()
            ensure_daily_folders()
            day_folder = os.path.join(RECORDINGS_DIR, today_str)
            os.makedirs(day_folder, exist_ok=True)

            safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", self.config.get("name", "Cam"))
            time_str = dt_now.strftime("%H-%M-%S")
            tag = "MOTION" if mode == "motion" else "24x7"
            filename = f"{self.cam_id}__{safe_name}__{time_str}__{tag}.mp4"
            filepath = os.path.join(day_folder, filename)

            h, w = frame.shape[:2]
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            out_fps = max(5.0, min(30.0, float(fps if fps and fps > 0 else 15.0)))
            self.writer = cv2.VideoWriter(filepath, fourcc, out_fps, (w, h))
            self.current_rec_start = now
            self.current_rec_date = today_str
            self.current_rec_mode = mode

        if self.writer is not None and self.writer.isOpened():
            self.writer.write(frame)

    def _run_loop(self):
        cap = None
        prev_gray = None
        fps_estimate = 15.0

        while self.running:
            rtsp_url = self.config.get("rtsp_url", "").strip()
            if not rtsp_url:
                self.connected = False
                with self.lock:
                    self.latest_jpeg = self._generate_placeholder_jpeg("Keine RTSP-URL konfiguriert")
                time.sleep(2)
                continue

            # Watchdog: Prüft auf Verbindungsabbruch oder > 25 Sekunden ohne Frame
            if cap is None or not cap.isOpened() or (time.time() - self.last_frame_time > 25.0):
                self.connected = False
                self._close_writer()
                if cap is not None:
                    try:
                        cap.release()
                    except Exception:
                        pass
                with self.lock:
                    self.latest_jpeg = self._generate_placeholder_jpeg("Verbinde Stream (Watchdog)...")
                try:
                    cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
                    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                except Exception:
                    cap = None
                if cap is None or not cap.isOpened():
                    time.sleep(3)
                    continue
                self.last_frame_time = time.time()
                prev_gray = None

            ret, frame = cap.read()
            if not ret or frame is None:
                time.sleep(0.1)
                continue

            now = time.time()
            self.last_frame_time = now
            self.connected = True
            self.latest_raw_shape = frame.shape[:2]

            cam_fps = cap.get(cv2.CAP_PROP_FPS)
            if cam_fps and 1.0 <= cam_fps <= 60.0:
                fps_estimate = cam_fps

            with self.lock:
                privacy_zones = list(self.config.get("privacy_zones", []))
                motion_zones = list(self.config.get("motion_zones", []))
                sensitivity = self.config.get("motion_sensitivity", 50)
                cam_name = self.config.get("name", "Camera")

            # 1. Privacy Zones permanent schwärzen (vor Live-Stream UND vor Aufnahme)
            self._apply_privacy_zones(frame, privacy_zones)

            # 2. Bewegungserkennung berechnen
            small = cv2.resize(frame, (320, 180))
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (9, 9), 0)

            is_moving = self._detect_motion(prev_gray, gray, motion_zones, sensitivity)
            prev_gray = gray
            self.motion_detected = is_moving

            if is_moving:
                self.last_motion_time = now
                settings = load_json(SETTINGS_FILE, {})
                cooldown = int(settings.get("alert_cooldown", 60))
                if now - self.last_alert_time >= cooldown:
                    self.last_alert_time = now
                    threading.Thread(
                        target=trigger_motion_alert,
                        args=(cam_name, self.cam_id),
                        daemon=True
                    ).start()

            # 3. Aufnahme verarbeiten (ohne künstliches Text-Overlay, da Kamera-OSD vorhanden)
            try:
                self._handle_recording(frame, fps_estimate)
            except Exception as e:
                print(f"[Recording] Fehler bei {cam_name}: {e}")

            # 4. JPEG für Live-Stream & Sofort-Snapshots aktualisieren
            ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            if ok:
                with self.lock:
                    self.latest_jpeg = buffer.tobytes()

        self._close_writer()
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass


def sync_camera_workers():
    cameras = load_json(CAMERAS_FILE, [])
    active_ids = set()
    with workers_lock:
        for cam in cameras:
            cid = cam["id"]
            active_ids.add(cid)
            if cid in camera_workers:
                camera_workers[cid].update_config(cam)
            else:
                camera_workers[cid] = CameraStreamWorker(cam)

        for cid in list(camera_workers.keys()):
            if cid not in active_ids:
                camera_workers[cid].stop()
                del camera_workers[cid]


def background_housekeeping():
    while True:
        try:
            ensure_daily_folders()
            cleanup_old_recordings()
        except Exception as e:
            print(f"[Housekeeping] Fehler: {e}")
        time.sleep(1800)


sync_camera_workers()
threading.Thread(target=background_housekeeping, daemon=True).start()


# ==============================================================================
# HYBRID ONVIF SCANNER (MULTICAST + PARALLELER TCP-SCAN AB .2 BIS .254)
# ==============================================================================

def get_local_subnet_prefix():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        parts = ip.split(".")
        if len(parts) == 4 and not ip.startswith("172.17."):
            return f"{parts[0]}.{parts[1]}.{parts[2]}"
    except Exception:
        pass
    return "192.168.178"


def is_router_ip(ip_str):
    """Filtert strikt .1 (Router/Gateway) und ungültige IPs heraus."""
    parts = ip_str.strip().split(".")
    if len(parts) != 4:
        return True
    try:
        last_octet = int(parts[3])
        return last_octet < 2 or last_octet > 254
    except ValueError:
        return True


def ws_discovery_scan(timeout=2.0):
    discovered_ips = set()
    probe_id = str(uuid.uuid4())
    probe_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
                xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
                xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
                xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
        <e:Header>
            <w:MessageID>uuid:{probe_id}</w:MessageID>
            <w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005/04/discovery</w:To>
            <w:Action a:mustUnderstand="true" xmlns:a="http://www.w3.org/2005/08/addressing">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
        </e:Header>
        <e:Body>
            <d:Probe>
                <d:Types>dn:NetworkVideoTransmitter</d:Types>
            </d:Probe>
        </e:Body>
    </e:Envelope>"""

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(timeout)
    try:
        sock.sendto(probe_xml.encode("utf-8"), ("239.255.255.250", 3702))
        start = time.time()
        while time.time() - start < timeout:
            try:
                data, addr = sock.recvfrom(65535)
                sender_ip = addr[0]
                if not is_router_ip(sender_ip):
                    discovered_ips.add(sender_ip)
                text = data.decode("utf-8", errors="ignore")
                for match in re.findall(r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})", text):
                    if not is_router_ip(match):
                        discovered_ips.add(match)
            except socket.timeout:
                break
            except Exception:
                break
    except Exception:
        pass
    finally:
        sock.close()
    return discovered_ips


def probe_host_ports(ip):
    """Prüft typische Kamera-Ports (RTSP 554, Tapo ONVIF 2020, Hikvision 8000, ONVIF 8899/8554)."""
    if is_router_ip(ip):
        return None

    ports_to_check = [554, 2020, 8000, 8899, 8554]
    open_ports = []
    for port in ports_to_check:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.35)
        try:
            if s.connect_ex((ip, port)) == 0:
                open_ports.append(port)
        except Exception:
            pass
        finally:
            s.close()

    if not open_ports:
        return None

    brand = "generic"
    onvif_port = 80
    rtsp_path = "/stream1"
    label = "ONVIF / RTSP Camera"

    if 2020 in open_ports:
        brand = "tapo"
        onvif_port = 2020
        rtsp_path = "/stream1"
        label = "TP-Link Tapo Camera"
    elif 8000 in open_ports:
        brand = "hikvision"
        onvif_port = 80
        rtsp_path = "/Streaming/Channels/101"
        label = "Hikvision Camera"
    elif 8899 in open_ports:
        brand = "onvif"
        onvif_port = 8899
        rtsp_path = "/live/ch0"
        label = "ONVIF Camera"

    return {
        "ip": ip,
        "brand": brand,
        "name": f"{label} ({ip})",
        "open_ports": open_ports,
        "onvif_port": onvif_port,
        "suggested_rtsp": f"rtsp://admin:password@{ip}:554{rtsp_path}"
    }


def hybrid_network_scan(subnet_prefix=None):
    if not subnet_prefix or not re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}$", subnet_prefix.strip()):
        subnet_prefix = get_local_subnet_prefix()
    subnet_prefix = subnet_prefix.strip()

    ws_ips = ws_discovery_scan(timeout=1.5)
    # Strikt ab .2 bis .254 scannen (Router auf .1 wird ausgeschlossen!)
    target_ips = [f"{subnet_prefix}.{i}" for i in range(2, 255)]
    for wip in ws_ips:
        if not is_router_ip(wip) and wip not in target_ips:
            target_ips.append(wip)

    results = []
    with ThreadPoolExecutor(max_workers=64) as executor:
        futures = {executor.submit(probe_host_ports, ip): ip for ip in target_ips}
        for future in as_completed(futures):
            res = future.result()
            if res is not None:
                results.append(res)

    results.sort(key=lambda x: [int(p) for p in x["ip"].split(".")])
    return results


# ==============================================================================
# SIGNED TAPO & ONVIF PTZ (WS-SECURITY DIGEST AUTHENTICATION)
# ==============================================================================

def build_wsse_header(username, password):
    nonce_raw = os.urandom(16)
    created = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = hashlib.sha1(nonce_raw + created.encode("utf-8") + password.encode("utf-8")).digest()
    password_digest = base64.b64encode(digest).decode("utf-8")
    nonce_b64 = base64.b64encode(nonce_raw).decode("utf-8")

    return f"""
    <s:Header>
        <Security s:mustUnderstand="1" xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
            <UsernameToken>
                <Username>{username}</Username>
                <Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{password_digest}</Password>
                <Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{nonce_b64}</Nonce>
                <Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">{created}</Created>
            </UsernameToken>
        </Security>
    </s:Header>"""


def execute_onvif_ptz(cam, direction, speed=0.5):
    ip = cam.get("ip", "").strip()
    port = int(cam.get("onvif_port", 2020) or 2020)
    user = cam.get("username", "admin")
    password = cam.get("password", "")
    profile_token = cam.get("profile_token", "profile_1")

    if not ip:
        return False, "Keine Kamera-IP hinterlegt"

    vx, vy, vz = 0.0, 0.0, 0.0
    if direction == "left":
        vx = -speed
    elif direction == "right":
        vx = speed
    elif direction == "up":
        vy = speed
    elif direction == "down":
        vy = -speed
    elif direction == "zoom_in":
        vz = speed
    elif direction == "zoom_out":
        vz = -speed

    wsse = build_wsse_header(user, password)

    if direction == "stop":
        body = f"""
        <s:Body xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
            <Stop xmlns="http://www.onvif.org/ver20/ptz/wsdl">
                <ProfileToken>{profile_token}</ProfileToken>
                <PanTilt>true</PanTilt>
                <Zoom>true</Zoom>
            </Stop>
        </s:Body>"""
    else:
        body = f"""
        <s:Body xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
            <ContinuousMove xmlns="http://www.onvif.org/ver20/ptz/wsdl">
                <ProfileToken>{profile_token}</ProfileToken>
                <Velocity>
                    <PanTilt x="{vx}" y="{vy}" xmlns="http://www.onvif.org/ver10/schema"/>
                    <Zoom x="{vz}" xmlns="http://www.onvif.org/ver10/schema"/>
                </Velocity>
            </ContinuousMove>
        </s:Body>"""

    envelope = f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">{wsse}{body}</s:Envelope>'
    endpoints = [
        f"http://{ip}:{port}/onvif/ptz_service",
        f"http://{ip}:{port}/onvif/device_service",
        f"http://{ip}:2020/onvif/ptz_service"
    ]

    for url in endpoints:
        try:
            resp = requests.post(
                url,
                data=envelope.encode("utf-8"),
                headers={"Content-Type": "application/soap+xml; charset=utf-8"},
                timeout=2.5
            )
            if resp.status_code < 400:
                if direction != "stop":
                    threading.Timer(0.45, execute_onvif_ptz, args=(cam, "stop", 0)).start()
                return True, "OK"
        except Exception:
            continue

    return False, "PTZ-Endpunkt antwortet nicht"


# ==============================================================================
# AUTHENTIFIZIERUNG & ROLLEN-DEKORATOREN (ADMIN vs. READONLY)
# ==============================================================================

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Nicht angemeldet"}), 401
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            return jsonify({"error": "Nicht angemeldet"}), 401
        if session.get("role") != "admin":
            return jsonify({"error": "Keine Berechtigung (Nur für Administratoren)"}), 403
        return f(*args, **kwargs)
    return decorated


# ==============================================================================
# ROUTEN: LOGIN & HAUPTSEITE
# ==============================================================================

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    settings = load_json(SETTINGS_FILE, {})
    lang = settings.get("language", "en")

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        users = load_json(USERS_FILE, {})

        if username in users and check_password_hash(users[username]["password_hash"], password):
            session["user"] = username
            session["role"] = users[username].get("role", "readonly")
            return redirect(url_for("index"))
        error = "Ungültiger Benutzername oder Passwort / Invalid credentials"

    return render_template("login.html", error=error, lang=lang, version=APP_VERSION)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return render_template(
        "index.html",
        username=session.get("user"),
        role=session.get("role", "readonly"),
        version=APP_VERSION
    )


# ==============================================================================
# API: BOOTSTRAP, KAMERAS & EINSTELLUNGEN
# ==============================================================================

@app.route("/api/bootstrap", methods=["GET"])
@login_required
def api_bootstrap():
    role = session.get("role", "readonly")
    settings = load_json(SETTINGS_FILE, {})
    cameras = load_json(CAMERAS_FILE, [])

    safe_cameras = []
    for c in cameras:
        with workers_lock:
            w = camera_workers.get(c["id"])
            online = w.connected if w else False
            motion = w.motion_detected if w else False

        if role == "admin":
            cam_copy = dict(c)
        else:
            cam_copy = {
                "id": c["id"],
                "name": c.get("name", "Camera"),
                "recording_mode": c.get("recording_mode", "live"),
                "ptz_enabled": False
            }
        cam_copy["online"] = online
        cam_copy["motion"] = motion
        safe_cameras.append(cam_copy)

    return jsonify({
        "version": APP_VERSION,
        "user": session.get("user"),
        "role": role,
        "settings": settings if role == "admin" else {"language": settings.get("language", "en")},
        "cameras": safe_cameras,
        "detected_subnet": get_local_subnet_prefix()
    })


@app.route("/api/cameras", methods=["GET", "POST"])
@login_required
def api_cameras():
    if request.method == "GET":
        if session.get("role") != "admin":
            return jsonify({"error": "Forbidden"}), 403
        return jsonify(load_json(CAMERAS_FILE, []))

    if session.get("role") != "admin":
        return jsonify({"error": "Keine Berechtigung"}), 403

    data = request.json or {}
    cameras = load_json(CAMERAS_FILE, [])
    cam_id = data.get("id") or str(uuid.uuid4())[:8]

    ip = (data.get("ip") or "").strip()
    user = (data.get("username") or "admin").strip()
    password = data.get("password") or ""
    brand = data.get("brand") or "tapo"
    rtsp_url = (data.get("rtsp_url") or "").strip()

    if not rtsp_url and ip:
        if brand == "tapo":
            rtsp_url = f"rtsp://{user}:{password}@{ip}:554/stream1"
        elif brand == "hikvision":
            rtsp_url = f"rtsp://{user}:{password}@{ip}:554/Streaming/Channels/101"
        elif brand == "reolink":
            rtsp_url = f"rtsp://{user}:{password}@{ip}:554/h264Preview_01_main"
        else:
            rtsp_url = f"rtsp://{user}:{password}@{ip}:554/stream1"

    rec_mode = data.get("recording_mode", "live")
    if rec_mode not in ("live", "motion", "24/7"):
        rec_mode = "live"

    existing = next((c for c in cameras if c["id"] == cam_id), None)
    cam_obj = {
        "id": cam_id,
        "name": (data.get("name") or f"Kamera {ip}").strip(),
        "brand": brand,
        "ip": ip,
        "onvif_port": int(data.get("onvif_port") or (2020 if brand == "tapo" else 80)),
        "username": user,
        "password": password,
        "rtsp_url": rtsp_url,
        "recording_mode": rec_mode,
        "ptz_enabled": bool(data.get("ptz_enabled", True)),
        "motion_sensitivity": int(data.get("motion_sensitivity", existing.get("motion_sensitivity", 50) if existing else 50)),
        "privacy_zones": data.get("privacy_zones", existing.get("privacy_zones", []) if existing else []),
        "motion_zones": data.get("motion_zones", existing.get("motion_zones", []) if existing else [])
    }

    if existing:
        idx = cameras.index(existing)
        cameras[idx] = cam_obj
    else:
        cameras.append(cam_obj)

    save_json(CAMERAS_FILE, cameras)
    sync_camera_workers()
    return jsonify({"status": "ok", "camera": cam_obj})


@app.route("/api/cameras/<cam_id>", methods=["DELETE"])
@admin_required
def api_delete_camera(cam_id):
    cameras = load_json(CAMERAS_FILE, [])
    cameras = [c for c in cameras if c["id"] != cam_id]
    save_json(CAMERAS_FILE, cameras)
    sync_camera_workers()
    return jsonify({"status": "ok"})


@app.route("/api/cameras/<cam_id>/mode", methods=["POST"])
@admin_required
def api_camera_mode(cam_id):
    data = request.json or {}
    mode = data.get("recording_mode", "live")
    if mode not in ("live", "motion", "24/7"):
        return jsonify({"error": "Ungültiger Modus"}), 400

    cameras = load_json(CAMERAS_FILE, [])
    for c in cameras:
        if c["id"] == cam_id:
            c["recording_mode"] = mode
            break
    save_json(CAMERAS_FILE, cameras)
    sync_camera_workers()
    return jsonify({"status": "ok", "recording_mode": mode})


@app.route("/api/cameras/<cam_id>/zones", methods=["POST"])
@admin_required
def api_camera_zones(cam_id):
    data = request.json or {}
    cameras = load_json(CAMERAS_FILE, [])
    for c in cameras:
        if c["id"] == cam_id:
            if "privacy_zones" in data:
                c["privacy_zones"] = data["privacy_zones"]
            if "motion_zones" in data:
                c["motion_zones"] = data["motion_zones"]
            if "motion_sensitivity" in data:
                c["motion_sensitivity"] = int(data["motion_sensitivity"])
            break
    save_json(CAMERAS_FILE, cameras)
    sync_camera_workers()
    return jsonify({"status": "ok"})


@app.route("/api/scan", methods=["POST"])
@admin_required
def api_scan_network():
    data = request.json or {}
    subnet = data.get("subnet", "").strip()
    found = hybrid_network_scan(subnet_prefix=subnet)
    return jsonify({"found": found, "subnet": subnet or get_local_subnet_prefix()})


@app.route("/api/ptz/<cam_id>", methods=["POST"])
@admin_required
def api_ptz(cam_id):
    data = request.json or {}
    direction = data.get("direction", "stop")
    cameras = load_json(CAMERAS_FILE, [])
    cam = next((c for c in cameras if c["id"] == cam_id), None)
    if not cam:
        return jsonify({"error": "Kamera nicht gefunden"}), 404
    ok, msg = execute_onvif_ptz(cam, direction)
    return jsonify({"status": "ok" if ok else "error", "message": msg})


@app.route("/api/settings", methods=["POST"])
@admin_required
def api_save_settings():
    data = request.json or {}
    settings = load_json(SETTINGS_FILE, {})

    if "language" in data and data["language"] in ("en", "de", "fr", "es"):
        settings["language"] = data["language"]

    if "recording_interval" in data:
        val = int(data["recording_interval"])
        if val in (15, 30, 45, 60, 75, 90, 105, 120):
            settings["recording_interval"] = val
        else:
            settings["recording_interval"] = max(15, min(120, val))

    if "retention_days" in data:
        settings["retention_days"] = max(1, min(365, int(data["retention_days"])))
    if "motion_post_buffer" in data:
        settings["motion_post_buffer"] = max(5, min(300, int(data["motion_post_buffer"])))
    if "ntfy_url" in data:
        settings["ntfy_url"] = data["ntfy_url"].strip()
    if "ha_webhook_url" in data:
        settings["ha_webhook_url"] = data["ha_webhook_url"].strip()
    if "alert_cooldown" in data:
        settings["alert_cooldown"] = max(10, min(3600, int(data["alert_cooldown"])))

    save_json(SETTINGS_FILE, settings)
    return jsonify({"status": "ok", "settings": settings})


# ==============================================================================
# API: BENUTZERVERWALTUNG (ADMIN & READONLY)
# ==============================================================================

@app.route("/api/users", methods=["GET", "POST"])
@admin_required
def api_users():
    users = load_json(USERS_FILE, {})
    if request.method == "GET":
        result = [
            {"username": u, "role": info.get("role", "readonly"), "created_at": info.get("created_at", "")}
            for u, info in users.items()
        ]
        return jsonify(result)

    data = request.json or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    role = data.get("role", "readonly")

    if not username or not password:
        return jsonify({"error": "Benutzername und Passwort erforderlich"}), 400
    if role not in ("admin", "readonly"):
        role = "readonly"

    users[username] = {
        "password_hash": generate_password_hash(password),
        "role": role,
        "created_at": datetime.datetime.now().isoformat()
    }
    save_json(USERS_FILE, users)
    return jsonify({"status": "ok"})


@app.route("/api/users/<username>", methods=["DELETE"])
@admin_required
def api_delete_user(username):
    users = load_json(USERS_FILE, {})
    if username not in users:
        return jsonify({"error": "Benutzer nicht gefunden"}), 404

    admin_count = sum(1 for u, info in users.items() if info.get("role") == "admin")
    if users[username].get("role") == "admin" and admin_count <= 1:
        return jsonify({"error": "Der letzte Administrator kann nicht gelöscht werden"}), 400

    del users[username]
    save_json(USERS_FILE, users)
    return jsonify({"status": "ok"})


# ==============================================================================
# API: LIVE STREAM & INSTANT SNAPSHOT (SINGLE RTSP STREAM)
# ==============================================================================

def generate_mjpeg(cam_id):
    while True:
        with workers_lock:
            worker = camera_workers.get(cam_id)
        if worker is None:
            break
        jpg = worker.get_jpeg()
        if jpg:
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n"
            )
        time.sleep(0.08)


@app.route("/api/stream/<cam_id>")
@login_required
def api_stream(cam_id):
    return Response(
        generate_mjpeg(cam_id),
        mimetype="multipart/x-mixed-replace; boundary=frame"
    )


@app.route("/api/snapshot/<cam_id>")
@login_required
def api_snapshot(cam_id):
    with workers_lock:
        worker = camera_workers.get(cam_id)
    if not worker:
        abort(404)
    jpg = worker.get_jpeg()
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return Response(
        jpg,
        mimetype="image/jpeg",
        headers={"Content-Disposition": f'attachment; filename="snapshot_{cam_id}_{ts}.jpg"'}
    )


# ==============================================================================
# API: ARCHIV & ZEITRAUM-EXPORT (STRIKT NUR FÜR ADMINISTRATOREN!)
# ==============================================================================

@app.route("/api/archive", methods=["GET"])
@admin_required
def api_archive_list():
    ensure_daily_folders()
    date_filter = request.args.get("date", "").strip()
    time_from = request.args.get("from", "00:00").strip() or "00:00"
    time_to = request.args.get("to", "23:59").strip() or "23:59"
    cam_filter = request.args.get("camera_id", "").strip()

    available_dates = sorted(
        [d for d in os.listdir(RECORDINGS_DIR) if os.path.isdir(os.path.join(RECORDINGS_DIR, d)) and re.match(r"^\d{4}-\d{2}-\d{2}$", d)],
        reverse=True
    )

    if not date_filter and available_dates:
        date_filter = available_dates[0]

    files_list = []
    target_dir = os.path.join(RECORDINGS_DIR, date_filter) if date_filter else None

    if target_dir and os.path.isdir(target_dir):
        for fname in sorted(os.listdir(target_dir), reverse=True):
            if not fname.endswith(".mp4"):
                continue
            fpath = os.path.join(target_dir, fname)
            parts = fname[:-4].split("__")
            cid = parts[0] if len(parts) > 0 else "unknown"
            cname = parts[1] if len(parts) > 1 else cid
            t_raw = parts[2] if len(parts) > 2 else "00-00-00"
            mode_tag = parts[3] if len(parts) > 3 else "REC"

            hh_mm = t_raw[:5].replace("-", ":")
            if cam_filter and cid != cam_filter:
                continue
            if not (time_from <= hh_mm <= time_to):
                continue

            size_mb = round(os.path.getsize(fpath) / (1024 * 1024), 2)
            files_list.append({
                "date": date_filter,
                "filename": fname,
                "camera_id": cid,
                "camera_name": cname.replace("_", " "),
                "time": t_raw.replace("-", ":"),
                "mode": mode_tag,
                "size_mb": size_mb
            })

    return jsonify({
        "available_dates": available_dates,
        "selected_date": date_filter,
        "recordings": files_list
    })


@app.route("/api/archive/video/<date_str>/<filename>")
@admin_required
def api_archive_video(date_str, filename):
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str) or "/" in filename or "\\" in filename:
        abort(400)
    fpath = os.path.join(RECORDINGS_DIR, date_str, filename)
    if not os.path.isfile(fpath):
        abort(404)
    dl = request.args.get("download") == "1"
    return send_file(fpath, mimetype="video/mp4", as_attachment=dl, download_name=filename)


@app.route("/api/archive/video/<date_str>/<filename>", methods=["DELETE"])
@admin_required
def api_archive_delete(date_str, filename):
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str) or "/" in filename or "\\" in filename:
        abort(400)
    fpath = os.path.join(RECORDINGS_DIR, date_str, filename)
    if os.path.isfile(fpath):
        os.remove(fpath)
    return jsonify({"status": "ok"})


@app.route("/api/archive/export", methods=["GET"])
@admin_required
def api_archive_export_zip():
    date_filter = request.args.get("date", "").strip()
    time_from = request.args.get("from", "00:00").strip() or "00:00"
    time_to = request.args.get("to", "23:59").strip() or "23:59"
    cam_filter = request.args.get("camera_id", "").strip()

    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_filter):
        return jsonify({"error": "Ungültiges Datum"}), 400

    target_dir = os.path.join(RECORDINGS_DIR, date_filter)
    if not os.path.isdir(target_dir):
        return jsonify({"error": "Ordner nicht gefunden"}), 404

    mem_zip = io.BytesIO()
    with zipfile.ZipFile(mem_zip, mode="w", compression=zipfile.ZIP_STORED) as zf:
        for fname in sorted(os.listdir(target_dir)):
            if not fname.endswith(".mp4"):
                continue
            parts = fname[:-4].split("__")
            cid = parts[0] if len(parts) > 0 else ""
            t_raw = parts[2] if len(parts) > 2 else "00-00-00"
            hh_mm = t_raw[:5].replace("-", ":")

            if cam_filter and cid != cam_filter:
                continue
            if not (time_from <= hh_mm <= time_to):
                continue

            fpath = os.path.join(target_dir, fname)
            zf.write(fpath, arcname=f"{date_filter}/{fname}")

    mem_zip.seek(0)
    zip_name = f"OpenCam_Archive_{date_filter}_{time_from.replace(':','')}-{time_to.replace(':','')}.zip"
    return send_file(mem_zip, mimetype="application/zip", as_attachment=True, download_name=zip_name)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, threaded=True)
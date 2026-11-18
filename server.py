import os
import io
import re
import cv2
import time
import json
import uuid
import base64
import socket
import subprocess
import queue
import shutil
import tempfile
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
    session, redirect, url_for, send_file, abort, after_this_request
)
from werkzeug.security import generate_password_hash, check_password_hash

APP_VERSION = "v0.3.0"
GITHUB_REPO = os.environ.get("GITHUB_REPO", "chrizzz82/opencam-lite")
PORT = int(os.environ.get("PORT", 7777))

# RTSP über TCP erzwingen und Timeout setzen (verhindert Hänger bei Tapo/Hikvision)
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|stimeout;5000000"

TEMPLATE_DIR = "templates" if os.path.isdir("templates") and os.path.exists(os.path.join("templates", "index.html")) else "."
app = Flask(__name__, template_folder=TEMPLATE_DIR)

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
RECORDINGS_DIR = os.environ.get("RECORDINGS_DIR", "/recordings")

os.makedirs(CONFIG_DIR, exist_ok=True)
os.makedirs(RECORDINGS_DIR, exist_ok=True)


def load_or_create_secret_key():
    """SECRET_KEY aus Umgebung, sonst einmalig zufaellig erzeugen und in /config speichern."""
    env_key = os.environ.get("SECRET_KEY", "").strip()
    if env_key:
        return env_key
    key_file = os.path.join(CONFIG_DIR, "secret.key")
    try:
        with open(key_file, encoding="utf-8") as f:
            key = f.read().strip()
        if key:
            return key
    except OSError:
        pass
    key = uuid.uuid4().hex + uuid.uuid4().hex
    try:
        with open(key_file, "w", encoding="utf-8") as f:
            f.write(key)
        os.chmod(key_file, 0o600)
    except OSError:
        pass
    return key


app.secret_key = load_or_create_secret_key()

USERS_FILE = os.path.join(CONFIG_DIR, "users.json")
CAMERAS_FILE = os.path.join(CONFIG_DIR, "cameras.json")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")

config_lock = threading.Lock()
workers_lock = threading.Lock()
camera_workers = {}

update_cache = {
    "last_checked": 0,
    "latest_version": APP_VERSION,
    "update_available": False,
    "repo_url": f"https://github.com/{GITHUB_REPO}"
}


# ==============================================================================
# UPDATE-CHECK GEGEN GITHUB
# ==============================================================================

def parse_version_tuple(v_str):
    cleaned = re.sub(r"[^0-9.]", "", str(v_str or ""))
    parts = [int(p) for p in cleaned.split(".") if p.isdigit()]
    return tuple(parts) if parts else (0, 0, 0)


def check_for_updates(force=False):
    now = time.time()
    if not force and (now - update_cache["last_checked"] < 900):
        return update_cache

    settings = load_json(SETTINGS_FILE, {})
    repo = (settings.get("github_repo") or GITHUB_REPO).strip()
    remote_version = None

    try:
        raw_url = f"https://raw.githubusercontent.com/{repo}/main/server.py"
        resp = requests.get(raw_url, timeout=3.5, headers={"Cache-Control": "no-cache"})
        if resp.status_code == 200:
            match = re.search(r'APP_VERSION\s*=\s*["\']([^"\']+)["\']', resp.text)
            if match:
                remote_version = match.group(1).strip()
    except Exception:
        pass

    if not remote_version:
        try:
            api_url = f"https://api.github.com/repos/{repo}/releases/latest"
            resp = requests.get(api_url, timeout=3.5)
            if resp.status_code == 200:
                tag = resp.json().get("tag_name")
                if tag:
                    remote_version = tag.strip()
        except Exception:
            pass

    if remote_version:
        is_newer = parse_version_tuple(remote_version) > parse_version_tuple(APP_VERSION)
        update_cache["latest_version"] = remote_version
        update_cache["update_available"] = is_newer
    else:
        update_cache["latest_version"] = APP_VERSION
        update_cache["update_available"] = False

    update_cache["last_checked"] = now
    update_cache["repo_url"] = f"https://github.com/{repo}"
    return update_cache


# ==============================================================================
# KONFIGURATION & PERSISTENZ (ÜBERSCHREIBT NIEMALS VORHANDENE DATEIEN)
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
        except Exception as e:
            print(f"[Config] Warnung beim Lesen von {path}: {e}")
            return default


def save_json(path, data):
    with config_lock:
        temp_path = path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        os.replace(temp_path, path)


def init_configs():
    # Nur anlegen, wenn die Datei noch überhaupt nicht existiert
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
        "language": "de",
        "recording_interval": 15,
        "retention_days": 7,
        "motion_post_buffer": 15,
        "ntfy_url": "",
        "ha_webhook_url": "",
        "alert_cooldown": 60,
        "github_repo": GITHUB_REPO
    }
    if not os.path.exists(SETTINGS_FILE):
        save_json(SETTINGS_FILE, default_settings)
    else:
        current_settings = load_json(SETTINGS_FILE, default_settings)
        changed = False
        for k, v in default_settings.items():
            if k not in current_settings:
                current_settings[k] = v
                changed = True
        if changed:
            save_json(SETTINGS_FILE, current_settings)


init_configs()


def ensure_daily_folders():
    now = datetime.datetime.now()
    for delta in (0, 1):
        day_str = (now + datetime.timedelta(days=delta)).strftime("%Y-%m-%d")
        day_path = os.path.join(RECORDINGS_DIR, day_str)
        os.makedirs(day_path, exist_ok=True)


EVENT_LOCK = threading.Lock()
TRANSCODE_SEM = threading.Semaphore(1)
CLIP_SEM = threading.Semaphore(2)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def log_event(cam_id, cam_name, ev_type, ts=None, extra=None):
    """Schreibt Ereignisse (motion_start/_end, offline_start/_end) in <tag>/events.jsonl."""
    when = datetime.datetime.fromtimestamp(ts) if ts else datetime.datetime.now()
    day = os.path.join(RECORDINGS_DIR, when.strftime("%Y-%m-%d"))
    try:
        os.makedirs(day, exist_ok=True)
        line = json.dumps({"ts": when.timestamp(), "cam_id": cam_id, "cam": cam_name, "type": ev_type, **(extra or {})})
        with EVENT_LOCK:
            with open(os.path.join(day, "events.jsonl"), "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as e:
        print(f"[Events] Fehler: {e}")


def load_events(date_str):
    path = os.path.join(RECORDINGS_DIR, date_str, "events.jsonl")
    midnight = datetime.datetime.strptime(date_str, "%Y-%m-%d").timestamp()
    now_sec = (time.time() - midnight) if date_str == datetime.date.today().isoformat() else 86400.0
    opened, snaps, out = {}, {}, []

    def make(key, start, end):
        ev = {"cam_id": key[0], "type": key[1], "start": start, "end": end}
        if snaps.get(key):
            ev["snap"] = snaps[key]
        return ev

    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                    kind, _, edge = e["type"].rpartition("_")
                    sec = max(0.0, min(86400.0, float(e["ts"]) - midnight))
                except (ValueError, KeyError):
                    continue
                key = (e.get("cam_id"), kind)
                if edge == "start":
                    if key in opened:  # alter Start ohne Ende (z.B. Neustart)
                        out.append(make(key, opened[key], opened[key] + 1))
                    opened[key] = sec
                    snaps[key] = e.get("snap")
                elif edge == "end" and key in opened:
                    start = opened.pop(key)
                    out.append(make(key, start, max(sec, start + 1)))
    for key, start in opened.items():
        w = camera_workers.get(key[0])
        live = bool(w and w.evt_open.get(key[1]))
        out.append(make(key, start, max(start + 1, now_sec) if live else start + 1))
    return out


def save_snapshot(cam_id, frame):
    """Speichert beim Start einer Bewegung ein Vorschaubild (Privatzonen sind bereits geschwaerzt)."""
    now = datetime.datetime.now()
    folder = os.path.join(RECORDINGS_DIR, now.strftime("%Y-%m-%d"), "snapshots")
    name = f"{cam_id}__{now.strftime('%H-%M-%S')}.jpg"
    try:
        os.makedirs(folder, exist_ok=True)
        h, w = frame.shape[:2]
        if w > 640:
            frame = cv2.resize(frame, (640, int(h * 640 / w)))
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 72])
        if ok:
            with open(os.path.join(folder, name), "wb") as f:
                f.write(buf.tobytes())
            return name
    except Exception as e:
        print(f"[Snapshot] Fehler: {e}")
    return None


# ---- NAS-Backup: abgeschlossene Tage auf ein eingebundenes Laufwerk spiegeln -------------------
BACKUP_DIR = os.environ.get("BACKUP_DIR", "").strip()
BACKUP_KEEP_DAYS = int(os.environ.get("BACKUP_KEEP_DAYS", "0") or 0)
BACKUP_STATE = {"last_run": None, "last_error": "", "files": 0}
BACKUP_LOCK = threading.Lock()


def backup_old_recordings():
    """Kopiert alle Tage vor heute nach BACKUP_DIR (nur Neues/Geaendertes) und raeumt dort nach BACKUP_KEEP_DAYS auf."""
    if not BACKUP_DIR:
        return
    if not BACKUP_LOCK.acquire(blocking=False):
        return
    copied = 0
    try:
        if not os.path.isdir(BACKUP_DIR):
            raise OSError("Backup-Ziel nicht erreichbar")
        today = datetime.date.today()
        for day in sorted(d for d in os.listdir(RECORDINGS_DIR) if DATE_RE.match(d) and d < today.isoformat()):
            src_day = os.path.join(RECORDINGS_DIR, day)
            for root, _, files in os.walk(src_day):
                target = os.path.normpath(os.path.join(BACKUP_DIR, day, os.path.relpath(root, src_day)))
                os.makedirs(target, exist_ok=True)
                for fname in files:
                    if fname.endswith((".rec.mp4", ".part")):
                        continue
                    src, dst = os.path.join(root, fname), os.path.join(target, fname)
                    st = os.stat(src)
                    if os.path.exists(dst):
                        ds = os.stat(dst)
                        if ds.st_size == st.st_size and ds.st_mtime >= st.st_mtime - 2:
                            continue
                    shutil.copy2(src, dst + ".part")   # erst .part, dann umbenennen: kein halbes File im Backup
                    os.replace(dst + ".part", dst)
                    copied += 1
        if BACKUP_KEEP_DAYS > 0:
            limit = (today - datetime.timedelta(days=BACKUP_KEEP_DAYS)).isoformat()
            for day in os.listdir(BACKUP_DIR):
                if DATE_RE.match(day) and day < limit:
                    shutil.rmtree(os.path.join(BACKUP_DIR, day), ignore_errors=True)
        BACKUP_STATE.update(last_run=time.time(), last_error="", files=copied)
    except Exception as e:
        BACKUP_STATE.update(last_run=time.time(), last_error=str(e)[:200], files=copied)
        print(f"[Backup] Fehler: {e}")
    finally:
        BACKUP_LOCK.release()


# ---- MQTT (ohne Zusatzpaket): Bewegung und Online-Status, z.B. fuer Home Assistant -------------
MQTT_HOST = os.environ.get("MQTT_HOST", "").strip()
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883") or 1883)
MQTT_USER = os.environ.get("MQTT_USER", "")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "")
MQTT_PREFIX = os.environ.get("MQTT_PREFIX", "opencam").strip("/") or "opencam"
_mqtt_queue = queue.Queue(maxsize=200)


def _mqtt_str(text):
    raw = text.encode("utf-8")
    return len(raw).to_bytes(2, "big") + raw


def _mqtt_packet(first_byte, body):
    n, length = len(body), bytearray()
    while True:
        digit, n = n % 128, n // 128
        length.append(digit | (0x80 if n else 0))
        if not n:
            break
    return bytes([first_byte]) + bytes(length) + body


def _mqtt_send(topic, payload, retain):
    flags = 0x02 | (0x80 if MQTT_USER else 0) | (0x40 if MQTT_USER and MQTT_PASSWORD else 0)
    connect = _mqtt_str("MQTT") + bytes([4, flags]) + (30).to_bytes(2, "big") + _mqtt_str("opencam-" + uuid.uuid4().hex[:8])
    if MQTT_USER:
        connect += _mqtt_str(MQTT_USER)
    if MQTT_USER and MQTT_PASSWORD:
        connect += _mqtt_str(MQTT_PASSWORD)
    with socket.create_connection((MQTT_HOST, MQTT_PORT), timeout=5) as sock:
        sock.sendall(_mqtt_packet(0x10, connect))
        ack = sock.recv(4)
        if len(ack) < 4 or ack[0] != 0x20 or ack[3] != 0:
            raise OSError("MQTT-Verbindung abgelehnt")
        sock.sendall(_mqtt_packet(0x30 | (1 if retain else 0), _mqtt_str(topic) + payload.encode("utf-8")))
        sock.sendall(b"\xe0\x00")   # DISCONNECT


def _mqtt_worker():
    while True:
        topic, payload, retain = _mqtt_queue.get()
        try:
            _mqtt_send(topic, payload, retain)
        except Exception as e:
            print(f"[MQTT] Fehler: {e}")


def mqtt_publish(topic, payload, retain=True):
    if MQTT_HOST:
        try:
            _mqtt_queue.put_nowait((topic, str(payload), retain))
        except queue.Full:
            pass


if MQTT_HOST:
    threading.Thread(target=_mqtt_worker, daemon=True).start()


def finalize_recording(rec_path):
    """Wandelt die mp4v-Rohdatei in browserfaehiges H.264 (faststart) um."""
    final = rec_path[:-len(".rec.mp4")] + ".mp4"
    part = final + ".part"
    with TRANSCODE_SEM:
        try:
            if not os.path.isfile(rec_path):
                return
            st = os.stat(rec_path)
            cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", rec_path,
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p",
                   "-an", "-movflags", "+faststart", "-f", "mp4", part]
            res = subprocess.run(cmd, capture_output=True, timeout=4 * 3600)
            if res.returncode == 0 and os.path.getsize(part) > 1024:
                os.utime(part, (st.st_atime, st.st_mtime))  # Endzeit bleibt erhalten
                os.replace(part, final)
                os.remove(rec_path)
                return
            print(f"[Transcode] ffmpeg Fehler: {res.stderr.decode(errors='ignore')[:300]}")
        except Exception as e:
            print(f"[Transcode] Fehler: {e}")
        try:  # Fallback: Rohdatei behalten, falls sie lesbar ist
            if os.path.exists(part):
                os.remove(part)
            cap = cv2.VideoCapture(rec_path)
            valid = cap.isOpened() and cap.get(cv2.CAP_PROP_FRAME_COUNT) > 0
            cap.release()
            if valid:
                os.replace(rec_path, final)
            else:
                os.remove(rec_path)
        except Exception as e:
            print(f"[Transcode] Fallback-Fehler: {e}")


def find_leftovers():
    found = []
    try:
        for day in os.listdir(RECORDINGS_DIR):
            d = os.path.join(RECORDINGS_DIR, day)
            if DATE_RE.match(day) and os.path.isdir(d):
                for root, _, files in os.walk(d):
                    found += [os.path.join(root, f) for f in files if f.endswith(".rec.mp4")]
    except Exception as e:
        print(f"[Recovery] Fehler: {e}")
    return found


def parse_clock(value, default, is_end=False):
    try:
        p = [int(x) for x in value.strip().split(":")]
        return p[0] * 3600 + p[1] * 60 + (p[2] if len(p) > 2 else (59 if is_end else 0))
    except Exception:
        return default


def camera_folder(day_dir, cam_id, name):
    """Unterordner pro Kamera: <Name>_<ID>. Bleibt auch nach Umbenennen der Kamera derselbe (Suche ueber die ID)."""
    cid = re.sub(r"[^a-zA-Z0-9_-]", "_", str(cam_id))
    try:
        for d in sorted(os.listdir(day_dir)):
            if d.endswith("_" + cid) and os.path.isdir(os.path.join(day_dir, d)):
                return d
    except OSError:
        pass
    base = re.sub(r"_+", "_", re.sub(r"[^a-zA-Z0-9_-]", "_", name or "Cam")).strip("_") or "Cam"
    return f"{base}_{cid}"


def iter_rec_files(day_dir):
    """(Unterordner, Dateiname) aller Dateien eines Tages: Kamera-Ordner und alte flache Ablage (Unterordner '')."""
    out = []
    try:
        entries = os.listdir(day_dir)
    except OSError:
        return out
    for e in entries:
        p = os.path.join(day_dir, e)
        if os.path.isdir(p):
            if e == "snapshots":
                continue
            try:
                out += [(e, f) for f in os.listdir(p)]
            except OSError:
                pass
        else:
            out.append(("", e))
    return out


def find_rec_file(date_str, filename):
    """Pfad einer Aufnahme (Kamera-Ordner oder alte flache Ablage) oder None."""
    day_dir = os.path.join(RECORDINGS_DIR, date_str)
    p = os.path.join(day_dir, filename)
    if os.path.isfile(p):
        return p
    try:
        for d in os.listdir(day_dir):
            p = os.path.join(day_dir, d, filename)
            if d != "snapshots" and os.path.isfile(p):
                return p
    except OSError:
        pass
    return None


def organize_legacy_recordings():
    """Einmalig: fertige Aufnahmen aus der alten flachen Ablage in Kamera-Ordner verschieben."""
    try:
        for day in os.listdir(RECORDINGS_DIR):
            day_dir = os.path.join(RECORDINGS_DIR, day)
            if not (DATE_RE.match(day) and os.path.isdir(day_dir)):
                continue
            for sub, fname in iter_rec_files(day_dir):
                if sub or not fname.endswith(".mp4") or fname.endswith(".rec.mp4"):
                    continue
                parsed = parse_rec_name(fname[:-4])
                if not parsed:
                    continue
                dst_dir = os.path.join(day_dir, camera_folder(day_dir, parsed[0], parsed[1]))
                os.makedirs(dst_dir, exist_ok=True)
                os.replace(os.path.join(day_dir, fname), os.path.join(dst_dir, fname))
    except Exception as e:
        print(f"[Archiv] Sortieren nach Kamera: {e}")


def parse_rec_name(base):
    """<cam_id>__<Name>__<HH-MM-SS>__<TAG>: Uhrzeit/Tag von rechts lesen, da der Kameraname selbst '__' enthalten kann
    (z.B. 'TP-Link Tapo Camera (192.168.1.5)')."""
    m = re.search(r"(\d{2})-(\d{2})-(\d{2})__([A-Za-z0-9]+)$", base)
    if not m:
        return None
    cam_id, sep, name = base[:m.start()].partition("__")
    if not sep or not cam_id:
        return None
    return cam_id, name.strip("_"), int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)), m.group(4)


def list_recordings(date_str, cam_filter="", from_sec=0, to_sec=86400):
    """Aufnahmen eines Tages (aufsteigend). Filter = zeitliche Ueberlappung, nicht nur Startzeit."""
    day_dir = os.path.join(RECORDINGS_DIR, date_str)
    if not os.path.isdir(day_dir):
        return []
    midnight = datetime.datetime.strptime(date_str, "%Y-%m-%d").timestamp()
    active = {w.current_filepath for w in list(camera_workers.values()) if w.current_filepath}
    files = iter_rec_files(day_dir)
    names = set(files)
    out = []
    for sub, fname in files:
        if not fname.endswith(".mp4"):
            continue
        raw = fname.endswith(".rec.mp4")
        base = fname[:-8] if raw else fname[:-4]
        if raw and (sub, base + ".mp4") in names:
            continue
        parsed = parse_rec_name(base)
        if not parsed:
            continue
        try:
            fpath = os.path.join(day_dir, sub, fname)
            st = os.stat(fpath)
        except OSError:
            continue
        rec_cam, rec_name, start, rec_tag = parsed
        status = ("recording" if fpath in active else "processing") if raw else "ready"
        end = (time.time() if status == "recording" else st.st_mtime) - midnight
        end = max(start + 1, min(86400.0, end))
        if cam_filter and rec_cam != cam_filter:
            continue
        if not (start < to_sec and end > from_sec):
            continue
        out.append({
            "date": date_str, "filename": fname, "dir": sub, "camera_id": rec_cam,
            "camera_name": rec_name.replace("_", " "), "mode": rec_tag,
            "size_mb": round(st.st_size / (1024 * 1024), 2),
            "start_sec": start, "end_sec": round(end, 1), "status": status,
        })
    out.sort(key=lambda r: r["start_sec"])
    return out


def dir_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def cleanup_old_recordings():
    """Loescht nach Alter (retention_days) und danach die aeltesten Tage, bis das GB-Limit eingehalten ist."""
    if BACKUP_DIR:
        threading.Thread(target=backup_old_recordings, daemon=True).start()
    _cleanup_by_age()
    try:
        settings = load_json(SETTINGS_FILE, {})
        cap_gb = float(settings.get("max_storage_gb") or os.environ.get("MAX_STORAGE_GB") or 0)
        if cap_gb <= 0:
            return
        today = datetime.date.today().isoformat()
        total = dir_size(RECORDINGS_DIR)
        for day in sorted(d for d in os.listdir(RECORDINGS_DIR) if DATE_RE.match(d) and d < today):
            if total <= cap_gb * 1024 ** 3:
                break
            p = os.path.join(RECORDINGS_DIR, day)
            size = dir_size(p)
            shutil.rmtree(p, ignore_errors=True)
            total -= size
            print(f"[Cleanup] Speicherlimit: {day} geloescht")
    except Exception as e:
        print(f"[Cleanup] Limit-Fehler: {e}")


def _cleanup_by_age():
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

class LiveMp4Writer:
    """Ersatz fuer cv2.VideoWriter: schreibt ueber ffmpeg ein fragmentiertes MP4 (MPEG-4, wie bisher),
    das schon WAEHREND der Aufnahme lesbar ist (Live-Archiv). Gleiche Schnittstelle wie cv2.VideoWriter.
    Faellt ffmpeg aus, wird der Grund geloggt und mit dem alten OpenCV-Writer weitergeschrieben."""

    def __init__(self, path, fps, w, h):
        self.path, self.fps, self.w, self.h = path, fps, w, h
        self.proc, self.cv, self.err = None, None, None
        try:
            self.err = tempfile.TemporaryFile()
            self.proc = subprocess.Popen(
                ["ffmpeg", "-nostdin", "-loglevel", "error", "-y",
                 "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}", "-framerate", f"{fps:.3f}", "-i", "-",
                 "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                 "-c:v", "mpeg4", "-q:v", "5", "-g", "15", "-pix_fmt", "yuv420p", "-an",
                 "-movflags", "+frag_keyframe+empty_moov+default_base_moof", "-flush_packets", "1",
                 "-f", "mp4", path],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self.err)
        except Exception as e:
            print(f"[Recorder] ffmpeg konnte nicht gestartet werden: {e}")
            self.proc = None

    def _fallback(self):
        """ffmpeg-Writer ausgefallen -> Fehler loggen, OpenCV-Writer (ohne Live-Lesbarkeit) verwenden."""
        if self.cv is not None:
            return
        reason = ""
        try:
            self.err.seek(0)
            reason = self.err.read().decode(errors="ignore")[-300:].strip()
        except Exception:
            pass
        print(f"[Recorder] ffmpeg-Writer ausgefallen ({reason or 'unbekannt'}) -> OpenCV-Fallback fuer {os.path.basename(self.path)}")
        self.proc = None
        self.cv = cv2.VideoWriter(self.path, cv2.VideoWriter_fourcc(*"mp4v"), self.fps, (self.w, self.h))

    def isOpened(self):
        if self.cv is not None:
            return self.cv.isOpened()
        return self.proc is not None and self.proc.poll() is None

    def write(self, frame):
        if frame.shape[1] != self.w or frame.shape[0] != self.h:
            return
        if self.cv is None and (self.proc is None or self.proc.poll() is not None):
            self._fallback()
        if self.cv is not None:
            self.cv.write(frame)
            return
        try:
            self.proc.stdin.write(frame.tobytes())
        except Exception:
            pass

    def release(self):
        if self.cv is not None:
            self.cv.release()
            self.cv = None
        if self.proc is not None:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=30)
            except Exception:
                self.proc.kill()
            self.proc = None
        try:
            if self.err:
                self.err.close()
        except Exception:
            pass


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

        self.writer = None
        self.current_rec_start = 0
        self.current_rec_date = ""
        self.current_rec_mode = ""
        self.current_filepath = ""
        self.evt_open = {"motion": False, "offline": False}
        self.offline_since = 0

        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

    def _event(self, kind, active, ts=None, frame=None):
        if self.evt_open[kind] == active:
            return
        self.evt_open[kind] = active
        name = self.config.get("name", "Camera")
        extra = {}
        if kind == "motion" and active and frame is not None:
            snap = save_snapshot(self.cam_id, frame)
            if snap:
                extra["snap"] = snap
        log_event(self.cam_id, name, f"{kind}_{'start' if active else 'end'}", ts, extra)
        topic = f"{MQTT_PREFIX}/{re.sub(r'[^a-zA-Z0-9_-]', '_', name)}"
        if kind == "motion":
            mqtt_publish(f"{topic}/motion", "ON" if active else "OFF")
        elif kind == "offline":
            mqtt_publish(f"{topic}/online", "OFF" if active else "ON")

    def _close_events(self):
        for kind in ("motion", "offline"):
            self._event(kind, False)

    def update_config(self, new_config):
        with self.lock:
            rtsp_changed = self.config.get("rtsp_url") != new_config.get("rtsp_url")
            self.config = new_config
            if rtsp_changed:
                self.last_frame_time = 0

    def stop(self):
        self.running = False
        self._close_writer()
        self._close_events()

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
        area = float(h * w)
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
            area = float(max(1, cv2.countNonZero(mask)))   # Zonenflaeche, nicht das ganze Bild

        changed_pixels = cv2.countNonZero(thresh)
        total_pixels = area
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
            path, self.current_filepath = self.current_filepath, ""
            if path:
                threading.Thread(target=finalize_recording, args=(path,), daemon=True).start()

    def _handle_recording(self, frame, fps):
        settings = load_json(SETTINGS_FILE, {})
        interval_min = int(settings.get("recording_interval", 15))
        interval_min = max(15, min(120, interval_min))
        interval_sec = interval_min * 60
        post_buffer = int(settings.get("motion_post_buffer", 15))

        mode = self.config.get("recording_mode", "live")
        if mode == "live" and self.config.get("motion_zones"):
            mode = "motion"   # Live + eingerichtete Bewegungszonen: nur bei Bewegung in den Zonen aufnehmen
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
            filename = f"{self.cam_id}__{safe_name}__{time_str}__{tag}.rec.mp4"
            cam_dir = os.path.join(day_folder, camera_folder(day_folder, self.cam_id, safe_name))
            os.makedirs(cam_dir, exist_ok=True)
            filepath = os.path.join(cam_dir, filename)

            h, w = frame.shape[:2]
            out_fps = max(5.0, min(30.0, float(fps if fps and fps > 0 else 15.0)))
            self.writer = LiveMp4Writer(filepath, out_fps, w, h)
            self.current_rec_start = now
            self.current_rec_date = today_str
            self.current_rec_mode = mode
            self.current_filepath = filepath

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

            if cap is None or not cap.isOpened() or (time.time() - self.last_frame_time > 25.0):
                self.connected = False
                if not self.offline_since:
                    self.offline_since = time.time()
                if time.time() - self.offline_since > 10:
                    self._event("offline", True, self.offline_since)
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
            self.offline_since = 0
            self._event("offline", False)
            self.latest_raw_shape = frame.shape[:2]

            cam_fps = cap.get(cv2.CAP_PROP_FPS)
            if cam_fps and 1.0 <= cam_fps <= 60.0:
                fps_estimate = cam_fps

            with self.lock:
                privacy_zones = list(self.config.get("privacy_zones", []))
                motion_zones = list(self.config.get("motion_zones", []))
                sensitivity = self.config.get("motion_sensitivity", 50)
                cam_name = self.config.get("name", "Camera")

            self._apply_privacy_zones(frame, privacy_zones)

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

            if is_moving:
                self._event("motion", True, frame=frame)
            elif self.evt_open["motion"] and now - self.last_motion_time > 5:
                self._event("motion", False)

            try:
                self._handle_recording(frame, fps_estimate)
            except Exception as e:
                print(f"[Recording] Fehler bei {cam_name}: {e}")

            ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            if ok:
                with self.lock:
                    self.latest_jpeg = buffer.tobytes()

        self._close_writer()
        self._close_events()
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
            check_for_updates(force=False)
        except Exception as e:
            print(f"[Housekeeping] Fehler: {e}")
        time.sleep(900)


print("[OpenCam] Live-Archiv aktiv (laufende Aufnahmen sind abspielbar)")
_leftovers = find_leftovers()
threading.Thread(target=lambda: ([finalize_recording(p) for p in _leftovers], organize_legacy_recordings()), daemon=True).start()
sync_camera_workers()
threading.Thread(target=background_housekeeping, daemon=True).start()


# ==============================================================================
# HYBRID ONVIF SCANNER (AB .2 BIS .254)
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
# SIGNED TAPO & ONVIF PTZ
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
# AUTHENTIFIZIERUNG & ROLLEN
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


LOGIN_FAILS = {}
LOGIN_LOCK = threading.Lock()
_STATUS_CACHE = {"t": 0.0, "size": 0}


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True})


@app.route("/api/status")
@admin_required
def api_status():
    if time.time() - _STATUS_CACHE["t"] > 60:   # Ordnergroesse nur einmal pro Minute berechnen
        _STATUS_CACHE.update(t=time.time(), size=dir_size(RECORDINGS_DIR))
    usage = shutil.disk_usage(RECORDINGS_DIR)
    settings = load_json(SETTINGS_FILE, {})
    return jsonify({
        "disk_total": usage.total, "disk_free": usage.free, "recordings_bytes": _STATUS_CACHE["size"],
        "max_storage_gb": float(settings.get("max_storage_gb") or os.environ.get("MAX_STORAGE_GB") or 0),
        "retention_days": int(settings.get("retention_days", 7)),
        "backup": {"enabled": bool(BACKUP_DIR), **BACKUP_STATE},
        "mqtt": bool(MQTT_HOST),
        "live_archive": True   # Marker: Live-Archiv-Version laeuft
    })


def login_blocked(ip):
    now = time.time()
    with LOGIN_LOCK:
        LOGIN_FAILS[ip] = [t for t in LOGIN_FAILS.get(ip, []) if now - t < 600]
        return len(LOGIN_FAILS[ip]) >= 5


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    settings = load_json(SETTINGS_FILE, {})
    lang = settings.get("language", "de")

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        users = load_json(USERS_FILE, {})
        ip = request.remote_addr or "?"

        if login_blocked(ip):
            error = "Zu viele Fehlversuche, bitte 10 Minuten warten / Too many attempts, please wait 10 minutes"
            return render_template("login.html", error=error, lang=lang, version=APP_VERSION), 429

        if username in users and check_password_hash(users[username]["password_hash"], password):
            LOGIN_FAILS.pop(ip, None)
            session["user"] = username
            session["role"] = users[username].get("role", "readonly")
            return redirect(url_for("index"))
        with LOGIN_LOCK:
            LOGIN_FAILS.setdefault(ip, []).append(time.time())
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
# API: BOOTSTRAP, UPDATE-CHECK, KAMERAS & EINSTELLUNGEN
# ==============================================================================

@app.route("/api/bootstrap", methods=["GET"])
@login_required
def api_bootstrap():
    role = session.get("role", "readonly")
    settings = load_json(SETTINGS_FILE, {})
    cameras = load_json(CAMERAS_FILE, [])
    upd = check_for_updates(force=False) if role == "admin" else {}

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
        "update_info": upd,
        "user": session.get("user"),
        "role": role,
        "settings": settings if role == "admin" else {"language": settings.get("language", "de")},
        "cameras": safe_cameras,
        "detected_subnet": get_local_subnet_prefix()
    })


@app.route("/api/update_check", methods=["GET"])
@admin_required
def api_update_check():
    upd = check_for_updates(force=True)
    return jsonify({
        "current_version": APP_VERSION,
        "latest_version": upd.get("latest_version", APP_VERSION),
        "update_available": upd.get("update_available", False),
        "repo_url": upd.get("repo_url", f"https://github.com/{GITHUB_REPO}")
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
        "privacy_zones": clean_zones(data.get("privacy_zones", existing.get("privacy_zones", []) if existing else [])),
        "motion_zones": clean_zones(data.get("motion_zones", existing.get("motion_zones", []) if existing else []))
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


def clean_zones(zones):
    """Nur gueltige Rechtecke (0..1, relativ zum Bild) behalten."""
    out = []
    for z in (zones if isinstance(zones, list) else [])[:50]:
        try:
            x = min(1.0, max(0.0, float(z["x"])))
            y = min(1.0, max(0.0, float(z["y"])))
            w = min(1.0 - x, max(0.0, float(z["w"])))
            h = min(1.0 - y, max(0.0, float(z["h"])))
        except (KeyError, TypeError, ValueError):
            continue
        if w > 0.005 and h > 0.005:
            out.append({"x": round(x, 4), "y": round(y, 4), "w": round(w, 4), "h": round(h, 4)})
    return out


@app.route("/api/live-status")
@login_required
def api_live_status():
    out = {}
    with workers_lock:
        for cid, w in camera_workers.items():
            out[cid] = {"online": bool(w.connected), "motion": bool(w.motion_detected), "recording": w.writer is not None}
    return jsonify(out)


@app.route("/api/cameras/<cam_id>/zones", methods=["POST"])
@admin_required
def api_camera_zones(cam_id):
    data = request.json or {}
    cameras = load_json(CAMERAS_FILE, [])
    for c in cameras:
        if c["id"] == cam_id:
            if "privacy_zones" in data:
                c["privacy_zones"] = clean_zones(data["privacy_zones"])
            if "motion_zones" in data:
                c["motion_zones"] = clean_zones(data["motion_zones"])
            if "motion_sensitivity" in data:
                try:
                    c["motion_sensitivity"] = max(1, min(100, int(data["motion_sensitivity"])))
                except (TypeError, ValueError):
                    pass
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
    if "github_repo" in data and data["github_repo"].strip():
        settings["github_repo"] = data["github_repo"].strip()

    save_json(SETTINGS_FILE, settings)
    check_for_updates(force=True)
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
# API: LIVE STREAM & SNAPSHOT
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
# API: ARCHIV & ZEITRAUM-EXPORT (NUR ADMIN)
# ==============================================================================

@app.route("/api/archive", methods=["GET"])
@admin_required
def api_archive_list():
    ensure_daily_folders()
    today = datetime.date.today().isoformat()
    # ensure_daily_folders legt auch den morgigen Ordner an -> Zukunft nicht anzeigen
    available_dates = sorted(
        [d for d in os.listdir(RECORDINGS_DIR)
         if DATE_RE.match(d) and d <= today and os.path.isdir(os.path.join(RECORDINGS_DIR, d))],
        reverse=True
    )
    if today not in available_dates:
        available_dates.insert(0, today)
    date_filter = request.args.get("date", "").strip()
    if date_filter not in available_dates:
        date_filter = today
    cam_filter = request.args.get("camera_id", "").strip()
    t_from = parse_clock(request.args.get("from", ""), 0)
    t_to = parse_clock(request.args.get("to", ""), 86400, is_end=True)
    if t_to <= t_from:
        t_from, t_to = 0, 86400

    events = [e for e in load_events(date_filter)
              if (not cam_filter or e["cam_id"] == cam_filter) and e["start"] < t_to and e["end"] > t_from]
    now_sec = None
    if date_filter == today:
        now_sec = time.time() - datetime.datetime.strptime(today, "%Y-%m-%d").timestamp()
    return jsonify({
        "available_dates": available_dates,
        "selected_date": date_filter,
        "recordings": list_recordings(date_filter, cam_filter, t_from, t_to),
        "events": events,
        "window": {"from": t_from, "to": t_to},
        "now_sec": now_sec
    })


def probe_duration(path):
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        return float(out)
    except Exception:
        return 0.0


CLIP_CACHE_DIR = os.path.join(tempfile.gettempdir(), "opencam_live_clips")


def probe_live_duration(path):
    """Dauer einer (noch wachsenden) fragmentierten MP4: ffprobe, sonst Paket-Durchlauf ohne Re-Encoding."""
    d = probe_duration(path)
    if d > 0:
        return d
    try:
        res = subprocess.run(["ffmpeg", "-nostdin", "-i", path, "-map", "0:v:0", "-c", "copy", "-f", "null", "-"],
                             capture_output=True, timeout=120)
        times = re.findall(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", res.stderr.decode(errors="ignore"))
        if times:
            hh, mm, ss = times[-1]
            return int(hh) * 3600 + int(mm) * 60 + float(ss)
    except Exception:
        pass
    return 0.0


@app.route("/api/archive/clip")
@admin_required
def api_archive_clip():
    """Schneidet ein Ereignis (Start/Ende in Sekunden seit Mitternacht) als MP4-Clip aus den Aufnahmen."""
    date_str = request.args.get("date", "").strip()
    cam_id = request.args.get("camera_id", "").strip()
    try:
        t0 = max(0.0, float(request.args.get("start", "")))
        t1 = min(86400.0, float(request.args.get("end", "")))
    except ValueError:
        return jsonify({"error": "Ungültige Zeit"}), 400
    fmt = request.args.get("format", "mp4")
    inline = request.args.get("inline") == "1"   # Wiedergabe im Browser statt Download (Live-Archiv)
    cache_file = None
    if inline and fmt == "mp4":
        os.makedirs(CLIP_CACHE_DIR, exist_ok=True)
        for fn in os.listdir(CLIP_CACHE_DIR):   # alte Zwischenspeicher aufraeumen
            fp = os.path.join(CLIP_CACHE_DIR, fn)
            try:
                if time.time() - os.path.getmtime(fp) > 600:
                    os.remove(fp)
            except OSError:
                pass
        key = hashlib.md5(f"{date_str}|{cam_id}|{int(t0)}|{int(t1)}".encode()).hexdigest()
        cache_file = os.path.join(CLIP_CACHE_DIR, key + ".mp4")
        if os.path.isfile(cache_file) and time.time() - os.path.getmtime(cache_file) < 60:
            return send_file(cache_file, mimetype="video/mp4", conditional=True)
    if fmt == "gif":
        t1 = min(t1, t0 + 30)   # GIFs auf 30 Sekunden begrenzen
    if not DATE_RE.match(date_str) or not cam_id or t1 - t0 < 1 or t1 - t0 > 3600:
        return jsonify({"error": "Ungültige Anfrage"}), 400

    # Auch laufende ("recording") und gerade konvertierte ("processing") Aufnahmen verwenden -> Live-Suche
    recs = list_recordings(date_str, cam_id, t0, t1)
    if not recs:
        return jsonify({"error": "Keine Aufnahme für diesen Zeitraum"}), 404

    work = tempfile.mkdtemp(prefix="clip_")
    after_this_request(lambda r: (shutil.rmtree(work, ignore_errors=True), r)[1])
    parts = []
    with CLIP_SEM:
        for i, r in enumerate(recs):
            path = os.path.join(RECORDINGS_DIR, date_str, r["dir"], r["filename"])
            if not os.path.isfile(path) and r["filename"].endswith(".rec.mp4"):
                # Konvertierung war inzwischen fertig -> finale Datei verwenden
                path = os.path.join(RECORDINGS_DIR, date_str, r["dir"], r["filename"][:-len(".rec.mp4")] + ".mp4")
            live = r["status"] == "recording"
            wall = max(1.0, r["end_sec"] - r["start_sec"])
            dur = probe_live_duration(path) if live else probe_duration(path)
            scale = (dur / wall) if dur > 0 else 1.0   # Videodauer kann von der Echtzeit abweichen
            a = max(0.0, t0 - r["start_sec"]) * scale
            b = min(wall, t1 - r["start_sec"]) * scale
            if live and dur > 0:
                b = min(b, dur)   # nicht ueber das bisher geschriebene Ende hinaus
            if b - a < 0.5:
                continue
            out = os.path.join(work, f"p{i}.mp4")
            res = subprocess.run(
                ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-ss", f"{a:.3f}", "-i", path, "-t", f"{b - a:.3f}",
                 "-c:v", "libx264", "-preset", "ultrafast" if inline else "veryfast", "-crf", "26", "-pix_fmt", "yuv420p", "-an",
                 "-movflags", "+faststart", out], capture_output=True, timeout=600)
            if res.returncode == 0 and os.path.isfile(out):
                parts.append(out)
        if not parts:
            return jsonify({"error": "Clip konnte nicht erstellt werden"}), 500
        final = parts[0]
        if len(parts) > 1:
            lst = os.path.join(work, "list.txt")
            with open(lst, "w") as f:
                f.writelines(f"file '{p}'\n" for p in parts)
            final = os.path.join(work, "clip.mp4")
            res = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                                  "-i", lst, "-c", "copy", "-movflags", "+faststart", final], capture_output=True, timeout=600)
            if res.returncode != 0:
                final = parts[0]
    h, m, sec = int(t0 // 3600), int(t0 % 3600 // 60), int(t0 % 60)
    name = f"{re.sub(r'[^a-zA-Z0-9_-]', '_', recs[0]['camera_name'])}_{date_str}_{h:02d}{m:02d}{sec:02d}.mp4"
    if fmt == "gif":
        gif = os.path.join(work, "clip.gif")
        res = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", final, "-vf",
                              "fps=8,scale=480:-2:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=128[p];[s1][p]paletteuse",
                              "-loop", "0", gif], capture_output=True, timeout=600)
        if res.returncode != 0 or not os.path.isfile(gif):
            return jsonify({"error": "GIF konnte nicht erstellt werden"}), 500
        return send_file(gif, mimetype="image/gif", as_attachment=True, download_name=name[:-4] + ".gif")
    if cache_file:   # kurz zwischenspeichern: der Browser fragt fuer Vor-/Zurueckspulen mehrfach an
        tmp = cache_file + f".{uuid.uuid4().hex[:6]}.tmp"
        shutil.copyfile(final, tmp)
        os.replace(tmp, cache_file)
        return send_file(cache_file, mimetype="video/mp4", conditional=True)
    return send_file(final, mimetype="video/mp4", as_attachment=True, download_name=name)


@app.route("/api/archive/snapshot/<date_str>/<filename>")
@admin_required
def api_archive_snapshot(date_str, filename):
    if not DATE_RE.match(date_str) or not re.match(r"^[A-Za-z0-9_.-]+\.jpg$", filename):
        abort(404)
    path = os.path.join(RECORDINGS_DIR, date_str, "snapshots", filename)
    if not os.path.isfile(path):
        abort(404)
    return send_file(path, mimetype="image/jpeg", max_age=3600)


@app.route("/api/archive/video/<date_str>/<filename>")
@admin_required
def api_archive_video(date_str, filename):
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str) or "/" in filename or "\\" in filename:
        abort(400)
    fpath = find_rec_file(date_str, filename)
    if not fpath:
        abort(404)
    dl = request.args.get("download") == "1"
    return send_file(fpath, mimetype="video/mp4", as_attachment=dl, download_name=filename)


@app.route("/api/archive/video/<date_str>/<filename>", methods=["DELETE"])
@admin_required
def api_archive_delete(date_str, filename):
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str) or "/" in filename or "\\" in filename:
        abort(400)
    fpath = find_rec_file(date_str, filename)
    if fpath:
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

    t_from = parse_clock(time_from, 0)
    t_to = parse_clock(time_to, 86400, is_end=True)
    mem_zip = tempfile.TemporaryFile()
    with zipfile.ZipFile(mem_zip, mode="w", compression=zipfile.ZIP_STORED) as zf:
        for rec in list_recordings(date_filter, cam_filter, t_from, t_to):
            if rec["status"] == "ready":
                zf.write(os.path.join(target_dir, rec["dir"], rec["filename"]),
                         arcname="/".join(x for x in (date_filter, rec["dir"], rec["filename"]) if x))

    mem_zip.seek(0)
    zip_name = f"OpenCam_Archive_{date_filter}_{time_from.replace(':','')}-{time_to.replace(':','')}.zip"
    return send_file(mem_zip, mimetype="application/zip", as_attachment=True, download_name=zip_name)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
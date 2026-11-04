#!/usr/bin/env python3
import os
import sys
import json
import time
import shutil
import socket
import signal
import zipfile
import tempfile
import urllib.parse
import urllib.request
import subprocess
import threading
import datetime
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
RECORDINGS_DIR = Path(os.environ.get("RECORDINGS_DIR", "/recordings"))
STREAMS_DIR = Path(os.environ.get("STREAMS_DIR", "/tmp/streams"))
SETTINGS_FILE = CONFIG_DIR / "settings.json"
PORT = int(os.environ.get("PORT", "7777"))

CONFIG_DIR.mkdir(parents=True, exist_ok=True)
RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
STREAMS_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_SETTINGS = {
    "language": "de",
    "retention_days": 7,
    "segment_minutes": 15,
    "ntfy_topic": "",
    "ha_webhook": "",
    "cameras": []
}

BRAND_PRESETS = {
    "tapo": {"hd": "/stream1", "sd": "/stream2", "port": 554, "onvif_port": 2020},
    "reolink": {"hd": "/h264Preview_01_main", "sd": "/h264Preview_01_sub", "port": 554, "onvif_port": 8000},
    "hikvision": {"hd": "/Streaming/Channels/101", "sd": "/Streaming/Channels/102", "port": 554, "onvif_port": 80},
    "dahua": {"hd": "/cam/realmonitor?channel=1&subtype=0", "sd": "/cam/realmonitor?channel=1&subtype=1", "port": 554, "onvif_port": 80},
    "amcrest": {"hd": "/cam/realmonitor?channel=1&subtype=0", "sd": "/cam/realmonitor?channel=1&subtype=1", "port": 554, "onvif_port": 80},
    "foscam": {"hd": "/videoMain", "sd": "/videoSub", "port": 554, "onvif_port": 888},
    "uniview": {"hd": "/media/video1", "sd": "/media/video2", "port": 554, "onvif_port": 80},
    "axis": {"hd": "/axis-media/media.amp", "sd": "/axis-media/media.amp?resolution=640x480", "port": 554, "onvif_port": 80},
    "generic": {"hd": "/stream1", "sd": "/stream2", "port": 554, "onvif_port": 80}
}

settings_lock = threading.Lock()
processes_lock = threading.Lock()
camera_processes = {}
stop_event = threading.Event()


def sanitize_name(name: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_-]", "_", (name or "cam").strip())
    return clean or "cam"


def normalize_cameras(data: dict) -> bool:
    changed = False
    cams = data.get("cameras", [])
    for idx, cam in enumerate(cams):
        if not cam.get("id"):
            cam["id"] = f"cam_{idx}_{sanitize_name(cam.get('name', f'cam{idx}'))}"
            changed = True
        if "quality" not in cam and "res" in cam:
            cam["quality"] = "sd" if str(cam["res"]).lower() in ("sd", "sub", "low") else "hd"
            changed = True
        if "privacy_zones" not in cam and "mask_zones" in cam:
            cam["privacy_zones"] = cam["mask_zones"]
            changed = True
    return changed


def load_settings() -> dict:
    with settings_lock:
        if not SETTINGS_FILE.exists():
            save_settings_unlocked(DEFAULT_SETTINGS)
            return json.loads(json.dumps(DEFAULT_SETTINGS))
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in DEFAULT_SETTINGS.items():
                if k not in data:
                    data[k] = v
            if normalize_cameras(data):
                save_settings_unlocked(data)
            return data
        except Exception as e:
            print(f"[ERROR] Konnte settings.json nicht laden: {e}")
            return json.loads(json.dumps(DEFAULT_SETTINGS))


def save_settings_unlocked(data: dict):
    normalize_cameras(data)
    tmp_file = SETTINGS_FILE.with_suffix(".tmp")
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp_file.replace(SETTINGS_FILE)


def save_settings(data: dict):
    with settings_lock:
        save_settings_unlocked(data)


def get_cam_record_base(cam: dict) -> Path:
    safe_name = sanitize_name(cam.get("name") or cam.get("id") or "Kamera")
    folder = RECORDINGS_DIR / safe_name
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def ensure_daily_folders_for_cam(cam: dict):
    base = get_cam_record_base(cam)
    now = datetime.datetime.now()
    for delta in (-1, 0, 1, 2):
        day_str = (now + datetime.timedelta(days=delta)).strftime("%Y-%m-%d")
        (base / day_str).mkdir(parents=True, exist_ok=True)


def ensure_all_daily_folders():
    cfg = load_settings()
    for cam in cfg.get("cameras", []):
        try:
            ensure_daily_folders_for_cam(cam)
        except Exception as e:
            print(f"[WARN] Fehler beim Erstellen der Tagesordner: {e}")


def build_rtsp_url(cam: dict) -> str:
    brand = (cam.get("brand") or "tapo").lower()
    preset = BRAND_PRESETS.get(brand, BRAND_PRESETS["generic"])
    ip = (cam.get("ip") or "").strip()
    user = urllib.parse.quote(str(cam.get("user") or cam.get("username") or ""), safe="")
    password = urllib.parse.quote(str(cam.get("password") or cam.get("pass") or ""), safe="")
    quality = (cam.get("quality") or cam.get("res") or "hd").lower()
    if quality in ("sub", "low"):
        quality = "sd"
    elif quality not in ("hd", "sd"):
        quality = "hd"

    custom_path = (cam.get("custom_path") or cam.get("path") or "").strip()
    if custom_path:
        if custom_path.startswith("rtsp://"):
            return custom_path
        path = custom_path if custom_path.startswith("/") else "/" + custom_path
    else:
        path = preset.get(quality, preset["hd"])

    port = int(cam.get("port") or preset.get("port", 554))
    auth = f"{user}:{password}@" if (user or password) else ""
    return f"rtsp://{auth}{ip}:{port}{path}"


def build_privacy_filter(cam: dict) -> str:
    zones = cam.get("privacy_zones") or cam.get("mask_zones") or []
    boxes = []
    for z in zones:
        try:
            x = max(0.0, min(1.0, float(z.get("x", 0))))
            y = max(0.0, min(1.0, float(z.get("y", 0))))
            w = max(0.01, min(1.0 - x, float(z.get("w", 0))))
            h = max(0.01, min(1.0 - y, float(z.get("h", 0))))
            boxes.append(f"drawbox=x=iw*{x:.4f}:y=ih*{y:.4f}:w=iw*{w:.4f}:h=ih*{h:.4f}:color=black@1:t=fill")
        except Exception:
            continue
    return ",".join(boxes)


def log_ffmpeg_errors(cam_name: str, proc: subprocess.Popen):
    try:
        if not proc.stderr:
            return
        for line in iter(proc.stderr.readline, b""):
            msg = line.decode("utf-8", errors="ignore").strip()
            if msg:
                print(f"[FFMPEG {cam_name}] {msg}")
    except Exception:
        pass


def start_ffmpeg_for_cam(cam: dict, segment_minutes: int = 15, prev_restarts: int = 0):
    cam_id = str(cam.get("id", ""))
    ip = (cam.get("ip") or "").strip()
    if not cam_id or not ip:
        return

    ensure_daily_folders_for_cam(cam)

    stream_dir = STREAMS_DIR / cam_id
    stream_dir.mkdir(parents=True, exist_ok=True)
    for old_file in stream_dir.glob("*"):
        try:
            old_file.unlink()
        except Exception:
            pass

    rtsp_url = build_rtsp_url(cam)
    mode = (cam.get("mode") or "live").lower()
    has_audio = bool(cam.get("audio", False))
    privacy_vf = build_privacy_filter(cam)
    segment_seconds = max(60, int(segment_minutes or 15) * 60)

    cmd = [
        "ffmpeg", "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-fflags", "+genpts+discardcorrupt",
        "-rtsp_transport", "tcp",
        "-i", rtsp_url,
    ]

    cmd += ["-map", "0:v:0"]
    if privacy_vf:
        cmd += [
            "-vf", privacy_vf,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-tune", "zerolatency",
            "-pix_fmt", "yuv420p"
        ]
    else:
        cmd += ["-c:v", "copy"]

    if has_audio:
        cmd += ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "64k"]
    else:
        cmd += ["-an"]

    hls_playlist = str(stream_dir / "index.m3u8")
    hls_segment_pattern = str(stream_dir / "seg_%03d.ts")
    cmd += [
        "-f", "hls",
        "-hls_time", "2",
        "-hls_list_size", "6",
        "-hls_flags", "delete_segments+independent_segments",
        "-hls_segment_filename", hls_segment_pattern,
        hls_playlist
    ]

    if mode in ("always", "24/7", "record", "motion"):
        cam_base = get_cam_record_base(cam)
        out_pattern = str(cam_base / "%Y-%m-%d" / "%H-%M-%S.mp4")

        cmd += ["-map", "0:v:0"]
        if privacy_vf:
            cmd += [
                "-vf", privacy_vf,
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-pix_fmt", "yuv420p"
            ]
        else:
            cmd += ["-c:v", "copy"]

        if has_audio:
            cmd += ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "64k"]
        else:
            cmd += ["-an"]

        cmd += [
            "-f", "segment",
            "-segment_time", str(segment_seconds),
            "-segment_atclocktime", "1",
            "-reset_timestamps", "1",
            "-strftime", "1",
            out_pattern
        ]

    cam_name = cam.get("name", cam_id)
    print(f"[STREAM] Starte Kamera '{cam_name}' ({ip}) | Modus: {mode} | Reconnects: {prev_restarts}")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True
    )

    threading.Thread(target=log_ffmpeg_errors, args=(cam_name, proc), daemon=True).start()

    with processes_lock:
        camera_processes[cam_id] = {
            "proc": proc,
            "started_at": time.time(),
            "restarts": prev_restarts,
            "cam_cfg": json.loads(json.dumps(cam)),
            "segment_minutes": segment_minutes
        }


def stop_ffmpeg_for_cam(cam_id: str):
    cam_id = str(cam_id)
    with processes_lock:
        info = camera_processes.pop(cam_id, None)
    if info and info.get("proc"):
        proc = info["proc"]
        try:
            if proc.poll() is None:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def sync_all_cameras():
    cfg = load_settings()
    seg_mins = int(cfg.get("segment_minutes", 15))
    cams = cfg.get("cameras", [])
    active_ids = {str(c.get("id")) for c in cams if c.get("id")}

    with processes_lock:
        running_ids = set(camera_processes.keys())

    for cid in running_ids - active_ids:
        stop_ffmpeg_for_cam(cid)

    for cam in cams:
        cid = str(cam.get("id"))
        if not cid:
            continue
        with processes_lock:
            existing = camera_processes.get(cid)
        if not existing:
            start_ffmpeg_for_cam(cam, seg_mins, prev_restarts=0)
        else:
            if existing.get("cam_cfg") != cam or existing.get("segment_minutes") != seg_mins:
                restarts = existing.get("restarts", 0)
                stop_ffmpeg_for_cam(cid)
                start_ffmpeg_for_cam(cam, seg_mins, prev_restarts=restarts)


def get_stream_last_activity(cam_id: str) -> float:
    stream_dir = STREAMS_DIR / str(cam_id)
    if not stream_dir.exists():
        return 0.0
    latest = 0.0
    for f in stream_dir.glob("*"):
        try:
            mtime = f.stat().st_mtime
            if mtime > latest:
                latest = mtime
        except Exception:
            pass
    return latest


def watchdog_and_maintenance_loop():
    last_cleanup = 0.0
    last_folder_prep = 0.0

    while not stop_event.is_set():
        now = time.time()

        if now - last_folder_prep > 600:
            ensure_all_daily_folders()
            last_folder_prep = now

        if now - last_cleanup > 3600:
            cleanup_old_recordings()
            last_cleanup = now

        cfg = load_settings()
        seg_mins = int(cfg.get("segment_minutes", 15))
        cam_map = {str(c.get("id")): c for c in cfg.get("cameras", []) if c.get("id")}

        for cid, cam in cam_map.items():
            if not (cam.get("ip") or "").strip():
                continue
            with processes_lock:
                info = camera_processes.get(cid)

            if not info:
                start_ffmpeg_for_cam(cam, seg_mins, prev_restarts=0)
                continue

            proc = info.get("proc")
            started_at = info.get("started_at", now)
            restarts = info.get("restarts", 0)

            needs_restart = False
            reason = ""

            if proc is None or proc.poll() is not None:
                needs_restart = True
                code = proc.returncode if proc else "None"
                reason = f"FFmpeg beendet (Code {code})"
            else:
                uptime = now - started_at
                if uptime > 25:
                    last_act = get_stream_last_activity(cid)
                    if last_act == 0.0 or (now - last_act) > 25:
                        needs_restart = True
                        reason = "Keine Pakete seit >25s (Kamera-Reboot erkannt)"

            if needs_restart:
                print(f"[WATCHDOG] '{cam.get('name', cid)}': {reason} -> Reconnect in 8s...")
                stop_ffmpeg_for_cam(cid)
                if stop_event.wait(8):
                    break
                ensure_daily_folders_for_cam(cam)
                start_ffmpeg_for_cam(cam, seg_mins, prev_restarts=restarts + 1)

        if stop_event.wait(8):
            break


def cleanup_old_recordings():
    cfg = load_settings()
    retention_days = int(cfg.get("retention_days", 7))
    if retention_days <= 0:
        return
    cutoff = time.time() - (retention_days * 86400)
    try:
        for mp4 in RECORDINGS_DIR.rglob("*.mp4"):
            try:
                if mp4.stat().st_mtime < cutoff:
                    mp4.unlink()
            except Exception:
                pass
        today_str = datetime.datetime.now().strftime("%Y-%m-%d")
        tomorrow_str = (datetime.datetime.now() + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        for folder in sorted(RECORDINGS_DIR.rglob("*"), key=lambda p: len(str(p)), reverse=True):
            if folder.is_dir() and folder.name not in (today_str, tomorrow_str):
                try:
                    if not any(folder.iterdir()):
                        folder.rmdir()
                except Exception:
                    pass
    except Exception as e:
        print(f"[WARN] Aufräumen fehlgeschlagen: {e}")


def list_all_recordings():
    items = []
    if not RECORDINGS_DIR.exists():
        return items

    for mp4 in RECORDINGS_DIR.rglob("*.mp4"):
        try:
            stat = mp4.stat()
            if stat.st_size < 16384 and (time.time() - stat.st_mtime) < 5:
                continue
            rel_path = mp4.relative_to(RECORDINGS_DIR).as_posix()
            parts = rel_path.split("/")

            cam_name = parts[0] if len(parts) > 1 else "Kamera"
            date_str = None
            for p in parts:
                if re.match(r"^\d{4}-\d{2}-\d{2}$", p):
                    date_str = p
                    break
            if not date_str:
                date_str = datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d")

            items.append({
                "filename": mp4.name,
                "rel_path": rel_path,
                "url": f"/recordings/{urllib.parse.quote(rel_path)}",
                "cam_name": cam_name,
                "date": date_str,
                "size_mb": round(stat.st_size / (1024 * 1024), 2),
                "timestamp": int(stat.st_mtime),
                "time_str": datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%H:%M:%S")
            })
        except Exception:
            continue

    items.sort(key=lambda x: x["timestamp"], reverse=True)
    return items


def check_port(ip: str, port: int, timeout: float = 0.28) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False


def probe_single_ip(ip: str):
    if check_port(ip, 2020, 0.28):
        return {"ip": ip, "brand": "tapo", "name": f"Tapo ({ip})"}
    if check_port(ip, 554, 0.28):
        if check_port(ip, 8000, 0.2):
            return {"ip": ip, "brand": "reolink", "name": f"Reolink ({ip})"}
        if check_port(ip, 888, 0.2):
            return {"ip": ip, "brand": "foscam", "name": f"Foscam ({ip})"}
        return {"ip": ip, "brand": "tapo", "name": f"Kamera ({ip})"}
    return None


def detect_lan_prefixes(hint_host: str = "") -> list:
    prefixes = []
    def add_ip_prefix(ip_str: str):
        ip_clean = (ip_str or "").split(":")[0].strip()
        m = re.match(r"^(\d{1,3}\.\d{1,3}\.\d{1,3})\.\d{1,3}$", ip_clean)
        if m:
            pref = m.group(1)
            if not pref.startswith(("127.", "172.17.", "172.18.", "172.19.")):
                if pref not in prefixes:
                    prefixes.append(pref)

    add_ip_prefix(hint_host)
    try:
        cfg = load_settings()
        for c in cfg.get("cameras", []):
            add_ip_prefix(c.get("ip", ""))
    except Exception:
        pass

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        add_ip_prefix(s.getsockname()[0])
        s.close()
    except Exception:
        pass

    if not prefixes:
        prefixes.append("192.168.178")
    return prefixes[:2]


def discover_onvif_devices(hint_host: str = "") -> list:
    found_ips = {}
    ws_discovery_msg = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
        'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
        'xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
        '<e:Header>'
        '<w:MessageID>uuid:84ede3de-7dec-11d0-c360-f01234567890</w:MessageID>'
        '<w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>'
        '<w:Action a:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>'
        '</e:Header>'
        '<e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body>'
        '</e:Envelope>'
    ).encode("utf-8")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(0.9)
    try:
        sock.sendto(ws_discovery_msg, ("239.255.255.250", 3702))
        start = time.time()
        while time.time() - start < 0.9:
            try:
                data, addr = sock.recvfrom(65535)
                ip = addr[0]
                text = data.decode("utf-8", errors="ignore").lower()
                brand = "generic"
                for b in ("tapo", "tp-link", "reolink", "hikvision", "dahua", "amcrest", "foscam", "axis", "uniview"):
                    if b in text:
                        brand = "tapo" if b == "tp-link" else b
                        break
                found_ips[ip] = {"ip": ip, "brand": brand, "name": f"{brand.capitalize()} ({ip})"}
            except socket.timeout:
                break
            except Exception:
                break
    except Exception:
        pass
    finally:
        sock.close()

    prefixes = detect_lan_prefixes(hint_host)
    candidate_ips = [f"{pref}.{i}" for pref in prefixes for i in range(2, 255) if f"{pref}.{i}" not in found_ips]

    with ThreadPoolExecutor(max_workers=64) as executor:
        futures = {executor.submit(probe_single_ip, ip): ip for ip in candidate_ips}
        for fut in as_completed(futures):
            try:
                res = fut.result()
                if res:
                    found_ips[res["ip"]] = res
            except Exception:
                pass

    return sorted(found_ips.values(), key=lambda x: [int(p) for p in x["ip"].split(".")])


def send_onvif_ptz(cam: dict, action: str, direction: str = "", speed: float = 0.5) -> bool:
    ip = cam.get("ip")
    if not ip:
        return False
    brand = (cam.get("brand") or "tapo").lower()
    onvif_port = BRAND_PRESETS.get(brand, BRAND_PRESETS["generic"]).get("onvif_port", 2020)
    url = f"http://{ip}:{onvif_port}/onvif/ptz_service"

    user = str(cam.get("user") or cam.get("username") or "").strip()
    password = str(cam.get("password") or cam.get("pass") or "").strip()

    vx, vy, vz = 0.0, 0.0, 0.0
    if direction == "left":
        vx = -speed
    elif direction == "right":
        vx = speed
    elif direction == "up":
        vy = speed
    elif direction == "down":
        vy = -speed

    if action == "stop" or direction == "stop":
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
            'xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl">'
            '<s:Body><tptz:Stop><tptz:ProfileToken>000</tptz:ProfileToken>'
            '<tptz:PanTilt>true</tptz:PanTilt><tptz:Zoom>true</tptz:Zoom></tptz:Stop></s:Body></s:Envelope>'
        )
    else:
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
            'xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl" '
            'xmlns:tt="http://www.onvif.org/ver10/schema">'
            '<s:Body><tptz:ContinuousMove><tptz:ProfileToken>000</tptz:ProfileToken>'
            f'<tptz:Velocity><tt:PanTilt x="{vx}" y="{vy}" space="http://www.onvif.org/ver10/tptz/PanTiltSpaces/VelocityGenericSpace"/></tt:Velocity>'
            '</tptz:ContinuousMove></s:Body></s:Envelope>'
        )

    headers = {"Content-Type": "application/soap+xml; charset=utf-8"}
    req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
    if user and password:
        pass_mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        pass_mgr.add_password(None, url, user, password)
        auth_handler = urllib.request.HTTPDigestAuthHandler(pass_mgr)
        opener = urllib.request.build_opener(auth_handler)
        try:
            opener.open(req, timeout=3)
            return True
        except Exception:
            return False

    try:
        with urllib.request.urlopen(req, timeout=3):
            return True
    except Exception:
        return False


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class RequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        if self.path.startswith("/stream/"):
            return
        sys.stdout.write(f"[HTTP] {self.address_string()} - {format % args}\n")

    def send_json(self, data, status=200):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def serve_file_with_range(self, file_path: Path, content_type: str, as_attachment: bool = False):
        if not file_path.exists() or not file_path.is_file():
            self.send_error(404, "File not found")
            return
        file_size = file_path.stat().st_size
        range_header = self.headers.get("Range")

        if range_header and range_header.startswith("bytes="):
            try:
                ranges = range_header.replace("bytes=", "").split("-")
                start = int(ranges[0]) if ranges[0] else 0
                end = int(ranges[1]) if (len(ranges) > 1 and ranges[1]) else file_size - 1
                end = min(end, file_size - 1)
                length = end - start + 1

                self.send_response(206)
                self.send_header("Content-Type", content_type)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                self.send_header("Content-Length", str(length))
                self.end_headers()
                with open(file_path, "rb") as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = f.read(min(65536, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                return
            except Exception:
                pass

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(file_size))
        self.send_header("Accept-Ranges", "bytes")
        if as_attachment:
            self.send_header("Content-Disposition", f'attachment; filename="{file_path.name}"')
        self.end_headers()
        with open(file_path, "rb") as f:
            shutil.copyfileobj(f, self.wfile)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self.serve_file_with_range(Path(__file__).parent / "index.html", "text/html; charset=utf-8")
            return

        if path == "/api/settings":
            self.send_json(load_settings())
            return

        if path == "/api/status":
            cfg = load_settings()
            status_map = {}
            now = time.time()
            for cam in cfg.get("cameras", []):
                cid = str(cam.get("id"))
                with processes_lock:
                    info = camera_processes.get(cid)
                last_act = get_stream_last_activity(cid)
                online = bool(
                    info and info.get("proc") and info["proc"].poll() is None
                    and last_act > 0 and (now - last_act) < 25
                )
                status_map[cid] = {
                    "online": online,
                    "mode": cam.get("mode", "live"),
                    "restarts": info.get("restarts", 0) if info else 0
                }
            self.send_json(status_map)
            return

        if path == "/api/disk":
            usage = shutil.disk_usage(RECORDINGS_DIR)
            self.send_json({
                "total_gb": round(usage.total / (1024 ** 3), 2),
                "used_gb": round(usage.used / (1024 ** 3), 2),
                "free_gb": round(usage.free / (1024 ** 3), 2),
                "percent": round((usage.used / usage.total) * 100, 1) if usage.total else 0
            })
            return

        if path in ("/api/events", "/api/recordings"):
            items = list_all_recordings()
            dates = sorted({i["date"] for i in items}, reverse=True)
            self.send_json({"events": items, "dates": dates})
            return

        if path == "/api/onvif_scan":
            host_header = self.headers.get("Host", "")
            self.send_json({"devices": discover_onvif_devices(hint_host=host_header)})
            return

        if path == "/api/snapshot":
            cam_id = (query.get("cam_id") or [""])[0]
            cfg = load_settings()
            cam = next((c for c in cfg.get("cameras", []) if str(c.get("id")) == str(cam_id)), None)
            if not cam:
                self.send_error(404, "Camera not found")
                return
            rtsp_url = build_rtsp_url(cam)
            try:
                res = subprocess.run(
                    ["ffmpeg", "-rtsp_transport", "tcp", "-i", rtsp_url,
                     "-frames:v", "1", "-f", "image2", "-vcodec", "mjpeg", "pipe:1"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=8
                )
                if res.stdout:
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(res.stdout)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(res.stdout)
                    return
            except Exception:
                pass
            self.send_error(500, "Snapshot failed")
            return

        if path == "/api/export":
            rel = (query.get("file") or [""])[0]
            date_filter = (query.get("date") or [""])[0]
            if rel:
                target = (RECORDINGS_DIR / rel).resolve()
                if not str(target).startswith(str(RECORDINGS_DIR.resolve())):
                    self.send_error(403, "Forbidden")
                    return
                self.serve_file_with_range(target, "video/mp4", as_attachment=True)
                return
            if date_filter:
                tmp_zip = Path(tempfile.mktemp(suffix=f"_{date_filter}.zip"))
                try:
                    with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_STORED) as zf:
                        for mp4 in RECORDINGS_DIR.rglob("*.mp4"):
                            if date_filter in mp4.relative_to(RECORDINGS_DIR).as_posix():
                                zf.write(mp4, mp4.relative_to(RECORDINGS_DIR).as_posix())
                    self.serve_file_with_range(tmp_zip, "application/zip", as_attachment=True)
                finally:
                    try:
                        tmp_zip.unlink()
                    except Exception:
                        pass
                return
            self.send_error(400, "Missing parameter")
            return

        if path.startswith("/stream/"):
            rel = path[len("/stream/"):]
            target = (STREAMS_DIR / rel).resolve()
            if not str(target).startswith(str(STREAMS_DIR.resolve())) or not target.exists():
                self.send_error(404, "Stream segment not ready")
                return
            ctype = "application/vnd.apple.mpegurl" if target.suffix == ".m3u8" else "video/MP2T"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            data = target.read_bytes()
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if path.startswith("/recordings/"):
            rel = urllib.parse.unquote(path[len("/recordings/"):])
            target = (RECORDINGS_DIR / rel).resolve()
            if not str(target).startswith(str(RECORDINGS_DIR.resolve())):
                self.send_error(403, "Forbidden")
                return
            self.serve_file_with_range(target, "video/mp4")
            return

        self.send_error(404, "Not found")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            payload = {}

        if path == "/api/settings":
            save_settings(payload)
            ensure_all_daily_folders()
            sync_all_cameras()
            self.send_json(load_settings())
            return

        if path == "/api/language":
            cfg = load_settings()
            cfg["language"] = payload.get("language", "de")
            save_settings(cfg)
            self.send_json({"ok": True, "language": cfg["language"]})
            return

        if path == "/api/ptz":
            cam_id = str(payload.get("cam_id", ""))
            action = payload.get("action", "move")
            direction = payload.get("direction", "stop")
            cfg = load_settings()
            cam = next((c for c in cfg.get("cameras", []) if str(c.get("id")) == cam_id), None)
            if not cam:
                self.send_json({"ok": False, "error": "Camera not found"}, status=404)
                return
            ok = send_onvif_ptz(cam, action=action, direction=direction)
            self.send_json({"ok": ok})
            return

        self.send_error(404, "Not found")

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path in ("/api/events", "/api/recordings"):
            rel = (query.get("file") or [""])[0]
            target = (RECORDINGS_DIR / rel).resolve()
            if str(target).startswith(str(RECORDINGS_DIR.resolve())) and target.exists():
                try:
                    target.unlink()
                    self.send_json({"ok": True})
                    return
                except Exception as e:
                    self.send_json({"ok": False, "error": str(e)}, status=500)
                    return
            self.send_json({"ok": False, "error": "File not found"}, status=404)
            return
        self.send_error(404, "Not found")


def shutdown_handler(signum, frame):
    print("\n[SYSTEM] Beende OpenCam Lite und stoppe alle FFmpeg-Prozesse...")
    stop_event.set()
    with processes_lock:
        cids = list(camera_processes.keys())
    for cid in cids:
        stop_ffmpeg_for_cam(cid)
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    ensure_all_daily_folders()
    sync_all_cameras()

    wd_thread = threading.Thread(target=watchdog_and_maintenance_loop, daemon=True)
    wd_thread.start()

    server = ThreadingHTTPServer(("0.0.0.0", PORT), RequestHandler)
    print(f"[SYSTEM] OpenCam Lite läuft auf http://0.0.0.0:{PORT}")
    server.serve_forever()


import os, json, time, shutil, subprocess, threading, urllib.parse, urllib.request
import hashlib, base64, datetime, re, sqlite3, zipfile, io, glob, socket, uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

APP_VERSION = "0.1.0"
CONFIG_FILE = "/config/settings.json"
DB_FILE = "/config/events.db"
WEB_DIR = "/app"
REC_DIR = "/recordings"
PAYPAL_FIXED_URL = "https://paypal.me/Chrizzkai"

os.makedirs("/config", exist_ok=True)
os.makedirs(f"{WEB_DIR}/stream", exist_ok=True)
os.makedirs(REC_DIR, exist_ok=True)
if not os.path.exists(f"{WEB_DIR}/archiv"):
    try:
        os.symlink(REC_DIR, f"{WEB_DIR}/archiv")
    except Exception:
        pass

BRAND_PRESETS = {
    "tapo":      {"hd": "stream1", "sd": "stream2", "onvif_port": 2020},
    "reolink":   {"hd": "h264Preview_01_main", "sd": "h264Preview_01_sub", "onvif_port": 8000},
    "hikvision": {"hd": "Streaming/Channels/101", "sd": "Streaming/Channels/102", "onvif_port": 80},
    "dahua":     {"hd": "cam/realmonitor?channel=1&subtype=0", "sd": "cam/realmonitor?channel=1&subtype=1", "onvif_port": 80},
    "amcrest":   {"hd": "cam/realmonitor?channel=1&subtype=0", "sd": "cam/realmonitor?channel=1&subtype=1", "onvif_port": 80},
    "foscam":    {"hd": "videoMain", "sd": "videoSub", "onvif_port": 88},
    "uniview":   {"hd": "media/video1", "sd": "media/video2", "onvif_port": 80},
    "axis":      {"hd": "axis-media/media.amp", "sd": "axis-media/media.amp", "onvif_port": 80},
    "generic":   {"hd": "stream1", "sd": "stream2", "onvif_port": 80},
}

ffmpeg_procs = {}
stream_signatures = {}
motion_state = {}
sound_state = {}
lock = threading.Lock()

def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                camera TEXT,
                filepath TEXT,
                type TEXT
            )
        """)
        conn.commit()

def default_admin():
    return {
        "username": "admin",
        "password_hash": hashlib.sha256("admin".encode("utf-8")).hexdigest(),
        "role": "admin"
    }

def load_settings():
    if not os.path.exists(CONFIG_FILE):
        default_cfg = {
            "version": APP_VERSION,
            "retention_days": 7,
            "segment_minutes": 60,
            "ntfy_topic": "",
            "ha_webhook": "",
            "paypal_url": PAYPAL_FIXED_URL,
            "language": "en",
            "users": [default_admin()],
            "cameras": []
        }
        save_settings(default_cfg)
        return default_cfg
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not data.get("users"):
            data["users"] = [default_admin()]
        if not data.get("language"):
            data["language"] = "en"
        if not data.get("segment_minutes"):
            data["segment_minutes"] = 60
        data["paypal_url"] = PAYPAL_FIXED_URL
        data["version"] = APP_VERSION
        return data
    except Exception:
        return {
            "version": APP_VERSION,
            "retention_days": 7,
            "segment_minutes": 60,
            "ntfy_topic": "",
            "ha_webhook": "",
            "paypal_url": PAYPAL_FIXED_URL,
            "language": "en",
            "users": [default_admin()],
            "cameras": []
        }

def save_settings(data):
    data["version"] = APP_VERSION
    data["paypal_url"] = PAYPAL_FIXED_URL
    if not data.get("language"):
        data["language"] = "en"
    if not data.get("segment_minutes"):
        data["segment_minutes"] = 60
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def safe_cam_id(name, idx):
    clean = re.sub(r"[^a-zA-Z0-9_-]", "_", name or f"cam_{idx}")
    return f"{idx}_{clean}"

def build_rtsp_url(cam):
    brand = cam.get("brand", "tapo").lower()
    preset = BRAND_PRESETS.get(brand, BRAND_PRESETS["tapo"])
    res = cam.get("res", "hd").lower()
    stream_path = cam.get("custom_path", "").strip()
    if not stream_path:
        stream_path = preset["hd"] if res == "hd" else preset["sd"]
    stream_path = stream_path.lstrip("/")
    user = urllib.parse.quote(cam.get("user", ""), safe="")
    pwd = urllib.parse.quote(cam.get("password", ""), safe="")
    ip = cam.get("ip", "").strip()
    port = int(cam.get("rtsp_port", 554) or 554)
    if user or pwd:
        return f"rtsp://{user}:{pwd}@{ip}:{port}/{stream_path}"
    return f"rtsp://{ip}:{port}/{stream_path}"

def build_drawbox_filter(mask_str):
    if not mask_str or not str(mask_str).strip():
        return None
    filters = []
    parts = str(mask_str).split(";")
    for part in parts:
        part = part.strip()
        if not part:
            continue
        coords = [c.strip() for c in part.split(",")]
        if len(coords) < 4:
            continue
        z_type = coords[4].lower() if len(coords) > 4 else "privacy"
        if z_type == "motion":
            continue
        try:
            x_pct = max(0.0, min(100.0, float(coords[0]))) / 100.0
            y_pct = max(0.0, min(100.0, float(coords[1]))) / 100.0
            w_pct = max(1.0, min(100.0, float(coords[2]))) / 100.0
            h_pct = max(1.0, min(100.0, float(coords[3]))) / 100.0
            filters.append(
                f"drawbox=x=iw*{x_pct:.4f}:y=ih*{y_pct:.4f}:w=iw*{w_pct:.4f}:h=ih*{h_pct:.4f}:color=black@1:t=fill"
            )
        except ValueError:
            continue
    return ",".join(filters) if filters else None

def get_stream_signature(idx, cam, segment_minutes):
    return json.dumps({
        "idx": idx,
        "ip": cam.get("ip", "").strip(),
        "rtsp_port": cam.get("rtsp_port", 554),
        "user": cam.get("user", "").strip(),
        "password": cam.get("password", ""),
        "brand": cam.get("brand", "tapo"),
        "res": cam.get("res", "hd"),
        "custom_path": cam.get("custom_path", "").strip(),
        "mode": cam.get("mode", "live"),
        "audio": cam.get("audio", True),
        "segment_minutes": int(segment_minutes or 60),
        "vf": build_drawbox_filter(cam.get("mask_zones", ""))
    }, sort_keys=True)

def send_notification(cam_name, event_type, filepath=""):
    settings = load_settings()
    ntfy_topic = settings.get("ntfy_topic", "").strip()
    ha_webhook = settings.get("ha_webhook", "").strip()
    msg = f"[{cam_name}] Event detected: {event_type}"

    if ntfy_topic:
        try:
            req = urllib.request.Request(
                f"https://ntfy.sh/{ntfy_topic}",
                data=msg.encode("utf-8"),
                headers={"Title": "OpenCam Lite Alert", "Priority": "high"}
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass

    if ha_webhook:
        try:
            payload = json.dumps({
                "camera": cam_name,
                "type": event_type,
                "filepath": filepath,
                "timestamp": datetime.datetime.now().isoformat()
            }).encode("utf-8")
            req = urllib.request.Request(
                ha_webhook,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass

def log_event(cam_name, rel_path, ev_type):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            "INSERT INTO events (timestamp, camera, filepath, type) VALUES (?, ?, ?, ?)",
            (ts, cam_name, rel_path, ev_type)
        )
        conn.commit()
    send_notification(cam_name, ev_type, rel_path)

def stop_cam_proc(cam_id):
    proc = ffmpeg_procs.pop(cam_id, None)
    stream_signatures.pop(cam_id, None)
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()

def start_ffmpeg_for_cam(idx, cam, segment_minutes):
    cam_id = safe_cam_id(cam.get("name", f"cam_{idx}"), idx)
    stop_cam_proc(cam_id)

    rtsp_url = build_rtsp_url(cam)
    stream_dir = f"{WEB_DIR}/stream/{cam_id}"
    os.makedirs(stream_dir, exist_ok=True)

    for old_file in glob.glob(f"{stream_dir}/*"):
        try:
            os.remove(old_file)
        except Exception:
            pass

    hls_out = f"{stream_dir}/index.m3u8"
    vf_filter = build_drawbox_filter(cam.get("mask_zones", ""))

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-i", rtsp_url
    ]

    if vf_filter:
        cmd += ["-vf", vf_filter, "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-crf", "23"]
    else:
        cmd += ["-c:v", "copy"]

    if cam.get("audio", True):
        cmd += ["-c:a", "aac", "-b:a", "64k"]
    else:
        cmd += ["-an"]

    cmd += [
        "-f", "hls",
        "-hls_time", "2",
        "-hls_list_size", "4",
        "-hls_flags", "delete_segments+independent_segments",
        hls_out
    ]

    if cam.get("mode") == "always":
        cam_folder = os.path.join(REC_DIR, cam_id)
        os.makedirs(cam_folder, exist_ok=True)
        out_pattern = os.path.join(cam_folder, "%Y%m%d_%H%M%S.mp4")
        seg_seconds = max(60, int(segment_minutes or 60) * 60)

        if vf_filter:
            cmd += ["-vf", vf_filter, "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23"]
        else:
            cmd += ["-c:v", "copy"]

        if cam.get("audio", True):
            cmd += ["-c:a", "aac", "-b:a", "64k"]
        else:
            cmd += ["-an"]

        cmd += [
            "-f", "segment",
            "-segment_time", str(seg_seconds),
            "-reset_timestamps", "1",
            "-strftime", "1",
            out_pattern
        ]

    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ffmpeg_procs[cam_id] = proc
    stream_signatures[cam_id] = get_stream_signature(idx, cam, segment_minutes)

def sync_streams():
    with lock:
        settings = load_settings()
        cams = settings.get("cameras", [])
        segment_minutes = settings.get("segment_minutes", 60)
        active_ids = set()

        for idx, cam in enumerate(cams):
            if not cam.get("ip"):
                continue
            cam_id = safe_cam_id(cam.get("name", f"cam_{idx}"), idx)
            active_ids.add(cam_id)
            new_sig = get_stream_signature(idx, cam, segment_minutes)
            proc = ffmpeg_procs.get(cam_id)

            if proc is None or proc.poll() is not None or stream_signatures.get(cam_id) != new_sig:
                start_ffmpeg_for_cam(idx, cam, segment_minutes)

        for cid in list(ffmpeg_procs.keys()):
            if cid not in active_ids:
                stop_cam_proc(cid)

def check_onvif_http(ip, port):
    soap = """<?xml version="1.0" encoding="UTF-8"?>
    <s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:tds="http://www.onvif.org/ver10/device/wsdl">
      <s:Body><tds:GetSystemDateAndTime/></s:Body>
    </s:Envelope>"""
    try:
        url = f"http://{ip}:{port}/onvif/device_service"
        req = urllib.request.Request(url, data=soap.encode("utf-8"), headers={"Content-Type": "application/soap+xml; charset=utf-8"})
        with urllib.request.urlopen(req, timeout=0.8) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            if "Envelope" in body or "SystemDateAndTime" in body:
                return True
    except urllib.error.HTTPError as e:
        if e.code in (401, 400):
            return True
    except Exception:
        pass
    return False

def probe_single_ip(ip):
    """Prüft eine IP auf RTSP (554) und bekannte ONVIF-Ports."""
    rtsp_open = False
    try:
        with socket.create_connection((ip, 554), timeout=0.45):
            rtsp_open = True
    except Exception:
        pass

    onvif_candidates = [
        (2020, "tapo"),
        (8000, "reolink"),
        (88, "foscam"),
        (80, "generic")
    ]
    for port, guessed_brand in onvif_candidates:
        try:
            with socket.create_connection((ip, port), timeout=0.4):
                if port in (2020, 8000, 88) and rtsp_open:
                    return {
                        "ip": ip,
                        "brand": guessed_brand,
                        "onvif_port": port,
                        "rtsp_port": 554,
                        "name": f"{guessed_brand.capitalize()} ({ip.split('.')[-1]})"
                    }
                if check_onvif_http(ip, port):
                    return {
                        "ip": ip,
                        "brand": guessed_brand,
                        "onvif_port": port,
                        "rtsp_port": 554,
                        "name": f"ONVIF Cam ({ip.split('.')[-1]})"
                    }
        except Exception:
            continue

    if rtsp_open:
        return {
            "ip": ip,
            "brand": "generic",
            "onvif_port": 80,
            "rtsp_port": 554,
            "name": f"RTSP Cam ({ip.split('.')[-1]})"
        }
    return None

def ws_discovery_multicast():
    """Sendet einen WS-Discovery Multicast-Probe für ONVIF-Geräte im LAN."""
    found = {}
    msg_id = f"urn:uuid:{uuid.uuid4()}"
    probe = f"""<?xml version="1.0" encoding="UTF-8"?>
    <e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
                xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
                xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
                xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
      <e:Header>
        <w:MessageID>{msg_id}</w:MessageID>
        <w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
        <w:Action a:mustUnderstand="true" xmlns:a="http://www.w3.org/2005/08/addressing">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
      </e:Header>
      <e:Body>
        <d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe>
      </e:Body>
    </e:Envelope>"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(1.2)
        sock.sendto(probe.encode("utf-8"), ("239.255.255.250", 3702))
        start = time.time()
        while time.time() - start < 1.2:
            try:
                data, addr = sock.recvfrom(4096)
                ip = addr[0]
                text = data.decode("utf-8", errors="ignore").lower()
                brand = "generic"
                onvif_port = 80
                if "tapo" in text or ":2020" in text:
                    brand, onvif_port = "tapo", 2020
                elif "reolink" in text or ":8000" in text:
                    brand, onvif_port = "reolink", 8000
                elif "hikvision" in text:
                    brand = "hikvision"
                elif "dahua" in text or "amcrest" in text:
                    brand = "dahua"
                found[ip] = {
                    "ip": ip,
                    "brand": brand,
                    "onvif_port": onvif_port,
                    "rtsp_port": 554,
                    "name": f"{brand.capitalize()} ({ip.split('.')[-1]})"
                }
            except socket.timeout:
                break
            except Exception:
                break
        sock.close()
    except Exception:
        pass
    return found

def discover_onvif_devices(subnet_prefix=""):
    discovered = ws_discovery_multicast()

    prefix = subnet_prefix.strip()
    if not prefix:
        settings = load_settings()
        for c in settings.get("cameras", []):
            cip = c.get("ip", "").strip()
            if cip.count(".") == 3:
                prefix = ".".join(cip.split(".")[:3])
                break
    if not prefix:
        prefix = "192.168.178"

    parts = prefix.split(".")
    if len(parts) >= 3:
        base = f"{parts[0]}.{parts[1]}.{parts[2]}"
        ips = [f"{base}.{i}" for i in range(2, 255) if f"{base}.{i}" not in discovered]
        with ThreadPoolExecutor(max_workers=64) as ex:
            futures = {ex.submit(probe_single_ip, ip): ip for ip in ips}
            for fut in as_completed(futures):
                res = fut.result()
                if res:
                    discovered[res["ip"]] = res

    return sorted(discovered.values(), key=lambda x: [int(p) if p.isdigit() else 0 for p in x["ip"].split(".")])

def send_onvif_ptz(cam, direction):
    brand = cam.get("brand", "tapo").lower()
    preset = BRAND_PRESETS.get(brand, BRAND_PRESETS["tapo"])
    port = int(cam.get("onvif_port", preset["onvif_port"]) or preset["onvif_port"])
    ip = cam.get("ip", "").strip()
    user = cam.get("user", "")
    pwd = cam.get("password", "")

    vel_map = {
        "up":    (0.0, 0.5, 0.0),
        "down":  (0.0, -0.5, 0.0),
        "left":  (-0.5, 0.0, 0.0),
        "right": (0.5, 0.0, 0.0),
        "stop":  (0.0, 0.0, 0.0)
    }
    vx, vy, vz = vel_map.get(direction, (0.0, 0.0, 0.0))

    if direction == "stop":
        body = """<?xml version="1.0" encoding="UTF-8"?>
        <s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl">
          <s:Body>
            <tptz:Stop>
              <tptz:ProfileToken>profile_1</tptz:ProfileToken>
              <tptz:PanTilt>true</tptz:PanTilt>
              <tptz:Zoom>true</tptz:Zoom>
            </tptz:Stop>
          </s:Body>
        </s:Envelope>"""
    else:
        body = f"""<?xml version="1.0" encoding="UTF-8"?>
        <s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">
          <s:Body>
            <tptz:ContinuousMove>
              <tptz:ProfileToken>profile_1</tptz:ProfileToken>
              <tptz:Velocity>
                <tt:PanTilt x="{vx}" y="{vy}"/>
                <tt:Zoom x="{vz}"/>
              </tptz:Velocity>
            </tptz:ContinuousMove>
          </s:Body>
        </s:Envelope>"""

    try:
        url = f"http://{ip}:{port}/onvif/ptz_service"
        pwd_mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        pwd_mgr.add_password(None, url, user, pwd)
        handler = urllib.request.HTTPDigestAuthHandler(pwd_mgr)
        opener = urllib.request.build_opener(handler)
        req = urllib.request.Request(url, data=body.encode("utf-8"), headers={"Content-Type": "application/soap+xml; charset=utf-8"})
        opener.open(req, timeout=3)
        return True
    except Exception:
        return False

def watchdog_loop():
    time.sleep(2)
    sync_streams()
    while True:
        time.sleep(5)
        sync_streams()

def cleanup_loop():
    while True:
        try:
            settings = load_settings()
            days = int(settings.get("retention_days", 7) or 7)
            cutoff = time.time() - (days * 86400)
            for root, dirs, files in os.walk(REC_DIR):
                for file in files:
                    fpath = os.path.join(root, file)
                    if os.path.getmtime(fpath) < cutoff:
                        try:
                            os.remove(fpath)
                        except Exception:
                            pass
            cutoff_dt = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
            with sqlite3.connect(DB_FILE) as conn:
                conn.execute("DELETE FROM events WHERE timestamp < ?", (cutoff_dt,))
                conn.commit()
        except Exception:
            pass
        time.sleep(3600)

class AppHandler(SimpleHTTPRequestHandler):
    def send_json(self, data, status=200):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def get_auth_user(self):
        settings = load_settings()
        auth_header = self.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Basic "):
            return None
        try:
            decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
            user, pwd = decoded.split(":", 1)
            pwd_hash = hashlib.sha256(pwd.encode("utf-8")).hexdigest()
            for u in settings.get("users", []):
                if u["username"] == user and u["password_hash"] == pwd_hash:
                    return u
        except Exception:
            pass
        return None

    def do_AUTHHEAD(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="OpenCam Lite Login"')
        self.end_headers()

    def do_GET(self):
        auth_user = self.get_auth_user()
        if not auth_user:
            self.do_AUTHHEAD()
            return

        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/settings":
            s = load_settings().copy()
            s["version"] = APP_VERSION
            s["current_user"] = {"username": auth_user["username"], "role": auth_user.get("role", "admin")}
            for idx, c in enumerate(s.get("cameras", [])):
                c["cam_id"] = safe_cam_id(c.get("name", f"cam_{idx}"), idx)
            if auth_user.get("role") == "admin":
                s["users"] = [{"username": u["username"], "role": u.get("role", "viewer")} for u in s.get("users", [])]
            else:
                s.pop("users", None)
                s.pop("ntfy_topic", None)
                s.pop("ha_webhook", None)
                for c in s.get("cameras", []):
                    c.pop("password", None)
                    c.pop("user", None)
            self.send_json(s)

        elif path == "/api/status":
            self.send_json({"version": APP_VERSION, "motion": motion_state, "sound": sound_state})

        elif path == "/api/events":
            with sqlite3.connect(DB_FILE) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 100").fetchall()
                events = [dict(row) for row in rows]
            self.send_json(events)

        elif path == "/api/recordings":
            qs = urllib.parse.parse_qs(parsed.query)
            cam_id = qs.get("cam_id", [""])[0]
            rec_list = []
            settings = load_settings()
            cams = settings.get("cameras", [])
            cam_map = {safe_cam_id(c.get("name", f"cam_{i}"), i): c.get("name", f"Camera {i+1}") for i, c in enumerate(cams)}

            target_folders = [cam_id] if cam_id else list(cam_map.keys())
            for cid in target_folders:
                folder = os.path.join(REC_DIR, cid)
                if not os.path.exists(folder):
                    continue
                for fname in os.listdir(folder):
                    if not fname.endswith(".mp4"):
                        continue
                    fpath = os.path.join(folder, fname)
                    if os.path.isfile(fpath):
                        mtime = os.path.getmtime(fpath)
                        size_mb = round(os.path.getsize(fpath) / (1024 * 1024), 1)
                        rec_list.append({
                            "camera": cam_map.get(cid, cid),
                            "cam_id": cid,
                            "filename": fname,
                            "timestamp": datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S"),
                            "mtime": mtime,
                            "size_mb": size_mb,
                            "filepath": f"/archiv/{cid}/{fname}"
                        })
            rec_list.sort(key=lambda x: x["mtime"], reverse=True)
            self.send_json(rec_list[:150])

        elif path == "/api/onvif_scan":
            if auth_user.get("role") != "admin":
                self.send_json({"error": "Forbidden"}, status=403)
                return
            qs = urllib.parse.parse_qs(parsed.query)
            subnet = qs.get("subnet", [""])[0]
            devices = discover_onvif_devices(subnet)
            self.send_json({"devices": devices})

        elif path == "/api/disk":
            stat = shutil.disk_usage(REC_DIR)
            disk_info = {
                "total": round(stat.total / (1024**3), 1),
                "used": round(stat.used / (1024**3), 1),
                "free": round(stat.free / (1024**3), 1),
                "percent": round(stat.used / stat.total * 100, 1)
            }
            self.send_json(disk_info)

        elif path == "/api/snapshot":
            qs = urllib.parse.parse_qs(parsed.query)
            idx = int(qs.get("idx", ["0"])[0])
            settings = load_settings()
            cams = settings.get("cameras", [])
            if idx < 0 or idx >= len(cams):
                self.send_error(404, "Camera not found")
                return
            cam = cams[idx]
            cam_id = safe_cam_id(cam.get("name", f"cam_{idx}"), idx)
            stream_dir = f"{WEB_DIR}/stream/{cam_id}"

            ts_files = sorted(glob.glob(f"{stream_dir}/*.ts"), key=os.path.getmtime)
            out = None

            if ts_files:
                latest_ts = ts_files[-1]
                cmd_local = [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-i", latest_ts,
                    "-frames:v", "1",
                    "-f", "image2", "-c:v", "mjpeg", "pipe:1"
                ]
                try:
                    out = subprocess.check_output(cmd_local, timeout=5)
                except Exception:
                    out = None

            if not out:
                rtsp_url = build_rtsp_url(cam)
                vf_filter = build_drawbox_filter(cam.get("mask_zones", ""))
                cmd_rtsp = [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-rtsp_transport", "tcp",
                    "-i", rtsp_url,
                    "-frames:v", "1"
                ]
                if vf_filter:
                    cmd_rtsp += ["-vf", vf_filter]
                cmd_rtsp += ["-f", "image2", "-c:v", "mjpeg", "pipe:1"]
                try:
                    out = subprocess.check_output(cmd_rtsp, timeout=8)
                except Exception as e:
                    self.send_error(500, f"Snapshot failed: {e}")
                    return

            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Disposition", f'attachment; filename="snapshot_{cam_id}.jpg"')
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        elif path == "/api/export":
            qs = urllib.parse.parse_qs(parsed.query)
            cam_id = qs.get("cam_id", [""])[0]
            dt_from = qs.get("from", [""])[0]
            dt_to = qs.get("to", [""])[0]
            try:
                t_start = datetime.datetime.fromisoformat(dt_from).timestamp()
                t_end = datetime.datetime.fromisoformat(dt_to).timestamp()
            except Exception:
                self.send_error(400, "Invalid date format")
                return

            cam_folder = os.path.join(REC_DIR, cam_id)
            mem_zip = io.BytesIO()
            with zipfile.ZipFile(mem_zip, mode="w", compression=zipfile.ZIP_STORED) as zf:
                if os.path.exists(cam_folder):
                    for f in os.listdir(cam_folder):
                        fpath = os.path.join(cam_folder, f)
                        if os.path.isfile(fpath):
                            mtime = os.path.getmtime(fpath)
                            if t_start <= mtime <= t_end:
                                zf.write(fpath, arcname=f)
            data = mem_zip.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="export_{cam_id}.zip"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        else:
            super().do_GET()

    def do_POST(self):
        auth_user = self.get_auth_user()
        if not auth_user:
            self.do_AUTHHEAD()
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        try:
            payload = json.loads(body)
        except Exception:
            payload = {}

        if self.path == "/api/language":
            lang = payload.get("language", "en")
            if lang not in ("en", "de", "fr", "es"):
                lang = "en"
            cfg = load_settings()
            cfg["language"] = lang
            save_settings(cfg)
            self.send_json({"status": "ok", "language": lang})

        elif self.path == "/api/settings":
            if auth_user.get("role") != "admin":
                self.send_json({"error": "Forbidden"}, status=403)
                return

            old_settings = load_settings()
            old_users = {u["username"]: u["password_hash"] for u in old_settings.get("users", [])}

            new_users = []
            for u in payload.get("users", []):
                uname = u.get("username", "").strip()
                if not uname:
                    continue
                raw_pwd = u.get("password", "").strip()
                if raw_pwd:
                    phash = hashlib.sha256(raw_pwd.encode("utf-8")).hexdigest()
                else:
                    phash = old_users.get(uname, hashlib.sha256("admin".encode("utf-8")).hexdigest())
                new_users.append({
                    "username": uname,
                    "password_hash": phash,
                    "role": u.get("role", "viewer")
                })

            if not new_users:
                new_users = [default_admin()]

            new_cfg = {
                "version": APP_VERSION,
                "retention_days": int(payload.get("retention_days", old_settings.get("retention_days", 7)) or 7),
                "segment_minutes": int(payload.get("segment_minutes", old_settings.get("segment_minutes", 60)) or 60),
                "ntfy_topic": payload.get("ntfy_topic", old_settings.get("ntfy_topic", "")).strip(),
                "ha_webhook": payload.get("ha_webhook", old_settings.get("ha_webhook", "")).strip(),
                "paypal_url": PAYPAL_FIXED_URL,
                "language": payload.get("language", old_settings.get("language", "en")),
                "users": new_users,
                "cameras": payload.get("cameras", [])
            }
            save_settings(new_cfg)
            threading.Thread(target=sync_streams, daemon=True).start()
            self.send_json({"status": "ok", "version": APP_VERSION})

        elif self.path == "/api/ptz":
            idx = int(payload.get("idx", 0))
            direction = payload.get("dir", "stop")
            settings = load_settings()
            cams = settings.get("cameras", [])
            if 0 <= idx < len(cams):
                ok = send_onvif_ptz(cams[idx], direction)
                self.send_json({"status": "ok" if ok else "failed"})
            else:
                self.send_json({"error": "Invalid camera"}, status=404)

        else:
            self.send_json({"error": "Unknown endpoint"}, status=404)

if __name__ == "__main__":
    init_db()
    threading.Thread(target=watchdog_loop, daemon=True).start()
    threading.Thread(target=cleanup_loop, daemon=True).start()

    class CustomHandler(AppHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=WEB_DIR, **kwargs)

    print(f"OpenCam Lite v{APP_VERSION} running on port 7777...")
    ThreadingHTTPServer(("0.0.0.0", 7777), CustomHandler).serve_forever()
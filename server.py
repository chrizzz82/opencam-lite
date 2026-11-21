import os
import json
import re
import time
import shutil
import threading
import subprocess
import requests
from pathlib import Path
from typing import List, Optional, Tuple, Dict
from fastapi import FastAPI, Form, HTTPException, Response
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from pydantic import BaseModel
try:
    from PIL import Image
except Exception:
    Image = None

app = FastAPI(title="Theme Fetcher")

DATA_DIR = Path("/data")
COLLECTIONS_DIR = DATA_DIR / "collections"
LOCAL_THEMES_DIR = DATA_DIR / "local_themes"
POSTERS_DIR = DATA_DIR / "posters"
POSTERS_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = DATA_DIR / "config.json"
STATE_FILE = DATA_DIR / "state.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)
COLLECTIONS_DIR.mkdir(parents=True, exist_ok=True)
LOCAL_THEMES_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_CONFIG = {
    "local_media_paths": os.getenv("LOCAL_MEDIA_DIR", "/media"),
    "local_scan_enabled": os.getenv("LOCAL_SCAN_ENABLED", "true").lower() in ("true", "1", "yes"),
    "jellyfin_url": os.getenv("JELLYFIN_URL", "").rstrip("/"),
    "jellyfin_api_key": os.getenv("JELLYFIN_API_KEY", ""),
    "jf_metadata_dir": os.getenv("JF_METADATA_DIR", "/jellyfin-metadata"),
    "plex_url": os.getenv("PLEX_URL", "").rstrip("/"),
    "plex_token": os.getenv("PLEX_TOKEN", ""),
    "min_duration_sec": int(os.getenv("MIN_DURATION_SEC", "25")),
    "max_duration_sec": int(os.getenv("MAX_DURATION_SEC", "270")),
    "scan_interval_hours": int(os.getenv("SCAN_INTERVAL_HOURS", "12")),
    "audio_fade": True,
    "embed_cover": True,
    "auto_download": False,
    "ui_language": "en"
}

EXCLUDE_KEYWORDS = [
    "scene", "szene", "clip", "movieclip", "movie clip", "dialog", "dialogue",
    "teaser", "trailer", "ausschnitt", "interview", "making of", "featurette",
    "reaction", "reaktion", "review", "kritik", "talk", "hörspiel", "ganzer film",
    "full movie", "parody", "parodie", "gameplay", "walkthrough", "1 hour",
    "10 hours", "loop", "tutorial", "piano", "klavier", "guitar", "gitarre",
    "karaoke", "bass boosted", "nightcore", "slowed", "reverb", "8d audio",
    "extended", "compilation", "suite", "blooper", "deleted scene", "fight scene",
    "ending scene", "death scene"
]

THEME_REGEX = re.compile(r".*themes?.*\.(mp3|m4a|flac|ogg|wav|aac|opus)$", re.IGNORECASE)

# Multi-Language Dictionary
LANG_PACK = {
    "de": {
        "app_title": "Theme Fetcher",
        "movies": "Filme",
        "shows": "Serien",
        "collections": "Collections",
        "libraries": "Eingelesene Mediatheken",
        "movies_plural": "Filme",
        "shows_plural": "Serien",
        "collections_plural": "Collections",
        "all_media": "alle Medien",
        "settings": "Einstellungen",
        "settings_title": "Einstellungen & Quellen",
        "back_to_lib": "← Zurück zur Mediathek",
        "scan_only": "🔍 Nur {scope} scannen",
        "scan_all": "🌐 Alles scannen",
        "download_missing": "⬇️ {scope} laden ({count})",
        "cancel": "🛑 Abbrechen",
        "cancelling": "Abbruch angefordert...",
        "cancel_requested": "Abbruch angefordert",
        "ready": "Bereit",
        "scanning": "Lese {scope} ein...",
        "fetching_jf": "Lese Jellyfin ({scope}) ein...",
        "fetching_plex": "Lese Plex ({scope}) ein...",
        "scanning_local": "Prüfe lokale Ordner ({scope})...",
        "scan_cancelled": "Scan abgebrochen!",
        "scan_complete": "Scan abgeschlossen: {count} {scope} erfasst.",
        "scan_error": "Fehler beim Scan: {err}",
        "init_queue": "Initialisiere Download-Warteschlange...",
        "no_missing": "Keine fehlenden Themes in dieser Kategorie!",
        "download_cancelled": "Download abgebrochen!",
        "downloading_item": "Lade ({idx}/{total}): {title}...",
        "download_complete": "Download fertig! {down} geladen, {fail} fehlgeschlagen.",
        "download_error": "Fehler beim Download: {err}",
        "searching_soundtrack": "Suche Soundtrack...",
        "no_soundtrack_found": "Kein Soundtrack gefunden",
        "no_clean_theme": "Keine reine Titelmusik gefunden (nur Trailer/Szenen oder keine Treffer)",
        "downloading_candidate": "Lade ({idx}/{total}): {title}...",
        "mastering_audio": "Schneide Audio (Fade-In/Out)...",
        "download_success": "Erfolgreich geladen!",
        "error_prefix": "Fehler: ",
        "all_failed": "Alle Treffer fehlgeschlagen ({err})",
        "op_in_progress": "Aktion läuft...",
        "processing": "Verarbeite...",
        "downloaded_stat": "Erfolgreich geladen",
        "failed_stat": "Fehler",
        "next_in_queue": "📋 Nächste in der Download-Warteschlange:",
        "recently_finished": "✓ Zuletzt abgeschlossen:",
        "no_more_queue": "Keine weiteren Titel in der Warteschlange.",
        "filter_label": "Filter:",
        "all_pill": "Alle ({count})",
        "missing_pill": "❌ Fehlt ({count})",
        "present_pill": "✓ Vorhanden ({count})",
        "search_placeholder": "{scope} durchsuchen...",
        "search_btn": "Suchen",
        "th_cover": "Cover",
        "th_title_path": "Titel & Pfad",
        "th_status": "Status",
        "th_preview": "Vorschau",
        "th_actions": "Aktionen",
        "th_progress": "Fortschritt",
        "status_present": "✓ Vorhanden",
        "status_missing": "✕ Fehlt",
        "status_ready": "✓ Bereit",
        "no_entries": "Keine Einträge unter {scope} gefunden.",
        "btn_select": "🎵 Auswählen",
        "btn_select_title": "Theme auswählen (Top Vorschläge vorhören)",
        "btn_dl_placeholder": "YouTube URL...",
        "btn_dl_title": "Direkt laden",
        "btn_recheck_title": "Nur diesen Eintrag neu prüfen",
        "btn_delete_confirm": "Theme löschen und sperren?",
        "btn_delete_title": "Löschen & Blockieren",
        "modal_title": "🎵 Theme-Vorschläge: {title}",
        "modal_search_placeholder": "Titel / Komponist / YouTube URL...",
        "modal_search_btn": "🔍 Neue Suche",
        "modal_searching": "Suche Soundtrack-Kandidaten (MusicBrainz, ThemerrDB, YouTube)...",
        "modal_identified": "🎯 <strong>Erkannter Soundtrack:</strong> {display} <span style=\"color:#8b929a;\">(Quelle: {source})</span>",
        "modal_no_matches": "Keine passenden Soundtracks gefunden. Probiere einen anderen Suchbegriff oben!",
        "modal_open_yt": "Auf YouTube öffnen ↗",
        "modal_length": "Länge: ",
        "modal_apply": "✓ Übernehmen",
        "modal_seek_mid": "⏩ Mitte",
        "modal_seek_title": "Zur Mitte springen (50%)",
        "settings_saved_banner": "✓ Einstellungen erfolgreich gespeichert!",
        "sec_local": "📁 1. Lokale Medienquellen (Festplatte)",
        "btn_test_path": "📂 Pfad testen",
        "lbl_paths": "Medienpfad(e) im Container",
        "sub_paths": "Standard: <code>/media</code>. Entspricht deinem Docker Volume Mount.",
        "lbl_local_scan": "Lokalen Festplatten-Scan aktivieren (wird automatisch dedupliziert)",
        "sec_servers": "🎬 2. Medienserver (Jellyfin / Plex)",
        "btn_test_jf": "🔌 Jellyfin testen",
        "btn_test_plex": "🍿 Plex testen",
        "lbl_jf_url": "Jellyfin Server-IP & Port",
        "sub_jf_url": "Standard-Port: <strong>8096</strong> (z. B. <code>192.168.1.50:8096</code>)",
        "lbl_jf_key": "Jellyfin API-Key",
        "sub_jf_key": "Erstellt unter Jellyfin: Dashboard ➔ API-Schlüssel",
        "lbl_plex_url": "Plex Server-IP & Port (Optional)",
        "sub_plex_url": "Standard-Port: <strong>32400</strong> (z. B. <code>192.168.1.50:32400</code>)",
        "lbl_plex_token": "Plex Token (Optional)",
        "sub_plex_token": "Dein persönlicher X-Plex-Token",
        "sec_audio": "⚡ 3. Download-Kriterien & Audio-Veredelung",
        "lbl_min_dur": "Min. Titellänge (Sekunden)",
        "sub_min_dur": "Verhindert Jingles und kurze Soundeffekte (Standard: 25s)",
        "lbl_max_dur": "Max. Titellänge (Sekunden)",
        "sub_max_dur": "Verhindert ganze Alben und Loops (Standard: 270s)",
        "lbl_fade": "<strong>Sanftes Ein- und Ausblenden (Fade-In / Fade-Out):</strong> 1.5s sanft anblenden, 3s ausblenden",
        "sec_lang": "🌐 4. Sprache / Language",
        "lbl_lang": "Benutzeroberfläche / Interface Language",
        "btn_save_settings": "💾 Einstellungen speichern & anwenden",
        "btn_cancel_settings": "Abbrechen",
        "lib_title": "Gefundene Mediatheken & Verzeichnisse:",
        "lib_desc": "Hier siehst du alle Mediatheken und Pfade, die Jellyfin und deine Festplatte gemeldet haben:",
        "lib_empty": "Bisher keine Bibliotheken erfasst. Klicke auf 'Alles scannen'!"
    },
    "en": {
        "app_title": "Theme Fetcher",
        "movies": "Movies",
        "shows": "TV Shows",
        "collections": "Collections",
        "libraries": "Libraries",
        "movies_plural": "Movies",
        "shows_plural": "TV Shows",
        "collections_plural": "Collections",
        "all_media": "all media",
        "settings": "Settings",
        "settings_title": "Settings & Sources",
        "back_to_lib": "← Back to Library",
        "scan_only": "🔍 Scan {scope} Only",
        "scan_all": "🌐 Scan All",
        "download_missing": "⬇️ Download {scope} ({count})",
        "cancel": "🛑 Cancel",
        "cancelling": "Cancelling...",
        "cancel_requested": "Cancellation requested",
        "ready": "Ready",
        "scanning": "Scanning {scope}...",
        "fetching_jf": "Fetching Jellyfin ({scope})...",
        "fetching_plex": "Fetching Plex ({scope})...",
        "scanning_local": "Scanning local folders ({scope})...",
        "scan_cancelled": "Scan cancelled!",
        "scan_complete": "Scan complete: {count} {scope} indexed.",
        "scan_error": "Scan error: {err}",
        "init_queue": "Initializing download queue...",
        "no_missing": "No missing themes in this category!",
        "download_cancelled": "Download cancelled!",
        "downloading_item": "Downloading ({idx}/{total}): {title}...",
        "download_complete": "Download complete! {down} downloaded, {fail} failed.",
        "download_error": "Download error: {err}",
        "searching_soundtrack": "Searching soundtrack...",
        "no_soundtrack_found": "No soundtrack found",
        "no_clean_theme": "No clean theme music found (only trailers/scenes or no matches)",
        "downloading_candidate": "Downloading ({idx}/{total}): {title}...",
        "mastering_audio": "Mastering audio (Fade-in/out)...",
        "download_success": "Successfully downloaded!",
        "error_prefix": "Error: ",
        "all_failed": "All candidates failed ({err})",
        "op_in_progress": "Operation in progress...",
        "processing": "Processing...",
        "downloaded_stat": "Downloaded",
        "failed_stat": "Failed",
        "next_in_queue": "📋 Next in Download Queue:",
        "recently_finished": "✓ Recently Finished:",
        "no_more_queue": "No more items in queue.",
        "filter_label": "Filter:",
        "all_pill": "All ({count})",
        "missing_pill": "❌ Missing ({count})",
        "present_pill": "✓ Present ({count})",
        "search_placeholder": "Search {scope}...",
        "search_btn": "Search",
        "th_cover": "Cover",
        "th_title_path": "Title & Path",
        "th_status": "Status",
        "th_preview": "Preview",
        "th_actions": "Actions",
        "th_progress": "Progress",
        "status_present": "✓ Present",
        "status_missing": "✕ Missing",
        "status_ready": "✓ Ready",
        "no_entries": "No entries found in {scope}.",
        "btn_select": "🎵 Select",
        "btn_select_title": "Select theme (preview top recommendations)",
        "btn_dl_placeholder": "YouTube URL / query...",
        "btn_dl_title": "Download directly",
        "btn_recheck_title": "Re-check this item",
        "btn_delete_confirm": "Delete theme and block URL?",
        "btn_delete_title": "Delete & block",
        "modal_title": "🎵 Theme Recommendations: {title}",
        "modal_search_placeholder": "Title / Composer / YouTube URL...",
        "modal_search_btn": "🔍 Search",
        "modal_searching": "Searching soundtrack candidates (MusicBrainz, Wikidata, YouTube)...",
        "modal_identified": "🎯 <strong>Identified Soundtrack:</strong> {display} <span style=\"color:#8b929a;\">(Source: {source})</span>",
        "modal_no_matches": "No matching soundtracks found. Try a different search query above!",
        "modal_open_yt": "Open on YouTube ↗",
        "modal_length": "Length: ",
        "modal_apply": "✓ Apply",
        "modal_seek_mid": "⏩ Mid",
        "modal_seek_title": "Jump to middle (50%)",
        "settings_saved_banner": "✓ Settings saved successfully!",
        "sec_local": "📁 1. Local Media Storage (Hard Drives & Mounts)",
        "btn_test_path": "📂 Test Path",
        "lbl_paths": "Container Media Path(s)",
        "sub_paths": "Default: <code>/media</code>. Matches your Docker volume mount.",
        "lbl_local_scan": "Enable local storage scan (automatically deduplicated with Jellyfin/Plex)",
        "sec_servers": "🎬 2. Media Servers (Jellyfin / Plex)",
        "btn_test_jf": "🔌 Test Jellyfin",
        "btn_test_plex": "🍿 Test Plex",
        "lbl_jf_url": "Jellyfin Server IP & Port",
        "sub_jf_url": "Default Port: <strong>8096</strong> (e.g. <code>192.168.1.50:8096</code>)",
        "lbl_jf_key": "Jellyfin API Key",
        "sub_jf_key": "Generated in Jellyfin: Dashboard ➔ API Keys",
        "lbl_plex_url": "Plex Server IP & Port (Optional)",
        "sub_plex_url": "Default Port: <strong>32400</strong> (e.g. <code>192.168.1.50:32400</code>)",
        "lbl_plex_token": "Plex Token (Optional)",
        "sub_plex_token": "Your personal X-Plex-Token",
        "sec_audio": "⚡ 3. Download Criteria & Audio Mastering",
        "lbl_min_dur": "Min. Track Duration (seconds)",
        "sub_min_dur": "Prevents stingers, logos and sound effects (Default: 25s)",
        "lbl_max_dur": "Max. Track Duration (seconds)",
        "sub_max_dur": "Prevents full OST albums and long loops (Default: 270s)",
        "lbl_fade": "<strong>Smooth Audio Fade-In & Fade-Out:</strong> 1.5s gentle fade-in, 3.0s fade-out",
        "sec_lang": "🌐 4. Interface Language",
        "lbl_lang": "Select UI Language",
        "btn_save_settings": "💾 Save & Apply Settings",
        "btn_cancel_settings": "Cancel",
        "lib_title": "Discovered Libraries & Media Directories:",
        "lib_desc": "Overview of all storage paths and media server libraries indexed by Theme Fetcher:",
        "lib_empty": "No libraries indexed yet. Click 'Scan All' to scan your media!"
    }
}

def get_t(cfg: dict, key: str, **kwargs) -> str:
    lang = cfg.get("ui_language", "de")
    pack = LANG_PACK.get(lang, LANG_PACK["de"])
    val = pack.get(key, LANG_PACK["en"].get(key, key))
    if kwargs:
        try:
            return val.format(**kwargs)
        except Exception:
            return val
    return val


state_lock = threading.Lock()
stop_requested = False

scan_progress = {
    "is_running": False,
    "action_type": "",
    "current_step": "Ready",
    "current_item_id": "",
    "current_item_title": "",
    "item_status": {},  # item_id -> {"status": str, "percent": int, "step": str}
    "processed": 0,
    "total": 0,
    "percent": 0,
    "new_downloaded": 0,
    "failed": 0,
    "queue": [],
    "recent_finished": []
}


def clean_url_input(val: str) -> str:
    val = (val or "").strip().rstrip("/")
    if not val:
        return ""
    if not val.startswith("http://") and not val.startswith("https://"):
        return f"http://{val}"
    return val


def load_config() -> dict:
    cfg = DEFAULT_CONFIG.copy()
    if CONFIG_FILE.exists():
        try:
            stored = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            cfg.update(stored)
        except Exception as e:
            print(f"[Config Warnung] {e}")
    return cfg


def save_config(cfg: dict):
    with state_lock:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"items": {}, "blacklist": {}, "folders": [], "soundtrack_cache": {}}


def save_state(state: dict):
    with state_lock:
        STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def slugify(text: str) -> str:
    clean = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip().lower()
    return re.sub(r"[-\s]+", "_", clean) or "item"


def normalize_tab(tab_name: str) -> str:
    t = (tab_name or "").lower().strip()
    if "seri" in t or "show" in t or "tv" in t:
        return "shows"
    if "col" in t or "box" in t:
        return "collections"
    if "ordn" in t or "fold" in t or "lib" in t:
        return "libraries"
    return "movies"


def find_theme_file_in_dir(d: Path) -> Optional[Path]:
    """Findet tolerant jede Theme-Audiodatei im Ordner oder in Staffel-Unterordnern"""
    if not d.exists() or not d.is_dir():
        return None
    try:
        for f in d.iterdir():
            if f.is_file() and THEME_REGEX.match(f.name):
                return f
        for sub in d.iterdir():
            if sub.is_dir() and re.search(r"season|staffel|\bs\d+", sub.name, re.IGNORECASE):
                for sf in sub.iterdir():
                    if sf.is_file() and THEME_REGEX.match(sf.name):
                        return sf
    except Exception:
        pass
    return None


def find_local_cover_image(d: Path, title: str = "", raw_path_str: str = "") -> Optional[Path]:
    """Tolerante Cover-Erkennung: Prüft Verzeichnis, Dateinamen des Films und typische Plakat-Muster"""
    img_exts = {".jpg", ".jpeg", ".png", ".webp"}

    # Prüfe zuerst direkt neben der Videodatei (z. B. Inception-poster.jpg oder Inception.jpg)
    if raw_path_str:
        try:
            rp = Path(raw_path_str)
            base_dir = rp.parent if rp.is_file() or rp.suffix else rp
            if base_dir.exists() and base_dir.is_dir():
                stem = rp.stem.lower()
                for ext in img_exts:
                    for suffix in ("-poster", ".poster", "-cover", ""):
                        candidate = base_dir / f"{rp.stem}{suffix}{ext}"
                        if candidate.exists() and candidate.is_file() and candidate.stat().st_size > 1000:
                            return candidate
        except Exception:
            pass

    if not d.exists() or not d.is_dir():
        return None

    candidates = []
    try:
        for f in d.iterdir():
            if f.is_file() and f.suffix.lower() in img_exts and f.stat().st_size > 1000:
                candidates.append(f)
    except Exception:
        return None

    if not candidates:
        return None

    title_slug = slugify(title) if title else ""

    # 1. Bevorzuge typische Plakat-Namen (Kodi, Plex, Jellyfin Standard)
    for f in candidates:
        fname = f.stem.lower()
        if fname in ("poster", "folder", "cover", "front", "default", "show", "movie"):
            return f
        if "poster" in fname or "cover" in fname or "folder" in fname or "front" in fname:
            return f
        if title_slug and title_slug in slugify(fname):
            return f

    # 2. Metadaten-Check: Plakate sind Hochformat (Höhe > Breite)
    portraits = []
    if Image is not None:
        for f in candidates:
            try:
                with Image.open(f) as img:
                    w, h = img.size
                    if h > w and (h / max(1, w)) >= 1.15:
                        portraits.append((h / w, f))
            except Exception:
                pass

    if portraits:
        portraits.sort(key=lambda x: abs(x[0] - 1.48))
        return portraits[0][1]

    # 3. Fallback: Erstes Bild, das kein backdrop / fanart im Namen hat
    for f in candidates:
        fname = f.stem.lower()
        if not any(x in fname for x in ("backdrop", "fanart", "thumb", "banner", "logo", "clearart", "art", "landscape")):
            return f

    return candidates[0]


def resolve_media_path(raw_path_str: str) -> Path:
    """Multi-disk path mapping: Resolves media paths across container mounts"""
    raw_str = raw_path_str.replace("\\", "/")
    p = Path(raw_str)
    if p.exists():
        return p

    parts = [part for part in p.parts if part not in ("/", "\\", "")]
    if not parts:
        return Path("/media")

    media_base = Path("/media")
    cand_media = media_base.joinpath(*parts)
    if cand_media.exists() or cand_media.parent.exists():
        return cand_media

    if media_base.exists():
        try:
            for disk in media_base.iterdir():
                if disk.is_dir():
                    disk_cand = disk.joinpath(*parts)
                    if disk_cand.exists() or disk_cand.parent.exists():
                        return disk_cand
                    if len(parts) > 1:
                        disk_cand2 = disk.joinpath(*parts[1:])
                        if disk_cand2.exists() or disk_cand2.parent.exists():
                            return disk_cand2
        except Exception:
            pass

    target_name = parts[-1].lower()
    target_slug = slugify(parts[-1])
    if media_base.exists():
        try:
            for root, dirs, _ in os.walk(media_base):
                for d in dirs:
                    if d.lower() == target_name or slugify(d) == target_slug:
                        return Path(root) / d
                if len(Path(root).relative_to(media_base).parts) >= 4:
                    dirs.clear()
        except Exception:
            pass

    return cand_media


def get_jellyfin_headers(api_key: str) -> dict:
    clean_key = api_key.strip()
    return {
        "X-Emby-Token": clean_key,
        "X-MediaBrowser-Token": clean_key,
        "Authorization": f'MediaBrowser Token="{clean_key}", Client="ThemeFetcher", Device="MediaServer", DeviceId="theme-fetcher-app", Version="2.4.0"',
        "Accept": "application/json"
    }


def check_jellyfin_has_theme(jf_url: str, api_key: str, jf_raw_id: str) -> Tuple[bool, str]:
    """Fragt Jellyfin direkt über die API, ob bereits ein ThemeSong hinterlegt ist"""
    if not jf_url or not api_key or not jf_raw_id:
        return False, ""
    headers = get_jellyfin_headers(api_key)
    try:
        r = requests.get(f"{jf_url}/Items/{jf_raw_id}/ThemeSongs", headers=headers, timeout=4)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and len(data) > 0:
                t_name = data[0].get("Name") or "Jellyfin Theme"
                return True, t_name
            elif isinstance(data, dict):
                items = data.get("Items", [])
                if items:
                    t_name = items[0].get("Name") or "Jellyfin Theme"
                    return True, t_name
                if data.get("TotalRecordCount", 0) > 0:
                    return True, "Jellyfin Theme"
    except Exception:
        pass
    return False, ""



"""Soundtrack-Spuerhund Integration"""
import re
import requests

def discover_soundtrack(title: str, orig: str = "", tmdb_id: str = "") -> dict:
    info = {"title": "", "composer": "", "source": "", "display": "", "direct_url": ""}
    # MusicBrainz
    clean = re.sub(r"[^\w\s]", "", orig or title).strip()
    headers = {"User-Agent": "ThemeFetcher/1.6.0"}
    try:
        url = "https://musicbrainz.org/ws/2/release-group"
        p = {"query": f'releasegroup:"{clean}" AND type:soundtrack', "fmt": "json", "limit": "3"}
        r = requests.get(url, headers=headers, params=p, timeout=4)
        if r.status_code == 200:
            rgs = r.json().get("release-groups", [])
            if rgs:
                rg = rgs[0]
                artists = rg.get("artist-credit", [])
                c_name = artists[0].get("name", "") if artists else ""
                info["composer"] = c_name
                info["title"] = "Soundtrack"
                info["source"] = "MusicBrainz"
                info["display"] = f"{c_name} - Soundtrack" if c_name else "Soundtrack"
                return info
    except Exception:
        pass

    # Wikidata
    try:
        w_url = "https://www.wikidata.org/w/api.php"
        wp = {"action": "wbsearchentities", "search": clean, "language": "de", "format": "json", "limit": "3"}
        wr = requests.get(w_url, params=wp, timeout=3)
        if wr.status_code == 200:
            for ent in wr.json().get("search", []):
                e_id = ent.get("id")
                if e_id:
                    cp = {"action": "wbgetclaims", "entity": e_id, "property": "P86", "format": "json"}
                    cr = requests.get(w_url, params=cp, timeout=3)
                    if cr.status_code == 200:
                        cl = cr.json().get("claims", {}).get("P86", [])
                        if cl:
                            comp_id = cl[0].get("mainsnak", {}).get("datavalue", {}).get("value", {}).get("id")
                            if comp_id:
                                lp = {"action": "wbgetentities", "ids": comp_id, "props": "labels", "languages": "de", "format": "json"}
                                lr = requests.get(w_url, params=lp, timeout=3)
                                if lr.status_code == 200:
                                    lbl = lr.json().get("entities", {}).get(comp_id, {}).get("labels", {}).get("de", {}).get("value")
                                    if lbl:
                                        info["composer"] = lbl
                                        info["title"] = "Titelmusik"
                                        info["source"] = "Wikidata"
                                        info["display"] = f"{lbl} - Titelmusik"
                                        return info
    except Exception:
        pass

    return info


def discover_soundtrack_info(item: dict) -> dict:
    state = load_state()
    cache = state.setdefault("soundtrack_cache", {})
    item_id = item.get("id", "")
    if item_id in cache:
        return cache[item_id]
    info = discover_soundtrack(
        item.get("title", ""),
        item.get("original_title", ""),
        item.get("tmdb_id", "")
    )
    cache[item_id] = info
    save_state(state)
    return info

def check_item_theme(it: dict, existing: Optional[dict] = None, cfg: Optional[dict] = None) -> Tuple[bool, str, Optional[Path]]:
    """Tiefensuche: Prüft Festplatten & Jellyfin-API auf vorhandene theme.mp3"""
    existing = existing or {}
    candidates = []

    if it.get("dir"):
        candidates.append(Path(it["dir"]))
        candidates.append(resolve_media_path(it["dir"]))

    if existing.get("dir") and existing.get("dir") != it.get("dir"):
        candidates.append(Path(existing["dir"]))
        candidates.append(resolve_media_path(existing["dir"]))

    raw_p = it.get("raw_path") or existing.get("raw_path")
    if raw_p:
        candidates.append(Path(raw_p))
        candidates.append(resolve_media_path(raw_p))

    item_id = it.get("id") or existing.get("id")
    if item_id:
        candidates.append(LOCAL_THEMES_DIR / item_id)

    seen = set()
    for d in candidates:
        d_str = str(d)
        if d_str in seen:
            continue
        seen.add(d_str)

        theme_file = find_theme_file_in_dir(d)
        if theme_file:
            if theme_file.name.lower() != "theme.mp3":
                standard_mp3 = d / "theme.mp3"
                if not standard_mp3.exists():
                    try:
                        shutil.copy2(theme_file, standard_mp3)
                        theme_file = standard_mp3
                    except Exception:
                        pass
            it["dir"] = d_str
            return True, d_str, theme_file

    # Recursive search in /media mount for matching folder name
    title_raw = it.get("title", "").strip().lower()
    title_slug = slugify(title_raw)
    media_base = Path("/media")
    if media_base.exists() and len(title_slug) >= 2:
        try:
            for root, dirs, _ in os.walk(media_base):
                for d in dirs:
                    d_lower = d.lower()
                    d_slug = slugify(d)
                    if d_lower == title_raw or d_slug == title_slug or (len(title_slug) > 4 and (title_slug in d_slug or d_slug in title_slug)):
                        cand_dir = Path(root) / d
                        theme_file = find_theme_file_in_dir(cand_dir)
                        if theme_file:
                            it["dir"] = str(cand_dir)
                            return True, str(cand_dir), theme_file
                        it["dir"] = str(cand_dir)
                if len(Path(root).relative_to(media_base).parts) >= 4:
                    dirs.clear()
        except Exception:
            pass

    # Doppelter Boden: Prüfe direkt über Jellyfin API
    if cfg and it.get("source") == "Jellyfin" and it.get("jf_raw_id") and cfg.get("jellyfin_url"):
        has_jf_theme, t_name = check_jellyfin_has_theme(cfg["jellyfin_url"], cfg.get("jellyfin_api_key", ""), it["jf_raw_id"])
        if has_jf_theme:
            default_dir = str(it.get("dir") or (candidates[0] if candidates else "/media"))
            it["video_title"] = t_name
            return True, default_dir, None

    default_dir = str(it.get("dir") or (candidates[0] if candidates else (str(LOCAL_THEMES_DIR / item_id) if item_id else "/media")))
    return False, default_dir, None


def find_jellyfin_boxset_dir(title: str, jf_meta_base: str) -> Optional[Path]:
    base = Path(jf_meta_base)
    if not base.exists():
        return None
    target_slug = slugify(title)
    try:
        for p in base.rglob("*"):
            if p.is_dir() and slugify(p.name) == target_slug:
                if any(x in str(p).lower() for x in ("boxset", "collection", "metadata")):
                    return p
    except Exception:
        pass
    return None


def sync_collection_theme(item: dict, mp3_path: Path, jf_meta_base: str):
    if item.get("type") != "Collection" or not mp3_path or not mp3_path.exists():
        return
    jf_dir = find_jellyfin_boxset_dir(item["title"], jf_meta_base)
    if jf_dir:
        try:
            shutil.copy2(mp3_path, jf_dir / "theme.mp3")
        except Exception as e:
            print(f"[Collection Sync Warnung] {e}")


def scan_local_folders(paths_str: str, filter_type: Optional[str] = None, exclude_slugs: set = None) -> tuple[List[dict], List[str]]:
    results = []
    scanned_dirs = []
    if not paths_str:
        return results, scanned_dirs

    exclude_slugs = exclude_slugs or set()
    paths = [p.strip() for p in paths_str.replace(";", ",").split(",") if p.strip()]
    video_exts = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".iso"}

    for base_str in paths:
        base = Path(base_str)
        if not base.exists() or not base.is_dir():
            scanned_dirs.append(f"❌ {base_str} (Path does not exist in container)")
            continue

        scanned_dirs.append(f"📁 Local Storage Mount: {base_str}")
        try:
            for root, dirs, files in os.walk(base):
                global stop_requested
                if stop_requested:
                    break

                rpath = Path(root)
                if rpath.name.startswith(".") or rpath.name.lower() in ("@eadir", "$recycle.bin", "system volume information"):
                    continue

                try:
                    rel_parts = rpath.relative_to(base).parts
                except ValueError:
                    rel_parts = ()
                if len(rel_parts) > 3:
                    continue

                has_video = any(Path(f).suffix.lower() in video_exts for f in files)
                has_theme = bool(find_theme_file_in_dir(rpath))
                has_seasons = any(re.search(r"season|staffel|specials", d, re.IGNORECASE) for d in dirs)

                if has_video or has_theme or has_seasons:
                    if re.search(r"^(season|staffel|specials|\bs\d+)", rpath.name, re.IGNORECASE):
                        continue

                    name = rpath.name
                    year = None
                    m = re.search(r"^(.*?)\s*[\(\[](\d{4})[\)\]]", name)
                    if m:
                        title = m.group(1).strip()
                        year = m.group(2)
                    else:
                        title = re.sub(r"[\._]", " ", name).strip()

                    itype = "Serie" if has_seasons or any("s01" in f.lower() or "staffel" in f.lower() for f in files) else "Film"
                    if filter_type and itype != filter_type:
                        continue

                    item_slug = f"{slugify(title)}_{year or ''}"
                    alt_slug = slugify(title)
                    if item_slug in exclude_slugs or alt_slug in exclude_slugs or str(rpath) in exclude_slugs:
                        continue

                    item_id = f"local_{abs(hash(str(rpath)))}"

                    results.append({
                        "id": item_id,
                        "source": "Lokal",
                        "type": itype,
                        "title": title,
                        "original_title": "",
                        "year": year,
                        "dir": str(rpath)
                    })
        except Exception as e:
            print(f"[Lokaler Scan Fehler bei {base_str}] {e}")

    return results, scanned_dirs


def test_jellyfin_connection(url: str, api_key: str) -> dict:
    url = clean_url_input(url)
    api_key = api_key.strip()
    if not url:
        return {"ok": False, "msg": "Please enter Jellyfin IP and port (e.g. 192.168.1.50:8096)"}
    if not api_key:
        return {"ok": False, "msg": "Please enter Jellyfin API Key"}

    headers = get_jellyfin_headers(api_key)
    try:
        sys_resp = requests.get(f"{url}/System/Info", headers=headers, timeout=6)
        server_name = "Jellyfin"
        version = ""
        if sys_resp.status_code == 200:
            s_data = sys_resp.json()
            server_name = s_data.get("ServerName", "Jellyfin")
            version = s_data.get("Version", "")

        users_resp = requests.get(f"{url}/Users", headers=headers, params={"api_key": api_key}, timeout=6)
        if users_resp.status_code in (401, 403):
            return {"ok": False, "msg": f"URL reachable, but API key is invalid (HTTP {users_resp.status_code})"}
        users_resp.raise_for_status()
        users = users_resp.json()

        it_resp = requests.get(
            f"{url}/Items",
            headers=headers,
            params={"IncludeItemTypes": "Movie,Series,BoxSet", "Recursive": "true", "Limit": "1", "api_key": api_key},
            timeout=6
        )
        item_count = it_resp.json().get("TotalRecordCount", 0) if it_resp.status_code == 200 else 0

        info_txt = f"Connected to '{server_name}' (v{version}) • {len(users)} users • {item_count} media items found!"
        return {"ok": True, "msg": info_txt}
    except requests.exceptions.ConnectTimeout:
        return {"ok": False, "msg": "Connection timeout. Is Jellyfin running on this IP and port?"}
    except requests.exceptions.ConnectionError:
        return {"ok": False, "msg": "Connection refused. Please verify IP and port (e.g. 192.168.1.50:8096)!"}
    except Exception as e:
        return {"ok": False, "msg": f"Fehler: {str(e)}"}


def test_plex_connection(url: str, token: str) -> dict:
    url = clean_url_input(url)
    token = token.strip()
    if not url:
        return {"ok": False, "msg": "Please enter Plex IP and port (e.g. 192.168.1.50:32400)"}
    if not token:
        return {"ok": False, "msg": "Please enter Plex Token"}

    headers = {"X-Plex-Token": token, "Accept": "application/json"}
    try:
        resp = requests.get(f"{url}/", headers=headers, timeout=6)
        if resp.status_code in (401, 403):
            return {"ok": False, "msg": "URL reachable, but Plex token is invalid!"}
        resp.raise_for_status()
        data = resp.json().get("MediaContainer", {})
        friendly_name = data.get("friendlyName", "Plex Server")
        version = data.get("version", "")
        return {"ok": True, "msg": f"Connected to '{friendly_name}' (v{version})!"}
    except Exception as e:
        return {"ok": False, "msg": f"Plex Fehler: {str(e)}"}


def test_folder_path(paths_str: str) -> dict:
    paths_str = paths_str.strip()
    if not paths_str:
        return {"ok": False, "msg": "No path specified"}

    paths = [p.strip() for p in paths_str.replace(";", ",").split(",") if p.strip()]
    details = []

    for p in paths:
        path_obj = Path(p)
        if not path_obj.exists():
            details.append(f"❌ '{p}' does not exist in container")
        elif not path_obj.is_dir():
            details.append(f"❌ '{p}' is not a directory")
        else:
            try:
                subdirs = [d.name for d in path_obj.iterdir() if d.is_dir() and not d.name.startswith(".")]
                files = [f.name for f in path_obj.iterdir() if f.is_file()]
                details.append(f"✓ '{p}': {len(subdirs)} subdirectories, {len(files)} files")
            except Exception as e:
                details.append(f"⚠️ '{p}' Lesefehler: {e}")

    ok = any("✓" in d for d in details)
    return {"ok": ok, "msg": " | ".join(details)}


def fetch_jellyfin_items(url: str, api_key: str, filter_type: Optional[str] = None) -> tuple[List[dict], List[str]]:
    if not url or not api_key:
        return [], []
    headers = get_jellyfin_headers(api_key)
    results = []
    scanned_folders = []
    seen_ids = set()

    type_mapping = {"Film": "Movie", "Serie": "Series", "Collection": "BoxSet"}
    item_types = type_mapping.get(filter_type, "Movie,Series,BoxSet")

    try:
        vf_resp = requests.get(f"{url}/Library/VirtualFolders", headers=headers, params={"api_key": api_key.strip()}, timeout=10)
        if vf_resp.status_code == 200:
            for vf in vf_resp.json():
                name = vf.get("Name", "Unbenannt")
                locations = vf.get("Locations", [])
                loc_txt = ", ".join(locations) if locations else "Standardpfad"
                scanned_folders.append(f"🎬 Jellyfin Library: '{name}' [{loc_txt}]")
    except Exception as e:
        print(f"[Jellyfin VirtualFolders Warnung] {e}")

    try:
        params = {
            "IncludeItemTypes": item_types,
            "Recursive": "true",
            "Fields": "Path,ProductionYear,OriginalTitle,ImageTags,PrimaryImageAspectRatio,ProviderIds",
            "EnableTotalRecordCount": "true",
            "api_key": api_key.strip()
        }
        resp = requests.get(f"{url}/Items", headers=headers, params=params, timeout=35)
        resp.raise_for_status()

        for item in resp.json().get("Items", []):
            item_type = item.get("Type")
            title = item.get("Name", "Unbekannt")
            clean_id = item["Id"]
            item_id = f"jf_{clean_id}"

            if item_id in seen_ids:
                continue
            seen_ids.add(item_id)

            has_image = bool(item.get("ImageTags", {}).get("Primary"))
            p_ids = item.get("ProviderIds") or {}
            tmdb_id = p_ids.get("Tmdb") or p_ids.get("TmdbCollection")
            imdb_id = p_ids.get("Imdb")
            tvdb_id = p_ids.get("Tvdb")

            if item_type == "BoxSet":
                if filter_type and filter_type != "Collection":
                    continue
                col_dir = COLLECTIONS_DIR / f"{item_id}_{slugify(title)}"
                col_dir.mkdir(parents=True, exist_ok=True)
                results.append({
                    "id": item_id,
                    "jf_raw_id": clean_id,
                    "source": "Jellyfin",
                    "type": "Collection",
                    "title": title,
                    "original_title": item.get("OriginalTitle") or "",
                    "year": item.get("ProductionYear"),
                    "dir": str(col_dir),
                    "has_poster": has_image,
                    "tmdb_id": tmdb_id,
                    "imdb_id": imdb_id,
                    "tvdb_id": tvdb_id
                })
                continue

            raw_path = item.get("Path")
            if not raw_path:
                continue

            resolved = resolve_media_path(raw_path)
            target_dir = resolved.parent if item_type == "Movie" else resolved
            itype = "Serie" if item_type == "Series" else "Film"

            if filter_type and itype != filter_type:
                continue

            results.append({
                "id": item_id,
                "jf_raw_id": clean_id,
                "source": "Jellyfin",
                "type": itype,
                "title": title,
                "original_title": item.get("OriginalTitle") or "",
                "year": item.get("ProductionYear"),
                "dir": str(target_dir),
                "raw_path": raw_path,
                "has_poster": has_image,
                "tmdb_id": tmdb_id,
                "imdb_id": imdb_id,
                "tvdb_id": tvdb_id
            })
    except Exception as e:
        print(f"[Jellyfin Fetch Fehler] {e}")
        scanned_folders.append(f"❌ Jellyfin Fetch Error: {e}")

    return results, scanned_folders


def fetch_plex_items(url: str, token: str, filter_type: Optional[str] = None) -> tuple[List[dict], List[str]]:
    if not url or not token:
        return [], []
    headers = {"X-Plex-Token": token, "Accept": "application/json"}
    results = []
    scanned_folders = []
    try:
        sec_resp = requests.get(f"{url}/library/sections", headers=headers, timeout=10)
        sec_resp.raise_for_status()
        data = sec_resp.json().get("MediaContainer", {})

        for directory in data.get("Directory", []):
            sec_id = directory.get("key")
            sec_type = directory.get("type")
            sec_title = directory.get("title", "Library")
            if sec_type not in ("movie", "show"):
                continue

            scanned_folders.append(f"🍿 Plex Library: '{sec_title}' ({sec_type})")
            items_resp = requests.get(f"{url}/library/sections/{sec_id}/all", headers=headers, timeout=25)
            items_data = items_resp.json().get("MediaContainer", {})

            for video in items_data.get("Metadata", []):
                rkey = video.get("ratingKey")
                vtype = "Film" if sec_type == "movie" else "Serie"
                if filter_type and vtype != filter_type:
                    continue

                vtitle = video.get("title", "Unbekannt")
                thumb = video.get("thumb")

                media_parts = video.get("Media", [{}])[0].get("Part", [{}])
                file_path = media_parts[0].get("file") if media_parts else None
                if not file_path:
                    continue

                resolved = resolve_media_path(file_path)
                p = resolved.parent if vtype == "Film" else resolved

                results.append({
                    "id": f"plex_{rkey}",
                    "plex_thumb": thumb,
                    "source": "Plex",
                    "type": vtype,
                    "title": vtitle,
                    "original_title": video.get("originalTitle", ""),
                    "year": video.get("year"),
                    "dir": str(p),
                    "raw_path": file_path,
                    "has_poster": bool(thumb)
                })

            if not filter_type or filter_type == "Collection":
                try:
                    col_resp = requests.get(f"{url}/library/sections/{sec_id}/collections", headers=headers, timeout=15)
                    for col in col_resp.json().get("MediaContainer", {}).get("Metadata", []):
                        rkey = col.get("ratingKey")
                        ctitle = col.get("title", "Collection")
                        cid = f"plex_col_{rkey}"
                        cdir = COLLECTIONS_DIR / f"{cid}_{slugify(ctitle)}"
                        cdir.mkdir(parents=True, exist_ok=True)
                        results.append({
                            "id": cid,
                            "plex_thumb": col.get("thumb"),
                            "source": "Plex",
                            "type": "Collection",
                            "title": ctitle,
                            "original_title": "",
                            "year": col.get("maxYear") or col.get("minYear"),
                            "dir": str(cdir),
                            "has_poster": bool(col.get("thumb"))
                        })
                except Exception:
                    pass

    except Exception as e:
        print(f"[Plex Fehler] {e}")
    return results, scanned_folders


def build_search_queries(item: dict) -> List[str]:
    title = item["title"].strip()
    orig = (item.get("original_title") or "").strip()
    year = item.get("year")
    itype = item["type"]
    queries = []

    st_info = discover_soundtrack_info(item)
    composer = st_info.get("composer", "")
    exact_song = st_info.get("title", "")
    direct_url = st_info.get("direct_url", "")

    if direct_url:
        queries.append(direct_url)

    if composer and exact_song:
        queries.append(f"ytsearch8:{composer} {exact_song} {title} Official Soundtrack")
        queries.append(f"ytsearch6:{composer} {exact_song} Official Audio")
    elif composer:
        queries.append(f"ytsearch8:{composer} {title} Soundtrack Main Theme")
        queries.append(f"ytsearch6:{composer} {orig or title} Score")
    elif exact_song:
        queries.append(f"ytsearch8:{title} {exact_song} Soundtrack Theme")

    if itype == "Serie":
        queries.append(f"ytsearch8:{title} Opening Theme Song Soundtrack")
        queries.append(f"ytsearch6:{title} Titelmelodie Instrumental")
        if orig and orig.lower() != title.lower():
            queries.append(f"ytsearch8:{orig} TV Theme Song Audio")
            queries.append(f"ytsearch6:{orig} Opening Credits Soundtrack")
    elif itype == "Collection":
        queries.append(f"ytsearch8:{title} Soundtrack Main Theme")
        clean_col = re.sub(r"\b(collection|reihe|sammlung|filmreihe)\b", "", title, flags=re.IGNORECASE).strip()
        if clean_col and clean_col.lower() != title.lower():
            queries.append(f"ytsearch8:{clean_col} Main Theme Soundtrack")
        if orig and orig.lower() != title.lower():
            queries.append(f"ytsearch8:{orig} Main Theme Score")
    else:
        if orig and orig.lower() != title.lower():
            queries.append(f"ytsearch8:{orig} {year or ''} Soundtrack Main Title".strip())
            queries.append(f"ytsearch6:{orig} Official Audio Theme")
        queries.append(f"ytsearch8:{title} {year or ''} Soundtrack Main Title".strip())
        queries.append(f"ytsearch6:{title} Titelmusik Instrumental")

    base_name = orig if orig else title
    queries.append(f"scsearch6:{base_name} Main Theme Soundtrack")
    return queries


def find_all_candidates(search_expr: str, blacklist: List[str], min_dur: int, max_dur: int) -> List[dict]:
    cmd = [
        "yt-dlp",
        search_expr,
        "--dump-json",
        "--flat-playlist",
        "--no-warnings"
    ]
    candidates = []
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        lines = res.stdout.strip().splitlines()
        for line in lines:
            if not line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            vid_id = entry.get("id", "")
            vid_title = entry.get("title", "")
            duration = entry.get("duration") or 0
            uploader = entry.get("uploader") or entry.get("channel") or ""
            url = entry.get("url") or entry.get("webpage_url") or (
                f"https://www.youtube.com/watch?v={vid_id}" if vid_id else ""
            )
            lower_title = vid_title.lower()

            if not url or vid_id in blacklist:
                continue
            if duration and (duration < min_dur or duration > max_dur):
                continue
            if any(bad in lower_title for bad in EXCLUDE_KEYWORDS):
                continue

            candidates.append({
                "id": vid_id or url,
                "url": url,
                "title": vid_title,
                "duration": duration,
                "uploader": uploader
            })
    except Exception:
        pass
    return candidates


def post_process_audio(input_file: Path, output_file: Path, duration: int, fade: bool = True):
    temp_processed = input_file.parent / f"processed_{input_file.name}"
    
    af_filter = []
    if fade:
        af_filter.append("afade=t=in:ss=0:d=1.5")
        if duration and duration > 6:
            out_start = max(1.0, duration - 3.0)
            af_filter.append(f"afade=t=out:st={out_start}:d=3.0")

    af_arg = ",".join(af_filter) if af_filter else "anull"
    cmd = ["ffmpeg", "-y", "-i", str(input_file), "-c:a", "libmp3lame", "-b:a", "192k", "-af", af_arg, str(temp_processed)]

    try:
        subprocess.run(cmd, capture_output=True, timeout=30)
        if temp_processed.exists() and temp_processed.stat().st_size > 1024:
            if output_file.exists():
                output_file.unlink()
            temp_processed.rename(output_file)
            input_file.unlink(missing_ok=True)
            return True
    except Exception as e:
        print(f"[FFmpeg Filter Warnung] {e}")

    if input_file.exists():
        if output_file.exists():
            output_file.unlink()
        input_file.rename(output_file)
    return True


def search_and_download(item: dict, custom_query_or_url: Optional[str] = None, custom_title: Optional[str] = None, blacklist: List[str] = None, min_dur: int = 25, max_dur: int = 270, jf_meta_base: str = "/jellyfin-metadata", fade: bool = True) -> dict:
    global scan_progress
    item_id = item["id"]
    blacklist = blacklist or []
    target_dir = Path(item["dir"])

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        test_write = target_dir / ".theme_test_write"
        test_write.write_text("ok")
        test_write.unlink()
    except Exception:
        safe_fallback = LOCAL_THEMES_DIR / item_id
        safe_fallback.mkdir(parents=True, exist_ok=True)
        target_dir = safe_fallback
        item["dir"] = str(safe_fallback)

    output_mp3 = target_dir / "theme.mp3"
    temp_template = str(target_dir / "theme_temp.%(ext)s")

    for old_tmp in target_dir.glob("theme_temp.*"):
        old_tmp.unlink(missing_ok=True)

    is_direct_url = custom_query_or_url and (
        custom_query_or_url.startswith("http://") or custom_query_or_url.startswith("https://")
    )

    candidates = []
    if is_direct_url:
        cand_title = (custom_title or "").strip()
        if not cand_title:
            try:
                t_res = subprocess.run(["yt-dlp", "--print", "title", custom_query_or_url], capture_output=True, text=True, timeout=12)
                if t_res.returncode == 0 and t_res.stdout.strip():
                    cand_title = t_res.stdout.strip()
            except Exception:
                pass
        if not cand_title:
            cand_title = "Theme Music"
        candidates = [{"url": custom_query_or_url, "title": cand_title, "id": custom_query_or_url, "duration": 180}]
    else:
        scan_progress["item_status"][item_id] = {"status": "searching", "percent": 20, "step": "Searching soundtrack..."}
        search_list = (
            [f"ytsearch8:{custom_query_or_url}", f"scsearch6:{custom_query_or_url}"]
            if custom_query_or_url
            else build_search_queries(item)
        )
        for expr in search_list:
            cands = find_all_candidates(expr, blacklist, min_dur, max_dur)
            if cands:
                candidates.extend(cands)
                if len(candidates) >= 5:
                    break

    if not candidates:
        scan_progress["item_status"][item_id] = {"status": "failed", "percent": 100, "step": "No soundtrack found"}
        return {"ok": False, "msg": "No clean theme music found (only trailers/scenes or no matches)"}

    last_err = ""
    for idx, chosen_video in enumerate(candidates, 1):
        scan_progress["item_status"][item_id] = {
            "status": "downloading",
            "percent": int(30 + (idx / len(candidates)) * 40),
            "step": f"Downloading ({idx}/{len(candidates)}): {chosen_video['title'][:35]}..."
        }

        dl_cmd = [
            "yt-dlp",
            "-x",
            "--audio-format", "mp3",
            "--audio-quality", "192K",
            "--no-playlist",
            "-o", temp_template,
            chosen_video["url"]
        ]
        try:
            res = subprocess.run(dl_cmd, capture_output=True, text=True, timeout=90)
            if res.returncode != 0:
                err_line = res.stderr.strip().splitlines()[-1] if res.stderr else "Error"
                last_err = err_line[:80]
                blacklist.append(chosen_video["id"])
                continue

            downloaded = target_dir / "theme_temp.mp3"
            if downloaded.exists() and downloaded.stat().st_size > 1024:
                scan_progress["item_status"][item_id] = {"status": "processing", "percent": 85, "step": "Mastering audio (Fade-in/out)..."}

                post_process_audio(downloaded, output_mp3, chosen_video.get("duration", 180), fade=fade)
                sync_collection_theme(item, output_mp3, jf_meta_base)

                scan_progress["item_status"][item_id] = {"status": "done", "percent": 100, "step": "Successfully downloaded!"}
                return {
                    "ok": True,
                    "video_id": chosen_video["id"],
                    "video_title": chosen_video["title"]
                }
        except Exception as e:
            last_err = str(e)
            continue

    scan_progress["item_status"][item_id] = {"status": "failed", "percent": 100, "step": f"Error: {last_err[:40]}"}
    return {"ok": False, "msg": f"All candidates failed ({last_err[:60]})"}


def run_scan(scope: str = "all"):
    """Führt entweder einen strikten Einzelkategorie-Scan oder einen globalen Komplett-Scan aus"""
    global scan_progress, stop_requested
    if scan_progress["is_running"]:
        return

    stop_requested = False
    scan_progress["is_running"] = True
    scan_progress["action_type"] = "scan"
    cfg = load_config()
    scope_key = "all_media" if scope == "all" else ("movies_plural" if scope in ("Movie", "Film") else ("shows_plural" if scope in ("Serie", "Series") else "collections_plural"))
    scope_name = get_t(cfg, scope_key)
    scan_progress["current_step"] = get_t(cfg, "scanning", scope=scope_name)
    scan_progress["processed"] = 0
    scan_progress["total"] = 0
    scan_progress["percent"] = 0
    scan_progress["new_downloaded"] = 0
    scan_progress["failed"] = 0

    try:
        cfg = load_config()
        state = load_state()

        filter_type = None if scope == "all" else scope

        raw_items = []
        all_scanned_folders = []
        jf_slugs = set()

        if cfg.get("jellyfin_url") and cfg.get("jellyfin_api_key"):
            scan_progress["current_step"] = get_t(cfg, "fetching_jf", scope=scope_name)
            jf_items, jf_folders = fetch_jellyfin_items(cfg.get("jellyfin_url", ""), cfg.get("jellyfin_api_key", ""), filter_type=filter_type)
            raw_items.extend(jf_items)
            all_scanned_folders.extend(jf_folders)
            for it in jf_items:
                jf_slugs.add(f"{slugify(it['title'])}_{it.get('year') or ''}")
                jf_slugs.add(slugify(it["title"]))
                if it.get("dir"):
                    jf_slugs.add(it["dir"])

        if cfg.get("plex_url") and cfg.get("plex_token"):
            scan_progress["current_step"] = get_t(cfg, "fetching_plex", scope=scope_name)
            plex_items, plex_folders = fetch_plex_items(cfg.get("plex_url", ""), cfg.get("plex_token", ""), filter_type=filter_type)
            raw_items.extend(plex_items)
            all_scanned_folders.extend(plex_folders)
            for it in plex_items:
                jf_slugs.add(f"{slugify(it['title'])}_{it.get('year') or ''}")
                jf_slugs.add(slugify(it["title"]))
                if it.get("dir"):
                    jf_slugs.add(it["dir"])

        if cfg.get("local_scan_enabled", True):
            scan_progress["current_step"] = get_t(cfg, "scanning_local", scope=scope_name)
            loc_items, loc_folders = scan_local_folders(
                cfg.get("local_media_paths", "/media"),
                filter_type=filter_type,
                exclude_slugs=jf_slugs
            )
            raw_items.extend(loc_items)
            all_scanned_folders.extend(loc_folders)

        # 1. Sammle neu eingelesene Items
        current_state_items = {}
        for it in raw_items:
            current_state_items[it["id"]] = it

        # 2. DEDUPLIZIERUNG: 1 Film / Serie = 1 Zeile
        unified_items = {}
        source_prio = {"Jellyfin": 3, "Plex": 2, "Lokal": 1}

        # Behalte Items anderer Kategorien unangetastet!
        existing_other = {}
        existing_same = {}
        for k, v in state.get("items", {}).items():
            if scope != "all" and v.get("type") != scope:
                existing_other[k] = v
            else:
                existing_same[k] = v

        combined = list(existing_same.values()) + list(current_state_items.values())
        for it in combined:
            it_type = it.get("type", "Film")
            it_title_slug = slugify(it.get("title", ""))
            it_year = it.get("year") or ""
            unique_key = f"{it_type}_{it_title_slug}_{it_year}"

            if unique_key not in unified_items:
                unified_items[unique_key] = it
            else:
                existing = unified_items[unique_key]
                prio_new = source_prio.get(it.get("source"), 0)
                prio_old = source_prio.get(existing.get("source"), 0)
                if prio_new > prio_old:
                    unified_items[unique_key] = {**existing, **it}
                else:
                    unified_items[unique_key] = {**it, **existing}

        # Strikte Filterung: Verarbeite NUR Items der gewählten Rubrik!
        items_to_process = list(unified_items.values())
        if scope != "all":
            items_to_process = [it for it in items_to_process if it.get("type") == scope]

        total_items = len(items_to_process)
        scan_progress["total"] = total_items
        jf_meta = cfg.get("jf_metadata_dir", "/jellyfin-metadata")

        for idx, it in enumerate(items_to_process, 1):
            if stop_requested:
                scan_progress["current_step"] = get_t(cfg, "scan_cancelled")
                break

            item_id = it["id"]
            scan_progress["processed"] = idx
            scan_progress["percent"] = int((idx / max(1, total_items)) * 100)
            scan_progress["current_item_title"] = f"{it['title']}"
            scan_progress["current_item_id"] = item_id

            existing = existing_same.get(item_id, {})
            theme_exists, actual_dir, mp3_file = check_item_theme(it, existing, cfg)

            it["dir"] = actual_dir

            if theme_exists and it.get("type") == "Collection" and mp3_file:
                sync_collection_theme(it, mp3_file, jf_meta)

            if theme_exists:
                status_txt = it.get("video_title") or existing.get("video_title") or (mp3_file.name if mp3_file else "Present")
                scan_progress["item_status"][item_id] = {"status": "ok", "percent": 100, "step": "Present"}
            else:
                prev_status = existing.get("status", "")
                status_txt = prev_status if prev_status and prev_status not in ("Vorhanden", "Present") else "Missing"
                scan_progress["item_status"][item_id] = {"status": "missing", "percent": 0, "step": "Missing"}

            record = {
                **existing,
                **it,
                "has_theme": theme_exists,
                "status": status_txt
            }
            existing_same[item_id] = record

        # Speichere die bearbeitete Kategorie zusammen mit den unangetasteten anderen Kategorien ab
        state["items"] = {**existing_other, **existing_same}

        if scope == "all":
            state["folders"] = all_scanned_folders

        save_state(state)
        if not stop_requested:
            scan_progress["current_step"] = get_t(cfg, "scan_complete", count=total_items, scope=scope_name)
    except Exception as e:
        scan_progress["current_step"] = get_t(cfg, "scan_error", err=e)
    finally:
        scan_progress["is_running"] = False


def run_download_queue(scope: str = "all"):
    """Lädt fehlende Themes in die Warteschlange – unterstützt Fortschrittsbalken pro Zeile & global"""
    global scan_progress, stop_requested
    if scan_progress["is_running"]:
        return

    stop_requested = False
    scan_progress["is_running"] = True
    scan_progress["action_type"] = "download"
    scan_progress["current_step"] = "Initializing download queue..."
    scan_progress["processed"] = 0
    scan_progress["percent"] = 0
    scan_progress["new_downloaded"] = 0
    scan_progress["failed"] = 0
    scan_progress["queue"] = []
    scan_progress["recent_finished"] = []

    try:
        cfg = load_config()
        state = load_state()
        min_dur = cfg.get("min_duration_sec", 25)
        max_dur = cfg.get("max_duration_sec", 270)
        fade_active = cfg.get("audio_fade", True)
        jf_meta = cfg.get("jf_metadata_dir", "/jellyfin-metadata")

        all_missing = [it for it in state["items"].values() if not it.get("has_theme")]
        if scope != "all":
            missing_items = [it for it in all_missing if it.get("type") == scope]
        else:
            missing_items = all_missing

        total_missing = len(missing_items)
        scan_progress["total"] = total_missing

        if total_missing == 0:
            scan_progress["current_step"] = get_t(cfg, "no_missing")
            return

        scan_progress["queue"] = [it["title"] for it in missing_items]

        for idx, it in enumerate(missing_items, 1):
            if stop_requested:
                scan_progress["current_step"] = get_t(cfg, "download_cancelled")
                break

            item_id = it["id"]
            title = it["title"]
            scan_progress["processed"] = idx
            scan_progress["percent"] = int((idx / total_missing) * 100)
            scan_progress["current_item_title"] = f"{title} ({it.get('type')})"
            scan_progress["current_item_id"] = item_id
            scan_progress["current_step"] = f"Lade ({idx}/{total_missing}): {title}..."

            if scan_progress["queue"]:
                scan_progress["queue"].pop(0)

            bl = state["blacklist"].get(item_id, [])
            dl_res = search_and_download(
                it,
                blacklist=bl,
                min_dur=min_dur,
                max_dur=max_dur,
                jf_meta_base=jf_meta,
                fade=fade_active
            )

            if dl_res["ok"]:
                it["has_theme"] = True
                it["video_id"] = dl_res["video_id"]
                it["video_title"] = dl_res["video_title"]
                it["status"] = dl_res["video_title"] or "Downloaded"
                scan_progress["new_downloaded"] += 1
                scan_progress["recent_finished"].insert(0, {"title": title, "ok": True, "msg": dl_res["video_title"] or "Downloaded"})
            else:
                it["status"] = dl_res["msg"]
                scan_progress["failed"] += 1
                scan_progress["recent_finished"].insert(0, {"title": title, "ok": False, "msg": dl_res["msg"]})

            scan_progress["recent_finished"] = scan_progress["recent_finished"][:8]

            state["items"][item_id] = it
            save_state(state)

        if not stop_requested:
            scan_progress["current_step"] = get_t(cfg, "download_complete", down=scan_progress["new_downloaded"], fail=scan_progress["failed"])
    except Exception as e:
        scan_progress["current_step"] = get_t(cfg, "download_error", err=e)
    finally:
        scan_progress["is_running"] = False
        scan_progress["current_item_id"] = ""


@app.get("/poster/{item_id}")
def get_poster(item_id: str):
    """Liefert das Cover mit lokalem Cache: Erst Jellyfin/Plex-API ohne falsche JSON-Headers, dann Festplatte"""
    state = load_state()
    cfg = load_config()
    item = state["items"].get(item_id)
    if not item:
        raise HTTPException(status_code=404)

    # 0. Schneller lokaler Cache: Verhindert 100 parallele API-Anfragen an Jellyfin!
    cached_cover = POSTERS_DIR / f"{item_id}.jpg"
    if cached_cover.exists() and cached_cover.stat().st_size > 500:
        return FileResponse(cached_cover, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=604800"})

    # 1. Prio: Jellyfin Image API (WICHTIG: OHNE Accept: application/json, sonst 406 oder kein Bild!)
    if item.get("source") == "Jellyfin" and item.get("jf_raw_id") and cfg.get("jellyfin_url"):
        jf_url = clean_url_input(cfg["jellyfin_url"])
        raw_id = item["jf_raw_id"]
        api_key = cfg.get("jellyfin_api_key", "").strip()
        
        # Spezielle Header für Binärbilder
        img_headers = {
            "X-Emby-Token": api_key,
            "Authorization": f'MediaBrowser Token="{api_key}", Client="ThemeFetcher", Device="MediaServer", DeviceId="theme-fetcher-app", Version="2.4.0"'
        }

        for img_type in ("Primary", "Thumb", "Backdrop"):
            img_url = f"{jf_url}/Items/{raw_id}/Images/{img_type}"
            try:
                r = requests.get(
                    img_url,
                    headers=img_headers,
                    params={"fillWidth": "300", "quality": "90", "api_key": api_key},
                    timeout=8
                )
                if r.status_code == 200 and len(r.content) > 500:
                    ct = r.headers.get("Content-Type", "image/jpeg")
                    try:
                        cached_cover.write_bytes(r.content)
                    except Exception:
                        pass
                    return Response(content=r.content, media_type=ct, headers={"Cache-Control": "public, max-age=604800"})
            except Exception:
                pass

    # 2. Prio: Plex Thumbnail
    if item.get("source") == "Plex" and item.get("plex_thumb") and cfg.get("plex_url"):
        plex_url = clean_url_input(cfg["plex_url"])
        token = cfg.get("plex_token", "").strip()
        try:
            r = requests.get(f"{plex_url}{item['plex_thumb']}", headers={"X-Plex-Token": token}, timeout=8)
            if r.status_code == 200 and len(r.content) > 500:
                ct = r.headers.get("Content-Type", "image/jpeg")
                try:
                    cached_cover.write_bytes(r.content)
                except Exception:
                    pass
                return Response(content=r.content, media_type=ct, headers={"Cache-Control": "public, max-age=604800"})
        except Exception:
            pass

    # 3. Prio: Lokale Festplatten-Erkennung (Hochformat & flexible Dateinamen)
    p = Path(item.get("dir", ""))
    cover_file = find_local_cover_image(p, title=item.get("title", ""), raw_path_str=item.get("raw_path", ""))
    if cover_file and cover_file.exists():
        try:
            shutil.copyfile(cover_file, cached_cover)
        except Exception:
            pass
        return FileResponse(cover_file, headers={"Cache-Control": "public, max-age=604800"})

    # 4. Fallback: Saubere SVG-Initialen
    initial = (item.get("title") or "?")[0].upper()
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="60" height="90" viewBox="0 0 60 90">
        <rect width="100%" height="100%" fill="#262a32" rx="4"/>
        <text x="50%" y="54%" font-family="sans-serif" font-size="24" fill="#7d8590" text-anchor="middle" dominant-baseline="middle">{initial}</text>
    </svg>"""
    return Response(content=svg, media_type="image/svg+xml")


@app.get("/audio/{item_id}")
def stream_audio(item_id: str):
    state = load_state()
    item = state["items"].get(item_id)
    if not item:
        raise HTTPException(status_code=404)

    has_theme, real_dir, mp3_file = check_item_theme(item)
    if not has_theme or not mp3_file or not mp3_file.exists():
        raise HTTPException(status_code=404)

    return FileResponse(mp3_file, media_type="audio/mpeg", headers={"Accept-Ranges": "bytes"})


@app.get("/api/candidate-stream")
def candidate_stream(v: str):
    """Extrahiert direkte Audio-Stream-URL via yt-dlp für schnelles Vorhören ohne Download"""
    target = f"https://www.youtube.com/watch?v={v}" if not v.startswith("http") else v
    try:
        cmd = ["yt-dlp", "-g", "-f", "ba[ext=m4a]/ba/b", target]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=12)
        if res.returncode == 0 and res.stdout.strip():
            direct_url = res.stdout.strip().splitlines()[0]
            return RedirectResponse(direct_url)
    except Exception:
        pass
    raise HTTPException(status_code=404, detail="Audio-Stream konnte nicht geladen werden")


@app.get("/api/theme-candidates/{item_id}")
def api_theme_candidates(item_id: str, q: Optional[str] = None):
    """Liefert die Top 5-8 Theme-Vorschläge inklusive Spürhund-Metadaten für das Auswahl-Modal"""
    state = load_state()
    cfg = load_config()
    item = state["items"].get(item_id)
    if not item:
        return {"ok": False, "msg": "Eintrag nicht gefunden", "candidates": []}

    blacklist = state.get("blacklist", {}).get(item_id, [])
    min_dur = cfg.get("min_duration_sec", 20)
    max_dur = cfg.get("max_duration_sec", 360)

    st_info = discover_soundtrack_info(item)

    if q and q.strip():
        search_exprs = [f"ytsearch8:{q.strip()}", f"scsearch5:{q.strip()}"]
    else:
        search_exprs = build_search_queries(item)

    all_found = []
    seen_ids = set()

    if not q and st_info.get("direct_url"):
        direct_u = st_info["direct_url"]
        seen_ids.add(direct_u)
        all_found.append({
            "id": direct_u,
            "url": direct_u,
            "title": f"🎯 [Offiziell] {st_info.get('display') or 'Theme'}",
            "duration": 180,
            "duration_str": "~03:00",
            "uploader": st_info.get("source", "Community-DB")
        })

    for expr in search_exprs:
        if expr.startswith("http"):
            continue
        cands = find_all_candidates(expr, blacklist, min_dur, max_dur)
        for c in cands:
            cid = c.get("id")
            if cid and cid not in seen_ids:
                seen_ids.add(cid)
                dur = c.get("duration") or 0
                dur_str = f"{int(dur // 60):02d}:{int(dur % 60):02d}" if dur else "-:--"
                c["duration_str"] = dur_str
                all_found.append(c)
        if len(all_found) >= 6:
            break

    return {
        "ok": True,
        "item_id": item_id,
        "title": item.get("title", ""),
        "soundtrack_info": st_info,
        "candidates": all_found[:8]
    }


@app.get("/api/progress")
def get_progress():
    return scan_progress


@app.post("/api/cancel")
def cancel_action():
    global stop_requested, scan_progress
    stop_requested = True
    scan_progress["current_step"] = "Cancelling..."
    return {"ok": True, "msg": "Cancellation requested"}


@app.post("/scan")
def trigger_scan(
    scope: str = Form("all"),
    tab: str = Form("filme"),
    filter_status: str = Form("all"),
    q: str = Form("")
):
    threading.Thread(target=run_scan, kwargs={"scope": scope}, daemon=True).start()
    clean_tab = normalize_tab(tab)
    return RedirectResponse(f"/?tab={clean_tab}&filter_status={filter_status}&q={q}", status_code=303)


@app.post("/download")
def trigger_download(
    scope: str = Form("all"),
    tab: str = Form("filme"),
    filter_status: str = Form("all"),
    q: str = Form("")
):
    threading.Thread(target=run_download_queue, kwargs={"scope": scope}, daemon=True).start()
    clean_tab = normalize_tab(tab)
    return RedirectResponse(f"/?tab={clean_tab}&filter_status={filter_status}&q={q}", status_code=303)


@app.post("/scan-single/{item_id}")
def scan_single(
    item_id: str,
    tab: str = Form("filme"),
    filter_status: str = Form("all"),
    q: str = Form("")
):
    state = load_state()
    cfg = load_config()
    item = state["items"].get(item_id)
    if not item:
        raise HTTPException(status_code=404)

    theme_exists, actual_dir, mp3_file = check_item_theme(item, cfg=cfg)
    item["dir"] = actual_dir
    item["has_theme"] = theme_exists
    if theme_exists:
        item["status"] = item.get("video_title") or (mp3_file.name if mp3_file else "Present")
        if item.get("type") == "Collection" and mp3_file:
            sync_collection_theme(item, mp3_file, cfg.get("jf_metadata_dir", "/jellyfin-metadata"))
    else:
        item["status"] = "Missing"

    state["items"][item_id] = item
    save_state(state)

    clean_tab = normalize_tab(tab)
    return RedirectResponse(f"/?tab={clean_tab}&filter_status={filter_status}&q={q}", status_code=303)


@app.post("/replace/{item_id}")
def replace_theme(
    item_id: str,
    custom_input: str = Form(""),
    custom_title: Optional[str] = Form(""),
    tab: str = Form("filme"),
    filter_status: str = Form("all"),
    q: str = Form("")
):
    state = load_state()
    cfg = load_config()
    item = state["items"].get(item_id)
    if not item:
        raise HTTPException(status_code=404)

    old_vid = item.get("video_id")
    if old_vid:
        bl = state["blacklist"].setdefault(item_id, [])
        if old_vid not in bl:
            bl.append(old_vid)

    dl_res = search_and_download(
        item,
        custom_query_or_url=custom_input.strip() or None,
        custom_title=custom_title.strip() if custom_title else None,
        blacklist=state["blacklist"].get(item_id, []),
        min_dur=cfg.get("min_duration_sec", 25),
        max_dur=cfg.get("max_duration_sec", 270),
        jf_meta_base=cfg.get("jf_metadata_dir", "/jellyfin-metadata"),
        fade=cfg.get("audio_fade", True)
    )
    if dl_res["ok"]:
        item["has_theme"] = True
        item["video_id"] = dl_res["video_id"]
        item["video_title"] = dl_res["video_title"]
        item["status"] = dl_res["video_title"] or "Heruntergeladen"
    else:
        item["status"] = dl_res["msg"]

    state["items"][item_id] = item
    save_state(state)

    clean_tab = normalize_tab(tab)
    return RedirectResponse(f"/?tab={clean_tab}&filter_status={filter_status}&q={q}", status_code=303)


@app.post("/delete/{item_id}")
def delete_theme(
    item_id: str,
    tab: str = Form("filme"),
    filter_status: str = Form("all"),
    q: str = Form("")
):
    state = load_state()
    item = state["items"].get(item_id)
    if item:
        for p in (Path(item.get("dir", "")) / "theme.mp3", LOCAL_THEMES_DIR / item["id"] / "theme.mp3"):
            p.unlink(missing_ok=True)

        old_vid = item.get("video_id")
        if old_vid:
            state["blacklist"].setdefault(item_id, []).append(old_vid)
        item["has_theme"] = False
        item["status"] = "Deleted (blocked)"
        save_state(state)

    clean_tab = normalize_tab(tab)
    return RedirectResponse(f"/?tab={clean_tab}&filter_status={filter_status}&q={q}", status_code=303)


class TestJellyfinReq(BaseModel):
    url: str
    api_key: str

class TestPlexReq(BaseModel):
    url: str
    token: str

class TestFolderReq(BaseModel):
    paths: str

@app.post("/api/test-jellyfin")
def api_test_jellyfin(data: TestJellyfinReq):
    return test_jellyfin_connection(data.url, data.api_key)

@app.post("/api/test-plex")
def api_test_plex(data: TestPlexReq):
    return test_plex_connection(data.url, data.token)

@app.post("/api/test-folder")
def api_test_folder(data: TestFolderReq):
    return test_folder_path(data.paths)



@app.get("/set-language")
def set_language(lang: str = "en", tab: str = "movies", filter_status: str = "all", q: str = ""):
    cfg = load_config()
    cfg["ui_language"] = "de" if lang == "de" else "en"
    save_config(cfg)
    clean_tab = normalize_tab(tab)
    return RedirectResponse(f"/?tab={clean_tab}&filter_status={filter_status}&q={q}", status_code=303)

@app.get("/settings", response_class=HTMLResponse)
def get_settings_page():
    cfg = load_config()
    return f"""
    <!doctype html>
    <html lang="de">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>{get_t(cfg, "settings_title")} • Theme Fetcher</title>
        <style>
            :root {{
                --bg: #121417;
                --card-bg: #181b20;
                --border: #262a32;
                --text: #e4e6eb;
                --text-muted: #9aa0a6;
                --primary: #2b7de9;
                --primary-hover: #1b66c9;
                --success: #28a745;
                --danger: #c93b3b;
            }}
            * {{ box-sizing: border-box; }}
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 24px; max-width: 900px; margin: 0 auto; }}
            .nav-back {{ margin-bottom: 20px; }}
            .nav-back a {{ color: var(--text-muted); text-decoration: none; font-size: 0.95rem; font-weight: 600; display: inline-flex; align-items: center; gap: 6px; }}
            .nav-back a:hover {{ color: #fff; }}
            h1 {{ margin: 0 0 20px 0; font-size: 1.7rem; }}
            .card {{ background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; padding: 22px; margin-bottom: 20px; }}
            .card h2 {{ margin-top: 0; font-size: 1.15rem; border-bottom: 1px solid var(--border); padding-bottom: 8px; display: flex; justify-content: space-between; align-items: center; }}
            .form-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; margin-top: 14px; }}
            .form-group {{ display: flex; flex-direction: column; gap: 6px; }}
            .form-group label {{ font-size: 0.85rem; font-weight: 600; color: var(--text-muted); }}
            .form-group small {{ font-size: 0.78rem; color: #7d8590; }}
            input[type="text"], input[type="number"] {{ background: #1e2126; border: 1px solid #343840; color: #fff; padding: 8px 12px; border-radius: 6px; font-size: 0.9rem; }}
            input[type="text"]:focus, input[type="number"]:focus {{ outline: none; border-color: var(--primary); }}
            .checkbox-group {{ display: flex; align-items: center; gap: 10px; font-size: 0.9rem; margin-top: 6px; }}
            .checkbox-group input {{ width: 18px; height: 18px; cursor: pointer; }}
            button, .btn {{ background: var(--primary); color: white; border: none; padding: 10px 18px; border-radius: 6px; cursor: pointer; font-weight: 600; font-size: 0.95rem; text-decoration: none; display: inline-flex; align-items: center; gap: 6px; }}
            button.test {{ background: #2f3e54; color: #7db2ff; border: 1px solid #415575; font-size: 0.8rem; padding: 6px 12px; }}
            .test-result {{ margin-top: 6px; font-size: 0.85rem; padding: 8px 12px; border-radius: 4px; display: none; }}
            .test-result.ok {{ display: block; background: #133824; color: #5ce69a; border: 1px solid #1f5938; }}
            .test-result.err {{ display: block; background: #421c1c; color: #ff8585; border: 1px solid #6b2d2d; }}
        </style>
    </head>
    <body>
        <div class="nav-back">
            <a href="/">{get_t(cfg, "back_to_lib")}</a>
        </div>
        <h1>⚙️ {get_t(cfg, "settings_title")} <span style="font-size:0.75rem; background:#2b7de9; color:#fff; padding:2px 8px; border-radius:12px; vertical-align:middle; font-weight:600;">v24</span></h1>

        <form method="post" action="/settings">
            <div class="card">
                <h2>
                    <span>{get_t(cfg, "sec_local")}</span>
                    <button type="button" class="test" onclick="runTestFolder()">{get_t(cfg, "btn_test_path")}</button>
                </h2>
                <div class="form-grid">
                    <div class="form-group" style="grid-column: 1 / -1;">
                        <label>{get_t(cfg, "lbl_paths")}</label>
                        <input type="text" id="cfg_paths" name="local_media_paths" value="{cfg.get('local_media_paths', '/media')}">
                        <small>{get_t(cfg, "sub_paths")}</small>
                        <div id="res_folder" class="test-result"></div>
                    </div>
                    <div class="checkbox-group" style="grid-column: 1 / -1;">
                        <input type="checkbox" id="local_scan_enabled" name="local_scan_enabled" {'checked' if cfg.get('local_scan_enabled', True) else ''}>
                        <label for="local_scan_enabled">{get_t(cfg, "lbl_local_scan")}</label>
                    </div>
                </div>
            </div>

            <div class="card">
                <h2>
                    <span>{get_t(cfg, "sec_servers")}</span>
                    <div style="display:flex; gap:6px;">
                        <button type="button" class="test" onclick="runTestJellyfin()">{get_t(cfg, "btn_test_jf")}</button>
                        <button type="button" class="test" onclick="runTestPlex()">{get_t(cfg, "btn_test_plex")}</button>
                    </div>
                </h2>
                <div class="form-grid">
                    <div class="form-group">
                        <label>{get_t(cfg, "lbl_jf_url")}</label>
                        <input type="text" id="cfg_jf_url" name="jellyfin_url" value="{cfg.get('jellyfin_url', '')}">
                        <small>{get_t(cfg, "sub_jf_url")}</small>
                    </div>
                    <div class="form-group">
                        <label>{get_t(cfg, "lbl_jf_key")}</label>
                        <input type="text" id="cfg_jf_key" name="jellyfin_api_key" value="{cfg.get('jellyfin_api_key', '')}">
                        <small>{get_t(cfg, "sub_jf_key")}</small>
                    </div>
                    <div id="res_jf" class="test-result" style="grid-column: 1 / -1;"></div>

                    <div class="form-group">
                        <label>{get_t(cfg, "lbl_plex_url")}</label>
                        <input type="text" id="cfg_plex_url" name="plex_url" value="{cfg.get('plex_url', '')}">
                        <small>{get_t(cfg, "sub_plex_url")}</small>
                    </div>
                    <div class="form-group">
                        <label>{get_t(cfg, "lbl_plex_token")}</label>
                        <input type="text" id="cfg_plex_token" name="plex_token" value="{cfg.get('plex_token', '')}">
                        <small>{get_t(cfg, "sub_plex_token")}</small>
                    </div>
                    <div id="res_plex" class="test-result" style="grid-column: 1 / -1;"></div>
                </div>
            </div>

            <div class="card">
                <h2>{get_t(cfg, "sec_audio")}</h2>
                <div class="form-grid">
                    <div class="form-group">
                        <label>{get_t(cfg, "lbl_min_dur")}</label>
                        <input type="number" name="min_duration_sec" value="{cfg.get('min_duration_sec', 25)}">
                        <small>{get_t(cfg, "sub_min_dur")}</small>
                    </div>
                    <div class="form-group">
                        <label>{get_t(cfg, "lbl_max_dur")}</label>
                        <input type="number" name="max_duration_sec" value="{cfg.get('max_duration_sec', 270)}">
                        <small>{get_t(cfg, "sub_max_dur")}</small>
                    </div>
                    <div class="checkbox-group">
                        <input type="checkbox" id="audio_fade" name="audio_fade" {'checked' if cfg.get('audio_fade', True) else ''}>
                        <label for="audio_fade">{get_t(cfg, "lbl_fade")}</label>
                    </div>
                </div>
            </div>

            <div class="card">
                <h2><span>{get_t(cfg, "sec_lang")}</span></h2>
                <div class="form-grid">
                    <div class="form-group" style="grid-column: 1 / -1;">
                        <label>{get_t(cfg, "lbl_lang")}</label>
                        <select name="ui_language" style="padding: 8px 12px; font-size: 0.95rem; width: 220px;">
                            <option value="de" {'selected' if cfg.get('ui_language', 'de') == 'de' else ''}>🇩🇪 Deutsch (German)</option>
                            <option value="en" {'selected' if cfg.get('ui_language', 'de') == 'en' else ''}>🇬🇧 English</option>
                        </select>
                    </div>
                </div>
            </div>

            <div style="margin-top:20px; display:flex; gap:12px;">
                <button type="submit" style="background:#28a745; font-size:1.05rem; padding:12px 24px;">{get_t(cfg, "btn_save_settings")}</button>
                <a href="/" class="btn" style="background:#2a2e36;">{get_t(cfg, "btn_cancel_settings")}</a>
            </div>
        </form>

        <script>
            async function runTestJellyfin() {{
                const resEl = document.getElementById('res_jf');
                resEl.className = 'test-result';
                resEl.style.display = 'block';
                resEl.innerText = 'Testing connection to Jellyfin...';
                
                const url = document.getElementById('cfg_jf_url').value;
                const apiKey = document.getElementById('cfg_jf_key').value;

                try {{
                    const resp = await fetch('/api/test-jellyfin', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ url: url, api_key: apiKey }})
                    }});
                    const data = await resp.json();
                    resEl.className = 'test-result ' + (data.ok ? 'ok' : 'err');
                    resEl.innerText = (data.ok ? '✓ ' : '✕ ') + data.msg;
                }} catch (e) {{
                    resEl.className = 'test-result err';
                    resEl.innerText = '✕ Request failed: ' + e;
                }}
            }}

            async function runTestPlex() {{
                const resEl = document.getElementById('res_plex');
                resEl.className = 'test-result';
                resEl.style.display = 'block';
                resEl.innerText = 'Testing connection to Plex...';
                
                const url = document.getElementById('cfg_plex_url').value;
                const token = document.getElementById('cfg_plex_token').value;

                try {{
                    const resp = await fetch('/api/test-plex', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ url: url, token: token }})
                    }});
                    const data = await resp.json();
                    resEl.className = 'test-result ' + (data.ok ? 'ok' : 'err');
                    resEl.innerText = (data.ok ? '✓ ' : '✕ ') + data.msg;
                }} catch (e) {{
                    resEl.className = 'test-result err';
                    resEl.innerText = '✕ Request failed: ' + e;
                }}
            }}

            async function runTestFolder() {{
                const resEl = document.getElementById('res_folder');
                resEl.className = 'test-result';
                resEl.style.display = 'block';
                resEl.innerText = 'Testing container media path...';
                
                const paths = document.getElementById('cfg_paths').value;

                try {{
                    const resp = await fetch('/api/test-folder', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ paths: paths }})
                    }});
                    const data = await resp.json();
                    resEl.className = 'test-result ' + (data.ok ? 'ok' : 'err');
                    resEl.innerText = data.msg;
                }} catch (e) {{
                    resEl.className = 'test-result err';
                    resEl.innerText = '✕ Request failed: ' + e;
                }}
            }}
        </script>
    </body>
    </html>
    """


@app.post("/settings")
def update_settings(
    local_media_paths: str = Form("/media"),
    local_scan_enabled: Optional[str] = Form(None),
    jellyfin_url: str = Form(""),
    jellyfin_api_key: str = Form(""),
    plex_url: str = Form(""),
    plex_token: str = Form(""),
    min_duration_sec: int = Form(25),
    max_duration_sec: int = Form(270),
    audio_fade: Optional[str] = Form(None),
    ui_language: str = Form("de"),
    scan_interval_hours: int = Form(12)
):
    cfg = load_config()
    cfg["local_media_paths"] = local_media_paths.strip()
    cfg["local_scan_enabled"] = bool(local_scan_enabled)
    cfg["jellyfin_url"] = clean_url_input(jellyfin_url)
    cfg["jellyfin_api_key"] = jellyfin_api_key.strip()
    cfg["plex_url"] = clean_url_input(plex_url)
    cfg["plex_token"] = plex_token.strip()
    cfg["min_duration_sec"] = min_duration_sec
    cfg["max_duration_sec"] = max_duration_sec
    cfg["audio_fade"] = bool(audio_fade)
    cfg["ui_language"] = ui_language.strip() or "de"
    cfg["scan_interval_hours"] = scan_interval_hours

    save_config(cfg)
    threading.Thread(target=run_scan, kwargs={"scope": "all"}, daemon=True).start()
    return RedirectResponse("/?settings=saved", status_code=303)


@app.get("/", response_class=HTMLResponse)
def index(q: str = "", filter_status: str = "all", tab: str = "filme", settings: str = ""):
    cfg = load_config()
    state = load_state()
    all_items = list(state["items"].values())

    clean_tab = normalize_tab(tab)

    total_filme = sum(1 for i in all_items if i.get("type") == "Film")
    filme_ok = sum(1 for i in all_items if i.get("type") == "Film" and i.get("has_theme"))
    filme_miss = total_filme - filme_ok

    total_serien = sum(1 for i in all_items if i.get("type") == "Serie")
    serien_ok = sum(1 for i in all_items if i.get("type") == "Serie" and i.get("has_theme"))
    serien_miss = total_serien - serien_ok

    total_cols = sum(1 for i in all_items if i.get("type") == "Collection")
    cols_ok = sum(1 for i in all_items if i.get("type") == "Collection" and i.get("has_theme"))
    cols_miss = total_cols - cols_ok

    scanned_folders = state.get("folders", [])

    type_map = {"movies": "Film", "shows": "Serie", "collections": "Collection", "filme": "Film", "serien": "Serie"}
    current_scope = type_map.get(clean_tab, "Film")

    tab_items = [i for i in all_items if i.get("type") == current_scope]
    tab_total = len(tab_items)
    tab_ok = sum(1 for i in tab_items if i.get("has_theme"))
    tab_miss = tab_total - tab_ok

    items = tab_items
    if q:
        items = [
            i for i in items
            if q.lower() in i.get("title", "").lower() or q.lower() in (i.get("original_title") or "").lower()
        ]
    if filter_status == "missing":
        items = [i for i in items if not i.get("has_theme")]
    elif filter_status == "ok":
        items = [i for i in items if i.get("has_theme")]

    items.sort(key=lambda x: (x.get("has_theme", False), x.get("title", "").lower()))
    st_cache = state.get("soundtrack_cache", {})

    del_confirm_msg = get_t(cfg, "btn_delete_confirm")
    rows = ""
    for it in items:
        iid = it["id"]
        has_theme = bool(it.get("has_theme"))
        st_hint = ""
        c_info = st_cache.get(iid)
        if c_info and c_info.get("display"):
            st_hint = f"<div style='font-size:0.75rem; color:#7db2ff; margin-top:2px;'>🎯 {c_info['display']} <span style='color:#8b929a;'>({c_info.get('source')})</span></div>"
        orig_hint = (
            f" <small style='color:#7d8590;'>[{it['original_title']}]</small>"
            if it.get("original_title") and it["original_title"].lower() != it["title"].lower()
            else ""
        )

        badge = (
            f'<span class="badge ok">{get_t(cfg, "status_present")}</span>'
            if has_theme
            else f'<span class="badge miss">{get_t(cfg, "status_missing")}</span>'
        )
        player = (
            f'<audio controls preload="none" src="/audio/{iid}?t={int(time.time())}"></audio>'
            if has_theme
            else "-"
        )

        if has_theme:
            v_title = it.get("video_title") or it.get("status") or "theme.mp3"
            if v_title in ("Vorhanden", "Present", "Heruntergeladen", "Downloaded", "Manueller Link"):
                v_title = "theme.mp3"
            sub_info = f"<div style='font-size:0.75rem; color:#5ce69a; margin-top:3px;'>🎵 {v_title}</div>"
        else:
            err_msg = it.get("status") or ""
            if err_msg and err_msg not in ("Fehlt", "Missing", "Vorhanden", "Present"):
                sub_info = f"<div style='font-size:0.75rem; color:#ff8585; margin-top:3px;'>⚠️ {err_msg}</div>"
            else:
                sub_info = ""

        rows += f"""
        <tr id="row_{iid}">
            <td style="width: 50px; padding: 6px;">
                <img src="/poster/{iid}" style="width: 44px; height: 64px; object-fit: cover; border-radius: 4px; display: block;" loading="lazy" alt="Cover">
            </td>
            <td>
                <strong>{it['title']}</strong>{orig_hint} <small>({it.get('year') or '-'})</small><br>
                <span class="meta">{it.get('source', 'Local')} • <code>{it.get('dir') or it.get('path', '')}</code></span>
                {st_hint}
            </td>
            <td>
                <div id="status_box_{iid}">
                    {badge}
                    {sub_info}
                </div>
            </td>
            <td>{player}</td>
            <td style="white-space: nowrap;">
                <div style="display:inline-flex; gap:4px; align-items:center;">
                    <button type="button" class="btn-sm" style="background:#5c3b99;" onclick="openThemeModal('{iid}')" title="{get_t(cfg, 'btn_select_title')}">{get_t(cfg, "btn_select")}</button>
                    <form method="post" action="/replace/{iid}" class="inline-form" onsubmit="this.querySelector('button').innerText='⏳';">
                        <input type="hidden" name="tab" value="{clean_tab}">
                        <input type="hidden" name="filter_status" value="{filter_status}">
                        <input type="hidden" name="q" value="{q}">
                        <input type="text" name="custom_input" placeholder="{get_t(cfg, 'btn_dl_placeholder')}" style="width:115px; font-size:0.8rem; padding:4px 6px;">
                        <button type="submit" class="btn-sm" title="{get_t(cfg, 'btn_dl_title')}">▶️</button>
                    </form>
                    <form method="post" action="/scan-single/{iid}" style="display:inline;" onsubmit="this.querySelector('button').innerText='⏳';">
                        <input type="hidden" name="tab" value="{clean_tab}">
                        <input type="hidden" name="filter_status" value="{filter_status}">
                        <input type="hidden" name="q" value="{q}">
                        <button type="submit" class="btn-sm secondary" title="{get_t(cfg, 'btn_recheck_title')}">🔄</button>
                    </form>
                    <form method="post" action="/delete/{iid}" style="display:inline;" onsubmit="return confirm('{del_confirm_msg}');">
                        <input type="hidden" name="tab" value="{clean_tab}">
                        <input type="hidden" name="filter_status" value="{filter_status}">
                        <input type="hidden" name="q" value="{q}">
                        <button type="submit" class="btn-sm danger" title="{get_t(cfg, 'btn_delete_title')}">✕</button>
                    </form>
                </div>
            </td>
            <td style="min-width: 140px; font-size: 0.8rem;" id="prog_col_{iid}">
                <div id="prog_bar_box_{iid}" style="display:none;">
                    <div style="background:#262a32; height:8px; border-radius:4px; overflow:hidden; margin-bottom:4px;">
                        <div id="prog_bar_fill_{iid}" style="background:#28a745; height:100%; width:0%; transition:width 0.3s;"></div>
                    </div>
                    <span id="prog_bar_txt_{iid}" style="color:#7db2ff; font-size:0.75rem;">Processing...</span>
                </div>
                <span id="prog_static_{iid}" style="color:#7d8590;">{get_t(cfg, 'status_ready') if has_theme else get_t(cfg, 'status_missing')}</span>
            </td>
        </tr>
        """

    saved_banner = f'<div class="banner">{get_t(cfg, "settings_saved_banner")}</div>' if settings == "saved" else ""
    folder_list_html = "".join(f"<li style='margin-bottom:6px;'><code>{f}</code></li>" for f in scanned_folders) or "<li>{lib_empty_placeholder}</li>"

    scope_label = "Movies" if current_scope == "Film" else ("TV Shows" if current_scope == "Serie" else "Collections")

    return f"""
    <!doctype html>
    <html lang="de">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Theme Fetcher</title>
        <style>
            :root {{
                --bg: #121417;
                --card-bg: #181b20;
                --border: #262a32;
                --text: #e4e6eb;
                --text-muted: #9aa0a6;
                --primary: #2b7de9;
                --primary-hover: #1b66c9;
                --danger: #c93b3b;
                --success: #28a745;
                --success-bg: #1d4d32;
                --success-text: #5ce69a;
                --miss-bg: #4d2323;
                --miss-text: #ff7b7b;
            }}
            * {{ box-sizing: border-box; }}
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 20px; }}
            .header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; flex-wrap: wrap; gap: 14px; }}
            h1 {{ margin: 0; font-size: 1.6rem; letter-spacing: -0.5px; display: flex; align-items: center; gap: 8px; }}
            .stats {{ color: var(--text-muted); font-size: 0.95rem; margin-top: 4px; }}
            button, .btn {{ background: var(--primary); color: white; border: none; padding: 8px 14px; border-radius: 6px; cursor: pointer; font-weight: 500; font-size: 0.9rem; text-decoration: none; display: inline-flex; align-items: center; gap: 6px; }}
            button:hover, .btn:hover {{ background: var(--primary-hover); }}
            button.secondary, .btn.secondary {{ background: #2a2e36; color: var(--text); }}
            button.secondary:hover, .btn.secondary:hover {{ background: #353b45; }}
            button.success, .btn.success {{ background: var(--success); }}
            button.success:hover, .btn.success:hover {{ background: #218838; }}
            button.danger, .btn.danger {{ background: var(--danger); }}
            button.danger:hover, .btn.danger:hover {{ background: #a82e2e; }}
            .btn-sm {{ padding: 4px 8px; font-size: 0.8rem; border-radius: 4px; }}
            input[type="text"], input[type="number"], select {{ background: #1e2126; border: 1px solid #343840; color: #fff; padding: 7px 10px; border-radius: 6px; font-size: 0.85rem; }}
            input[type="text"]:focus, select:focus {{ outline: none; border-color: var(--primary); }}
            table {{ width: 100%; border-collapse: collapse; background: var(--card-bg); border-radius: 8px; overflow: hidden; margin-top: 10px; }}
            th, td {{ padding: 8px 10px; border-bottom: 1px solid var(--border); text-align: left; vertical-align: middle; }}
            th {{ background: #1f2329; font-size: 0.8rem; text-transform: uppercase; color: var(--text-muted); letter-spacing: 0.5px; }}
            .meta {{ font-size: 0.75rem; color: #8b929a; }}
            .badge {{ padding: 3px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; display:inline-block; }}
            .badge.ok {{ background: var(--success-bg); color: var(--success-text); }}
            .badge.miss {{ background: var(--miss-bg); color: var(--miss-text); }}
            .inline-form {{ display: inline-flex; gap: 4px; }}
            audio {{ height: 32px; width: 190px; }}
            .banner {{ background: #1d4d32; color: #5ce69a; padding: 10px 16px; border-radius: 6px; margin-bottom: 16px; font-weight: 500; }}
            
            /* PROGRESS & QUEUE CARD */
            .progress-card {{ background: var(--card-bg); border: 1px solid #344256; border-radius: 8px; padding: 16px; margin-bottom: 18px; box-shadow: 0 4px 12px rgba(0,0,0,0.3); }}
            .progress-bar-bg {{ background: #262a32; height: 14px; border-radius: 7px; overflow: hidden; margin: 10px 0; }}
            .progress-bar-fill {{ background: #28a745; height: 100%; width: 0%; transition: width 0.3s ease; }}
            .queue-box {{ background: #131519; border: 1px solid #232730; border-radius: 6px; padding: 10px; margin-top: 12px; font-size: 0.85rem; }}
            
            /* TABS */
            .tab-nav {{ display: flex; gap: 8px; margin-bottom: 14px; border-bottom: 1px solid var(--border); padding-bottom: 8px; flex-wrap: wrap; }}
            .tab-btn {{ background: transparent; color: var(--text-muted); border: 1px solid transparent; padding: 8px 16px; border-radius: 6px; font-size: 0.95rem; cursor: pointer; text-decoration: none; font-weight: 600; }}
            .tab-btn:hover {{ background: #20242b; color: #fff; }}
            .tab-btn.active {{ background: #2b7de9; color: #fff; }}
            
            /* FILTER PILLS */
            .pill-filter {{ display: flex; gap: 8px; align-items: center; flex-wrap:wrap; }}
            .pill {{ background: #20242b; color: var(--text-muted); border: 1px solid #2d323b; padding: 5px 12px; border-radius: 20px; font-size: 0.85rem; text-decoration: none; font-weight: 500; cursor: pointer; }}
            .pill:hover {{ color: #fff; border-color: #3f4552; }}
            .pill.active {{ background: #2b7de9; color: #fff; border-color: #2b7de9; }}
            .pill.miss.active {{ background: #c93b3b; color: #fff; border-color: #c93b3b; }}
            .pill.ok.active {{ background: #28a745; color: #fff; border-color: #28a745; }}

            /* MODAL DIALOG */
            .modal-backdrop {{ position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0, 0, 0, 0.75); display: none; align-items: center; justify-content: center; z-index: 1000; }}
            .modal {{ background: var(--card-bg); border: 1px solid #343b47; border-radius: 10px; width: 92%; max-width: 680px; padding: 22px; box-shadow: 0 10px 25px rgba(0,0,0,0.6); max-height: 90vh; overflow-y: auto; }}
            .modal h2 {{ margin-top: 0; font-size: 1.2rem; border-bottom: 1px solid var(--border); padding-bottom: 10px; display: flex; justify-content: space-between; align-items: center; }}
            .modal-close {{ background: transparent; color: var(--text-muted); border: none; font-size: 1.4rem; cursor: pointer; padding: 0 4px; }}
            .modal-close:hover {{ color: #fff; }}
            .candidate-row {{ background: #121417; border: 1px solid #282d37; border-radius: 6px; padding: 10px 12px; margin-bottom: 10px; display: flex; justify-content: space-between; align-items: center; gap: 10px; flex-wrap: wrap; }}
        </style>
    </head>
    <body>
        <div class="header">
            <div>
                <h1>🎵 Theme Fetcher <span style="font-size:0.75rem; background:#2b7de9; color:#fff; padding:2px 8px; border-radius:12px; vertical-align:middle; font-weight:600;">v24</span></h1>
                <div class="stats">{get_t(cfg, "movies")}: {filme_ok}/{total_filme} • {get_t(cfg, "shows")}: {serien_ok}/{total_serien} • {get_t(cfg, "collections")}: {cols_ok}/{total_cols}</div>
            </div>
            <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
                <div style="display:inline-flex; border:1px solid #343840; border-radius:6px; overflow:hidden; margin-right:4px;"><a href="/set-language?lang=en&tab={clean_tab}&filter_status={filter_status}&q={q}" style="padding:6px 10px; font-size:0.8rem; text-decoration:none; font-weight:600; background:{'#2b7de9' if cfg.get('ui_language','en')=='en' else '#1e2126'}; color:#fff;">🇬🇧 EN</a><a href="/set-language?lang=de&tab={clean_tab}&filter_status={filter_status}&q={q}" style="padding:6px 10px; font-size:0.8rem; text-decoration:none; font-weight:600; background:{'#2b7de9' if cfg.get('ui_language','en')=='de' else '#1e2126'}; color:#fff;">🇩🇪 DE</a></div><a href="/settings" class="btn secondary">⚙️ {get_t(cfg, "settings")}</a>

                <form method="post" action="/scan" style="display:inline;">
                    <input type="hidden" name="scope" value="{current_scope}">
                    <input type="hidden" name="tab" value="{clean_tab}">
                    <input type="hidden" name="filter_status" value="{filter_status}">
                    <input type="hidden" name="q" value="{q}">
                    <button type="submit">{get_t(cfg, "scan_only", scope=scope_label)}</button>
                </form>

                <form method="post" action="/scan" style="display:inline;">
                    <input type="hidden" name="scope" value="all">
                    <input type="hidden" name="tab" value="{clean_tab}">
                    <input type="hidden" name="filter_status" value="{filter_status}">
                    <input type="hidden" name="q" value="{q}">
                    <button type="submit" class="secondary">{get_t(cfg, "scan_all")}</button>
                </form>

                <form method="post" action="/download" style="display:inline;">
                    <input type="hidden" name="scope" value="{current_scope}">
                    <input type="hidden" name="tab" value="{clean_tab}">
                    <input type="hidden" name="filter_status" value="{filter_status}">
                    <input type="hidden" name="q" value="{q}">
                    <button type="submit" class="success">{get_t(cfg, "download_missing", scope=scope_label, count=tab_miss)}</button>
                </form>

                <button type="button" id="btnCancel" class="danger" style="display:none;" onclick="cancelRunningAction()">{get_t(cfg, "cancel")}</button>
            </div>
        </div>

        {saved_banner}

        <!-- LIVE FORTSCHRITTS- & WARTESCHLANGEN-KARTE -->
        <div id="progressCard" class="progress-card" style="display:none;">
            <div style="display:flex; justify-content:space-between; align-items:center;">
                <strong id="progStep" style="font-size:1.05rem;">{get_t(cfg, "op_in_progress")}</strong>
                <span id="progCount" style="color:var(--text-muted); font-size:0.95rem; font-weight:600;">0 / 0</span>
            </div>
            <div class="progress-bar-bg">
                <div id="progFill" class="progress-bar-fill"></div>
            </div>
            <div style="display:flex; justify-content:space-between; font-size:0.85rem; color:var(--text-muted);">
                <span id="progItem">{get_t(cfg, "processing")}</span>
                <span>{get_t(cfg, "downloaded_stat")}: <strong id="progDown" style="color:#5ce69a;">0</strong> | {get_t(cfg, "failed_stat")}: <strong id="progFail" style="color:#ff8585;">0</strong></span>
            </div>

            <div id="queueBox" class="queue-box" style="display:none;">
                <div style="font-weight:600; margin-bottom:6px; color:#7db2ff;">{get_t(cfg, "next_in_queue")}</div>
                <div id="queueList" style="color:var(--text-muted); font-size:0.8rem; margin-bottom:10px;">-</div>
                <div style="font-weight:600; margin-bottom:4px; color:#5ce69a;">{get_t(cfg, "recently_finished")}</div>
                <div id="recentList" style="font-size:0.8rem;">-</div>
            </div>
        </div>

        <!-- REITER / TABS -->
        <div class="tab-nav">
            <a href="/?tab=movies&filter_status={filter_status}{f'&q={q}' if q else ''}" class="tab-btn {'active' if clean_tab in ('movies', 'filme') else ''}">🎬 {get_t(cfg, "movies")} ({filme_ok}/{total_filme})</a>
            <a href="/?tab=shows&filter_status={filter_status}{f'&q={q}' if q else ''}" class="tab-btn {'active' if clean_tab in ('shows', 'serien') else ''}">📺 {get_t(cfg, "shows")} ({serien_ok}/{total_serien})</a>
            <a href="/?tab=collections&filter_status={filter_status}{f'&q={q}' if q else ''}" class="tab-btn {'active' if clean_tab=='collections' else ''}">📁 {get_t(cfg, "collections")} ({cols_ok}/{total_cols})</a>
            <a href="/?tab=libraries" class="tab-btn {'active' if clean_tab in ('libraries', 'ordner') else ''}">📂 {get_t(cfg, "libraries")} ({len(scanned_folders)})</a>
        </div>

        {'<!-- ORDNER TAB -->' if clean_tab in ('libraries', 'ordner') else ''}
        <div style="display: {'block' if clean_tab in ('libraries', 'ordner') else 'none'}; background:var(--card-bg); padding:20px; border-radius:8px;">
            <h3 style="margin-top:0;">{get_t(cfg, "lib_title")}</h3>
            <p style="color:var(--text-muted); font-size:0.9rem;">{get_t(cfg, "lib_desc")}</p>
            <ul style="line-height:1.8; font-size:0.95rem;">
                {folder_list_html}
            </ul>
        </div>

        <!-- FILTER-LEISTE & TABELLE -->
        <div style="display: {'none' if clean_tab in ('libraries', 'ordner') else 'block'};">
            <div style="background:var(--card-bg); padding:12px; border-radius:8px; margin-bottom:14px; display:flex; gap:14px; align-items:center; justify-content:space-between; flex-wrap:wrap;">
                
                <div class="pill-filter">
                    <span style="font-size:0.85rem; color:var(--text-muted); margin-right:4px;">{get_t(cfg, "filter_label")}</span>
                    <a href="/?tab={clean_tab}&filter_status=all{f'&q={q}' if q else ''}" class="pill {'active' if filter_status=='all' else ''}">{get_t(cfg, "all_pill", count=tab_total)}</a>
                    <a href="/?tab={clean_tab}&filter_status=missing{f'&q={q}' if q else ''}" class="pill miss {'active' if filter_status=='missing' else ''}">{get_t(cfg, "missing_pill", count=tab_miss)}</a>
                    <a href="/?tab={clean_tab}&filter_status=ok{f'&q={q}' if q else ''}" class="pill ok {'active' if filter_status=='ok' else ''}">{get_t(cfg, "present_pill", count=tab_ok)}</a>
                </div>

                <form method="get" action="/" style="display:flex; gap:6px; align-items:center;">
                    <input type="hidden" name="tab" value="{clean_tab}">
                    <input type="hidden" name="filter_status" value="{filter_status}">
                    <input type="text" name="q" value="{q}" placeholder="{get_t(cfg, 'search_placeholder', scope=scope_label)}" style="width:200px;">
                    <button type="submit" class="btn-sm">{get_t(cfg, "search_btn")}</button>
                    {f'<a href="/?tab={clean_tab}&filter_status={filter_status}" class="btn-sm secondary">✕</a>' if q else ''}
                </form>
            </div>

            <table>
                <thead>
                    <tr>
                        <th style="width:50px;">{get_t(cfg, "th_cover")}</th>
                        <th>{get_t(cfg, "th_title_path")}</th>
                        <th>{get_t(cfg, "th_status")}</th>
                        <th>{get_t(cfg, "th_preview")}</th>
                        <th>{get_t(cfg, "th_actions")}</th>
                        <th>{get_t(cfg, "th_progress")}</th>
                    </tr>
                </thead>
                <tbody>{rows or f'<tr><td colspan="6" style="text-align:center; padding:30px; color:var(--text-muted);">{get_t(cfg, "no_entries", scope=scope_label)}</td></tr>'}</tbody>
            </table>
        </div>

        <!-- THEME AUSWAHL-MODAL -->
        <div id="themeModal" class="modal-backdrop">
            <div class="modal">
                <h2>
                    <span id="themeModalTitle">{get_t(cfg, "modal_title", title="")}</span>
                    <button class="modal-close" onclick="closeModal('themeModal')">&times;</button>
                </h2>
                
                <div id="themeSpuerhundBanner" style="display:none; background:#192636; border:1px solid #2b4566; color:#7db2ff; padding:8px 12px; border-radius:6px; margin: 10px 0; font-size:0.85rem;"></div>

                <div style="display:flex; gap:6px; margin: 12px 0 16px 0;">
                    <input type="text" id="themeSearchInput" placeholder="{get_t(cfg, 'modal_search_placeholder')}" style="flex:1;">
                    <button type="button" class="btn-sm" onclick="fetchCandidates()">{get_t(cfg, "modal_search_btn")}</button>
                </div>

                <div id="themeModalLoading" style="padding:25px; text-align:center; color:var(--text-muted);">
                    Suche Soundtrack-Kandidaten auf YouTube & SoundCloud...
                </div>

                <div id="themeModalList" style="display:none;"></div>
            </div>
        </div>

        <script>
            let currentModalItemId = null;

            // Audio-Autostopp & 50% Standard-Lautstärke
            document.addEventListener('play', function(e){{
                e.target.volume = 0.5; // Angenehme Standard-Lautstärke
                var audios = document.getElementsByTagName('audio');
                for(var i = 0, len = audios.length; i < len; i++){{
                    if(audios[i] != e.target){{
                        audios[i].pause();
                    }}
                }}
            }}, true);

            function formatTimeSec(sec) {{
                if (isNaN(sec) || sec < 0) return '00:00';
                const m = Math.floor(sec / 60);
                const s = Math.floor(sec % 60);
                return (m < 10 ? '0' : '') + m + ':' + (s < 10 ? '0' : '') + s;
            }}

            function togglePlay(id) {{
                const audio = document.getElementById('audio_' + id);
                const btn = document.getElementById('btn_play_' + id);
                if (!audio) return;
                if (audio.paused) {{
                    audio.volume = 0.5;
                    document.querySelectorAll('audio').forEach(a => {{ if (a !== audio) a.pause(); }});
                    document.querySelectorAll('[id^="btn_play_"]').forEach(b => {{ b.innerText = '▶'; }});
                    audio.play();
                    if (btn) btn.innerText = '⏸';
                }} else {{
                    audio.pause();
                    if (btn) btn.innerText = '▶';
                }}
            }}

            function seekMid(id) {{
                const audio = document.getElementById('audio_' + id);
                if (!audio) return;
                if (audio.duration && !isNaN(audio.duration) && audio.duration > 0) {{
                    audio.currentTime = audio.duration / 2;
                }} else {{
                    audio.currentTime = 45;
                }}
                if (audio.paused) {{
                    togglePlay(id);
                }}
            }}

            function onSeekInput(id) {{
                const audio = document.getElementById('audio_' + id);
                const slider = document.getElementById('seek_' + id);
                if (!audio || !slider) return;
                if (audio.duration && !isNaN(audio.duration)) {{
                    audio.currentTime = (slider.value / 100) * audio.duration;
                }}
            }}

            function updateSeek(id) {{
                const audio = document.getElementById('audio_' + id);
                const slider = document.getElementById('seek_' + id);
                const timeSpan = document.getElementById('time_' + id);
                const btn = document.getElementById('btn_play_' + id);
                if (!audio || !slider) return;

                if (audio.paused && btn && btn.innerText !== '▶') {{
                    btn.innerText = '▶';
                }} else if (!audio.paused && btn && btn.innerText !== '⏸') {{
                    btn.innerText = '⏸';
                }}

                if (audio.duration && !isNaN(audio.duration) && audio.duration > 0) {{
                    slider.value = (audio.currentTime / audio.duration) * 100;
                    if (timeSpan) {{
                        timeSpan.innerText = formatTimeSec(audio.currentTime) + ' / ' + formatTimeSec(audio.duration);
                    }}
                }}
            }}

            function initSeek(id) {{
                const audio = document.getElementById('audio_' + id);
                const timeSpan = document.getElementById('time_' + id);
                if (audio && timeSpan && audio.duration && !isNaN(audio.duration)) {{
                    timeSpan.innerText = '00:00 / ' + formatTimeSec(audio.duration);
                }}
            }}

            function closeModal(id) {{
                const modal = document.getElementById(id);
                if (modal) {{
                    const audios = modal.getElementsByTagName('audio');
                    for (let a of audios) a.pause();
                    document.querySelectorAll('[id^="btn_play_"]').forEach(b => {{ b.innerText = '▶'; }});
                    modal.style.display = 'none';
                }}
            }}

            async function openThemeModal(itemId) {{
                currentModalItemId = itemId;
                const modal = document.getElementById('themeModal');
                const row = document.getElementById('row_' + itemId);
                const titleStr = row ? row.querySelector('strong').innerText : 'Titel';

                document.getElementById('themeModalTitle').innerText = '{get_t(cfg, "btn_select")}: ' + titleStr;
                document.getElementById('themeSearchInput').value = '';
                modal.style.display = 'flex';

                await fetchCandidates();
            }}

            async function fetchCandidates() {{
                const loading = document.getElementById('themeModalLoading');
                const list = document.getElementById('themeModalList');
                const q = document.getElementById('themeSearchInput').value;

                loading.style.display = 'block';
                list.style.display = 'none';
                list.innerHTML = '';

                try {{
                    const url = '/api/theme-candidates/' + currentModalItemId + (q ? ('?q=' + encodeURIComponent(q)) : '');
                    const resp = await fetch(url);
                    const data = await resp.json();

                    loading.style.display = 'none';
                    list.style.display = 'block';

                    const banner = document.getElementById('themeSpuerhundBanner');
                    if (data.soundtrack_info && data.soundtrack_info.display) {{
                        banner.style.display = 'block';
                        banner.innerHTML = '🎯 <strong>Erkannter Soundtrack:</strong> ' + data.soundtrack_info.display + ' <span style="color:#8b929a;">(Quelle: ' + data.soundtrack_info.source + ')</span>';
                        if (!q && !document.getElementById('themeSearchInput').value) {{
                            document.getElementById('themeSearchInput').value = data.soundtrack_info.display;
                        }}
                    }}

                    if (!data.ok || !data.candidates || data.candidates.length === 0) {{
                        list.innerHTML = '<div style=\"text-align:center; padding:20px; color:var(--text-muted);\">No matching soundtracks found. Try a different search query above!</div>';
                        return;
                    }}

                    list.innerHTML = data.candidates.map(c => `
                        <div class="candidate-row" style="padding:12px; border-bottom:1px solid #2b2f38; display:flex; flex-direction:column; gap:8px;">
                            <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:10px;">
                                <div style="flex:1;">
                                    <div style="font-weight:600; font-size:0.92rem; color:#e4e6eb;">${{c.title}}</div>
                                    <div style="font-size:0.75rem; color:#8b929a; margin-top:2px;">
                                        ${{c.uploader ? (c.uploader + ' • ') : ''}}{get_t(cfg, "modal_length")}${{c.duration_str}}
                                        • <a href="${{c.url}}" target="_blank" rel="noopener" style="color:#7db2ff; text-decoration:none;">{get_t(cfg, "modal_open_yt")}</a>
                                    </div>
                                </div>
                                <button type="button" class="btn-sm success" style="padding:6px 14px; font-weight:600;" onclick="applyCandidateTheme('${{currentModalItemId}}', '${{c.url}}', '${{encodeURIComponent(c.title || \'\')}}')">{get_t(cfg, "modal_apply")}</button>
                            </div>
                            
                            <div style="display:flex; align-items:center; gap:8px; background:#181b20; padding:6px 10px; border-radius:6px; border:1px solid #2d323b;">
                                <audio id="audio_${{c.id}}" preload="none" src="/api/candidate-stream?v=${{encodeURIComponent(c.id)}}" ontimeupdate="updateSeek('${{c.id}}')" onloadedmetadata="initSeek('${{c.id}}')"></audio>
                                
                                <button type="button" class="btn-sm" id="btn_play_${{c.id}}" onclick="togglePlay('${{c.id}}')" style="background:#2b7de9; min-width:32px; padding:4px 8px;" title="Play / Pause">▶</button>
                                <button type="button" class="btn-sm secondary" onclick="seekMid('${{c.id}}')" style="font-size:0.75rem; padding:4px 8px; white-space:nowrap;" title="{get_t(cfg, 'modal_seek_title')}">{get_t(cfg, "modal_seek_mid")}</button>
                                
                                <input type="range" id="seek_${{c.id}}" min="0" max="100" value="0" style="flex:1; cursor:pointer; accent-color:#2b7de9;" oninput="onSeekInput('${{c.id}}')">
                                
                                <span id="time_${{c.id}}" style="font-size:0.75rem; color:#8b929a; font-family:monospace; min-width:85px; text-align:right;">00:00 / ${{c.duration_str || '--:--'}}</span>
                            </div>
                        </div>
                    `).join('');

                }} catch (e) {{
                    loading.style.display = 'none';
                    list.style.display = 'block';
                    list.innerHTML = '<div style=\"color:#ff8585; padding:15px;\">Search error: ' + e + '</div>';
                }}
            }}

            async function applyCandidateTheme(itemId, customUrl, encodedTitle) {{
                const modal = document.getElementById('themeModal');
                closeModal('themeModal');

                const form = document.createElement('form');
                form.method = 'POST';
                form.action = '/replace/' + itemId;
                
                const inpTab = document.createElement('input');
                inpTab.type = 'hidden';
                inpTab.name = 'tab';
                inpTab.value = '{clean_tab}';
                form.appendChild(inpTab);

                const inpFilter = document.createElement('input');
                inpFilter.type = 'hidden';
                inpFilter.name = 'filter_status';
                inpFilter.value = '{filter_status}';
                form.appendChild(inpFilter);

                const inpQ = document.createElement('input');
                inpQ.type = 'hidden';
                inpQ.name = 'q';
                inpQ.value = '{q}';
                form.appendChild(inpQ);

                const inpUrl = document.createElement('input');
                inpUrl.type = 'hidden';
                inpUrl.name = 'custom_input';
                inpUrl.value = customUrl;
                form.appendChild(inpUrl);

                const inpTitle = document.createElement('input');
                inpTitle.type = 'hidden';
                inpTitle.name = 'custom_title';
                try {{
                    inpTitle.value = decodeURIComponent(encodedTitle || '');
                }} catch(e) {{
                    inpTitle.value = encodedTitle || '';
                }}
                form.appendChild(inpTitle);

                document.body.appendChild(form);
                form.submit();
            }}

            async function cancelRunningAction() {{
                try {{
                    await fetch('/api/cancel', {{ method: 'POST' }});
                    document.getElementById('progStep').innerText = 'Abbruch angefordert...';
                }} catch (e) {{}}
            }}

            async function pollProgress() {{
                try {{
                    const resp = await fetch('/api/progress');
                    const data = await resp.json();
                    const card = document.getElementById('progressCard');
                    const cancelBtn = document.getElementById('btnCancel');
                    const qBox = document.getElementById('queueBox');
                    
                    if (data.is_running) {{
                        card.style.display = 'block';
                        cancelBtn.style.display = 'inline-flex';
                        document.getElementById('progStep').innerText = (data.action_type === 'download' ? '⬇️ ' : '🔍 ') + data.current_step;
                        document.getElementById('progCount').innerText = data.processed + ' / ' + data.total + ' (' + data.percent + '%)';
                        document.getElementById('progFill').style.width = data.percent + '%';
                        document.getElementById('progItem').innerText = data.current_item_title || '';
                        document.getElementById('progDown').innerText = data.new_downloaded;
                        document.getElementById('progFail').innerText = data.failed;

                        // Tabellen-Fortschrittsbalken pro Zeile
                        if (data.current_item_id) {{
                            const barBox = document.getElementById('prog_bar_box_' + data.current_item_id);
                            const barFill = document.getElementById('prog_bar_fill_' + data.current_item_id);
                            const barTxt = document.getElementById('prog_bar_txt_' + data.current_item_id);
                            const barStatic = document.getElementById('prog_static_' + data.current_item_id);
                            
                            if (barBox && barFill && barTxt) {{
                                barBox.style.display = 'block';
                                if (barStatic) barStatic.style.display = 'none';
                                const itemInfo = (data.item_status && data.item_status[data.current_item_id]) || {{}};
                                const pct = itemInfo.percent || 50;
                                barFill.style.width = pct + '%';
                                barTxt.innerText = itemInfo.step || 'Lädt Theme...';
                            }}
                        }}

                        if (data.action_type === 'download') {{
                            qBox.style.display = 'block';
                            if (data.queue && data.queue.length > 0) {{
                                document.getElementById('queueList').innerText = data.queue.slice(0, 6).join(' • ') + (data.queue.length > 6 ? ' ... (+' + (data.queue.length - 6) + ' weitere)' : '');
                            }} else {{
                                document.getElementById('queueList').innerText = '{get_t(cfg, "no_more_queue")}';
                            }}

                            if (data.recent_finished && data.recent_finished.length > 0) {{
                                document.getElementById('recentList').innerHTML = data.recent_finished.slice(0, 4).map(r => 
                                    '<div style=\"color:' + (r.ok ? '#5ce69a' : '#ff8585') + '; margin-bottom:2px;\">' + (r.ok ? '✓ ' : '✕ ') + '<strong>' + r.title + '</strong>: ' + r.msg + '</div>'
                                ).join('');
                            }}
                        }} else {{
                            qBox.style.display = 'none';
                        }}
                    }} else {{
                        cancelBtn.style.display = 'none';
                        if (data.processed > 0 && data.processed === data.total) {{
                            card.style.display = 'block';
                            document.getElementById('progStep').innerText = data.current_step;
                            document.getElementById('progFill').style.width = '100%';
                            document.getElementById('progDown').innerText = data.new_downloaded;
                            document.getElementById('progFail').innerText = data.failed;
                        }}
                    }}
                }} catch (e) {{}}
            }}

            setInterval(pollProgress, 1200);
            pollProgress();
        </script>
    </body>
    </html>
    """

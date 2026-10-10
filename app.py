"""OpenShelf — a minimal local 'continue watching' shelf over folders of video files.

Native Tkinter window, single file, stdlib only. Scans folders you add, groups
sNNeNN files into shows, reads VLC's saved playback positions for Netflix-style
resume, and launches VLC to play.

Run:  python app.py   (or the packaged OpenShelf.exe — see README)
State: state.json next to this file. Nothing leaves the machine.
"""

__version__ = "0.3.0"

import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# frozen (PyInstaller) runs from a temp extraction dir — anchor state to the exe instead
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent

# under pythonw/windowed exe the std streams are None; anything that writes to them
# (Tk callback error reporting, thread excepthooks) kills the process silently
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(APP_DIR / "last-run.log", "a", encoding="utf-8", buffering=1)
elif hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

STATE_FILE = APP_DIR / "state.json"        # app-managed watch history
SETTINGS_FILE = APP_DIR / "settings.json"  # user-editable configuration

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".webm", ".ts", ".flv", ".mpg", ".mpeg"}
SKIP_DIR_NAMES = {"apps", "books", "games", "extras", "featurettes", "samples", "sample", "subs", "subtitles"}

EP_PATTERNS = [
    re.compile(r"[Ss](\d{1,2})[ ._-]?[Ee](\d{1,3})"),
    re.compile(r"(?<!\d)(\d{1,2})x(\d{2,3})(?!\d)"),
    re.compile(r"[Ee]pisode[ ._-]?(\d{1,3})"),
]
JUNK_SPLIT = re.compile(
    r"\b(1080p|720p|2160p|480p|4k|uhd|bluray|blu-ray|brrip|bdrip|web[ ._-]?dl|webrip|hdtv|hdrip|"
    r"x264|x265|h[ .]?264|h[ .]?265|hevc|avc|10bit|8bit|hdr|sdr|remastered|proper|repack\d?|"
    r"dvdrip|amzn|atvp|nf|hulu|dsnp|ddp?[ .]?\d|aac\d?|ac3|truehd|atmos|dts|extended|unrated|"
    r"remux|limited|multi|dubbed|subbed)\b",
    re.IGNORECASE,
)
YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
QUALITY_RE = re.compile(r"\b(2160p|1080p|720p|480p)\b", re.IGNORECASE)

VLC_CANDIDATES = [
    r"C:\Program Files\VideoLAN\VLC\vlc.exe",
    r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
    "/Applications/VLC.app/Contents/MacOS/VLC",
    "/usr/bin/vlc",
    "/usr/local/bin/vlc",
    "/snap/bin/vlc",
]


def find_vlc():
    override = SETTINGS.get("vlc_path", "")
    if override and os.path.exists(override):
        return override
    for p in VLC_CANDIDATES:
        if os.path.exists(p):
            return p
    import shutil
    return shutil.which("vlc")


# ---------- settings + state ----------

DEFAULT_SETTINGS = {
    "folders": [],
    "vlc_path": "",
    "video_extensions": sorted(VIDEO_EXTS),
    "skip_dirs": sorted(SKIP_DIR_NAMES),
    "rewind_seconds": 3,
    "sort_mode": "recent",
    "hide_watched": False,
    "finish_remaining_seconds": 180,  # this close to the end counts as finished (credits)
    "finish_percent": 90,             # ...or this much of it watched
}


def _read_json(path):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def load_settings():
    s = dict(DEFAULT_SETTINGS)
    s.update({k: v for k, v in _read_json(SETTINGS_FILE).items() if k in DEFAULT_SETTINGS})
    return s


def save_settings(settings):
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")


def load_state():
    st = _read_json(STATE_FILE)
    st.setdefault("history", {})
    st.setdefault("suppress", {})  # paths whose VLC resume point is ignored (manual marks)
    st.setdefault("show_prefs", {})  # show key -> {"audio", "sub", "sub_margin"} applied at launch
    return st


def save_state(state):
    STATE_FILE.write_text(
        json.dumps({"history": state["history"], "suppress": state["suppress"],
                    "show_prefs": state["show_prefs"]},
                   indent=2, ensure_ascii=False),
        encoding="utf-8")


SETTINGS = load_settings()
STATE = load_state()
# migrate folders out of a pre-0.1 combined state.json
_old = _read_json(STATE_FILE)
if _old.get("folders") and not SETTINGS["folders"]:
    SETTINGS["folders"] = _old["folders"]
    save_state(STATE)
if not SETTINGS_FILE.exists():
    save_settings(SETTINGS)
STATE_LOCK = threading.Lock()


# ---------- VLC resume positions ----------

def _vlc_ini_path():
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", "")) / "vlc" / "vlc-qt-interface.ini"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Preferences" / "org.videolan.vlc" / "vlc-qt-interface.ini"
    return Path.home() / ".config" / "vlc" / "vlc-qt-interface.ini"


def vlc_positions():
    """Map absolute lowercase path -> (resume_seconds, recency_rank). Rank 0 = most recent."""
    ini = _vlc_ini_path()
    out = {}
    if not ini.exists():
        return out
    try:
        text = ini.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return out
    m = re.search(r"\[RecentsMRL\](.*?)(?=\n\[|\Z)", text, re.DOTALL)
    if not m:
        return out
    section = m.group(1)
    list_m = re.search(r"^list=(.*)$", section, re.MULTILINE)
    times_m = re.search(r"^times=(.*)$", section, re.MULTILINE)
    if not list_m:
        return out
    mrls = re.split(r",\s*(?=[A-Za-z][A-Za-z0-9+.-]*://)", list_m.group(1).strip())
    times = [t.strip() for t in times_m.group(1).split(",")] if times_m else []
    for i, mrl in enumerate(mrls):
        if not mrl.lower().startswith("file:///"):
            continue
        if sys.platform == "win32":
            path = urllib.parse.unquote(mrl[8:]).replace("/", "\\")
        else:
            path = urllib.parse.unquote(mrl[7:])
        try:
            ms = int(times[i]) if i < len(times) else 0
        except ValueError:
            ms = 0
        out[path.lower()] = (max(ms // 1000, 0), i)
    return out


# ---------- media probe (duration + tracks, stdlib) ----------

LANG_NAMES = {
    "eng": "English", "en": "English", "jpn": "Japanese", "ja": "Japanese",
    "spa": "Spanish", "es": "Spanish", "fre": "French", "fra": "French", "fr": "French",
    "ger": "German", "deu": "German", "de": "German", "ita": "Italian", "it": "Italian",
    "por": "Portuguese", "pt": "Portuguese", "kor": "Korean", "ko": "Korean",
    "chi": "Chinese", "zho": "Chinese", "zh": "Chinese", "rus": "Russian", "ru": "Russian",
    "und": "Unknown",
}

_EBML_MASTERS = {0x18538067, 0x1549A966, 0x1654AE6B, 0xAE}  # Segment, Info, Tracks, TrackEntry


def _ebml_id(f):
    b = f.read(1)
    if not b:
        return None
    n = next((i for i in range(4) if b[0] & (0x80 >> i)), None)
    if n is None:
        return None
    return int.from_bytes(b + f.read(n), "big")


def _ebml_size(f):
    b = f.read(1)
    if not b:
        return None
    n = next((i for i in range(8) if b[0] & (0x80 >> i)), None)
    if n is None:
        return None
    val = b[0] & (0xFF >> (n + 1))
    for c in f.read(n):
        val = (val << 8) | c
    return -1 if val == (1 << (7 * (n + 1))) - 1 else val  # -1 = unknown size


def _probe_mkv(path):
    import io
    import struct
    out = {"duration": 0, "tracks": []}
    with open(path, "rb") as f:
        if _ebml_id(f) != 0x1A45DFA3:
            return None
        f.seek(_ebml_size(f), 1)
        if _ebml_id(f) != 0x18538067:
            return None
        _ebml_size(f)
        scale, dur, got = 1_000_000, 0.0, set()
        while len(got) < 2 and f.tell() < 64 * 1024 * 1024:
            eid = _ebml_id(f)
            size = _ebml_size(f)
            if eid is None or size is None or size < 0 or eid == 0x1F43B675:  # Cluster: headers are done
                break
            if eid not in (0x1549A966, 0x1654AE6B):
                f.seek(size, 1)
                continue
            got.add(eid)
            body = io.BytesIO(f.read(size))
            while True:
                cid = _ebml_id(body)
                csize = _ebml_size(body)
                if cid is None or csize is None or csize < 0:
                    break
                data = body.read(csize)
                if eid == 0x1549A966:  # Info
                    if cid == 0x2AD7B1:
                        scale = int.from_bytes(data, "big")
                    elif cid == 0x4489:
                        dur = struct.unpack(">f" if len(data) == 4 else ">d", data)[0]
                elif cid == 0xAE:  # TrackEntry
                    t = {"type": 0, "lang": "eng", "name": "", "default": True, "forced": False}
                    te = io.BytesIO(data)
                    while True:
                        tid = _ebml_id(te)
                        tsize = _ebml_size(te)
                        if tid is None or tsize is None or tsize < 0:
                            break
                        v = te.read(tsize)
                        if tid == 0x83:
                            t["type"] = int.from_bytes(v, "big")
                        elif tid == 0x22B59C:
                            t["lang"] = v.decode("utf-8", "replace").strip("\0") or "eng"
                        elif tid == 0x536E:
                            t["name"] = v.decode("utf-8", "replace").strip("\0")
                        elif tid == 0x88:
                            t["default"] = bool(int.from_bytes(v, "big"))
                        elif tid == 0x55AA:
                            t["forced"] = bool(int.from_bytes(v, "big"))
                    kind = {2: "audio", 0x11: "sub"}.get(t["type"])
                    if kind:
                        out["tracks"].append({"kind": kind, "lang": t["lang"], "name": t["name"],
                                              "default": t["default"], "forced": t["forced"]})
        out["duration"] = int(dur * scale / 1e9)
    return out


def _probe_mp4(path):
    """Duration from moov/mvhd. Track listing is mkv-only; mp4 falls back to language options."""
    import struct
    with open(path, "rb") as f:
        end = os.fstat(f.fileno()).st_size
        while f.tell() < end:
            hdr = f.read(8)
            if len(hdr) < 8:
                return None
            size, typ = struct.unpack(">I4s", hdr)
            if size == 1:
                size = struct.unpack(">Q", f.read(8))[0] - 16
            else:
                size = (end - f.tell()) if size == 0 else size - 8
            if typ != b"moov":
                f.seek(size, 1)
                continue
            moov = f.read(min(size, 64 * 1024 * 1024))
            i = moov.find(b"mvhd")
            if i < 0:
                return None
            body = moov[i + 4:]
            if body[0] == 1:
                timescale, dur = struct.unpack(">IQ", body[20:32])
            else:
                timescale, dur = struct.unpack(">II", body[12:20])
            return {"duration": int(dur / timescale) if timescale else 0, "tracks": []}
    return None


PROBE_CACHE = {}


def probe(path):
    """{'duration': seconds, 'tracks': [...]} or None. Reads only container headers; cached."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path.lower(), st.st_size, int(st.st_mtime))
    if key in PROBE_CACHE:
        return PROBE_CACHE[key]
    ext = Path(path).suffix.lower()
    try:
        info = (_probe_mkv(path) if ext in (".mkv", ".webm")
                else _probe_mp4(path) if ext in (".mp4", ".m4v", ".mov") else None)
    except Exception:
        info = None
    PROBE_CACHE[key] = info
    return info


def near_end(path, resume):
    """True when the saved position is in the credits: few minutes left or most of it watched."""
    if not resume:
        return False
    info = probe(path)
    dur = info["duration"] if info else 0
    if dur <= 0 or resume > dur:
        return False
    left = dur - resume
    return (left <= int(SETTINGS.get("finish_remaining_seconds", 180))
            or resume * 100 >= dur * int(SETTINGS.get("finish_percent", 90)))


def track_label(t, i):
    lang = LANG_NAMES.get(t["lang"].lower(), t["lang"])
    name = t["name"] or lang
    if lang.lower() not in name.lower():
        name += f" ({lang})"
    return f"{i + 1}. {name}"


# ---------- scanning / grouping ----------

def _clean(name):
    s = re.sub(r"[._]+", " ", name)
    s = re.sub(r"[\[\(].*?[\]\)]", " ", s)  # bracketed junk
    s = JUNK_SPLIT.split(s)[0]
    s = re.sub(r"\s+", " ", s).strip(" -–")
    return s.strip()


def _norm_key(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def parse_episode(filename):
    """Return (season, episode, name_before_pattern) or None."""
    stem = Path(filename).stem
    for i, pat in enumerate(EP_PATTERNS):
        m = pat.search(stem)
        if m:
            if i == 2:  # Episode NN — assume season 1
                season, ep = 1, int(m.group(1))
            else:
                season, ep = int(m.group(1)), int(m.group(2))
            return season, ep, stem[: m.start()]
    return None


def scan_folders(folders):
    """Walk folders, return (shows, movies)."""
    files_by_dir = {}
    for root in folders:
        root = Path(root)
        if not root.exists():
            continue
        exts = {e.lower() if e.startswith(".") else "." + e.lower()
                for e in SETTINGS["video_extensions"]}
        skip = {d.lower() for d in SETTINGS["skip_dirs"]}
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d.lower() not in skip]
            vids = [f for f in filenames
                    if Path(f).suffix.lower() in exts
                    and "sample" not in f.lower() and "trailer" not in f.lower()]
            if vids:
                files_by_dir[dirpath] = vids

    shows = {}       # key -> {"names": Counter, "episodes": [...]}
    movie_files = [] # (path, filename, parent_dir)

    for dirpath, vids in files_by_dir.items():
        for f in vids:
            full = os.path.join(dirpath, f)
            parsed = parse_episode(f)
            if parsed:
                season, ep, prefix = parsed
                name = _clean(prefix)
                if len(name) < 2:
                    name = _clean(Path(dirpath).name)
                    # dir names often carry their own Sxx tokens; cut there too
                    name = re.sub(r"\b[Ss]\d{1,2}([ -]?[Ss]?\d{1,2})?\b.*$", "", name).strip()
                    name = name or "Unknown Show"
                name = re.sub(r"\s*\b(19|20)\d{2}\b\s*$", "", name).strip() or name
                key = _norm_key(name) or "unknown"
                entry = shows.setdefault(key, {"names": Counter(), "episodes": []})
                entry["names"][name] += 1
                entry["episodes"].append({"season": season, "ep": ep, "path": full, "file": f})
            else:
                movie_files.append((full, f, dirpath))

    # multi-video dirs with no episode patterns => collection (hand-numbered anime packs etc.)
    leftovers = []
    by_parent = {}
    for full, f, dirpath in movie_files:
        by_parent.setdefault(dirpath, []).append((full, f))
    for dirpath, items in by_parent.items():
        if len(items) >= 3 and not any(YEAR_RE.search(f) for _, f in items):
            name = _clean(Path(dirpath).name)
            name = re.sub(r"\b\d{1,3}\s*-\s*\d{1,3}\b.*$", "", name).strip()
            name = re.sub(r"\b[Ee]pisodes?\b.*$", "", name).strip()
            name = re.sub(r"\s*\b(19|20)\d{2}\b\s*$", "", name).strip()
            if len(name) < 3:
                name = _clean(Path(dirpath).parent.name) or name or "Unknown Show"
            # "c:" namespace keeps hand-numbered packs from colliding with sNNeNN rips of the same show
            key = "c:" + (_norm_key(name) or "unknown")
            entry = shows.setdefault(key, {"names": Counter(), "episodes": []})
            entry["names"][name] += 1
            for i, (full, f) in enumerate(sorted(items, key=lambda x: x[1].lower()), 1):
                num = re.search(r"(\d{1,3})\s*$", _clean(Path(f).stem))
                epno = int(num.group(1)) if num else i
                entry["episodes"].append({"season": 1, "ep": epno, "path": full, "file": f})
        else:
            leftovers.extend(items)

    # movies, deduped by (title, year) with quality disambiguation
    movies = {}
    for full, f in leftovers:
        stem = Path(f).stem
        year_m = YEAR_RE.search(stem)
        year = year_m.group(1) if year_m else None
        title = _clean(stem[: year_m.start()] if year_m else stem)
        if len(title) < 2:
            title = _clean(Path(full).parent.name)
            ym2 = YEAR_RE.search(Path(full).parent.name)
            if not year and ym2:
                year = ym2.group(1)
        qual_m = QUALITY_RE.search(f)
        movies.setdefault((_norm_key(title), year), []).append({
            "title": title or "Unknown",
            "year": year,
            "quality": qual_m.group(1) if qual_m else None,
            "path": full,
        })

    movie_list = []
    for versions in movies.values():
        versions.sort(key=lambda v: v["path"].lower())
        for v in versions:
            v["tag"] = v["quality"] if len(versions) > 1 else None
            movie_list.append(v)

    show_list = {}
    for key, entry in shows.items():
        eps = sorted(entry["episodes"], key=lambda e: (e["season"], e["ep"], e["file"].lower()))
        seen, unique = set(), []
        for e in eps:
            k = (e["season"], e["ep"])
            if k in seen:
                continue
            seen.add(k)
            unique.append(e)
        show_list[key] = {"title": entry["names"].most_common(1)[0][0], "episodes": unique}
    return show_list, movie_list


SCAN_CACHE = {"at": 0.0, "shows": {}, "movies": []}
SCAN_LOCK = threading.Lock()


def get_library(force=False):
    with SCAN_LOCK:
        if force or time.time() - SCAN_CACHE["at"] > 300:
            with STATE_LOCK:
                folders = list(SETTINGS["folders"])
            shows, movies = scan_folders(folders)
            SCAN_CACHE.update(at=time.time(), shows=shows, movies=movies)
        return SCAN_CACHE["shows"], SCAN_CACHE["movies"]


# ---------- library assembly (merge scan + history + VLC positions) ----------

def fmt_time(sec):
    if sec >= 3600:
        return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"
    return f"{sec // 60}:{sec % 60:02d}"


def _ini_mtime():
    try:
        return datetime.fromtimestamp(_vlc_ini_path().stat().st_mtime)
    except OSError:
        return None


def build_payload():
    shows, movies = get_library()
    pos = vlc_positions()
    ini_at = _ini_mtime()
    with STATE_LOCK:
        history = {k: dict(v) for k, v in STATE["history"].items()}
        suppress = dict(STATE["suppress"])
        folders = list(SETTINGS["folders"])
    latest = max(history, key=lambda k: history[k].get("last_played", ""), default=None)

    def playing(path):
        """Launched after VLC last saved anything: VLC is (probably) still open on it."""
        h = history.get(path.lower())
        if not h or path.lower() != latest or not ini_at:
            return False
        try:
            return datetime.fromisoformat(h["last_played"]) > ini_at
        except (KeyError, ValueError):
            return False

    newly_done = []

    def done_of(path, r):
        """Watched for real: marked by hand, stopped in the credits, or played through.
        Opening a file for a few seconds is not watching it."""
        lp = path.lower()
        h = history.get(lp)
        if not h:
            return False
        if h.get("manual") or h.get("finished"):
            return True
        fin = near_end(path, r)
        if not fin and not r and lp == latest and lp in pos and ini_at:
            # VLC drops the resume point when a file plays to the end; time the session instead
            try:
                ran = (ini_at - datetime.fromisoformat(h["last_played"])).total_seconds()
                info = probe(path)
                fin = bool(info and info["duration"] and ran >= 0.8 * info["duration"])
            except (KeyError, ValueError):
                pass
        if fin:
            h["finished"] = True
            newly_done.append(lp)
        return fin

    def resume_of(path):
        lp = path.lower()
        if lp in suppress:
            return 0
        p = pos.get(lp)
        return p[0] if p and p[0] > 0 else 0

    def hist_of(path):
        return history.get(path.lower())

    out_shows = []
    for key, s in shows.items():
        eps = []
        last_played_idx, last_played_ts = None, ""
        for i, e in enumerate(s["episodes"]):
            h = hist_of(e["path"])
            r = resume_of(e["path"])
            ts = h["last_played"] if h else ""
            if ts and ts >= last_played_ts:
                last_played_ts, last_played_idx = ts, i
            eps.append({
                "season": e["season"], "ep": e["ep"],
                "label": f"S{e['season']:02d}E{e['ep']:02d}",
                "file": e["file"], "path": e["path"],
                "resume": r, "resume_h": fmt_time(r) if r else None,
                "played": bool(h), "finished": near_end(e["path"], r),
                "done": done_of(e["path"], r), "playing": playing(e["path"]),
            })
        next_up = eps[0]
        if last_played_idx is not None:
            cur = eps[last_played_idx]
            if not cur["done"]:
                next_up = cur
            elif last_played_idx + 1 < len(eps):
                next_up = eps[last_played_idx + 1]
            else:
                next_up = cur
        seasons = sorted({e["season"] for e in eps})
        out_shows.append({
            "kind": "show", "key": key, "title": s["title"],
            "ep_count": len(eps), "season_count": len(seasons),
            "next": next_up, "last_played": last_played_ts,
            "episodes": eps,
        })
    out_shows.sort(key=lambda s: s["title"].lower())

    out_movies = []
    for m in movies:
        h = hist_of(m["path"])
        r = resume_of(m["path"])
        out_movies.append({
            "kind": "movie", "title": m["title"], "year": m["year"], "tag": m["tag"],
            "path": m["path"], "resume": r, "resume_h": fmt_time(r) if r else None,
            "played": bool(h), "last_played": h["last_played"] if h else "",
            "finished": near_end(m["path"], r), "done": done_of(m["path"], r),
            "playing": playing(m["path"]),
        })
    out_movies.sort(key=lambda m: (m["title"].lower(), m["year"] or ""))

    # continue-watching shelf: app history first (true recency), then VLC-only recents
    cont = []
    used = set()
    hist_items = []
    path_index = {}
    for s in out_shows:
        for e in s["episodes"]:
            path_index[e["path"].lower()] = (s, e)
    for m in out_movies:
        path_index[m["path"].lower()] = (None, m)

    for lp, h in history.items():
        if lp in path_index and os.path.exists(h.get("path", "")):
            hist_items.append((h["last_played"], lp))
    hist_items.sort(reverse=True)
    vlc_only = sorted(
        (rank, lp) for lp, (sec, rank) in pos.items()
        if lp in path_index and lp not in suppress and lp not in {p for _, p in hist_items}
    )
    seen_titles = set()
    for lp in [p for _, p in hist_items] + [p for _, p in vlc_only]:
        if lp in used:
            continue
        s, item = path_index[lp]
        title_key = _norm_key(s["title"] if s else item["title"] + (item.get("year") or ""))
        if title_key in seen_titles:
            continue
        seen_titles.add(title_key)
        if s is not None:
            if s["key"] in used:
                continue
            used.add(s["key"])
            used.add(lp)
            nxt = _next_after(s, item)
            alt = None
            if item["playing"]:
                sub, play = f"Watching {item['label']} now · position saves when VLC closes", item
            elif item["done"] and nxt is not item:
                sub, play = f"Finished {item['label']} · up next {nxt['label']}", nxt
                alt = item if item["resume"] else None
            elif item["resume"]:
                sub, play = f"Resume {item['label']} · {item['resume_h']}", item
            elif item["done"]:
                sub, play = f"Watched {item['label']}", item
            else:
                sub, play = f"Started {item['label']}", item
            cont.append({"kind": "show", "title": s["title"], "key": s["key"],
                         "sub": sub, "play": play, "alt": alt})
        else:
            used.add(lp)
            if item["playing"]:
                sub = "Watching now · position saves when VLC closes"
            elif item["done"]:
                sub = f"Finished · stopped at {item['resume_h']}" if item["resume"] else "Watched"
            elif item["resume"]:
                sub = f"Resume · {item['resume_h']}"
            else:
                sub = "Watched"
            cont.append({
                "kind": "movie", "title": item["title"] + (f" ({item['year']})" if item["year"] else ""),
                "sub": sub, "play": item, "alt": None,
            })
        if len(cont) >= 12:
            break

    if newly_done:  # remember it even after VLC's 30-entry recents list forgets the file
        with STATE_LOCK:
            for lp in newly_done:
                if lp in STATE["history"]:
                    STATE["history"][lp]["finished"] = True
            save_state(STATE)

    return {"folders": folders, "vlc": bool(find_vlc()), "continue": cont,
            "shows": out_shows, "movies": out_movies}


def _next_after(show, ep):
    eps = show["episodes"]
    for i, e in enumerate(eps):
        if e["path"] == ep["path"]:
            return eps[i + 1] if i + 1 < len(eps) else e
    return ep


# ---------- playback ----------

def _pick_track(tracks, want):
    """Index (0-based, within its kind) of the saved track in this file, or None."""
    for test in (lambda t: t["lang"] == want.get("lang") and t["name"] == want.get("name"),
                 lambda t: want.get("name") and t["name"] == want.get("name")):
        for i, t in enumerate(tracks):
            if test(t):
                return i
    i = want.get("index")
    if isinstance(i, int) and 0 <= i < len(tracks):
        return i
    return None


def playback_args(path, prefs):
    if not prefs:
        return []
    info = probe(path) or {"tracks": []}
    args = []
    for kind in ("audio", "sub"):
        want = prefs.get(kind)
        if not want:
            continue
        if want == "off":
            args.append("--no-spu")
            continue
        idx = _pick_track([t for t in info["tracks"] if t["kind"] == kind], want)
        if idx is not None:
            args.append(f"--{kind}-track={idx}")
        elif want.get("lang"):
            args.append(f"--{kind}-language={want['lang']}")
    margin = int(prefs.get("sub_margin") or 0)
    if margin:
        args.append(f"--sub-margin={margin}")
    return args


def launch(path, resume, show_key=None):
    if not os.path.exists(path):
        return {"ok": False, "error": "File not found:\n" + path}
    vlc = find_vlc()
    if not vlc:
        return {"ok": False, "error": "VLC not found — install VLC, or set \"vlc_path\" in settings.json"}
    rewind = int(SETTINGS.get("rewind_seconds", 3))
    with STATE_LOCK:
        prefs = dict(STATE["show_prefs"].get(show_key, {})) if show_key else {}
    cmd = [vlc, path] + playback_args(path, prefs)
    if resume and resume > rewind:
        cmd.append(f"--start-time={max(resume - rewind, 0)}")  # back up a little for context
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with STATE_LOCK:
        lp = path.lower()
        h = STATE["history"].setdefault(lp, {"play_count": 0, "path": path})
        h["path"] = path
        h["play_count"] += 1
        h["last_played"] = datetime.now().isoformat(timespec="seconds")
        STATE["suppress"].pop(lp, None)  # actually playing re-enables VLC resume tracking
        save_state(STATE)
    return {"ok": True}


def set_watched(paths, watched):
    """Manually flag files watched/unwatched. Both directions suppress any stale
    VLC resume point; playing the file again lifts the suppression."""
    with STATE_LOCK:
        base = datetime.now()
        for i, p in enumerate(paths):
            lp = p.lower()
            if watched:
                h = STATE["history"].setdefault(lp, {"play_count": 0, "path": p})
                h["path"] = p
                h["manual"] = True
                # microsecond ladder keeps mark-order == episode-order for next-up logic
                h["last_played"] = (base + timedelta(microseconds=i)).isoformat()
            else:
                STATE["history"].pop(lp, None)
            STATE["suppress"][lp] = True
        save_state(STATE)


# ---------- UI ----------

BG = "#0e0f12"
CARD = "#191b20"
CARD2 = "#22252c"
FG = "#e8e6e0"
DIM = "#8a8f99"
ACC = "#e5a00d"
GREEN = "#5fae6e"

FONT = ("Segoe UI", 10)
FONT_B = ("Segoe UI", 10, "bold")
FONT_SM = ("Segoe UI", 9)
FONT_H = ("Segoe UI", 14, "bold")


def dark_title_bar(win):
    """Ask DWM for a dark title bar (Windows 10 20H1+ / 11). No-op elsewhere."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        if not hwnd:
            return
        value = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE, pre-20H1 fallback
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                break
    except Exception:
        pass


class ScrollFrame(tk.Frame):
    """Vertical scrollable frame (canvas + inner frame + mousewheel)."""

    def __init__(self, master):
        super().__init__(master, bg=BG)
        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0)
        vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=BG)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=vsb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.inner.bind("<Configure>",
                        lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>",
                         lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._wheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

    def _wheel(self, e):
        if self.inner.winfo_height() <= self.canvas.winfo_height():
            return  # content fits — nothing to scroll
        self.canvas.yview_scroll(int(-e.delta / 120) * 3, "units")

    def clear(self, reset=True):
        pos = self.canvas.yview()[0]
        for c in self.inner.winfo_children():
            c.destroy()
        if reset:
            self.canvas.yview_moveto(0)
        else:  # re-render of the same view: stay where the user was
            self.after(30, lambda: self.canvas.yview_moveto(pos))

    def scroll_to(self, widget):
        self.update_idletasks()
        h = self.inner.winfo_height()
        if h > self.canvas.winfo_height():
            self.canvas.yview_moveto(max(widget.winfo_y() - 8, 0) / h)


def make_button(parent, text, cmd, primary=False):
    return tk.Button(
        parent, text=text, command=cmd, relief="flat", cursor="hand2",
        bg=ACC if primary else CARD2, fg="#141414" if primary else FG,
        activebackground="#f0b429" if primary else "#2c303a",
        activeforeground="#141414" if primary else FG,
        font=FONT_B if primary else FONT_SM, padx=12, pady=3, bd=0,
    )


def make_row(parent, title, sub=None, badge=None, buttons=(), on_click=None,
             on_context=None, dim_title=False):
    f = tk.Frame(parent, bg=CARD, padx=12, pady=8)
    f.pack(fill="x", padx=10, pady=3)
    right = tk.Frame(f, bg=CARD)
    right.pack(side="right", padx=(8, 0))
    for text, cmd, primary in buttons:
        make_button(right, text, cmd, primary).pack(side="right", padx=(8, 0))
    left = tk.Frame(f, bg=CARD)
    left.pack(side="left", fill="x", expand=True)
    labels = [tk.Label(left, text=title, bg=CARD, fg=DIM if dim_title else FG,
                       font=FONT_B, anchor="w")]
    labels[0].pack(fill="x")
    if sub:
        labels.append(tk.Label(left, text=sub, bg=CARD, fg=DIM, font=FONT_SM, anchor="w"))
        labels[-1].pack(fill="x")
    if badge:
        labels.append(tk.Label(left, text=badge, bg=CARD, fg=ACC, font=FONT_SM, anchor="w"))
        labels[-1].pack(fill="x")
    if on_click:
        for w in [f, left] + labels:
            w.bind("<Button-1>", lambda e: on_click())
            w.configure(cursor="hand2")
    if on_context:
        for w in [f, left] + labels:
            w.bind("<Button-3>", on_context)
    return f


class OpenShelf(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("OpenShelf")
        self.geometry("980x720")
        self.minsize(640, 480)
        self.configure(bg=BG)

        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(8, 6, 8, 0))
        style.configure("TNotebook.Tab", background=CARD, foreground=DIM,
                        padding=(13, 5), font=FONT, borderwidth=0)
        style.map("TNotebook.Tab",
                  background=[("selected", CARD2)],
                  foreground=[("selected", ACC)],
                  padding=[("selected", (22, 9))],
                  expand=[("selected", (2, 2, 2, 2))])
        style.configure("Vertical.TScrollbar", background=CARD2, troughcolor=BG,
                        bordercolor=BG, arrowcolor=DIM)
        style.configure("TCombobox", fieldbackground=CARD2, background=CARD2, foreground=FG,
                        arrowcolor=FG, bordercolor=CARD2, lightcolor=CARD2, darkcolor=CARD2)
        style.map("TCombobox", fieldbackground=[("readonly", CARD2)],
                  foreground=[("readonly", FG)], selectbackground=[("readonly", CARD2)],
                  selectforeground=[("readonly", FG)])
        self.option_add("*TCombobox*Listbox.background", CARD2)
        self.option_add("*TCombobox*Listbox.foreground", FG)
        self.option_add("*TCombobox*Listbox.selectBackground", ACC)
        self.option_add("*TCombobox*Listbox.selectForeground", "#141414")

        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", padx=14, pady=(12, 6))
        tk.Label(top, text="OpenShelf", bg=BG, fg=FG, font=FONT_H).pack(side="left")
        self.q = tk.StringVar()
        self.q.trace_add("write", lambda *a: self.render())
        entry = tk.Entry(top, textvariable=self.q, bg=CARD, fg=FG, insertbackground=FG,
                         relief="flat", font=FONT)
        entry.pack(side="left", fill="x", expand=True, padx=12, ipady=6)
        make_button(top, "Rescan", lambda: self.refresh(force=True)).pack(side="left")

        self.nb = ttk.Notebook(self)
        self.tabs = {}
        for name in ("Continue", "Shows", "Movies", "Folders"):
            sf = ScrollFrame(self.nb)
            self.tabs[name] = sf
            self.nb.add(sf, text=name)
        self.nb.pack(fill="both", expand=True, padx=8, pady=6)

        self.status = tk.Label(self, text="", bg=BG, fg=DIM, font=FONT_SM, anchor="w")
        self.status.pack(fill="x", padx=14, pady=(0, 8))

        self.payload = None
        self._last_refresh = 0.0
        self._busy = False
        self._expanded = set()   # show keys whose episode list is open inline
        self._scroll_to = None   # show key to bring into view after the next render
        self._last_q = ""
        self.bind("<FocusIn>", self._on_focus)
        dark_title_bar(self)
        self.refresh(force=True)

    # -- data --

    def _on_focus(self, event):
        # coming back from VLC: re-read its saved positions (cheap; no disk rescan)
        if event.widget is self and time.time() - self._last_refresh > 5 and not self._busy:
            self.refresh()

    def refresh(self, force=False):
        if self._busy:
            return
        self._busy = True
        self._last_refresh = time.time()
        self.status.config(text="Scanning…" if force else "Refreshing…")

        def work():
            try:
                if force:
                    with STATE_LOCK:
                        SETTINGS.update(load_settings())  # pick up hand-edits to settings.json
                get_library(force=force)
                payload = build_payload()
            except Exception:
                payload = {"error": traceback.format_exc()}
            self.after(0, lambda: self._apply(payload))

        threading.Thread(target=work, daemon=True).start()

    def _apply(self, payload):
        self._busy = False
        if "error" in payload:
            self.status.config(text="Scan failed — see error dialog")
            messagebox.showerror("OpenShelf", payload["error"][-1500:])
            return
        self.payload = payload
        self.render()

    # -- rendering --

    def render(self):
        if not self.payload:
            return
        p = self.payload
        q = self.q.get().strip().lower()
        reset = q != self._last_q
        self._last_q = q
        match = lambda s: not q or q in s.lower()
        sort_recent = SETTINGS.get("sort_mode", "recent") == "recent"
        hide_watched = SETTINGS.get("hide_watched", False)

        if not p["folders"]:
            for name in ("Continue", "Shows", "Movies"):
                tab = self.tabs[name]
                tab.clear(reset)
                self._empty_state(tab.inner)
            self._render_folders(p)
            self.status.config(text="Add a folder to get started.")
            return

        cont = self.tabs["Continue"]
        cont.clear(reset)
        items = [c for c in p["continue"] if match(c["title"])]
        for c in items:
            play, alt, key = c["play"], c.get("alt"), c.get("key")
            resuming = play["resume"] and not play.get("done")
            btn_label = "▶ Resume" if resuming else ("▶ Next" if alt else "▶ Play")
            if c["kind"] == "show":
                btn_label += " " + play["label"]
            buttons = [(btn_label, lambda pl=play, k=key: self.play(pl, k), True)]
            if alt:
                buttons.append((f"Resume {alt['label']} · {alt['resume_h']}",
                                lambda pl=alt, k=key: self.play(pl, k), False))
            make_row(cont.inner, c["title"], sub=c["sub"], buttons=buttons,
                     on_click=(lambda k=key: self.expand_show(k)) if c["kind"] == "show" else
                              (lambda pl=play: self.play(pl)))
        if not items:
            tk.Label(cont.inner, text="Nothing yet — play something.",
                     bg=BG, fg=DIM, font=FONT).pack(pady=18)

        shows_tab = self.tabs["Shows"]
        shows_tab.clear(reset and not self._scroll_to)
        self._controls_row(shows_tab.inner, sort_recent, hide_watched)
        shown = [s for s in p["shows"] if match(s["title"])]
        if hide_watched:
            shown = [s for s in shown
                     if not all(e["done"] for e in s["episodes"])]
        shown.sort(key=lambda s: s["title"].lower())
        if sort_recent:
            shown.sort(key=lambda s: s["last_played"], reverse=True)
        for s in shown:
            nxt = s["next"]
            badge = None
            if nxt["resume"] and not nxt["done"]:
                badge = f"⏸ {nxt['label']} · {nxt['resume_h']}"
            elif s["last_played"]:
                badge = f"Next: {nxt['label']}"
            sub = f"{s['ep_count']} episode{'s' if s['ep_count'] > 1 else ''}"
            if s["season_count"] > 1:
                sub += f" · {s['season_count']} seasons"
            open_ = s["key"] in self._expanded
            row = make_row(shows_tab.inner, s["title"], sub=sub, badge=badge,
                     buttons=[("Episodes ▴" if open_ else "Episodes ▾",
                               lambda k=s["key"]: self.toggle_show(k), False),
                              (f"▶ {nxt['label']}", lambda pl=nxt, k=s["key"]: self.play(pl, k), True)],
                     on_click=lambda k=s["key"]: self.toggle_show(k),
                     on_context=lambda e, sh=s: self._menu(e, [
                         ("Mark all watched",
                          lambda: self._mark([x["path"] for x in sh["episodes"]], True)),
                         ("Mark all unwatched",
                          lambda: self._mark([x["path"] for x in sh["episodes"]], False)),
                     ]))
            if open_:
                self._show_panel(shows_tab.inner, s)
            if self._scroll_to == s["key"]:
                self.after(60, lambda r=row: shows_tab.scroll_to(r))
        self._scroll_to = None
        if not shown:
            tk.Label(shows_tab.inner, text="No shows found.", bg=BG, fg=DIM, font=FONT).pack(pady=18)

        movies_tab = self.tabs["Movies"]
        movies_tab.clear(reset)
        self._controls_row(movies_tab.inner, sort_recent, hide_watched)
        mshown = [m for m in p["movies"] if match(m["title"])]
        if hide_watched:
            mshown = [m for m in mshown if not m["done"]]
        mshown.sort(key=lambda m: (m["title"].lower(), m["year"] or ""))
        if sort_recent:
            mshown.sort(key=lambda m: m["last_played"], reverse=True)
        for m in mshown:
            title = m["title"]
            if m["year"]:
                title += f" ({m['year']})"
            if m["tag"]:
                title += f"  [{m['tag']}]"
            done = m["done"]
            if done:
                title += "  ✓"
            make_row(movies_tab.inner, title,
                     badge=f"⏸ {m['resume_h']}" if m["resume"] else None,
                     buttons=[("▶ Resume" if m["resume"] and not m["done"] else "▶ Play",
                               lambda pl=m: self.play(pl), True)],
                     on_click=lambda pl=m: self.play(pl),
                     on_context=lambda e, mv=m: self._menu(e, [
                         ("Mark watched", lambda: self._mark([mv["path"]], True)),
                         ("Mark unwatched", lambda: self._mark([mv["path"]], False)),
                     ]),
                     dim_title=done)
        if not mshown:
            tk.Label(movies_tab.inner, text="No movies found.", bg=BG, fg=DIM, font=FONT).pack(pady=18)

        self._render_folders(p)

        n_res = sum(1 for c in p["continue"] if c["play"]["resume"])
        vlc_note = "" if p["vlc"] else "   ⚠ VLC NOT FOUND — playback will fail"
        self.status.config(
            text=f"{len(p['shows'])} shows · {len(p['movies'])} movies · "
                 f"{n_res} resumable · positions update after you close VLC{vlc_note}")


    def _render_folders(self, p):
        folders_tab = self.tabs["Folders"]
        folders_tab.clear(False)
        bar = tk.Frame(folders_tab.inner, bg=BG)
        bar.pack(fill="x", padx=10, pady=(10, 4))
        make_button(bar, "+ Add folder…", self.add_folder, True).pack(side="left")
        make_button(bar, "Open settings.json", self.open_settings).pack(side="left", padx=(8, 0))
        tk.Label(folders_tab.inner,
                 text=(f"Settings: {SETTINGS_FILE}    VLC: {find_vlc() or 'NOT FOUND'}\n"
                       "Edit settings.json (video extensions, skipped dirs, VLC path, rewind), "
                       "save, then Rescan."),
                 bg=BG, fg=DIM, font=FONT_SM, justify="left", anchor="w").pack(
                     fill="x", padx=12, pady=(2, 8))
        for f in p["folders"]:
            make_row(folders_tab.inner, f,
                     buttons=[("Remove", lambda ff=f: self.remove_folder(ff), False)])

    def _controls_row(self, parent, sort_recent, hide_watched):
        bar = tk.Frame(parent, bg=BG)
        bar.pack(fill="x", padx=10, pady=(8, 2))
        make_button(bar, "Sort: Recent" if sort_recent else "Sort: A–Z",
                    self.toggle_sort).pack(side="left")
        make_button(bar, "Hide watched: on" if hide_watched else "Hide watched: off",
                    self.toggle_hide).pack(side="left", padx=(8, 0))

    def _empty_state(self, parent):
        box = tk.Frame(parent, bg=BG)
        box.pack(expand=True, fill="both", pady=70)
        tk.Label(box, text="No folders connected", bg=BG, fg=FG, font=FONT_H).pack(pady=(0, 6))
        tk.Label(box, text="Point OpenShelf at the folders where your videos live.",
                 bg=BG, fg=DIM, font=FONT).pack(pady=(0, 16))
        make_button(box, "＋ Add a video folder", self.add_folder, True).pack()

    def _menu(self, event, items):
        m = tk.Menu(self, tearoff=0, bg=CARD2, fg=FG, activebackground=ACC,
                    activeforeground="#141414", font=FONT_SM, bd=0)
        for label, cmd in items:
            m.add_command(label=label, command=cmd)
        m.tk_popup(event.x_root, event.y_root)

    def _mark(self, paths, watched):
        set_watched(paths, watched)
        self.refresh()

    def toggle_sort(self):
        with STATE_LOCK:
            SETTINGS["sort_mode"] = "az" if SETTINGS.get("sort_mode", "recent") == "recent" else "recent"
            save_settings(SETTINGS)
        self.render()

    def toggle_hide(self):
        with STATE_LOCK:
            SETTINGS["hide_watched"] = not SETTINGS.get("hide_watched", False)
            save_settings(SETTINGS)
        self.render()

    # -- actions --

    def play(self, item, show_key=None):
        res = launch(item["path"], item.get("resume") or 0, show_key)
        if not res["ok"]:
            messagebox.showerror("OpenShelf", res["error"])
            return
        self.status.config(text=f"Playing {os.path.basename(item['path'])} — "
                                "position saves when you close VLC")
        # pick up the new played-flag; VLC position lands later (on focus return)
        self.after(1200, lambda: self.refresh())

    def toggle_show(self, key):
        self._expanded.symmetric_difference_update({key})
        self.render()

    def expand_show(self, key):
        """Continue card click: open that show's episode list in the Shows tab."""
        if not key or not self.payload:
            return
        show = next((s for s in self.payload["shows"] if s["key"] == key), None)
        if not show:
            return
        self._expanded = {key}
        self._scroll_to = key
        self.nb.select(self.tabs["Shows"])
        if self.q.get().strip() and self.q.get().strip().lower() not in show["title"].lower():
            self.q.set("")  # trace re-renders
        else:
            self.render()

    def _show_panel(self, parent, show):
        panel = tk.Frame(parent, bg=BG)
        panel.pack(fill="x", padx=(30, 0), pady=(0, 8))
        self._prefs_row(panel, show)
        eps, season = show["episodes"], None
        for i, ep in enumerate(eps):
            if show["season_count"] > 1 and ep["season"] != season:
                season = ep["season"]
                tk.Label(panel, text=f"SEASON {season}", bg=BG, fg=DIM,
                         font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=14, pady=(10, 2))
            done = ep["done"]
            badge = None
            if ep["resume"]:
                badge = f"⏸ {ep['resume_h']}" + (" · finished" if ep["finished"] else "")
            make_row(panel, ep["label"] + ("  ✓" if done else ""), sub=ep["file"], badge=badge,
                     buttons=[("▶", lambda pl=ep, k=show["key"]: self.play(pl, k), True)],
                     on_context=lambda e, idx=i: self._menu(e, [
                         ("Mark watched",
                          lambda: self._mark([eps[idx]["path"]], True)),
                         ("Mark unwatched",
                          lambda: self._mark([eps[idx]["path"]], False)),
                         ("Mark watched up to here",
                          lambda: self._mark([x["path"] for x in eps[:idx + 1]], True)),
                     ]),
                     dim_title=done)

    def _prefs_row(self, parent, show):
        """Audio / subtitle track + subtitle height, saved per show, applied on every launch."""
        key = show["key"]
        sample = next((probe(e["path"]) for e in [show["next"]] + show["episodes"]
                       if probe(e["path"])), None)
        tracks = sample["tracks"] if sample else []
        with STATE_LOCK:
            prefs = dict(STATE["show_prefs"].get(key, {}))

        box = tk.Frame(parent, bg=CARD2, padx=12, pady=8)
        box.pack(fill="x", padx=10, pady=(2, 4))

        def save(field, value):
            with STATE_LOCK:
                cur = STATE["show_prefs"].setdefault(key, {})
                if value in (None, 0):
                    cur.pop(field, None)
                else:
                    cur[field] = value
                if not cur:
                    STATE["show_prefs"].pop(key, None)
                save_state(STATE)
            self.status.config(text=f"Saved for {show['title']} — applies next time you press play")

        for kind, label in (("audio", "Audio"), ("sub", "Subtitles")):
            ktracks = [t for t in tracks if t["kind"] == kind]
            labels = [track_label(t, i) for i, t in enumerate(ktracks)]
            values = ["VLC default"] + (["Off"] if kind == "sub" else []) + labels
            want = prefs.get(kind)
            if want == "off":
                current = "Off"
            elif want:
                idx = _pick_track(ktracks, want)
                current = labels[idx] if idx is not None else f"{LANG_NAMES.get(want.get('lang', ''), want.get('lang', '?'))} (saved)"
                if current not in values:
                    values.append(current)
            else:
                current = "VLC default"
            tk.Label(box, text=label, bg=CARD2, fg=DIM, font=FONT_SM).pack(side="left")
            var = tk.StringVar(value=current)
            cb = ttk.Combobox(box, textvariable=var, values=values, state="readonly",
                              width=max(14, min(30, max(len(v) for v in values) + 1)), font=FONT_SM)
            cb.pack(side="left", padx=(6, 16))

            def on_pick(_e, kind=kind, var=var, ktracks=ktracks, labels=labels):
                v = var.get()
                if v == "VLC default":
                    save(kind, None)
                elif v == "Off":
                    save(kind, "off")
                elif v in labels:
                    i = labels.index(v)
                    t = ktracks[i]
                    save(kind, {"lang": t["lang"], "name": t["name"], "index": i})
                cb_ref = _e.widget
                cb_ref.selection_clear()
            cb.bind("<<ComboboxSelected>>", on_pick)

        tk.Label(box, text="Subtitle height", bg=CARD2, fg=DIM, font=FONT_SM).pack(side="left")
        hv = tk.StringVar(value=str(int(prefs.get("sub_margin") or 0)))
        spin = tk.Spinbox(box, from_=0, to=600, increment=20, textvariable=hv, width=5,
                          bg=CARD, fg=FG, buttonbackground=CARD2, insertbackground=FG,
                          relief="flat", font=FONT_SM, justify="center")
        spin.pack(side="left", padx=(6, 4))
        tk.Label(box, text="px up", bg=CARD2, fg=DIM, font=FONT_SM).pack(side="left")

        def save_height(*_):
            try:
                v = max(0, min(600, int(hv.get())))
            except ValueError:
                return
            if v != int(prefs.get("sub_margin") or 0):
                prefs["sub_margin"] = v
                save("sub_margin", v)
        spin.configure(command=save_height)
        spin.bind("<Return>", save_height)
        spin.bind("<FocusOut>", save_height)
        if not tracks:
            tk.Label(box, text="  (no track list for this format — VLC picks)", bg=CARD2, fg=DIM,
                     font=FONT_SM).pack(side="left")

    def add_folder(self):
        d = filedialog.askdirectory(title="Add a video folder")
        if not d:
            return
        d = os.path.normpath(d)
        with STATE_LOCK:
            if d not in SETTINGS["folders"]:
                SETTINGS["folders"].append(d)
                save_settings(SETTINGS)
        self.refresh(force=True)

    def remove_folder(self, f):
        with STATE_LOCK:
            if f in SETTINGS["folders"]:
                SETTINGS["folders"].remove(f)
                save_settings(SETTINGS)
        self.refresh(force=True)

    def open_settings(self):
        if not SETTINGS_FILE.exists():
            save_settings(SETTINGS)
        try:
            os.startfile(SETTINGS_FILE)
        except AttributeError:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open",
                              str(SETTINGS_FILE)])


def main():
    try:
        OpenShelf().mainloop()
    except Exception:
        (APP_DIR / "last-run.log").write_text(traceback.format_exc(), encoding="utf-8")
        raise


if __name__ == "__main__":
    main()

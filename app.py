"""OpenShelf — a minimal local 'continue watching' shelf over folders of video files.

Native Tkinter window, single file, stdlib only. Scans folders you add, groups
sNNeNN files into shows, reads VLC's saved playback positions for Netflix-style
resume, and launches VLC to play.

Run:  python app.py   (or the packaged OpenShelf.exe — see README)
State: state.json next to this file. Nothing leaves the machine.
"""

__version__ = "0.2.0"

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
    return st


def save_state(state):
    STATE_FILE.write_text(
        json.dumps({"history": state["history"], "suppress": state["suppress"]},
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


def build_payload():
    shows, movies = get_library()
    pos = vlc_positions()
    with STATE_LOCK:
        history = dict(STATE["history"])
        suppress = dict(STATE["suppress"])
        folders = list(SETTINGS["folders"])

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
                "played": bool(h),
            })
        next_up = eps[0]
        if last_played_idx is not None:
            cur = eps[last_played_idx]
            if cur["resume"]:
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
            cont.append({
                "kind": "show", "title": s["title"], "key": s["key"],
                "sub": (f"Resume {item['label']} · {item['resume_h']}" if item["resume"]
                        else f"Watched {item['label']}"),
                "play": item if item["resume"] else _next_after(s, item),
            })
        else:
            used.add(lp)
            cont.append({
                "kind": "movie", "title": item["title"] + (f" ({item['year']})" if item["year"] else ""),
                "sub": f"Resume · {item['resume_h']}" if item["resume"] else "Watched",
                "play": item,
            })
        if len(cont) >= 12:
            break

    return {"folders": folders, "vlc": bool(find_vlc()), "continue": cont,
            "shows": out_shows, "movies": out_movies}


def _next_after(show, ep):
    eps = show["episodes"]
    for i, e in enumerate(eps):
        if e["path"] == ep["path"]:
            return eps[i + 1] if i + 1 < len(eps) else e
    return ep


# ---------- playback ----------

def launch(path, resume):
    if not os.path.exists(path):
        return {"ok": False, "error": "File not found:\n" + path}
    vlc = find_vlc()
    if not vlc:
        return {"ok": False, "error": "VLC not found — install VLC, or set \"vlc_path\" in settings.json"}
    rewind = int(SETTINGS.get("rewind_seconds", 3))
    cmd = [vlc, path]
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

    def clear(self):
        for c in self.inner.winfo_children():
            c.destroy()
        self.canvas.yview_moveto(0)


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
        self._show_win = None
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
        match = lambda s: not q or q in s.lower()
        sort_recent = SETTINGS.get("sort_mode", "recent") == "recent"
        hide_watched = SETTINGS.get("hide_watched", False)

        if not p["folders"]:
            for name in ("Continue", "Shows", "Movies"):
                tab = self.tabs[name]
                tab.clear()
                self._empty_state(tab.inner)
            self._render_folders(p)
            self.status.config(text="Add a folder to get started.")
            return

        cont = self.tabs["Continue"]
        cont.clear()
        items = [c for c in p["continue"] if match(c["title"])]
        for c in items:
            play = c["play"]
            btn_label = "▶ Resume" if play["resume"] else "▶ Play"
            if c["kind"] == "show":
                btn_label += " " + play["label"]
            make_row(cont.inner, c["title"], sub=c["sub"],
                     buttons=[(btn_label, lambda pl=play: self.play(pl), True)],
                     on_click=(lambda k=c.get("key"): self.open_show_by_key(k)) if c["kind"] == "show" else
                              (lambda pl=play: self.play(pl)))
        if not items:
            tk.Label(cont.inner, text="Nothing yet — play something.",
                     bg=BG, fg=DIM, font=FONT).pack(pady=18)

        shows_tab = self.tabs["Shows"]
        shows_tab.clear()
        self._controls_row(shows_tab.inner, sort_recent, hide_watched)
        shown = [s for s in p["shows"] if match(s["title"])]
        if hide_watched:
            shown = [s for s in shown if not all(e["played"] for e in s["episodes"])]
        shown.sort(key=lambda s: s["title"].lower())
        if sort_recent:
            shown.sort(key=lambda s: s["last_played"], reverse=True)
        for s in shown:
            nxt = s["next"]
            badge = None
            if nxt["resume"]:
                badge = f"⏸ {nxt['label']} · {nxt['resume_h']}"
            elif s["last_played"]:
                badge = f"Next: {nxt['label']}"
            sub = f"{s['ep_count']} episode{'s' if s['ep_count'] > 1 else ''}"
            if s["season_count"] > 1:
                sub += f" · {s['season_count']} seasons"
            make_row(shows_tab.inner, s["title"], sub=sub, badge=badge,
                     buttons=[("Episodes", lambda sh=s: self.open_show(sh), False),
                              (f"▶ {nxt['label']}", lambda pl=nxt: self.play(pl), True)],
                     on_click=lambda sh=s: self.open_show(sh),
                     on_context=lambda e, sh=s: self._menu(e, [
                         ("Mark all watched",
                          lambda: self._mark([x["path"] for x in sh["episodes"]], True)),
                         ("Mark all unwatched",
                          lambda: self._mark([x["path"] for x in sh["episodes"]], False)),
                     ]))
        if not shown:
            tk.Label(shows_tab.inner, text="No shows found.", bg=BG, fg=DIM, font=FONT).pack(pady=18)

        movies_tab = self.tabs["Movies"]
        movies_tab.clear()
        self._controls_row(movies_tab.inner, sort_recent, hide_watched)
        mshown = [m for m in p["movies"] if match(m["title"])]
        if hide_watched:
            mshown = [m for m in mshown if not (m["played"] and not m["resume"])]
        mshown.sort(key=lambda m: (m["title"].lower(), m["year"] or ""))
        if sort_recent:
            mshown.sort(key=lambda m: m["last_played"], reverse=True)
        for m in mshown:
            title = m["title"]
            if m["year"]:
                title += f" ({m['year']})"
            if m["tag"]:
                title += f"  [{m['tag']}]"
            if m["played"] and not m["resume"]:
                title += "  ✓"
            make_row(movies_tab.inner, title,
                     badge=f"⏸ {m['resume_h']}" if m["resume"] else None,
                     buttons=[("▶ Resume" if m["resume"] else "▶ Play",
                               lambda pl=m: self.play(pl), True)],
                     on_click=lambda pl=m: self.play(pl),
                     on_context=lambda e, mv=m: self._menu(e, [
                         ("Mark watched", lambda: self._mark([mv["path"]], True)),
                         ("Mark unwatched", lambda: self._mark([mv["path"]], False)),
                     ]),
                     dim_title=m["played"] and not m["resume"])
        if not mshown:
            tk.Label(movies_tab.inner, text="No movies found.", bg=BG, fg=DIM, font=FONT).pack(pady=18)

        self._render_folders(p)

        n_res = sum(1 for c in p["continue"] if c["play"]["resume"])
        vlc_note = "" if p["vlc"] else "   ⚠ VLC NOT FOUND — playback will fail"
        self.status.config(
            text=f"{len(p['shows'])} shows · {len(p['movies'])} movies · "
                 f"{n_res} resumable · positions update after you close VLC{vlc_note}")

        # keep an open episode window in sync with fresh data
        try:
            if self._show_win and self._show_win["win"].winfo_exists():
                fresh = next((s for s in p["shows"]
                              if s["key"] == self._show_win["key"]), None)
                if fresh:
                    self._fill_show(self._show_win["sf"], fresh)
        except tk.TclError:
            self._show_win = None

    def _render_folders(self, p):
        folders_tab = self.tabs["Folders"]
        folders_tab.clear()
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

    def play(self, item):
        res = launch(item["path"], item.get("resume") or 0)
        if not res["ok"]:
            messagebox.showerror("OpenShelf", res["error"])
            return
        self.status.config(text=f"Playing {os.path.basename(item['path'])} — "
                                "position saves when you close VLC")
        # pick up the new played-flag; VLC position lands later (on focus return)
        self.after(1200, lambda: self.refresh())

    def open_show_by_key(self, key):
        if not key or not self.payload:
            return
        for s in self.payload["shows"]:
            if s["key"] == key:
                return self.open_show(s)

    def open_show(self, show):
        if self._show_win:
            try:
                self._show_win["win"].destroy()
            except tk.TclError:
                pass
        win = tk.Toplevel(self)
        win.title(show["title"])
        win.configure(bg=BG)
        win.geometry("680x560")
        dark_title_bar(win)
        head = tk.Frame(win, bg=BG)
        head.pack(fill="x", padx=14, pady=(12, 4))
        tk.Label(head, text=show["title"], bg=BG, fg=FG, font=FONT_H).pack(side="left")
        sf = ScrollFrame(win)
        sf.pack(fill="both", expand=True, padx=6, pady=6)
        self._show_win = {"win": win, "key": show["key"], "sf": sf}
        self._fill_show(sf, show)

    def _fill_show(self, sf, show):
        sf.clear()
        season = None
        eps = show["episodes"]
        for i, ep in enumerate(eps):
            if show["season_count"] > 1 and ep["season"] != season:
                season = ep["season"]
                tk.Label(sf.inner, text=f"SEASON {season}", bg=BG, fg=DIM,
                         font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=14, pady=(12, 2))
            title = ep["label"] + ("  ✓" if ep["played"] else "")
            make_row(sf.inner, title, sub=ep["file"],
                     badge=f"⏸ {ep['resume_h']}" if ep["resume"] else None,
                     buttons=[("▶", lambda pl=ep: self.play(pl), True)],
                     on_context=lambda e, idx=i: self._menu(e, [
                         ("Mark watched",
                          lambda: self._mark([eps[idx]["path"]], True)),
                         ("Mark unwatched",
                          lambda: self._mark([eps[idx]["path"]], False)),
                         ("Mark watched up to here",
                          lambda: self._mark([x["path"] for x in eps[:idx + 1]], True)),
                     ]),
                     dim_title=ep["played"] and not ep["resume"])

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

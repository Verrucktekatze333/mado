#!/usr/bin/env python3
"""Mado: a small web front end for ani-cli.

Search, episode lists and stream links all come from ani-cli itself. The app
drives it non-interactively with two stand-ins placed first on its PATH:

  * fzf / rofi / dmenu: records the menu ani-cli would show (search results,
    episode list) and then aborts like a cancelled pick;
  * mpv (via ANI_CLI_PLAYER): ani-cli calls it exactly as it would call mpv,
    with the referrer, subtitle file, title and stream link, and Mado records
    those arguments instead of playing anything.

Streams are served through /proxy so the browser can play them: the proxy adds
the Referer header the hosts demand and rewrites HLS playlists.
"""

import atexit
import os
import re
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from functools import wraps
from urllib.parse import urljoin

import requests
from flask import Flask, Response, abort, g, jsonify, request, send_from_directory, session
from itsdangerous import BadSignature, URLSafeTimedSerializer
from requests.adapters import HTTPAdapter
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("ANIWEB_DATA_DIR", os.path.join(BASE_DIR, "data"))
ANI_CLI = os.environ.get("ANI_CLI_BIN", "ani-cli")
MAX_JOBS = int(os.environ.get("ANIWEB_MAX_JOBS", "4"))
STREAM_TTL = 8 * 3600

os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "mado.db")
ANI_STATE_DIR = os.path.join(DATA_DIR, "ani-cli-state")
os.makedirs(ANI_STATE_DIR, exist_ok=True)


# --------------------------------------------------------------------------
# ani-cli constants (read from the installed script so they follow upgrades)
# --------------------------------------------------------------------------

def _ani_cli_agent():
    agent = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    path = shutil.which(ANI_CLI)
    if path:
        try:
            with open(path, "r", errors="ignore") as fh:
                m = re.search(r'^agent="([^"]+)"', fh.read(500_000), re.M)
            if m:
                agent = m.group(1)
        except OSError:
            pass
    return agent


UA = _ani_cli_agent()


def _shim_dir():
    # ani-cli expands $player_function unquoted and matches it against *mpv*,
    # *flatpak*mpv* ..., so the path must have no spaces and no "flatpak".
    preferred = os.path.join(DATA_DIR, "shim")
    if re.search(r"\s|flatpak", preferred):
        return tempfile.mkdtemp(prefix="mado-shim-")
    return preferred


SHIM_DIR = _shim_dir()
PLAYER_PATH = os.path.join(SHIM_DIR, "mado-mpv")


# --------------------------------------------------------------------------
# Flask app, secrets, database
# --------------------------------------------------------------------------

def _load_secret():
    path = os.path.join(DATA_DIR, "secret.key")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(secrets.token_hex(32))
    except FileExistsError:
        pass
    with open(path) as fh:
        return fh.read().strip()


SECRET = _load_secret()
app = Flask(__name__, static_folder=os.path.join(BASE_DIR, "static"), static_url_path="/static")
app.config.update(
    SECRET_KEY=SECRET,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("ANIWEB_HTTPS") == "1",
    PERMANENT_SESSION_LIFETIME=90 * 24 * 3600,
    MAX_CONTENT_LENGTH=64 * 1024,
)
stream_signer = URLSafeTimedSerializer(SECRET, salt="mado-stream")
if os.environ.get("ANIWEB_TRUST_PROXY") == "1":
    # trust one reverse proxy in front: real client IP and https scheme come from its headers
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  pw_hash TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  is_admin INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS searches(
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  query TEXT NOT NULL,
  qkey TEXT NOT NULL,
  mode TEXT NOT NULL,
  searched_at INTEGER NOT NULL,
  UNIQUE(user_id, qkey, mode)
);
CREATE TABLE IF NOT EXISTS progress(
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  show_key TEXT NOT NULL,
  mode TEXT NOT NULL,
  title TEXT NOT NULL,
  query TEXT NOT NULL,
  idx INTEGER NOT NULL,
  total INTEGER NOT NULL DEFAULT 0,
  episode TEXT NOT NULL,
  position REAL NOT NULL DEFAULT 0,
  duration REAL NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY(user_id, show_key, mode)
);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY,
  ts INTEGER NOT NULL,
  user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
  username TEXT NOT NULL,
  kind TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  ip TEXT NOT NULL DEFAULT '',
  status INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS events_user ON events(user_id, ts);
CREATE TABLE IF NOT EXISTS usage(
  day TEXT NOT NULL,
  user_id INTEGER NOT NULL DEFAULT 0,
  requests INTEGER NOT NULL DEFAULT 0,
  searches INTEGER NOT NULL DEFAULT 0,
  plays INTEGER NOT NULL DEFAULT 0,
  bytes INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(day, user_id)
);
"""


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    if "is_admin" not in [r[1] for r in conn.execute("PRAGMA table_info(users)")]:
        conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
    conn.commit()
    conn.close()


init_db()


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=15)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def fail(message, status=400):
    return jsonify(error=message), status


class AniError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.message = message
        self.status = status


@app.errorhandler(AniError)
def handle_ani_error(exc):
    return fail(exc.message, exc.status)


@app.after_request
def no_store_api(resp):
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


class TTLCache:
    def __init__(self, ttl, maxsize=500):
        self.ttl, self.maxsize = ttl, maxsize
        self._data = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            hit = self._data.get(key)
            if hit and hit[0] > time.time():
                return hit[1]
            self._data.pop(key, None)
            return None

    def clear(self):
        with self._lock:
            self._data.clear()

    def set(self, key, value):
        with self._lock:
            if len(self._data) >= self.maxsize:
                now = time.time()
                for k in [k for k, v in self._data.items() if v[0] <= now] or list(self._data)[:50]:
                    self._data.pop(k, None)
            self._data[key] = (time.time() + self.ttl, value)


SEARCH_CACHE = TTLCache(600)
EPISODE_CACHE = TTLCache(600)
PLAY_CACHE = TTLCache(1200)

_key_locks = {}
_key_locks_guard = threading.Lock()


def _key_lock(key):
    with _key_locks_guard:
        if len(_key_locks) > 2000:
            _key_locks.clear()
        return _key_locks.setdefault(key, threading.Lock())


def cached_call(cache, key, fn):
    """Return cache[key], computing it once even if several requests race."""
    hit = cache.get(key)
    if hit is not None:
        return hit
    with _key_lock((id(cache), key)):
        hit = cache.get(key)
        if hit is not None:
            return hit
        value = fn()
        cache.set(key, value)
        return value


# --------------------------------------------------------------------------
# Driving ani-cli
# --------------------------------------------------------------------------

MENU_SHIM = """#!/bin/sh
# Stand-in for fzf / rofi / dmenu. Records the menu ani-cli wants to show, then
# aborts the way a cancelled pick would. (Written by Mado, safe to delete.)
prompt=""
while [ $# -gt 0 ]; do
    case "$1" in --prompt|-p) prompt="$2"; shift ;; esac
    shift
done
cat > "$ANIWEB_MENU"
printf '%s' "$prompt" > "$ANIWEB_MENU.prompt"
exit 130
"""

PLAYER_SHIM = """#!/bin/sh
# Stand-in for mpv. ani-cli calls it the way it would call mpv; Mado only
# records the arguments (referrer, subtitle file, title, stream link).
: > "$ANIWEB_PLAYER"
for arg in "$@"; do printf '%s\\n' "$arg" >> "$ANIWEB_PLAYER"; done
exit 0
"""

ANSI_RE = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|[78=>]|\([A-Z0-9])")
JOBS = threading.BoundedSemaphore(MAX_JOBS)
JOBS_ACTIVE = 0
_jobs_lock = threading.Lock()


@contextmanager
def job_running():
    global JOBS_ACTIVE
    with _jobs_lock:
        JOBS_ACTIVE += 1
    try:
        yield
    finally:
        with _jobs_lock:
            JOBS_ACTIVE -= 1
FALLBACK_ERROR = "ani-cli couldn't finish that request. If it keeps happening, update it with `ani-cli -U`."


def strip_ansi(text):
    return ANSI_RE.sub("", text).replace("\r", "\n")


def ensure_shims():
    os.makedirs(SHIM_DIR, exist_ok=True)
    wanted = {name: MENU_SHIM for name in ("fzf", "rofi", "dmenu")}
    wanted["mado-mpv"] = PLAYER_SHIM
    for name, content in wanted.items():
        path = os.path.join(SHIM_DIR, name)
        try:
            if open(path).read() == content and os.access(path, os.X_OK):
                continue
        except OSError:
            pass
        with open(path, "w") as fh:
            fh.write(content)
        os.chmod(path, 0o755)


class Run:
    def __init__(self, out, err, menu, prompt, player):
        self.out, self.err, self.menu, self.prompt, self.player = out, err, menu, prompt, player


def _read(path):
    try:
        with open(path, errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def run_ani_cli(args, mode, timeout):
    ensure_shims()
    files = []
    for prefix in ("menu-", "player-"):
        fd, path = tempfile.mkstemp(prefix=prefix, dir=DATA_DIR)
        os.close(fd)
        files.append(path)
    menu_file, player_file = files
    env = os.environ.copy()
    env.update(
        PATH=SHIM_DIR + os.pathsep + env.get("PATH", ""),
        ANI_CLI_PLAYER=PLAYER_PATH,
        ANI_CLI_MODE=mode,
        ANI_CLI_HIST_DIR=ANI_STATE_DIR,
        ANI_CLI_LOG="0",
        ANIWEB_MENU=menu_file,
        ANIWEB_PLAYER=player_file,
        TERM="xterm",
    )
    try:
        with JOBS, job_running():
            try:
                proc = subprocess.Popen(
                    [ANI_CLI, "--no-detach", "--exit-after-play", *args],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    start_new_session=True,
                )
            except FileNotFoundError:
                raise AniError("ani-cli isn't installed on the server. Install it and restart Mado.", 500)
            try:
                out, err = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()
                raise AniError("ani-cli took too long to answer. Try again in a moment.", 504)
        player = [ln for ln in _read(player_file).splitlines() if ln]
        return Run(
            strip_ansi(out.decode("utf-8", "replace")),
            strip_ansi(err.decode("utf-8", "replace")),
            _read(menu_file),
            _read(menu_file + ".prompt"),
            player,
        )
    finally:
        for path in (menu_file, menu_file + ".prompt", player_file):
            try:
                os.remove(path)
            except OSError:
                pass


def last_line(text):
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def sentence(text):
    text = text.strip()
    return text if text.endswith((".", "!", ")")) else text + "."


def clean_query(raw):
    """Make a user query safe to hand to ani-cli.

    ani-cli treats any word starting with "-" as an option (-U self-updates,
    -D wipes its history), so strip leading dashes, quotes, backslashes and
    control characters.
    """
    text = re.sub(r'["\\\x00-\x1f]', " ", str(raw or ""))
    words = [w.lstrip("-") for w in text.split()]
    words = [w for w in words if w][:12]
    query = " ".join(words)[:100].strip()
    return query, query.split()


RESULT_RE = re.compile(r"^\s*(\d+)\s+(.+?)\s*$")


def parse_results(menu):
    """The search menu shows one 'N title' line per result."""
    results = []
    for line in menu.splitlines():
        m = RESULT_RE.match(line)
        if m:
            # "(N episodes)" changes while a show airs; keep it out of the stable key
            title = re.sub(r"\s*\(\d+\s+episodes?\)\s*$", "", m.group(2)) or m.group(2)
            results.append({"index": int(m.group(1)), "title": title, "show": title.lower(), "guessed": False})
    return results


def episode_sort_key(ep):
    try:
        return (0, float(ep), ep)
    except ValueError:
        return (1, 0.0, ep)


def menu_episodes(menu):
    seen = dict.fromkeys(ln.strip() for ln in menu.splitlines() if ln.strip())
    return sorted(seen, key=episode_sort_key)


def single_result(query, title=None):
    # With exactly one hit ani-cli skips the result menu, so the title is unknown
    # until something plays; keep the key stable and fix the display title later.
    return {"index": 1, "title": title or query, "show": "~" + query.lower(), "guessed": title is None}


def parse_player(args):
    """Turn the arguments ani-cli gave its (stand-in) mpv into a stream description."""
    if not args or not args[-1].startswith("http"):
        return None
    info = {"url": args[-1], "referer": "", "sub": "", "title": "", "episode": ""}
    for arg in args[:-1]:
        if arg.startswith("--referrer="):
            info["referer"] = arg.split("=", 1)[1]
        elif arg.startswith("--sub-file="):
            info["sub"] = arg.split("=", 1)[1]
        elif arg.startswith("--force-media-title="):
            media = arg.split("=", 1)[1]
            m = re.match(r"^(.*) Episode (\S+)$", media)
            info["title"], info["episode"] = (m.group(1), m.group(2)) if m else (media, "")
    return info


QUALITIES = ("best", "1080p", "720p", "480p", "360p")


def ani_search(query, words, mode):
    def work():
        r = run_ani_cli(words, mode, timeout=45)
        prompt = r.prompt.strip().lower()
        if r.menu and prompt.startswith("select anime"):
            results = parse_results(r.menu)
            if results:
                return results
        elif r.menu and prompt.startswith("select episode"):
            # exactly one hit: ani-cli went straight to the episode menu
            episodes = menu_episodes(r.menu)
            result = single_result(query)
            EPISODE_CACHE.set((mode, result["show"], 1), episodes)
            return [result]
        else:
            info = parse_player(r.player)
            if info:
                # one hit with one episode: ani-cli went straight to "playing" it
                result = single_result(query, info["title"] or None)
                ep = info["episode"] or "1"
                EPISODE_CACHE.set((mode, result["show"], 1), [ep])
                PLAY_CACHE.set((mode, result["show"], 1, ep, "best"), info)
                return [result]
        err = last_line(r.err)
        if not err or re.search(r"no results", err, re.I):
            raise AniError("No results found. Try another spelling or the original title.", 404)
        app.logger.warning("ani-cli search failed: %s", r.err[-500:])
        raise AniError(sentence(err), 502)

    return cached_call(SEARCH_CACHE, (mode, query.lower()), work)


def ani_episodes(words, mode, entry):
    def work():
        r = run_ani_cli(["-S", str(entry["index"]), *words], mode, timeout=45)
        prompt = r.prompt.strip().lower()
        if r.menu and prompt.startswith("select episode"):
            episodes = menu_episodes(r.menu)
            if episodes:
                return episodes
        else:
            info = parse_player(r.player)
            if info:
                # a one-episode title: ani-cli went straight to "playing" it
                ep = info["episode"] or "1"
                PLAY_CACHE.set((mode, entry["show"], entry["index"], ep, "best"), info)
                return [ep]
        app.logger.warning("ani-cli episode list failed: %s", r.err[-500:])
        err = last_line(r.err)
        raise AniError(sentence(err) if err else FALLBACK_ERROR, 502)

    return cached_call(EPISODE_CACHE, (mode, entry["show"], entry["index"]), work)


def ani_play(words, mode, entry, episode, quality):
    def work():
        r = run_ani_cli(
            ["-S", str(entry["index"]), "-e", episode, "-q", quality, *words], mode, timeout=90
        )
        info = parse_player(r.player)
        if info:
            return info
        err = last_line(r.err)
        app.logger.warning("ani-cli play failed: %s", r.err[-500:])
        if re.search(r"not released|no valid|no sources|invalid episode", err, re.I):
            raise AniError(sentence(err), 404)
        raise AniError(sentence(err) if err else FALLBACK_ERROR, 502)

    return cached_call(PLAY_CACHE, (mode, entry["show"], entry["index"], episode, quality), work)


def pick_result(query, words, mode, show_key, hint):
    """Find the search result the client means, even if the order has shifted."""
    results = ani_search(query, words, mode)
    by_index = {r["index"]: r for r in results}
    if not show_key:
        return by_index.get(hint, results[0])
    if hint in by_index and by_index[hint]["show"] == show_key:
        return by_index[hint]
    for r in results:
        if r["show"] == show_key:
            return r
    raise AniError("That title no longer shows up for this search. Search for it again.", 404)


def proxied(url, referers, ctype=None):
    payload = {"u": url, "r": referers}
    if ctype:
        payload["c"] = ctype
    return "/proxy/" + stream_signer.dumps(payload)


# --------------------------------------------------------------------------
# Accounts
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Activity log, usage counters, presence (feeds the admin panel)
# --------------------------------------------------------------------------

EVENT_RETENTION_DAYS = int(os.environ.get("ANIWEB_LOG_DAYS", "60"))
EVENT_MAX_ROWS = 50000
START_TIME = time.time()

_pending_bytes = {}
_pending_lock = threading.Lock()
_presence = {}
_live_lock = threading.Lock()
_streams_active = 0
_event_counter = 0


def today():
    return time.strftime("%Y-%m-%d")


def days_ago(n):
    return time.strftime("%Y-%m-%d", time.localtime(time.time() - n * 86400))


def note_bytes(uid, n):
    if n > 0:
        with _pending_lock:
            key = (today(), uid)
            _pending_bytes[key] = _pending_bytes.get(key, 0) + n


def flush_usage():
    with _pending_lock:
        items = list(_pending_bytes.items())
        _pending_bytes.clear()
    if not items:
        return
    try:
        conn = sqlite3.connect(DB_PATH, timeout=15)
        for (day, uid), n in items:
            conn.execute(
                "INSERT INTO usage(day, user_id, bytes) VALUES(?,?,?) "
                "ON CONFLICT(day, user_id) DO UPDATE SET bytes=bytes+excluded.bytes",
                (day, uid, n),
            )
        conn.commit()
        conn.close()
    except sqlite3.Error:
        for (day, uid), n in items:  # try again next time
            with _pending_lock:
                _pending_bytes[(day, uid)] = _pending_bytes.get((day, uid), 0) + n


def _flusher():
    while True:
        time.sleep(15)
        flush_usage()


threading.Thread(target=_flusher, daemon=True).start()
atexit.register(flush_usage)


def presence_key():
    uid = session.get("uid")
    return "u%s" % uid if uid else "g%s" % session.get("gid", "?")


def touch_presence(key):
    with _live_lock:
        _presence[key] = time.time()


def viewers_now():
    cutoff = time.time() - 120
    with _live_lock:
        for k in [k for k, t in _presence.items() if t < cutoff]:
            del _presence[k]
        return len(_presence)


def add_usage(uid, requests=0, searches=0, plays=0):
    db().execute(
        "INSERT INTO usage(day, user_id, requests, searches, plays) VALUES(?,?,?,?,?) "
        "ON CONFLICT(day, user_id) DO UPDATE SET requests=requests+excluded.requests, "
        "searches=searches+excluded.searches, plays=plays+excluded.plays",
        (today(), uid, requests, searches, plays),
    )


def prune_events(conn):
    cutoff = int(time.time()) - EVENT_RETENTION_DAYS * 86400
    conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
    conn.execute(
        "DELETE FROM events WHERE id <= (SELECT id FROM events ORDER BY id DESC LIMIT 1 OFFSET ?)", (EVENT_MAX_ROWS,)
    )
    conn.execute("DELETE FROM usage WHERE day < ?", (days_ago(400),))


def insert_event(kind, status, user_id, username, detail="", ip=""):
    global _event_counter
    conn = db()
    conn.execute(
        "INSERT INTO events(ts, user_id, username, kind, detail, ip, status) VALUES(?,?,?,?,?,?,?)",
        (int(time.time()), user_id, username, kind, detail[:300], ip, status),
    )
    _event_counter += 1
    if _event_counter % 500 == 0:
        prune_events(conn)
    conn.commit()


# kinds that are written to the request log
LOGGED = {
    ("GET", "/api/search"): "search",
    ("GET", "/api/episodes"): "episodes",
    ("POST", "/api/play"): "play",
    ("POST", "/api/login"): "login",
    ("POST", "/api/register"): "register",
    ("POST", "/api/guest"): "guest",
}


def record_event(kind, status):
    ok = status < 400
    uid = session.get("uid")
    if kind in ("login", "register") and not ok:
        kind += "_failed"
    if is_guest():
        # guests stay anonymous: only the kind of request and a counter, no query, no IP
        insert_event(kind, status, None, "guest")
        if ok and kind in ("search", "episodes", "play"):
            add_usage(0, requests=1, searches=int(kind == "search"), plays=int(kind == "play"))
            db().commit()
        return
    detail = ""
    if kind == "search":
        detail = clean_query(request.args.get("q"))[0]
        if request.args.get("mode") == "dub":
            detail += " [dub]"
    elif kind == "episodes":
        detail = (request.args.get("show") or "")[:120]
    elif kind == "play":
        d = body()
        detail = "%s ep %s" % (str(d.get("show", ""))[:100], str(d.get("episode", ""))[:16])
    elif kind in ("login_failed", "register_failed"):
        detail = str(body().get("username", ""))[:64]
    username = "-"
    if uid:
        row = db().execute("SELECT username FROM users WHERE id=?", (uid,)).fetchone()
        if row:
            username = row["username"]
        else:
            uid = None
    insert_event(kind, status, uid, username, detail, request.remote_addr or "")
    if uid and ok and kind in ("search", "episodes", "play"):
        add_usage(uid, requests=1, searches=int(kind == "search"), plays=int(kind == "play"))
        db().commit()


@app.after_request
def log_api(resp):
    try:
        kind = LOGGED.get((request.method, request.path))
        if kind:
            record_event(kind, resp.status_code)
    except Exception:  # logging must never break a response
        app.logger.exception("request log failed")
    return resp


USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
EPISODE_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,15}$")
_login_failures = {}


def current_user():
    return session.get("uid")


def is_guest():
    """Guests can search and watch but nothing about them is stored."""
    return bool(session.get("guest")) and not session.get("uid")


def user_row():
    """The signed-in user's database row (None for guests, anonymous visitors and deleted accounts)."""
    if "user_row" not in g:
        uid = session.get("uid")
        g.user_row = (
            db().execute("SELECT id, username, is_admin FROM users WHERE id=?", (uid,)).fetchone() if uid else None
        )
    return g.user_row


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if session.get("uid") and not user_row():
            session.clear()  # the account was deleted: end the session right away
        if not current_user() and not is_guest():
            return fail("Sign in to continue.", 401)
        return fn(*args, **kwargs)

    return wrapper


def body():
    return request.get_json(force=True, silent=True) or {}


@app.get("/api/me")
def me():
    uid = current_user()
    if not uid:
        return jsonify(user="Guest", guest=True) if is_guest() else jsonify(user=None)
    row = user_row()
    if not row:
        session.clear()
        return jsonify(user=None)
    return jsonify(user=row["username"], admin=bool(row["is_admin"]))


@app.post("/api/register")
def register():
    data = body()
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    if not USERNAME_RE.match(username):
        return fail("Usernames are 3 to 32 characters: letters, numbers, dots, dashes or underscores.")
    if len(password) < 6:
        return fail("Passwords need at least 6 characters.")
    try:
        cur = db().execute(
            "INSERT INTO users(username, pw_hash, created_at) VALUES(?,?,?)",
            (username, generate_password_hash(password), int(time.time())),
        )
        db().commit()
    except sqlite3.IntegrityError:
        return fail("That username is taken.", 409)
    session.clear()
    session["uid"] = cur.lastrowid
    session.permanent = True
    return jsonify(user=username)


@app.post("/api/login")
def login():
    ip = request.remote_addr or "?"
    now = time.time()
    recent = [t for t in _login_failures.get(ip, []) if now - t < 600]
    _login_failures[ip] = recent
    if len(recent) >= 10:
        return fail("Too many failed attempts. Wait a few minutes and try again.", 429)
    data = body()
    row = db().execute(
        "SELECT id, username, pw_hash, is_admin FROM users WHERE username=?", (str(data.get("username", "")).strip(),)
    ).fetchone()
    if not row or not check_password_hash(row["pw_hash"], str(data.get("password", ""))):
        recent.append(now)
        return fail("Wrong username or password.", 401)
    session.clear()
    session["uid"] = row["id"]
    session.permanent = True
    return jsonify(user=row["username"], admin=bool(row["is_admin"]))


@app.post("/api/guest")
def guest_login():
    # no database row, no history: the session cookie only says "this visitor is a guest"
    session.clear()
    session["guest"] = True
    session["gid"] = secrets.token_hex(6)  # only used to count how many guests are watching right now
    return jsonify(user="Guest", guest=True)


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(user=None)


# --------------------------------------------------------------------------
# Search, episodes, playback
# --------------------------------------------------------------------------

def get_mode():
    return "dub" if request.values.get("mode") == "dub" else "sub"


def get_hint():
    try:
        return max(1, min(200, int(request.values.get("index", 1))))
    except ValueError:
        return 1


def missing_tools():
    """Programs ani-cli needs (besides the menu and player, which Mado stands in for)."""
    missing = []
    if not shutil.which(ANI_CLI):
        missing.append("ani-cli")
    for tool in ("curl", "sed", "grep", "od"):
        if not shutil.which(tool):
            missing.append(tool)
    if not any(shutil.which(b) for b in ("base64", "openssl")):
        missing.append("base64")
    return missing


@app.get("/api/status")
def status():
    missing = missing_tools()
    return jsonify(ok=not missing, missing=missing)


@app.get("/api/search")
@login_required
def search():
    query, words = clean_query(request.args.get("q"))
    if not query:
        return fail("Type an anime name to search.")
    mode = get_mode()
    results = ani_search(query, words, mode)
    if is_guest():
        return jsonify(query=query, mode=mode, results=[dict(r, last_episode=None) for r in results])
    uid = current_user()
    now = int(time.time())
    conn = db()
    conn.execute(
        "INSERT INTO searches(user_id, query, qkey, mode, searched_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(user_id, qkey, mode) DO UPDATE SET query=excluded.query, searched_at=excluded.searched_at",
        (uid, query, query.lower(), mode, now),
    )
    conn.execute(
        "DELETE FROM searches WHERE user_id=? AND id NOT IN "
        "(SELECT id FROM searches WHERE user_id=? ORDER BY searched_at DESC LIMIT 100)",
        (uid, uid),
    )
    conn.commit()
    watched = {
        r["show_key"]: r["episode"]
        for r in conn.execute("SELECT show_key, episode FROM progress WHERE user_id=? AND mode=?", (uid, mode))
    }
    out = [dict(r, last_episode=watched.get(r["show"])) for r in results]
    return jsonify(query=query, mode=mode, results=out)


@app.get("/api/episodes")
@login_required
def episodes():
    query, words = clean_query(request.args.get("q"))
    if not query:
        return fail("Missing search text.")
    mode = get_mode()
    entry = pick_result(query, words, mode, request.args.get("show", ""), get_hint())
    eps = ani_episodes(words, mode, entry)
    if is_guest():
        return jsonify(entry=entry, episodes=eps, last=None)
    row = db().execute(
        "SELECT episode, position, duration FROM progress WHERE user_id=? AND show_key=? AND mode=?",
        (current_user(), entry["show"], mode),
    ).fetchone()
    return jsonify(entry=entry, episodes=eps, last=dict(row) if row else None)


@app.post("/api/play")
@login_required
def play():
    data = body()
    query, words = clean_query(data.get("q"))
    episode = str(data.get("episode", "")).strip()
    if not query or not EPISODE_RE.match(episode):
        return fail("Pick an episode to play.")
    mode = "dub" if data.get("mode") == "dub" else "sub"
    try:
        hint = max(1, min(200, int(data.get("index", 1))))
    except (TypeError, ValueError):
        hint = 1
    quality = data.get("quality") if data.get("quality") in QUALITIES else "best"
    entry = pick_result(query, words, mode, str(data.get("show", "")), hint)
    link = ani_play(words, mode, entry, episode, quality)
    # the stream host wants the embed site as referer (ani-cli hands it to mpv the same way)
    referers = [link["referer"]] if link["referer"] else [""]
    return jsonify(
        src=proxied(link["url"], referers),
        kind="hls" if ".m3u8" in link["url"].lower() else "file",
        subtitle=proxied(link["sub"], referers, "text/vtt; charset=utf-8") if link["sub"] else None,
        title=link["title"] or entry["title"],
        episode=episode,
        index=entry["index"],
    )


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------

@app.post("/api/progress")
@login_required
def save_progress():
    if is_guest():
        return "", 204
    d = body()
    show = str(d.get("show", ""))[:200]
    title = str(d.get("title", ""))[:200]
    episode = str(d.get("episode", "")).strip()
    query, _ = clean_query(d.get("q"))
    mode = "dub" if d.get("mode") == "dub" else "sub"
    if not show or not title or not query or not EPISODE_RE.match(episode):
        return fail("Incomplete progress update.")
    try:
        idx = max(1, min(200, int(d.get("index", 1))))
        total = max(0, min(10000, int(d.get("total", 0))))
        position = max(0.0, float(d.get("position", 0)))
        duration = max(0.0, float(d.get("duration", 0)))
    except (TypeError, ValueError):
        return fail("Incomplete progress update.")
    db().execute(
        "INSERT INTO progress(user_id, show_key, mode, title, query, idx, total, episode, position, duration, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(user_id, show_key, mode) DO UPDATE SET title=excluded.title, "
        "query=excluded.query, idx=excluded.idx, total=excluded.total, episode=excluded.episode, "
        "position=excluded.position, duration=excluded.duration, updated_at=excluded.updated_at",
        (current_user(), show, mode, title, query, idx, total, episode, position, duration, int(time.time())),
    )
    db().commit()
    return "", 204


@app.get("/api/history")
@login_required
def history():
    if is_guest():
        return jsonify(searches=[], watching=[])
    uid = current_user()
    conn = db()
    searches = [
        {"id": r["id"], "query": r["query"], "mode": r["mode"]}
        for r in conn.execute(
            "SELECT id, query, mode FROM searches WHERE user_id=? ORDER BY searched_at DESC LIMIT 30", (uid,)
        )
    ]
    watching = [
        {"show": r["show_key"], "mode": r["mode"], "title": r["title"], "query": r["query"],
         "index": r["idx"], "total": r["total"], "episode": r["episode"], "position": r["position"],
         "duration": r["duration"]}
        for r in conn.execute(
            "SELECT * FROM progress WHERE user_id=? ORDER BY updated_at DESC LIMIT 20", (uid,)
        )
    ]
    return jsonify(searches=searches, watching=watching)


@app.delete("/api/history/searches")
@login_required
def clear_searches():
    if is_guest():
        return "", 204
    db().execute("DELETE FROM searches WHERE user_id=?", (current_user(),))
    db().commit()
    return "", 204


@app.delete("/api/history/searches/<int:search_id>")
@login_required
def delete_search(search_id):
    if is_guest():
        return "", 204
    db().execute("DELETE FROM searches WHERE id=? AND user_id=?", (search_id, current_user()))
    db().commit()
    return "", 204


@app.post("/api/history/watching/remove")
@login_required
def remove_watching():
    if is_guest():
        return "", 204
    d = body()
    mode = "dub" if d.get("mode") == "dub" else "sub"
    db().execute(
        "DELETE FROM progress WHERE user_id=? AND show_key=? AND mode=?",
        (current_user(), str(d.get("show", "")), mode),
    )
    db().commit()
    return "", 204


# --------------------------------------------------------------------------
# Stream proxy: adds the Referer the hosts require and rewrites HLS playlists
# --------------------------------------------------------------------------

HTTP = requests.Session()
HTTP.mount("https://", HTTPAdapter(pool_connections=16, pool_maxsize=64))
HTTP.mount("http://", HTTPAdapter(pool_connections=16, pool_maxsize=64))
PASS_HEADERS = ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "ETag", "Last-Modified")
URI_ATTR = re.compile(r'URI="([^"]+)"')


def open_upstream(url, referers, range_header):
    for i, ref in enumerate(referers):
        headers = {"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "identity"}
        if ref:
            headers["Referer"] = ref
        if range_header:
            headers["Range"] = range_header
        try:
            resp = HTTP.get(url, headers=headers, stream=True, timeout=(10, 30))
        except requests.RequestException:
            continue
        if resp.status_code in (401, 403) and i < len(referers) - 1:
            resp.close()
            continue
        return resp, ref
    abort(502)


def rewrite_playlist(text, base_url, referers):
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            out.append(line)
        elif stripped.startswith("#"):
            out.append(
                URI_ATTR.sub(lambda m: 'URI="%s"' % proxied(urljoin(base_url, m.group(1)), referers), line)
            )
        else:
            out.append(proxied(urljoin(base_url, stripped), referers))
    return "\n".join(out) + "\n"


@app.get("/proxy/<token>")
def proxy(token):
    try:
        data = stream_signer.loads(token, max_age=STREAM_TTL)
    except BadSignature:
        abort(403)
    url, referers = data["u"], data.get("r") or [""]
    uid = session.get("uid") or 0  # 0 = guests / unknown
    touch_presence(presence_key())
    resp, used = open_upstream(url, referers, request.headers.get("Range"))
    ctype = resp.headers.get("Content-Type", "")
    path = resp.url.split("?")[0].lower()

    if ".m3u8" in path or "mpegurl" in ctype.lower():
        raw = resp.content[:8_000_000]
        resp.close()
        text = raw.decode("utf-8", "replace")
        if text.lstrip().startswith("#EXTM3U"):
            ordered = [used] + [r for r in referers if r != used]
            note_bytes(uid, len(raw))
            return Response(
                rewrite_playlist(text, resp.url, ordered),
                mimetype="application/vnd.apple.mpegurl",
                headers={"Cache-Control": "no-store"},
            )
        return Response(raw, status=resp.status_code, mimetype=ctype or "text/plain")

    headers = {h: resp.headers[h] for h in PASS_HEADERS if h in resp.headers}
    if headers.get("Content-Type", "").lower() in ("", "application/octet-stream", "binary/octet-stream") \
            and path.endswith(".mp4"):
        headers["Content-Type"] = "video/mp4"
    if data.get("c"):
        headers["Content-Type"] = data["c"]
    headers["Cache-Control"] = "private, max-age=600"

    def stream():
        global _streams_active
        with _live_lock:
            _streams_active += 1
        sent = 0
        try:
            for chunk in resp.iter_content(64 * 1024):
                if chunk:
                    sent += len(chunk)
                    yield chunk
        finally:
            resp.close()
            note_bytes(uid, sent)
            with _live_lock:
                _streams_active -= 1

    return Response(stream(), status=resp.status_code, headers=headers)


# --------------------------------------------------------------------------
# Pages and entry point
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Admin panel API (admins are made on the server: `python app.py make-admin NAME`)
# --------------------------------------------------------------------------

def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        row = user_row()
        if not row:
            return fail("Sign in to continue.", 401)
        if not row["is_admin"]:
            return fail("Admins only.", 403)
        # a custom header can't be sent cross-site, which blocks forged requests from other pages
        if request.method != "GET" and request.headers.get("X-Mado") != "1":
            return fail("Missing X-Mado header.", 403)
        return fn(*args, **kwargs)

    return wrapper


def audit(detail):
    row = user_row()
    insert_event("admin", 200, None, row["username"], detail, request.remote_addr or "")


def scalar(sql, args=()):
    row = db().execute(sql, args).fetchone()
    return (row[0] or 0) if row else 0


def system_info():
    info = {"uptime": int(time.time() - START_TIME)}
    try:
        info["load"] = [round(x, 2) for x in os.getloadavg()]
    except (OSError, AttributeError):
        info["load"] = None
    try:
        mem = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                k, _, v = line.partition(":")
                mem[k] = int(v.split()[0]) * 1024
        info["mem_total"], info["mem_available"] = mem["MemTotal"], mem["MemAvailable"]
    except (OSError, KeyError, ValueError):
        info["mem_total"] = info["mem_available"] = None
    try:
        du = shutil.disk_usage(DATA_DIR)
        info["disk_free"], info["disk_total"] = du.free, du.total
    except OSError:
        info["disk_free"] = info["disk_total"] = None
    size = 0
    for root, _dirs, files in os.walk(DATA_DIR):
        for name in files:
            try:
                size += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    info["data_size"] = size
    try:
        info["db_size"] = sum(os.path.getsize(DB_PATH + x) for x in ("", "-wal", "-shm") if os.path.exists(DB_PATH + x))
    except OSError:
        info["db_size"] = None
    return info


@app.get("/api/admin/overview")
@admin_required
def admin_overview():
    flush_usage()
    now = int(time.time())
    d1, d7, d30 = days_ago(0), days_ago(6), days_ago(29)
    kinds = {}
    for r in db().execute("SELECT kind, COUNT(*) AS n FROM events WHERE ts >= ? GROUP BY kind", (now - 86400,)):
        kinds[r["kind"]] = r["n"]
    return jsonify(
        users=scalar("SELECT COUNT(*) FROM users"),
        admins=scalar("SELECT COUNT(*) FROM users WHERE is_admin=1"),
        new_users_7d=scalar("SELECT COUNT(*) FROM users WHERE created_at >= ?", (now - 7 * 86400,)),
        saved_searches=scalar("SELECT COUNT(*) FROM searches"),
        tracked_shows=scalar("SELECT COUNT(*) FROM progress"),
        events_total=scalar("SELECT COUNT(*) FROM events"),
        events_24h=kinds,
        bytes_today=scalar("SELECT SUM(bytes) FROM usage WHERE day >= ?", (d1,)),
        bytes_7d=scalar("SELECT SUM(bytes) FROM usage WHERE day >= ?", (d7,)),
        bytes_30d=scalar("SELECT SUM(bytes) FROM usage WHERE day >= ?", (d30,)),
        bytes_total=scalar("SELECT SUM(bytes) FROM usage"),
        viewers_now=viewers_now(),
        streams_active=_streams_active,
        jobs_active=JOBS_ACTIVE,
        jobs_max=MAX_JOBS,
        log_days=EVENT_RETENTION_DAYS,
        system=system_info(),
    )


@app.get("/api/admin/usage")
@admin_required
def admin_usage():
    flush_usage()
    try:
        n = max(1, min(90, int(request.args.get("days", 14))))
    except ValueError:
        n = 14
    first = days_ago(n - 1)
    rows = {
        r["day"]: dict(r)
        for r in db().execute(
            "SELECT day, SUM(requests) AS requests, SUM(searches) AS searches, SUM(plays) AS plays, "
            "SUM(bytes) AS bytes FROM usage WHERE day >= ? GROUP BY day",
            (first,),
        )
    }
    daily = [
        rows.get(d) or {"day": d, "requests": 0, "searches": 0, "plays": 0, "bytes": 0}
        for d in (days_ago(i) for i in range(n - 1, -1, -1))
    ]
    top = [
        dict(r)
        for r in db().execute(
            "SELECT COALESCE(u.username, 'Guests') AS username, SUM(g.bytes) AS bytes, SUM(g.requests) AS requests, "
            "SUM(g.plays) AS plays FROM usage g LEFT JOIN users u ON u.id = g.user_id "
            "WHERE g.day >= ? GROUP BY g.user_id ORDER BY bytes DESC LIMIT 10",
            (first,),
        )
    ]
    return jsonify(days=n, daily=daily, top=top)


@app.get("/api/admin/users")
@admin_required
def admin_users():
    flush_usage()
    d7 = days_ago(6)
    users = [
        dict(r)
        for r in db().execute(
            "SELECT u.id, u.username, u.is_admin, u.created_at, "
            "(SELECT COUNT(*) FROM searches s WHERE s.user_id=u.id) AS searches_saved, "
            "(SELECT COUNT(*) FROM progress p WHERE p.user_id=u.id) AS shows_tracked, "
            "(SELECT MAX(ts) FROM events e WHERE e.user_id=u.id) AS last_seen, "
            "COALESCE((SELECT SUM(requests) FROM usage g WHERE g.user_id=u.id AND g.day >= ?), 0) AS requests_7d, "
            "COALESCE((SELECT SUM(bytes) FROM usage g WHERE g.user_id=u.id AND g.day >= ?), 0) AS bytes_7d, "
            "COALESCE((SELECT SUM(bytes) FROM usage g WHERE g.user_id=u.id), 0) AS bytes_total "
            "FROM users u ORDER BY (last_seen IS NULL), last_seen DESC, u.id DESC",
            (d7, d7),
        )
    ]
    guests = db().execute(
        "SELECT COALESCE(SUM(requests),0) AS requests_7d, COALESCE(SUM(bytes),0) AS bytes_7d "
        "FROM usage WHERE user_id=0 AND day >= ?",
        (d7,),
    ).fetchone()
    return jsonify(users=users, guests=dict(guests), me=user_row()["id"])


@app.delete("/api/admin/users/<int:user_id>")
@admin_required
def admin_delete_user(user_id):
    row = db().execute("SELECT id, username, is_admin FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        return fail("No such account.", 404)
    if row["is_admin"]:
        return fail("Admin accounts can't be deleted here. On the server run: python app.py remove-admin %s" % row["username"])
    with _pending_lock:
        for key in [k for k in _pending_bytes if k[1] == user_id]:
            del _pending_bytes[key]
    conn = db()
    conn.execute("DELETE FROM usage WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM users WHERE id=?", (user_id,))  # searches, progress and log rows cascade
    conn.commit()
    audit("deleted account %s" % row["username"])
    return "", 204


@app.post("/api/admin/users/<int:user_id>/clear")
@admin_required
def admin_clear_user(user_id):
    row = db().execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        return fail("No such account.", 404)
    conn = db()
    conn.execute("DELETE FROM searches WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM progress WHERE user_id=?", (user_id,))
    conn.commit()
    audit("cleared search and watch history of %s" % row["username"])
    return "", 204


@app.get("/api/admin/requests")
@admin_required
def admin_requests():
    try:
        limit = max(1, min(500, int(request.args.get("limit", 100))))
    except ValueError:
        limit = 100
    where, args = [], []
    if request.args.get("before", "").isdigit():
        where.append("id < ?")
        args.append(int(request.args["before"]))
    if request.args.get("kind"):
        where.append("kind = ?")
        args.append(request.args["kind"])
    q = (request.args.get("q") or "").strip()[:60]
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("(username LIKE ? ESCAPE '\\' OR detail LIKE ? ESCAPE '\\' OR ip LIKE ? ESCAPE '\\')")
        args += [like, like, like]
    sql = "SELECT id, ts, user_id, username, kind, detail, ip, status FROM events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    rows = [dict(r) for r in db().execute(sql, (*args, limit + 1))]
    more = len(rows) > limit
    return jsonify(events=rows[:limit], next=rows[limit - 1]["id"] if more else None)


@app.post("/api/admin/data/clear")
@admin_required
def admin_clear_data():
    d = body()
    target = d.get("target")
    conn = db()
    deleted = 0
    if target == "requests":
        try:
            days = int(d.get("days") or 0)
        except (TypeError, ValueError):
            return fail("Bad number of days.")
        if days > 0:
            cur = conn.execute("DELETE FROM events WHERE ts < ?", (int(time.time()) - days * 86400,))
        else:
            cur = conn.execute("DELETE FROM events")
        deleted = cur.rowcount
        conn.commit()
        audit("cleared request log" + (" older than %d days" % days if days > 0 else ""))
    elif target == "usage":
        with _pending_lock:
            _pending_bytes.clear()
        deleted = conn.execute("DELETE FROM usage").rowcount
        conn.commit()
        audit("reset usage statistics")
    elif target == "search_history":
        deleted = conn.execute("DELETE FROM searches").rowcount
        conn.commit()
        audit("cleared everyone's search history")
    elif target == "watch_progress":
        deleted = conn.execute("DELETE FROM progress").rowcount
        conn.commit()
        audit("cleared everyone's watch progress")
    elif target == "caches":
        for cache in (SEARCH_CACHE, EPISODE_CACHE, PLAY_CACHE):
            cache.clear()
        audit("cleared server caches")
    elif target == "vacuum":
        conn.commit()
        raw = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
        try:
            raw.execute("VACUUM")
        finally:
            raw.close()
        audit("compacted the database")
    else:
        return fail("Unknown target.")
    return jsonify(deleted=deleted)


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


def cli(argv):
    """Server-side admin commands:  make-admin NAME | remove-admin NAME | list-users"""
    cmd = argv[0]
    conn = sqlite3.connect(DB_PATH)
    try:
        if cmd in ("make-admin", "remove-admin"):
            if len(argv) != 2:
                print("usage: python app.py %s USERNAME" % cmd)
                return 2
            flag = 1 if cmd == "make-admin" else 0
            cur = conn.execute("UPDATE users SET is_admin=? WHERE username=?", (flag, argv[1]))
            conn.commit()
            if not cur.rowcount:
                print("No account named %r. Register it on the site first." % argv[1])
                return 1
            print("%s is %s an admin." % (argv[1], "now" if flag else "no longer"))
            return 0
        if cmd == "list-users":
            for uid, name, admin, created in conn.execute("SELECT id, username, is_admin, created_at FROM users ORDER BY id"):
                print("%4d  %-32s %s  joined %s" % (uid, name, "admin" if admin else "     ",
                                                    time.strftime("%Y-%m-%d", time.localtime(created))))
            return 0
        print("commands: make-admin NAME | remove-admin NAME | list-users")
        return 2
    finally:
        conn.close()


def main():
    if len(sys.argv) > 1:
        sys.exit(cli(sys.argv[1:]))
    for tool in missing_tools():
        print(f"[mado] warning: {tool} not found on PATH")
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    try:
        from waitress import serve
    except ImportError:
        print(f"[mado] http://{host}:{port}  (Flask dev server; `pip install waitress` for a sturdier one)")
        app.run(host=host, port=port, threaded=True)
    else:
        print(f"[mado] http://{host}:{port}")
        serve(app, host=host, port=port, threads=16)


if __name__ == "__main__":
    main()

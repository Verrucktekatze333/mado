# Mado

A self-hosted web front end for [ani-cli](https://github.com/pystardust/ani-cli). Search for an anime,
pick a result, pick an episode and watch it in your browser. It autoplays the next episode and
remembers, for every user, what they searched for and where they stopped watching.

Python (Flask) backend, a single static HTML page for the UI, SQLite for storage. No build step,
no JavaScript tooling, and it runs fine on small or old hardware.

## Features

- **Search** by anime name, with a Sub / Dub switch.
- **Pick a result, then an episode**, then watch it in the built-in player (HLS and MP4, subtitles when available).
- **Autoplay next episode**, which can be switched off. The next stream link is fetched in the
  background while you watch, so episodes follow each other without a long wait.
- **Accounts**: each user has their own search history and a "Continue watching" list with the last
  episode played and the position within it. Opening a show resumes where you left off.
- **Quality picker** (best, 1080p, 720p, 480p, 360p) and Previous / Next buttons.
- **Suggestions** below the search bar: a built-in list of well-known anime with genre filters and a shuffle button. Signed-in users get picks based on the genres of what they already watch, without titles they've seen. The list is the `CATALOG` array in `static/index.html`, so you can add your own entries.
- **Guest mode**: "Continue as guest" lets someone search and watch without an account; no history or personal details are stored.
- **Admin panel** for the server owner: usage overview, user management, request log and data cleanup (see below).
- Everything about finding shows, episodes and streams comes from ani-cli itself, so when ani-cli is
  updated to follow site changes, Mado benefits too.

## How it works

Mado runs ani-cli non-interactively. It puts two small stand-in programs first on ani-cli's `PATH`:

- one for `fzf` / `rofi` / `dmenu`, which records the menu ani-cli would show (the search results or
  the episode list) and then exits like a cancelled pick;
- one for `mpv`, which ani-cli calls with the stream URL, referrer, subtitle file and title. Mado
  records those instead of playing anything.

The stream is then served to your browser through a small proxy (`/proxy/...`) that adds the `Referer`
header the video hosts require and rewrites HLS playlists. Proxy links are signed and expire.
That means **you don't need mpv or fzf installed**.

```
browser ──► Flask (app.py) ──► ani-cli ──► streaming source
   ▲             │
   └── /proxy ◄──┘     SQLite: users, search history, watch progress
```

## Requirements

- Linux (or macOS / WSL) with Python 3.9+
- [ani-cli](https://github.com/pystardust/ani-cli) on the `PATH`
- `curl`, `sed`, `grep`, `od` and `base64` (or `openssl`), which ani-cli itself needs
- No special CPU features: everything the app installs is pure Python or baseline x86-64, so old
  Intel Atom machines are fine.

## Install

```bash
git clone <your-repo-url> mado
cd mado
./install.sh
./run.sh            # then open http://127.0.0.1:8000
```

`install.sh` creates a virtualenv in `.venv`, installs the Python dependencies, checks for the
programs above, offers to download ani-cli if it's missing and writes a `run.sh` start script.

Options:

| Option | What it does |
| --- | --- |
| `-y` | Say yes to every question (non-interactive) |
| `--service` | Install and start a systemd service called `mado` (run with `sudo`) |
| `--host ADDR` | Address to listen on (default `127.0.0.1`) |
| `--port N` | Port to listen on (default `8000`) |
| `--https` | Mark session cookies as secure (use when served over HTTPS) |
| `--behind-proxy` | Trust `X-Forwarded-For` / `-Proto` from one reverse proxy (real visitor IPs) |

Example for a home server behind a reverse proxy:

```bash
sudo ./install.sh -y --service --https --port 8000
```

### Manual install

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install waitress      # optional, sturdier than Flask's dev server
.venv/bin/python app.py
```

## Configuration

Set these as environment variables.

| Variable | Default | Meaning |
| --- | --- | --- |
| `HOST` | `127.0.0.1` | Address to listen on |
| `PORT` | `8000` | Port to listen on |
| `ANIWEB_DATA_DIR` | `./data` | Where the database and secret key are stored |
| `ANI_CLI_BIN` | `ani-cli` | Path to the ani-cli executable |
| `ANIWEB_MAX_JOBS` | `4` | How many ani-cli processes may run at once |
| `ANIWEB_HTTPS` | unset | Set to `1` to mark cookies as secure (when served over HTTPS) |
| `ANIWEB_TRUST_PROXY` | unset | Set to `1` when behind one reverse proxy so visitor IPs and https are read from its headers |
| `ANIWEB_LOG_DAYS` | `60` | How long the request log is kept (it also keeps at most 50,000 entries) |

## Admin panel

Admins are made on the server, never through the website:

```bash
# 1. register the account on the site as usual, then on the server:
.venv/bin/python app.py make-admin YOURNAME      # run it in the Mado folder, as the same user that runs Mado
.venv/bin/python app.py remove-admin YOURNAME
.venv/bin/python app.py list-users
```

Sign in again and an **Admin** button appears in the header. It has four tabs:

- **Overview**: viewers watching right now, open streams, running ani-cli jobs, data streamed (today / 7 / 30 days / all time),
  requests, failed logins, CPU load, memory, free disk space, plus a per-day usage table and the heaviest users.
- **Users**: every account with last activity, requests, data streamed and saved history. Clear a user's history or
  delete the account (which also removes their searches, watch progress, log entries and usage). Admin accounts can't be
  deleted from the panel; remove their admin rights on the server first. A deleted user is signed out immediately.
- **Requests**: the request log (searches, episode lists, plays, logins, failed logins, sign-ups, admin actions) with
  time, user, detail, IP and status; filter by kind or text.
- **Data**: delete the request log (all or older than 7 / 30 days), reset usage statistics, delete everybody's search
  history or watch progress, clear caches, compact the database.

Guests are logged only as anonymous counters: the kind of request, but no search text and no IP address.
Admin actions (deleting accounts, clearing data) are recorded in the request log.

## Running it publicly

Mado is meant to sit behind a reverse proxy (nginx, Caddy, ...) that terminates HTTPS. Keep these in mind:

- Set `ANIWEB_HTTPS=1` so the login cookie is only sent over HTTPS.
- **Anyone who can reach the site can register an account** and use your server to run ani-cli and
  relay video, which costs you bandwidth and CPU. Only expose it to people you trust, or restrict
  access at the proxy (basic auth, VPN, IP allowlist).
- Behind a proxy the app sees the proxy's address for every visitor, which makes the request log useless and applies
  the failed-login limit (10 attempts per 10 minutes) to everyone together. Set `ANIWEB_TRUST_PROXY=1` (or install with
  `--behind-proxy`) so the real visitor address from `X-Forwarded-For` is used. Only do this when the app is reachable
  *only* through your proxy, otherwise visitors could fake their address.

## Data and backups

Everything lives in `data/` (or `ANIWEB_DATA_DIR`):

- `mado.db`: SQLite database with users, search history, watch progress, the request log and usage statistics
- `secret.key`: signs sessions and stream links (deleting it logs everybody out)
- `ani-cli-state/`: ani-cli's own state directory

Copy the folder to back it up or move to another machine. Passwords are stored as salted hashes.

## Troubleshooting

- **"ani-cli isn't installed on the server"**: install it, or point `ANI_CLI_BIN` at it. Make sure it's on
  the `PATH` of whatever runs Mado (systemd services don't read your shell profile).
- **Search or playback suddenly stops working**: ani-cli breaks when its sources change. Run
  `ani-cli -U` to update it.
- **A stream won't load**: press "Try again", or pick a lower quality.
- **`Illegal instruction` when starting Python**: your Python build targets a newer CPU than yours.
  Use your distribution's Python package.

## Disclaimer

Mado is a personal, self-hosted tool. It doesn't host or index any content; it relays what ani-cli
finds from third-party sources. You are responsible for what you stream and for complying with the
laws and terms that apply to you.

## Credits

Built on [ani-cli](https://github.com/pystardust/ani-cli) and [hls.js](https://github.com/video-dev/hls.js).

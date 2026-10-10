#!/usr/bin/env bash
# Mado installer: sets up a Python virtualenv, installs dependencies, checks the
# programs ani-cli needs and (optionally) installs ani-cli and a systemd service.
#
#   ./install.sh                 set up in ./.venv and print how to start it
#   ./install.sh -y              answer "yes" to every question (non-interactive)
#   ./install.sh --service       also install + start a systemd service (needs root)
#   ./install.sh --port 8080     port used by the service / printed start command
#   ./install.sh --https         service sets ANIWEB_HTTPS=1 (use behind HTTPS)
#   ./install.sh --behind-proxy  service trusts X-Forwarded-For/-Proto from one reverse proxy
#   ./install.sh --host 0.0.0.0  address the service listens on (default 127.0.0.1)
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$DIR/.venv"
ANI_CLI_URL="https://raw.githubusercontent.com/pystardust/ani-cli/master/ani-cli"

ASSUME_YES=0; SERVICE=0; HTTPS=0; PROXY=0; PORT=8000; HOST=127.0.0.1

while [ $# -gt 0 ]; do
  case "$1" in
    -y|--yes) ASSUME_YES=1 ;;
    --service) SERVICE=1 ;;
    --https) HTTPS=1 ;;
    --behind-proxy) PROXY=1 ;;
    --port) PORT="${2:?--port needs a value}"; shift ;;
    --host) HOST="${2:?--host needs a value}"; shift ;;
    -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done

say()  { printf '\033[1;35m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }

ask() {  # ask "question" -> 0 for yes
  [ "$ASSUME_YES" = 1 ] && return 0
  [ -t 0 ] || return 1
  read -r -p "$1 [y/N] " reply
  case "$reply" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

[ -f "$DIR/app.py" ] && [ -f "$DIR/static/index.html" ] \
  || die "app.py or static/index.html is missing. Run this script from the Mado folder."

# ---- Python ---------------------------------------------------------------
say "Checking Python"
command -v python3 >/dev/null || die "python3 not found. Install Python 3.9 or newer."
python3 - <<'EOF' || die "Python 3.9 or newer is required."
import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)
EOF
echo "    $(python3 --version)"

say "Creating virtualenv in .venv"
if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV" 2>/dev/null || die "Couldn't create a virtualenv. On Debian/Ubuntu: sudo apt install python3-venv"
fi

say "Installing Python dependencies"
"$VENV/bin/python" -m pip install --quiet --upgrade pip || warn "pip upgrade failed, continuing"
"$VENV/bin/python" -m pip install --quiet -r "$DIR/requirements.txt"
# waitress is a sturdier server than Flask's built-in one; failing to get it is fine
"$VENV/bin/python" -m pip install --quiet waitress \
  || warn "waitress not installed; Mado will use Flask's built-in server."

# ---- Programs ani-cli depends on ------------------------------------------
say "Checking programs ani-cli needs"
missing=()
for tool in curl sed grep od; do command -v "$tool" >/dev/null || missing+=("$tool"); done
command -v base64 >/dev/null || command -v openssl >/dev/null || missing+=("base64 (or openssl)")
if [ ${#missing[@]} -gt 0 ]; then
  warn "Missing: ${missing[*]}"
  warn "Install them with your package manager (e.g. sudo apt install curl sed grep coreutils openssl)."
fi

# ---- ani-cli --------------------------------------------------------------
say "Checking ani-cli"
if command -v ani-cli >/dev/null; then
  echo "    found: $(command -v ani-cli)"
else
  warn "ani-cli is not installed."
  if ask "Download ani-cli from GitHub now?"; then
    if [ "$(id -u)" = 0 ]; then target=/usr/local/bin; else target="$HOME/.local/bin"; fi
    mkdir -p "$target"
    curl -fsSL "$ANI_CLI_URL" -o "$target/ani-cli" || die "Download failed. Install ani-cli manually: https://github.com/pystardust/ani-cli"
    chmod +x "$target/ani-cli"
    echo "    installed: $target/ani-cli"
    case ":$PATH:" in *":$target:"*) ;; *) warn "$target is not on your PATH; add it to your shell profile." ;; esac
    ANI_DIR="$target"
  else
    warn "Install it before starting Mado: https://github.com/pystardust/ani-cli"
  fi
fi

# ---- Start script ---------------------------------------------------------
say "Writing run.sh"
cat > "$DIR/run.sh" <<EOF
#!/usr/bin/env bash
# Starts Mado. Override with HOST=..., PORT=..., ANIWEB_HTTPS=1 in the environment.
cd "\$(dirname "\${BASH_SOURCE[0]}")"
export PATH="\$HOME/.local/bin:\$PATH"
export HOST="\${HOST:-$HOST}" PORT="\${PORT:-$PORT}"
exec .venv/bin/python app.py
EOF
chmod +x "$DIR/run.sh"

# ---- systemd service (optional) -------------------------------------------
if [ "$SERVICE" = 1 ]; then
  say "Installing systemd service"
  [ "$(id -u)" = 0 ] || die "--service needs root. Re-run with: sudo ./install.sh --service"
  command -v systemctl >/dev/null || die "systemctl not found; this system doesn't use systemd."
  RUN_USER="${SUDO_USER:-root}"
  RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
  chown -R "$RUN_USER" "$DIR"
  cat > /etc/systemd/system/mado.service <<EOF
[Unit]
Description=Mado (ani-cli web player)
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$DIR
Environment=HOST=$HOST
Environment=PORT=$PORT
Environment=ANIWEB_HTTPS=$HTTPS
Environment=ANIWEB_TRUST_PROXY=$PROXY
Environment=PATH=$RUN_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=$VENV/bin/python $DIR/app.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now mado.service
  echo "    service running: systemctl status mado"
fi

# ---- Done -----------------------------------------------------------------
say "Done"
if [ "$SERVICE" = 1 ]; then
  echo "    Mado is running at http://$HOST:$PORT  (logs: journalctl -u mado -f)"
else
  echo "    Start it with:  ./run.sh      then open http://$HOST:$PORT"
fi
[ "$HOST" = 127.0.0.1 ] && echo "    It only listens on this machine; put a reverse proxy in front of it to reach it elsewhere."
exit 0

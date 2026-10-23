#!/bin/sh
# Setu — one-command install (macOS).
#
#   ./install.sh                              # this machine: Zonito's account, local Kubera
#   ./install.sh U26948221 http://100.125.239.56:8000
#                                             # Nikhila's machine: her account, Kubera on the Mac Studio
#
# What it does, idempotently (re-run any time to upgrade or repair):
#   1. Creates ~/.setu with a Python venv and installs ibapi
#   2. Copies setu.py into ~/.setu
#   3. Writes ~/.setu/config.json if absent (keeps an existing one)
#   4. Installs and (re)starts the LaunchAgent com.molang.setu
#      (starts at login, restarts itself if it ever exits)
#   5. Shows the first log lines so you can see it connect
#
# The only prerequisite: the Kubera ingest token in ~/.kubera-ingest-token
# (the same file the IBKR Enhancer setup created on the Mac Studio; on a
# second machine, copy that one line into a file of the same name).
set -eu
cd "$(dirname "$0")"

ACCOUNT_ID="${1:-U19377323}"
KUBERA_BASE="${2:-http://127.0.0.1:8000}"
SETU_HOME="$HOME/.setu"
LABEL="com.molang.setu"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
TOKEN_FILE="$HOME/.kubera-ingest-token"

if [ ! -f "$TOKEN_FILE" ]; then
  echo "Missing $TOKEN_FILE"
  echo "Put the Kubera ingest token (one line) in that file, then re-run ./install.sh"
  exit 1
fi

mkdir -p "$SETU_HOME/logs" "$HOME/Library/LaunchAgents"

echo "1/5 Python environment"
PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "python3 not found"; exit 1; }
[ -x "$SETU_HOME/venv/bin/python" ] || "$PY" -m venv "$SETU_HOME/venv"
"$SETU_HOME/venv/bin/pip" install -q -r requirements.txt

echo "2/5 Program"
cp setu.py "$SETU_HOME/setu.py"

echo "3/5 Config"
if [ -f "$SETU_HOME/config.json" ]; then
  echo "    keeping existing $SETU_HOME/config.json"
else
  cat > "$SETU_HOME/config.json" <<EOF
{
  "account_id": "$ACCOUNT_ID",
  "account_label": null,
  "ib_host": "127.0.0.1",
  "ib_port": 4001,
  "client_id": 21,
  "kubera_base": "$KUBERA_BASE",
  "token_file": "~/.kubera-ingest-token",
  "interval_seconds": 60,
  "heartbeat_seconds": 300
}
EOF
  echo "    wrote $SETU_HOME/config.json (account $ACCOUNT_ID, kubera $KUBERA_BASE)"
fi

echo "4/5 Launch agent"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$SETU_HOME/venv/bin/python</string>
    <string>$SETU_HOME/setu.py</string>
    <string>--config</string>
    <string>$SETU_HOME/config.json</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>StandardOutPath</key><string>$SETU_HOME/logs/setu.log</string>
  <key>StandardErrorPath</key><string>$SETU_HOME/logs/setu.log</string>
</dict>
</plist>
EOF
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST"
launchctl kickstart -k "gui/$UID/$LABEL" || true

echo "5/5 First breath (log tail)"
sleep 4
tail -n 6 "$SETU_HOME/logs/setu.log" || echo "    (log not written yet; check $SETU_HOME/logs/setu.log)"
echo
echo "Done. Setu now starts at every login and reconnects on its own."
echo "Your side from here: just log in to IBKR Gateway. Setu does the rest."
echo "Logs: $SETU_HOME/logs/setu.log   Stop: ./uninstall.sh"

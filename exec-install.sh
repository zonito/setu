#!/bin/sh
# setu-exec — install the guarded GTC executor (macOS).
#
#   ./exec-install.sh
#
# Separate from ./install.sh (the read-only Setu daemon) on purpose:
# its own LaunchAgent (com.molang.setu-exec), its own program file,
# its own logs. It shares the ~/.setu venv and config that install.sh
# created — run ./install.sh first if the venv is missing.
#
# Installing does NOT arm anything. The executor starts disarmed and
# in simulate mode (executor.json). Daily life:
#   arm:      ~/.setu/venv/bin/python ~/.setu/setu_exec.py arm
#   disarm:   ~/.setu/venv/bin/python ~/.setu/setu_exec.py disarm
# Live mode additionally needs mode "live" in ~/.setu/executor.json
# AND the enable file (setu_exec.py enable-live). All three, or the
# executor refuses to place anything.
set -eu
cd "$(dirname "$0")"

SETU_HOME="$HOME/.setu"
LABEL="com.molang.setu-exec"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ ! -x "$SETU_HOME/venv/bin/python" ]; then
  echo "No venv at $SETU_HOME/venv — run ./install.sh first, then re-run ./exec-install.sh"
  exit 1
fi

mkdir -p "$SETU_HOME/logs" "$HOME/Library/LaunchAgents"

echo "1/3 Program"
cp setu_exec.py "$SETU_HOME/setu_exec.py"
cp webapi_probe.py "$SETU_HOME/webapi_probe.py"

echo "2/3 Executor config"
if [ -f "$SETU_HOME/executor.json" ]; then
  echo "    keeping existing $SETU_HOME/executor.json"
else
  cat > "$SETU_HOME/executor.json" <<EOF
{
  "mode": "simulate",
  "exec_client_id": 91,
  "exec_poll_seconds": 30
}
EOF
  echo "    wrote $SETU_HOME/executor.json (mode: simulate)"
fi

echo "3/3 Launch agent"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$SETU_HOME/venv/bin/python</string>
    <string>$SETU_HOME/setu_exec.py</string>
    <string>--config</string>
    <string>$SETU_HOME/config.json</string>
    <string>run</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>StandardOutPath</key><string>$SETU_HOME/logs/setu-exec.log</string>
  <key>StandardErrorPath</key><string>$SETU_HOME/logs/setu-exec.log</string>
</dict>
</plist>
EOF
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST"
launchctl kickstart -k "gui/$UID/$LABEL" || true

sleep 3
tail -n 4 "$SETU_HOME/logs/setu-exec.log" || echo "    (log not written yet; check $SETU_HOME/logs/setu-exec.log)"
echo
echo "Done. The executor is installed DISARMED and in simulate mode."
echo "Arm for today:  $SETU_HOME/venv/bin/python $SETU_HOME/setu_exec.py arm"
echo "Stop:           ./exec-uninstall.sh    Logs: $SETU_HOME/logs/setu-exec.log"

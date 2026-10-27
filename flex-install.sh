#!/bin/sh
# flex_pull — install the morning Flex statement fetcher (macOS).
#
#   ./flex-install.sh
#
# Its own LaunchAgent (com.molang.flex-pull), firing daily at
# 06:45 local time. It shares the ~/.setu venv and home that
# install.sh created — run ./install.sh first if the venv is
# missing. Installing does not create Flex queries or tokens:
# those are portal steps (see README, "Flex morning fetch").
# Until ~/.setu/flex-pull.json names real jobs, each run logs
# "no jobs configured" and exits without touching anything.
set -eu
cd "$(dirname "$0")"

SETU_HOME="$HOME/.setu"
LABEL="com.molang.flex-pull"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ ! -x "$SETU_HOME/venv/bin/python" ]; then
  echo "No venv at $SETU_HOME/venv — run ./install.sh first, then re-run ./flex-install.sh"
  exit 1
fi

mkdir -p "$SETU_HOME/logs" "$HOME/Library/LaunchAgents"

echo "1/3 Program"
cp flex_pull.py "$SETU_HOME/flex_pull.py"

echo "2/3 Flex config"
if [ -f "$SETU_HOME/flex-pull.json" ]; then
  echo "    keeping existing $SETU_HOME/flex-pull.json"
else
  cat > "$SETU_HOME/flex-pull.json" <<EOF
{
  "jobs": []
}
EOF
  echo "    wrote $SETU_HOME/flex-pull.json (no jobs yet — see README)"
fi

echo "3/3 Launch agent (daily 06:45 local)"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$SETU_HOME/venv/bin/python</string>
    <string>$SETU_HOME/flex_pull.py</string>
    <string>run</string>
    <string>--config</string>
    <string>$SETU_HOME/flex-pull.json</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>6</integer>
    <key>Minute</key><integer>45</integer>
  </dict>
  <key>StandardOutPath</key><string>$SETU_HOME/logs/flex-pull.log</string>
  <key>StandardErrorPath</key><string>$SETU_HOME/logs/flex-pull.log</string>
</dict>
</plist>
EOF
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST"

echo
echo "Done. flex_pull fires daily at 06:45 local time."
echo "Run once now:  launchctl kickstart gui/$UID/$LABEL"
echo "Config:        $SETU_HOME/flex-pull.json    Logs: $SETU_HOME/logs/flex-pull.log"
echo "Last run:      $SETU_HOME/flex-last-run.json"

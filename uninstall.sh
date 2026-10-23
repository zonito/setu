#!/bin/sh
# Setu — stop and unload the launch agent. Keeps ~/.setu (config, venv,
# logs) so a later ./install.sh picks up exactly where this left off.
set -eu
LABEL="com.molang.setu"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
rm -f "$PLIST"
echo "Setu stopped and unloaded. ~/.setu kept (config, venv, logs)."
echo "Re-run ./install.sh to start it again."

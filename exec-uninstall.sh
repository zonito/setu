#!/bin/sh
# setu-exec — stop and unload the executor's launch agent. Keeps
# ~/.setu (config, executor.json, arm/enable files, state, logs) so a
# later ./exec-install.sh picks up exactly where this left off. The
# read-only Setu daemon (com.molang.setu) is not touched.
set -eu
LABEL="com.molang.setu-exec"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
rm -f "$PLIST"
echo "setu-exec stopped and unloaded. ~/.setu kept."
echo "Re-run ./exec-install.sh to start it again."

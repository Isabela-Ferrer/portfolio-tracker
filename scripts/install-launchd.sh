#!/bin/bash
# Installs a launchd agent that keeps the tracker running whenever the Mac is
# on, so the dashboard is always at http://localhost:8000 and the scheduler
# can catch up on any refresh or digest the laptop slept through.
#
# Also moves the live database out of iCloud Drive: iCloud syncing a SQLite
# file mid-write is a corruption risk, so the agent points DB_PATH at
# ~/Library/Application Support/dream-tracker/ and the existing database is
# copied there on first install. Rerunning the script is safe; it reloads the
# agent with the current repo path and never overwrites an existing database.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY=/Library/Frameworks/Python.framework/Versions/3.11/bin/python3
LABEL=com.isa.dream-tracker
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DB_DIR="$HOME/Library/Application Support/dream-tracker"
LOG="$HOME/Library/Logs/dream-tracker.log"

mkdir -p "$DB_DIR" "$HOME/Library/LaunchAgents"

if [ ! -f "$DB_DIR/tracker.db" ] && [ -f "$REPO/data/tracker.db" ]; then
  cp "$REPO/data/tracker.db" "$DB_DIR/tracker.db"
  echo "Copied existing database to $DB_DIR/tracker.db"
fi

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>-m</string><string>uvicorn</string>
    <string>main:app</string>
    <string>--host</string><string>127.0.0.1</string>
    <string>--port</string><string>8000</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>DB_PATH</key><string>$DB_DIR/tracker.db</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLIST

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

echo "Installed and started."
echo "  Dashboard: http://localhost:8000"
echo "  Logs:      $LOG"
echo "  Database:  $DB_DIR/tracker.db"
echo "  Uninstall: launchctl unload \"$PLIST\" && rm \"$PLIST\""

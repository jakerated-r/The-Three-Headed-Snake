#!/usr/bin/env bash
# Install/restart all Three Headed Snake per-head listeners under launchd.
set -uo pipefail

ROOT="${THREE_HEADED_SNAKE_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
LA="$HOME/Library/LaunchAgents"
mkdir -p "$LA" "$ROOT/runs/listeners" "$ROOT/logs"

write_plist() {
  local head="$1"
  local lower="$2"
  local label="com.example.three-headed-snake-listener-$lower"
  local plist="$LA/$label.plist"
  cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
 <key>Label</key><string>$label</string>
 <key>ProgramArguments</key><array>
  <string>/usr/bin/python3</string><string>$ROOT/src/listeners/snake-listener.py</string><string>$head</string>
 </array>
 <key>WorkingDirectory</key><string>$ROOT</string>
 <key>EnvironmentVariables</key><dict>
  <key>THREE_HEADED_SNAKE_ROOT</key><string>$ROOT</string>
  <key>COOP_ROOT</key><string>$ROOT/data/coop</string>
  <key>THREE_HEADED_SNAKE_LISTENER_RUN_DIR</key><string>$ROOT/runs/listeners</string>
  <key>SNAKE_POLL_MS</key><string>${SNAKE_POLL_MS:-75}</string>
  <key>SNAKE_FRESH_S</key><string>${SNAKE_FRESH_S:-240}</string>
 </dict>
 <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
 <key>StandardOutPath</key><string>$ROOT/logs/listener-$lower.log</string>
 <key>StandardErrorPath</key><string>$ROOT/logs/listener-$lower.log</string>
</dict></plist>
PLIST
}

for spec in Codex:codex Maestro:maestro Gemini:gemini; do
  head="${spec%%:*}"
  lower="${spec##*:}"
  label="com.example.three-headed-snake-listener-$lower"
  write_plist "$head" "$lower"
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$LA/$label.plist" 2>&1 || launchctl load "$LA/$label.plist" 2>&1 || true
done

sleep 2
for lower in codex maestro gemini; do
  label="com.example.three-headed-snake-listener-$lower"
  launchctl print "gui/$(id -u)/$label" 2>/dev/null | grep -E 'state =|pid =|last exit code' | sed "s/^/[$lower] /" || true
done

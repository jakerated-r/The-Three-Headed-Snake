#!/usr/bin/env bash
# Install/restart all Three Headed Snake per-head listeners under launchd.
set -uo pipefail
BRAIN="/Users/rated-r/rated r brain"
S="$BRAIN/outputs/coop-tools/snake"
LA="$HOME/Library/LaunchAgents"
mkdir -p "$LA"

for head in codex maestro gemini; do
  src="$S/com.jakeratedr.snake-listener-$head.plist"
  dst="$LA/com.jakeratedr.snake-listener-$head.plist"
  cp "$src" "$dst"
  launchctl bootout "gui/$(id -u)/com.jakeratedr.snake-listener-$head" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$dst" 2>&1 || launchctl load "$dst" 2>&1 || true
  launchctl kickstart -k "gui/$(id -u)/com.jakeratedr.snake-listener-$head" 2>/dev/null || true
done

sleep 2
for head in codex maestro gemini; do
  label="com.jakeratedr.snake-listener-$head"
  launchctl print "gui/$(id -u)/$label" 2>/dev/null | grep -E 'state =|pid =|last exit code' | sed "s/^/[$head] /" || true
done

#!/bin/bash
# Build the Kakao MCP launcher, link `kakao-mcp` into ~/.local/bin, and run the
# daemon at login through launchd. Safe to run again after an update.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
LABEL="com.bsiku3622.kakao-mcp"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

APP="$(launcher/build.sh)"
bridge/build.sh >/dev/null
mkdir -p "$HOME/.local/bin" "$HOME/Library/LaunchAgents" "$ROOT/data"
ln -sf "$ROOT/bin/kkt-mcp" "$HOME/.local/bin/kakao-mcp"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$APP/Contents/MacOS/kakao-mcp-launcher</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardErrorPath</key>
    <string>$ROOT/data/launchd.err</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "daemon: $LABEL -> $APP"
echo "allow 'Kakao MCP' in System Settings > Privacy & Security > Accessibility and Screen & System Audio Recording"

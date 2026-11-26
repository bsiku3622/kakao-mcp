#!/bin/bash
# Build build/Kakao MCP.app with swiftc (no Xcode project). Rebuilding changes the
# ad-hoc signature, so macOS may ask for the privacy permissions again.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
APP="$ROOT/build/Kakao MCP.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
swiftc -O launcher/main.swift -o "$APP/Contents/MacOS/kakao-mcp-launcher"
sed "s#__KKT_ROOT__#$ROOT#" launcher/Info.plist > "$APP/Contents/Info.plist"
codesign --force --sign - "$APP" >/dev/null
echo "$APP"

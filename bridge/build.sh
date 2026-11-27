#!/bin/bash
# Build build/kakao-mcp-bridge, the small stdio relay that bin/kkt-mcp --bridge prefers.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p build
swiftc -O bridge/main.swift -o build/kakao-mcp-bridge
codesign --force --sign - build/kakao-mcp-bridge >/dev/null
echo "$PWD/build/kakao-mcp-bridge"

#!/usr/bin/env bash
# macOS: double-click to start AzCopy Runner (first time: chmod +x "Start AzCopyRunner.command")
cd "$(dirname "$0")"
exec bash ./start.sh "$@"

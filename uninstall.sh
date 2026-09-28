#!/bin/bash
# Removes the login item, Photoman.app and the virtual environment. Your photo library, config and import logs are left untouched.
set -euo pipefail
LABEL="com.photoman.menubar"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
rm -rf "$HOME/Applications/Photoman.app" "$HOME/.photoman/venv" "$HOME/.photoman/build" "$HOME/.photoman/photoman"
echo "Uninstalled. Config is kept at ~/.photoman/config.json; the photo library is unaffected."

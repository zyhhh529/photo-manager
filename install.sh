#!/bin/bash
# Photoman installer (macOS): installs exiftool, creates a Python venv, builds ~/Applications/Photoman.app
# and sets it up to start at login
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
HOME_DIR="$HOME/.photoman"
VENV="$HOME_DIR/venv"
LABEL="com.photoman.menubar"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
APP="$HOME/Applications/Photoman.app"

echo "==> Project directory: $PROJECT_DIR"

# 1. exiftool (reads capture time from Nikon NEF / MOV)
if ! command -v exiftool >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    echo "==> Installing exiftool"
    brew install exiftool
  else
    echo "!! Homebrew not found. Install Homebrew (https://brew.sh) first, or install exiftool manually."
    echo "   Photoman works without exiftool, but will use file modification times as capture dates."
  fi
fi

# 2. Python virtual environment
if [ -x /opt/homebrew/bin/python3 ]; then PY=/opt/homebrew/bin/python3; else PY=/usr/bin/python3; fi
echo "==> Creating virtual environment $VENV with $PY"
mkdir -p "$HOME_DIR"
"$PY" -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q rumps py2app

# 3. Command line shortcut
cat > "$HOME_DIR/photoman" <<EOF
#!/bin/bash
cd "$PROJECT_DIR" && exec "$VENV/bin/python" -m photoman.cli "\$@"
EOF
chmod +x "$HOME_DIR/photoman"

# 4. Photoman.app (alias build: runs the code in this folder, so keep the folder where it is)
echo "==> Building $APP"
mkdir -p "$HOME/Applications"
rm -rf "$APP"
(cd "$PROJECT_DIR" && "$VENV/bin/python" packaging/make_icon.py \
  && "$VENV/bin/python" setup.py -q py2app -A --dist-dir "$HOME/Applications" --bdist-base "$HOME_DIR/build" >/dev/null)

# 5. Start at login (LaunchAgent)
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$APP/Contents/MacOS/Photoman</string>
  </array>
  <key>WorkingDirectory</key><string>$PROJECT_DIR</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$HOME_DIR/menubar.log</string>
  <key>StandardErrorPath</key><string>$HOME_DIR/menubar.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo
echo "✅ Installed! A 📷 icon should appear in the menu bar. (Photoman is also in ~/Applications.)"
echo "   The first time you insert a card, macOS may ask whether Photoman can access removable volumes. Click \"Allow\"."
echo "   Command line: $HOME_DIR/photoman status"
echo "   Log: $HOME_DIR/menubar.log"

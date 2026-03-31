#!/bin/bash
# SpeakFlow — Update to latest version
set -e
cd "$HOME/.speakflow"

echo "Updating SpeakFlow..."
git pull --quiet
source venv/bin/activate
pip install --quiet -r requirements.txt

# Rebuild .app launcher in case it changed
APP="$HOME/Desktop/SpeakFlow.app"
if [ -f launcher.c ]; then
    cc -o "$APP/Contents/MacOS/SpeakFlow" launcher.c
fi

echo "Updated! Restart SpeakFlow to apply changes."

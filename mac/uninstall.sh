#!/bin/bash
# Removes Herdr Voice, its settings, autostart entry, caches and permissions.
set -uo pipefail

pkill -x HerdrVoice 2>/dev/null && sleep 1
rm -rf "$HOME/Applications/HerdrVoice.app" \
       "$HOME/Library/LaunchAgents/com.herdr.voice.plist" \
       "$HOME/Library/Application Support/HerdrVoice" \
       "$HOME/Library/Caches/HerdrVoice-build" \
       "$HOME/Library/Caches/HerdrVoice-model"
tccutil reset All com.herdr.voice >/dev/null 2>&1
printf 'Herdr Voice удалён.\n'

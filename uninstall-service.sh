#!/bin/bash
# Спира услугата (нищо не се трие — базата и кодът остават).
for n in newsroom newsroom.backup; do
  launchctl bootout "gui/$(id -u)/bg.obshtestvoto.$n" 2>/dev/null && echo "спряно: $n"
  rm -f "$HOME/Library/LaunchAgents/bg.obshtestvoto.$n.plist"
done

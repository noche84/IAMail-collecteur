#!/usr/bin/env bash
# Désinstalle IAMail (Mac) : arrête le LaunchAgent, retire l'exécutable et le raccourci.
# La config, la clé IA, l'archive et l'index sont CONSERVÉS.
set -e
APP="$HOME/Library/Application Support/IAMail"
PLIST="$HOME/Library/LaunchAgents/be.iamail.compagnon.plist"
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
rm -f "$PLIST" "$APP/iamail" "$HOME/Applications/Cockpit IAMail.webloc"
echo "✅ IAMail désinstallé. Conservés : « $APP » (config, clé) et ton archive."

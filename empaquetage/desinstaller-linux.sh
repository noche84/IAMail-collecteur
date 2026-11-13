#!/usr/bin/env bash
# Retire IAMail (service + exécutable + raccourci). Conserve la config et l'archive.
set -e
systemctl --user disable --now iamail.service >/dev/null 2>&1 || true
rm -f "$HOME/.config/systemd/user/iamail.service"
rm -f "$HOME/.local/bin/iamail"
rm -f "$HOME/.local/share/applications/iamail-cockpit.desktop"
systemctl --user daemon-reload || true
echo "✅ IAMail désinstallé. Config conservée dans ~/.config/iamail, archive intacte."
echo "   (Pour tout effacer : rm -rf ~/.config/iamail)"

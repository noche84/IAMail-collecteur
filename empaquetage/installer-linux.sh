#!/usr/bin/env bash
# Installe IAMail pour l'utilisateur courant : exécutable + service auto-démarrant
# (caché, sans terminal) + raccourci cockpit. AUCUN sudo, AUCUN Python requis.
#
#   bash empaquetage/installer-linux.sh [chemin/vers/iamail]
#
# Sans argument, prend compagnon/dist/iamail (sortie de construire-linux.sh).
set -e
ICI="$(cd "$(dirname "$0")" && pwd)"
EXE="${1:-$ICI/../compagnon/dist/iamail}"
[ -x "$EXE" ] || { echo "Exécutable introuvable : $EXE
Construis-le d'abord :  bash empaquetage/construire-linux.sh"; exit 1; }

BIN="$HOME/.local/bin"; CFG="$HOME/.config/iamail"
UNIT="$HOME/.config/systemd/user"; APPS="$HOME/.local/share/applications"
mkdir -p "$BIN" "$CFG" "$UNIT" "$APPS"

install -m 755 "$EXE" "$BIN/iamail"
echo "• Exécutable installé : $BIN/iamail"

# Clé IA (facultative) — fichier privé, jamais en clair dans la config.
if [ ! -f "$CFG/env" ]; then
  printf 'MISTRAL_API_KEY=\n' > "$CFG/env"; chmod 600 "$CFG/env"
  echo "• Clé IA : colle-la dans $CFG/env (MISTRAL_API_KEY=…) — facultatif (mode mock sinon)"
fi

# Config — créée une fois, à personnaliser (boîte, adresses, dossiers).
if [ ! -f "$CFG/config.json" ]; then
  cp "$ICI/../compagnon/config.exemple.json" "$CFG/config.json"
  echo "• Config créée : $CFG/config.json  (à personnaliser : mbox_sources, mes_adresses, dossiers)"
fi

# Service utilisateur : démarre tout seul, tourne caché, redémarre si besoin.
cat > "$UNIT/iamail.service" <<UNITEOF
[Unit]
Description=IAMail — tri des emails par IA (compagnon)
After=network-online.target

[Service]
Type=simple
EnvironmentFile=-%h/.config/iamail/env
ExecStart=%h/.local/bin/iamail --config %h/.config/iamail/config.json service
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
UNITEOF

# Raccourci « Ouvrir le cockpit » dans le menu.
cat > "$APPS/iamail-cockpit.desktop" <<DESKEOF
[Desktop Entry]
Type=Application
Name=Cockpit IAMail
Comment=Ouvrir le cockpit de tri des emails
Exec=xdg-open http://127.0.0.1:8740/
Icon=mail-message-new
Terminal=false
Categories=Office;Email;
DESKEOF
update-desktop-database "$APPS" >/dev/null 2>&1 || true

systemctl --user daemon-reload
systemctl --user enable --now iamail.service >/dev/null 2>&1 || true
loginctl enable-linger "$USER" >/dev/null 2>&1 || true   # tourne même sans session ouverte

echo
echo "✅ Installé. Le service tourne et redémarrera à l'ouverture de session."
echo "   Cockpit : http://127.0.0.1:8740/  (ou menu → « Cockpit IAMail »)"
echo "   État    : systemctl --user status iamail.service"

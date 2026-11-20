#!/usr/bin/env bash
# Installe IAMail sur un Mac pour l'utilisateur courant : exécutable + LaunchAgent
# (démarre à l'ouverture de session, caché, relancé s'il s'arrête) + raccourci cockpit.
# AUCUN sudo, AUCUN Python requis.
#
#   bash empaquetage/installer-macos.sh [chemin/vers/iamail]
#
# Sans argument, prend iamail-macos-arm64 / iamail-macos-x64 posé à côté (release
# GitHub), puis compagnon/dist/iamail (sortie de construire-macos.sh).
# Relancer l'installeur après avoir modifié la clé IA (fichier env) pour la prendre en compte.
set -e
ICI="$(cd "$(dirname "$0")" && pwd)"
case "$(uname -m)" in arm64) ARCH=arm64 ;; *) ARCH=x64 ;; esac

EXE="${1:-}"
if [ -z "$EXE" ]; then
  for c in "$ICI/iamail" "$ICI/iamail-macos-$ARCH" "$ICI/dist/iamail" \
           "$ICI/../compagnon/dist/iamail" "$ICI/../dist/iamail"; do
    [ -f "$c" ] && EXE="$c" && break
  done
fi
[ -n "$EXE" ] && [ -f "$EXE" ] || { echo "Exécutable introuvable.
Passe son chemin en argument, ou construis-le :  bash construire-macos.sh"; exit 1; }

EXEMPLE=""
for c in "$ICI/config.exemple.json" "$ICI/../compagnon/config.exemple.json" "$ICI/../config.exemple.json"; do
  [ -f "$c" ] && EXEMPLE="$c" && break
done

APP="$HOME/Library/Application Support/IAMail"
AGENTS="$HOME/Library/LaunchAgents"
LOGS="$HOME/Library/Logs/IAMail"
PLIST="$AGENTS/be.iamail.compagnon.plist"
mkdir -p "$APP" "$AGENTS" "$LOGS"

install -m 755 "$EXE" "$APP/iamail"
# Fichier téléchargé : macOS le met en quarantaine et refuserait de le lancer en arrière-plan.
xattr -d com.apple.quarantine "$APP/iamail" 2>/dev/null || true
echo "• Exécutable installé : $APP/iamail"

# Clé IA (facultative) — fichier privé, recopiée dans le LaunchAgent (lui aussi privé).
if [ ! -f "$APP/env" ]; then
  printf 'MISTRAL_API_KEY=\n' > "$APP/env"; chmod 600 "$APP/env"
  echo "• Clé IA : colle-la dans « $APP/env » (MISTRAL_API_KEY=…) puis relance l'installeur — facultatif"
fi
CLE="$(sed -n 's/^MISTRAL_API_KEY=//p' "$APP/env" | head -1)"

# Config — créée une fois, préréglée pour Apple Mail, à personnaliser.
if [ ! -f "$APP/config.json" ] && [ -n "$EXEMPLE" ]; then
  sed -e 's#"collecteur": "thunderbird"#"collecteur": "applemail"#' \
      -e "s#\"dossier_staging\": \"[^\"]*\"#\"dossier_staging\": \"$HOME/IAMail/Entree\"#" \
      -e "s#\"dossier_destination\": \"[^\"]*\"#\"dossier_destination\": \"$HOME/IAMail/Archive\"#" \
      -e "s#\"base_sqlite\": \"[^\"]*\"#\"base_sqlite\": \"$HOME/IAMail/iamail_index.sqlite3\"#" \
      "$EXEMPLE" > "$APP/config.json"
  mkdir -p "$HOME/IAMail/Archive"
  echo "• Config créée : $APP/config.json  (à personnaliser : mes_adresses, applemail_boites…)"
fi

# Échappe la clé pour le XML (& < >).
CLE_XML="$(printf '%s' "$CLE" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g')"
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>be.iamail.compagnon</string>
  <key>ProgramArguments</key>
  <array>
    <string>$APP/iamail</string>
    <string>--config</string><string>$APP/config.json</string>
    <string>service</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict><key>MISTRAL_API_KEY</key><string>$CLE_XML</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$LOGS/iamail.log</string>
  <key>StandardErrorPath</key><string>$LOGS/iamail.log</string>
</dict>
</plist>
PLISTEOF
chmod 600 "$PLIST"

# Raccourci « Cockpit IAMail » (double-clic) dans le dossier Applications de l'utilisateur.
mkdir -p "$HOME/Applications"
cat > "$HOME/Applications/Cockpit IAMail.webloc" <<'WEBEOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>URL</key><string>http://127.0.0.1:8740/</string></dict></plist>
WEBEOF

launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo
echo "✅ Installé. Le compagnon tourne et redémarrera à l'ouverture de session."
echo "   Au premier cycle, macOS demande « iamail souhaite contrôler Mail » → OK."
echo "   Cockpit : http://127.0.0.1:8740/  (ou ~/Applications → « Cockpit IAMail »)"
echo "   Journal : $LOGS/iamail.log"

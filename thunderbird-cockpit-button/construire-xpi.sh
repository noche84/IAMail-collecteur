#!/usr/bin/env bash
# Construit l'extension Thunderbird installable : iamail-thunderbird-<version>.xpi
#   bash thunderbird-cockpit-button/construire-xpi.sh [dossier de sortie]
# Thunderbird → Modules complémentaires → roue dentée → Installer un module depuis un fichier.
set -euo pipefail
ICI="$(cd "$(dirname "$0")" && pwd)"
SORTIE="$(mkdir -p "${1:-$ICI/..}" && cd "${1:-$ICI/..}" && pwd)"
VERSION=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["version"])' "$ICI/manifest.json")
XPI="$SORTIE/iamail-thunderbird-$VERSION.xpi"
rm -f "$XPI"
cd "$ICI"
zip -q -X "$XPI" manifest.json background.js mime.js tri.js gesent.js options.html options.js icon.svg
echo "$XPI"

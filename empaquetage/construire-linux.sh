#!/usr/bin/env bash
# Construit l'exécutable autonome IAMail pour Linux (aucun Python requis ensuite).
# À lancer SUR une machine Linux : bash empaquetage/construire-linux.sh
set -e
cd "$(dirname "$0")/../compagnon"
python3 -m PyInstaller --noconfirm --onefile --name iamail \
  --add-data "../moteur:moteur" \
  iamail_compagnon.py
echo
echo "✅ Exécutable : $(cd .. && pwd)/compagnon/dist/iamail"
echo "   Teste-le :  ./dist/iamail --config config.json service"

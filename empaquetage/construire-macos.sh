#!/usr/bin/env bash
# Exécutable autonome IAMail pour macOS. À lancer SUR un Mac (build non croisé).
set -e
cd "$(dirname "$0")/../compagnon"
python3 -m PyInstaller --noconfirm --onefile --name iamail \
  --add-data "../moteur:moteur" \
  iamail_compagnon.py
echo "✅ Exécutable : $(cd .. && pwd)/compagnon/dist/iamail"

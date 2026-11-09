#!/usr/bin/env bash
# Démarre IAMail : relève + jugement en continu ET cockpit.
# C'est LA commande à lancer. Laisse-la tourner ; le cockpit est à
# http://127.0.0.1:8740/ (ou via le bouton dans Thunderbird).
cd "$(dirname "$0")" || exit 1

# Charge la clé Mistral si elle est dans ~/.config/iamail/env (MISTRAL_API_KEY=...)
if [ -f "$HOME/.config/iamail/env" ]; then
  set -a; . "$HOME/.config/iamail/env"; set +a
fi

exec python3 iamail_compagnon.py --config config.json service "$@"

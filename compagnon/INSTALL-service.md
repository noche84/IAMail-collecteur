# Installer le compagnon comme service (démarrage automatique)

Objectif : l'utilisateur **n'a aucune commande à lancer**. Le compagnon démarre
au login, relève et juge en continu, et sert le cockpit. On ouvre juste le
cockpit (marque-page `http://127.0.0.1:8740/` ou le lanceur `ouvrir-cockpit.desktop`).

## Linux (systemd — service utilisateur)
En supposant le compagnon dans `~/IAMail-compagnon/` :

```bash
# 1) (facultatif) la clé Mistral dans un fichier protégé, PAS en clair ailleurs :
mkdir -p ~/.config/iamail
echo 'MISTRAL_API_KEY=ta_cle' > ~/.config/iamail/env
chmod 600 ~/.config/iamail/env

# 2) installer l'unité
mkdir -p ~/.config/systemd/user
cp ~/IAMail-compagnon/iamail.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now iamail.service

# 3) pour qu'il tourne même sans session graphique ouverte
loginctl enable-linger "$USER"
```

Vérifier / piloter :
```bash
systemctl --user status iamail.service
journalctl --user -u iamail.service -f      # voir les cycles en direct
systemctl --user restart iamail.service
systemctl --user stop iamail.service
```

Sans clé Mistral dans `~/.config/iamail/env`, le service relève et trie quand
même (le jugement IA est simplement sauté). Pour l'automatisation sans coût,
on peut remplacer `service` par `service --sans-ia` dans l'unité.

## macOS (launchd) — à venir
Un fichier `~/Library/LaunchAgents/be.iamail.plist` jouera le même rôle.

## Windows (colleagues, Outlook) — à venir
Le compagnon tournera en tâche planifiée / service, aux côtés de l'agent Outlook
(COM) qui, lui, apportera le write-back (marquer lu, transfert pré-rempli) et le
bouton dans Outlook.

## Le modèle, en clair
- **Le compagnon** (ce programme) = le moteur : rapatriement, tri, IA, cockpit.
  Installé une fois, démarre tout seul.
- **Le « plugin » client** (extension Thunderbird / agent Outlook) = plus tard,
  pour le write-back et un bouton dans le logiciel. Ce n'est pas lui qui relève.

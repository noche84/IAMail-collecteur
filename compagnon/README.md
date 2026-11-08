# IAMail — Compagnon natif (v1)

Petit programme Python (bibliothèque standard uniquement) qui **sort l'archive
du dossier Téléchargements**. Il surveille le dossier où l'extension Thunderbird
dépose les paires `.eml/.json`, les range sur un **dossier de destination réel**
(part serveur comprise) en arborescence `AAAA/MM`, et les **indexe dans SQLite**
en dédoublonnant par Message-ID.

Lecture seule vis-à-vis de la messagerie : le compagnon ne touche qu'aux
fichiers. C'est aussi la pièce qui portera, ensuite, la base locale du mode
autonome et la synchro GesEnt.

## Pourquoi ce découpage
Thunderbird (bac à sable) ne peut écrire que sous son dossier Téléchargements.
Le compagnon, lui, est un vrai programme : il écrit où tu veux. Faire transiter
les mails bruts par un canal « messagerie native » est limité à 1 Mo par
message (ingérable avec pièces jointes) ; passer par un dossier relais est plus
simple et plus robuste.

## Prérequis
Python 3.8+ (déjà présent sur Mac et Linux). Aucune dépendance à installer.

## Mise en route
```bash
# 1) Crée une configuration d'exemple + la base
python3 iamail_compagnon.py --config config.json init

# 2) Édite config.json :
#    - dossier_staging       = le dossier révélé par le popup de l'extension
#                              (ex. ~/Downloads/IAMail)
#    - dossier_destination   = ta part serveur (ex. /Volumes/serveur/ArchiveIAMail)
#    - base_sqlite           = où poser l'index
#    - deplacer              = true (déplace) ou false (copie)

# 3) Une passe manuelle
python3 iamail_compagnon.py --config config.json ingest

# 4) Surveillance continue (toutes les 10 s)
python3 iamail_compagnon.py --config config.json watch --intervalle 10
```

## Autres commandes
```bash
python3 iamail_compagnon.py --config config.json stats
python3 iamail_compagnon.py --config config.json search "facture"
```

## Index SQLite
Table `messages` : `message_id` (clé), `de_adresse`, `destinataires`, `copies`,
`date_mail`, `sujet`, `fichier_eml`, `fichier_json`, `pieces_jointes`,
`dossier_src`, `ingere_at`. Interrogeable avec n'importe quel outil SQLite.

## Tourner en tâche de fond
- **Linux/Mac** : lancer `watch` via un service (systemd, launchd) ou un simple
  `nohup … &`.
- **Windows (à venir)** : tâche planifiée ou service ; c'est aussi là que se
  branchera l'agent Outlook.

## Limites de la v1
- L'archive de masse (10 000+ mails historiques) reste bornée côté Thunderbird
  (l'extension ne doit pas inonder le gestionnaire de téléchargements). La reprise
  de l'historique se fera soit par lots, soit par lecture directe du stockage
  local de Thunderbird — étape suivante.
- Pas encore de synchro GesEnt : prochaine brique, sur la même base.

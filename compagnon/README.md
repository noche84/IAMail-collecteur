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

## Import direct du stock Thunderbird (mbox) — recommandé
Sur une installation Thunderbird **Flatpak/Snap**, l'extension ne peut pas
écrire hors de son bac à sable (l'API de téléchargement échoue en silence).
Le compagnon, lui, lit **directement** le fichier mbox du profil, ce qui
règle le problème et permet d'archiver **tout l'historique en une passe**.

Trouver le mbox :
```bash
find ~ ~/.var/app ~/snap -maxdepth 8 -path '*ImapMail*' -iname 'INBOX' 2>/dev/null
```
Renseigner son chemin dans `mbox_sources` (config.json), puis :
```bash
# Test prudent (50 messages)
python3 iamail_compagnon.py --config config.json import --limite 50
# Import complet (tout l'historique)
python3 iamail_compagnon.py --config config.json import
```
- **Lecture seule** : le mbox n'est jamais modifié. Relancer l'import est sûr
  (dédoublonnage par Message-ID) : seuls les nouveaux messages sont ajoutés.
- Prévoir, sur la destination, **l'espace équivalent au mbox** (un INBOX Gmail
  peut peser plusieurs Go).
- Idéalement lancer quand Thunderbird n'écrit pas activement ; au besoin, un
  message tronqué en fin de fichier sera rattrapé au passage suivant.

## Le cerveau — classement (règles déterministes, sans IA)
Une fois les mails importés et indexés, le compagnon peut les **classer** :
catégorie, urgence, « à moi / juste en copie / diffusion », courrier en masse,
soupçon d'hameçonnage, et une **priorité** de synthèse. C'est gratuit, instantané,
et ça tourne sur tout le corpus. (La couche IA viendra affiner par-dessus.)

```bash
# 1) Trouver ton/tes adresses pour distinguer "à moi" de "en copie"
python3 iamail_compagnon.py --config config.json suggerer-adresses
#    -> ajoute la bonne dans config.json :  "mes_adresses": ["toi@gmail.com"]

# 2) Classer (ré-analyse tout avec --retout)
python3 iamail_compagnon.py --config config.json analyser

# 3) L'aperçu priorisé — la graine du topo du matin
python3 iamail_compagnon.py --config config.json topo --nombre 20
```

Le classement s'écrit dans l'index SQLite (colonnes `categorie`, `urgence`,
`pour_moi`, `bulk`, `expediteur_humain`, `important`, `suspect`, `priorite`,
`raison`…), donc interrogeable librement. Les règles (mots-clés, expéditeurs
automatiques) sont de la **donnée éditable** en haut du script, pensée pour être
ajustée — et demain partagée avec GesEnt.

### Filet de sécurité « zéro perte »
Le déterministe ne peut pas comprendre le *sens* d'un message (ça, c'est le
travail de l'IA). Il applique donc une règle prudente : tout ce qui touche à
**l'argent, la sécurité, une commande, une réservation ou un contrat** est
marqué `important` et **n'est jamais enterré en « basse »**, même s'il s'agit
d'un courrier de masse (ex. une alerte bancaire ou crypto). Le `topo` les
remonte avec un `★`. Conséquence assumée : il vaut mieux montrer un peu trop
(une pub « soldes ») que cacher un mail critique. L'IA affinera.

Tu peux forcer des expéditeurs à toujours remonter via `expediteurs_importants`
dans la config (adresses ou domaines : `["binance.com", "ma-banque.be"]`).

## La couche IA — jugement contextuel (Mistral)
Après le déterministe, l'IA tranche le contextuel : échéance raisonnée du
contenu et des événements (pas des mots de l'expéditeur), calibration par
expéditeur (celui qui crie « urgent » en permanence est recalibré, sans être
ignoré), reclassement dans les deux sens (remonter un Binance critique, calmer
un faux urgent), et **une question posée à l'humain en cas de doute**. Son
verdict (`ia_priorite`) surclasse le classement déterministe.

Les fichiers partagés du moteur (prompt, règles, taxonomie) sont dans le dossier
`moteur/`, à côté du script. **On modifie la logique là, pas dans le code.**

```bash
# Tester tout le pipeline SANS clé (modèle bouchonné) :
python3 iamail_compagnon.py --config config.json analyser-ia --mock
python3 iamail_compagnon.py --config config.json brief

# En vrai avec Mistral : mettre la clé dans config.json -> "ia":
#   { "fournisseur": "mistral", "cle": "TA_CLE", "modele": "mistral-small-latest" }
python3 iamail_compagnon.py --config config.json analyser-ia --jours 2   # 2 derniers jours
python3 iamail_compagnon.py --config config.json brief
```

- L'IA ne voit que ce qui le mérite : tout sauf la pub franche, **plus tout ce
  qui est important** (même en masse). Elle ne re-juge pas ce qui l'a déjà été.
- `brief` affiche le topo en cases : **Maintenant / Cette semaine / Ce mois**,
  avec les tâches extraites, leurs échéances, et les questions en attente.

## Le cockpit — la fenêtre de tri (pas de terminal pour l'utilisateur)
```bash
python3 iamail_compagnon.py --config config.json cockpit
```
Ouvre une page dans le navigateur : les mails jugés par l'IA, rangés en cases
(Maintenant / Cette semaine / Ce mois / Pour info), avec résumé, tâche, échéance
et priorité. Par carte :
- **Ouvrir l'email** : le compagnon ouvre le `.eml` archivé dans ton logiciel de
  mail (Outlook/Thunderbird/Mail) ; comme les en-têtes sont préservés, Répondre
  et Transférer y fonctionnent normalement.
- **Déléguer** : ouvre le mail pour que tu fasses « Transférer » (collègue + note).
- **Traité** : sort le mail du cockpit.
- **Reporter** : le garde pour plus tard.

Case à cocher **« non-lus seulement »** : n'affiche que les non-lus — c'est ton
backlog au retour de vacances. L'état (à traiter / traité / reporté) est stocké
dans l'index, **sans toucher à ta vraie boîte** (le miroir vers le « lu/non-lu »
réel viendra avec le collecteur Outlook/Thunderbird).

Le cockpit n'affiche que ce que l'IA a déjà jugé (`analyser-ia`).

## Le service — tout automatique (pour un usage réel)
Un seul processus qui **relève + juge en continu ET sert le cockpit**. On le
lance une fois, on garde Thunderbird ouvert, et le cockpit reste à jour tout
seul : à chaque réception, TB écrit dans le mbox, le service le relève au cycle
suivant, le juge, et le mail apparaît dans la fenêtre.
```bash
export MISTRAL_API_KEY="ta_cle"          # pour le jugement IA
python3 iamail_compagnon.py --config config.json service
#   --intervalle 120   : cycle plus rapide (défaut 300 s)
#   --sans-ia          : automatisation sans coût IA (tri seul)
```
- Chaque cycle juge au plus `ia_max_par_cycle` nouveaux mails (config, défaut 20)
  — garde-fou de coût. Le backlog historique se juge donc progressivement ;
  pour tester l'automatisation sans dépenser, utilise `--sans-ia`.
- **Points d'accès au cockpit** : l'URL directe `http://127.0.0.1:8740/` (à
  mettre en marque-page, y compris dans Thunderbird), et le lanceur
  `ouvrir-cockpit.desktop` (Linux : rends-le exécutable, double-clic). Un bouton
  *dans* Thunderbird/Outlook viendra avec l'extension collecteur.

## Le topo du matin en audio (voix locale)
Un résumé parlé de quelques minutes, pour le trajet. La voix est **locale** —
le contenu de tes mails ne part jamais dehors. Un message suspect n'est **jamais
lu à voix haute** (juste signalé).
```bash
# une voix locale, gratuite (à installer une fois) :
sudo apt install espeak-ng          # ou : configure "piper" dans config.json pour une voix plus naturelle
python3 iamail_compagnon.py --config config.json topo-audio
```
Produit un `.mp3` (via ffmpeg) dans le dossier de destination, et imprime le
script. Dans le **cockpit**, le bouton **🔊 Topo audio** le génère et le joue
directement dans la page.

## Autres commandes
```bash
python3 iamail_compagnon.py --config config.json stats
python3 iamail_compagnon.py --config config.json search "facture"
python3 iamail_compagnon.py --config config.json topo        # aperçu déterministe
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

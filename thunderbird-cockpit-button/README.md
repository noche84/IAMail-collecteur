# IAMail — extension Thunderbird (cockpit, synchro « Traité », client GesEnt)

Version 2.0.0 · Thunderbird 128 ou plus récent.

## Deux façons de travailler, boîte par boîte

| | Autonome (compagnon local) | Relié à GesEnt |
|---|---|---|
| Qui fait foi pour « Traité » | le cockpit local (`127.0.0.1:8740`) | **GesEnt** |
| Les mails | lus dans le stock Thunderbird par le compagnon | envoyés à GesEnt **dès leur arrivée**, puis l'existant |
| Étiquette dans Thunderbird | « Traité IAMail » du cockpit | « Traité IAMail » de GesEnt |
| Cockpit local | actif | lecture seule (bandeau « relié à GesEnt ») |
| Jugement IA | par le compagnon | par GesEnt (le compagnon n'appelle plus l'IA) |

Une boîte est reliée quand GesEnt la déclare `relie` (`GET /moi`). Délié, ou jeton révoqué
dans GesEnt : retour automatique au fonctionnement autonome.

## Relier un poste

1. Dans GesEnt : **Courrier → Mes collecteurs → Relier un poste** → choisir les boîtes →
   un code de 8 caractères (15 minutes, usage unique).
2. Dans Thunderbird : **Modules complémentaires → IAMail → Options** (ou `about:addons`).
3. Saisir l'adresse de GesEnt, le code, et éventuellement un nom de poste → **Relier ce
   poste**. Thunderbird demande l'autorisation de joindre cette adresse (GesEnt n'envoie
   pas d'en-têtes CORS : c'est la permission d'hôte qui ouvre l'accès, pour cette seule
   adresse).

La page d'options montre ensuite les boîtes, l'avancement de l'historique, la file
d'envoi, le journal et les refus éventuels de GesEnt. Aucun compte système n'est envoyé ;
le nom de poste est celui que vous saisissez (CCT n° 81, ADR 0051 § 11).

## Ce que fait le client (protocole d'ingestion v1)

Contrat : `docs/iaemail-contrat-ingestion-v1.md` dans GesEnt (ADR 0051). Code : `gesent.js`.

- **À l'arrivée** : chaque mail d'une boîte de la portée (boîte de réception et dossiers
  ordinaires ; pas Envoyés, Brouillons, Corbeille, Indésirables, Archives) est préparé tout
  de suite — source brute gzip+base64, sha256, `tri_local` — et mis dans une **file
  persistante**. Il part en lot (≤ 100 messages, ≤ 25 Mo) dans les 10 secondes. Un mail
  supprimé juste après son arrivée est donc quand même archivé.
- **Gros messages** (> 5 Mo compressés) : annoncés (`a_televerser`), puis `PUT /sources`.
- **Historique** : après les arrivées en attente, du plus récent au plus ancien, 100 par
  lot, à faible cadence ; repris à `plus_ancien_recu` après une interruption.
- **Synchro « Traité »** (boîtes reliées) : `GET /sync/pour-client` toutes les 15 s,
  étiquette + lu dans Thunderbird, `POST /sync/confirme` avec ce qui a réellement été
  appliqué ; dans l'autre sens, étiquette posée / retirée → `traites` / `non_traites`,
  supprimé ou déplacé → `ranges` (`POST /sync/depuis-client`).
- **Boîte de la portée non reliée** : les mêmes gestes partent au **journal** (`gestes` :
  `traite`, `non_traite`, `supprime`, `deplace`), le cockpit local garde la main.
- **Moteur** : téléchargé depuis GesEnt (`GET /moteur`, taxonomie de l'entreprise), chaque
  fichier vérifié par son sha256 avant usage ; `409 moteur_incompatible` → nouveau moteur.
- **Erreurs** (§ 6) : 5xx et réseau → même lot (même `lot_id`, idempotent), 30 s → 15 min ;
  `429` → `Retry-After` ; `401` → arrêt, nouvel appairage ; `403` → portée rafraîchie ;
  `413` → lot découpé ; rejets par élément → journalisés, retirés de la file.
- Un rangement fait par un **filtre** sur un mail non lu de moins de 2 minutes n'est pas
  compté comme un geste.

## Le tri local en JavaScript (`tri.js`)

`tri_local` est un **témoin** (GesEnt rejoue tri et IA) : c'est la couche déterministe du
moteur, troisième harnais après le compagnon (Python) et GesEnt (PHP). Il lit les mêmes
`regles.json` + `taxonomie.json` et passe les **29 cas de conformité** du moteur ; sur ces
cas et sur des messages piégés (mots encodés, quoted-printable, base64 en windows-1252,
HTML seul, pièces jointes, message joint, groupes d'adresses), l'extraction et le tri sont
identiques au compagnon, au caractère près.

## Tests

```
node thunderbird-cockpit-button/tests/conformite-tri.test.js   # tri JS = cas du moteur
node thunderbird-cockpit-button/tests/protocole.test.js        # client ↔ faux GesEnt
node thunderbird-cockpit-button/tests/extension.test.js        # extension dans un faux Thunderbird
```

Le faux GesEnt (`tests/faux.js`) valide chaque requête et chaque réponse avec les schémas
du contrat (`tests/contrat/`, copiés de GesEnt `docs/contrats/ingestion-v1/`), décompresse
les sources et vérifie leurs empreintes, et tient l'idempotence par `lot_id` et par clé.
Cas de recette du contrat couverts côté collecteur : 1, 3, 4, 7, 8, 9, 11, 12.

## Installer

- Release GitHub : `iamail-thunderbird-<version>.xpi` → Thunderbird → Modules
  complémentaires → roue dentée → **Installer un module depuis un fichier**.
- Ou construire : `bash thunderbird-cockpit-button/construire-xpi.sh`.
- Pour essayer sans installer : `about:debugging` → Ce Thunderbird → Charger un module
  temporaire → `thunderbird-cockpit-button/manifest.json`.

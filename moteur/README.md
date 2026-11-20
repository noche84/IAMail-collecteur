# moteur/ — moteur de tri partagé (2.x)

Ces fichiers sont la **source de vérité unique** du tri des emails. Ils sont lus
**tels quels** par chaque programme qui classe :

- le module IAEmail de **GesEnt** (PHP/Laravel) — dépôt de référence ;
- le compagnon **IAMail** (Python) — copie locale, pour tourner hors ligne ;
- tout futur harnais (agent Outlook…).

Les collecteurs purs (extension Thunderbird, bouton cockpit) ne classent rien et
n'en ont pas besoin. On modifie la logique **ici**, jamais dans un seul harnais.

| Fichier | Rôle |
|---|---|
| `manifest.json` | Version semver, liste des fichiers, empreintes sha256. |
| `regles.json` | Règles déterministes, seuils, **algorithme pas à pas** (`_algorithme`) et **manière de comparer** (`_semantique`). |
| `taxonomie.json` | Classement **par défaut** : niveaux d'urgence, catégories, signaux. Chaque entreprise l'adapte (GesEnt : en base ; IAMail : copie locale). |
| `prompt.txt` | Consignes de l'IA (message système). Gabarit : deux emplacements d'extension. |
| `entree.txt` | Gabarit du message envoyé à l'IA pour UN email. |
| `sortie.schema.json` | Contrat du tronc commun de la réponse IA + ordre de validation. |
| `extensions/*.json` | Champs supplémentaires facultatifs (`action`, `chantier`, `evenement`, `portee`) : consigne, format, schéma. |
| `topo.txt` | Consignes de l'IA pour **rédiger le topo** du matin à partir des jugements (2.2). |
| `tests/` | Jeu de conformité commun : chaque harnais doit le passer intégralement. |

## Ce qui est commun, ce qui est propre

- **Commun à tous** : algorithme déterministe, consignes de l'IA, contrat de
  sortie (tronc), format de l'entrée, déduction de la case du topo.
- **Propre à une entreprise ou un utilisateur (donnée, pas code)** : la taxonomie
  (catégories actives, libellés, descriptions, urgence par défaut, signaux,
  libellés et délais des niveaux), la liste `importants`, les adresses de la
  boîte, les **extensions activées**, `selection_ia.ignorer_masse_non_importante`.
- **Fixe** : les 4 codes d'urgence `critique > haute > normale > basse` et les
  codes des catégories existantes (un code ne change jamais ; on le désactive).

## Gabarits (`prompt.txt`, `entree.txt`)

Syntaxe minimale, identique dans tous les langages :

1. `{{#nom}}…{{/nom}}` : le bloc est **supprimé** si la valeur `nom` est vide
   (chaîne vide ou blancs) ; sinon les balises disparaissent et le contenu reste.
2. Puis `{{nom}}` est remplacé par la valeur (vide si absente).

Pas d'échappement, pas de boucle. Python et PHP : deux regex (voir le prototype
PHP du brief GesEnt).

### `prompt.txt`

- `{{extensions_consignes}}` : vide sans extension ; sinon `"\n"` + consignes des
  extensions actives (ordre alphabétique du nom) jointes par `"\n\n"` + `"\n"`.
- `{{extensions_format}}` : vide sans extension ; sinon `",\n  "` + formats joints
  par `",\n  "`.
- Le harnais n'ajoute **rien d'autre** au message système, hormis, s'il le veut,
  une phrase finale demandant un JSON sans texte autour.

### `entree.txt` — valeurs à fournir

| Variable | Contenu exact |
|---|---|
| `date_du_jour` | `{jour de la semaine} {J} {mois} {AAAA} (AAAA-MM-JJ)` en français, minuscules, `1er` pour le premier du mois, dans le fuseau de l'utilisateur — ex. `lundi 16 novembre 2026 (2026-11-16)` (2.2 : le jour de la semaine évite les erreurs sur « vendredi prochain ») ; vecteurs : `tests/entree/date_du_jour.json` |
| `utilisateur` | facultatif (2.2) : **pour qui l'on trie** — profil de l'utilisateur, puis description de la boîte. Règle de rendu exacte dans la section « Bloc DESTINATAIRE » ci-dessous (2.2.1) ; vecteurs : `tests/entree/destinataire.json`. Vide → le bloc DESTINATAIRE disparaît et l'entrée est identique à la 2.1 |
| `niveaux` | une ligne par niveau, du plus urgent : `- {code} ({libelle}, à traiter sous {delai_heures} h) : {definition}` — sans `, à traiter…` si délai nul |
| `categories` | une ligne par catégorie **active**, dans l'ordre : `- {code} : {libelle}. {description} (urgence habituelle : {urgence_defaut})` |
| `expediteur` | en-tête From décodé, nom affiché et adresse : `Nom <adresse>` (adresse seule s'il n'y a pas de nom) |
| `expediteur_connu` | facultatif : GesEnt — contact ou fiche reconnus, ex. `client Dupont SRL` ; IAMail — `dans la liste des expéditeurs importants du destinataire` si c'est le cas |
| `profil` | facultatif : `{n} message(s) reçu(s), dont {recent} ces {fenetre_recent_jours} derniers jours, {pct} % au ton urgent` + `, CORRESPONDANT RÉCURRENT ET ACTIF` si actif (n ≥ `profil_actif_n_min` ET recent ≥ `profil_actif_recent_min`) + **(2.4)** `, le destinataire lui a écrit {ecrits} fois ces {profil_ecrits_fenetre_jours} derniers jours` si ecrits ≥ 1 — vide si n = 0. `n` compte le message jugé. `pct` = urgents × 100 / n arrondi **au plus proche, .5 vers le haut** (en entiers : `(200 × urgents + n) div (2 × n)`) — l'arrondi « bancaire » de Python donnait 12 % là où PHP donne 13 % (1 sur 8). `ecrits` = messages **envoyés par le destinataire** à cette adresse (À ou Cc) dans la fenêtre. Vecteurs : `tests/entree/profil.json` |
| `relances` | facultatif : `l'expéditeur a déjà écrit {k} fois ces {relance_fenetre_jours} derniers jours sans que ses messages soient traités` si k > 0. **Règle commune (2.2.1)** : k = messages du **même expéditeur** (adresse, minuscules), du **même fil** (identifiant de fil du client s'il existe, sinon clé de fil normalisée), reçus **avant** le message jugé et depuis moins de `relance_fenetre_jours`, **encore à traiter** (ni traités, ni reportés), hors courrier de masse. Le comptage de tous les messages de l'expéditeur (ancienne règle GesEnt) est abandonné : le statut « traité » n'est pas assez fiable pour lui **(2.4)** : s'il existe une réponse du destinataire dans ce fil, seuls comptent les messages reçus **après** cette réponse ; sans réponse connue, rien ne change. Vecteurs : `tests/entree/relances.json` |
| `reponse_attendue` | facultatif (2.3) : le message **répond à une affaire déléguée** par le destinataire. `délégué à {à qui} le {J} {mois} {AAAA}`, suivi éventuellement de `, suivi prévu le {J} {mois} {AAAA}` — `1er` pour le premier du mois, mois en minuscules ; `{à qui}` = nom affiché, sinon adresse, compacté. Fourni par le harnais quand il juge la réponse d'un tiers (ou d'un collègue) sur une délégation **avec suivi**. Vide → la ligne disparaît. Vecteurs : `tests/entree/reponse_attendue.json`. IAMail ne gère pas encore la délégation : toujours vide |
| `precision` | facultatif (2.4) : réponse de la personne à la question posée par l'IA sur ce message. Espaces consécutives compactées, espaces de début et de fin retirées, coupé à `precision_max` (500) caractères **sans** points de suspension, puis espaces et points finaux retirés. Vide → la ligne disparaît. Vecteurs : `tests/entree/precision.json` |
| `memoire` | facultatif. Entrées : `n` = messages du fil reçus depuis moins de `fil_fenetre_jours`, **le message jugé compris** ; `chauds` = parmi les **autres**, ceux jugés importants ou d'urgence `critique`/`haute` (IA, sinon règles) ; **(2.4)** `dernier_envoi` = date du plus récent message **envoyé par le destinataire** dans ce fil, antérieur à la réception du message jugé. Rendu : n ≥ 2 → `fil « {sujet, 60 car.} » : {n−1} autre(s) message(s) sur ce sujet` + `, dont {chauds} déjà jugé(s) important(s) ou urgent(s)` si chauds > 0 + `, dernière réponse du destinataire le {date}` s'il y a un envoi ; n < 2 avec envoi → `fil « {sujet, 60 car.} » : dernière réponse du destinataire le {date}` ; n < 2 sans envoi → vide. `{date}` = `{J} {mois}` (`1er`, minuscules), plus ` {AAAA}` si l'année diffère de celle de la date du jour. Vecteurs : `tests/entree/memoire.json` |
| `pre_classement` | éléments joints par `, ` dans cet ordre : `à moi` / `en copie` / `diffusion` ; `expéditeur humain` / `expéditeur automatique` ; `courrier de masse` si bulk ; `marqué important` si important ; `soupçon d'hameçonnage (Reply-To d'un autre domaine)` si suspect |
| `signaux` | `aucun`, ou pour chaque signal : `« {expression} » ({niveau}, monte)` / `(…, baisse)` / `(…, monte — ignoré : expéditeur automatique ou masse)`, joints par `, ` |
| `exemples` | facultatif (apprentissage) : messages semblables déjà classés ou corrigés par une personne, `exemples_max` au plus, une entrée par exemple : `- De <{adresse}> — « {objet, 120 car.} » — {extrait, 200 car.}` puis, à la ligne, `  → categorie: {code}, urgence: {code}` (+ `, action_attendue: false (le destinataire a indiqué que ce n'était pas à lui d'agir)` pour une correction « pas pour moi ») **(2.4)** : un exemple qui porte une précision du destinataire ajoute la ligne `  → précision du destinataire : « {texte, 200 car.} »` ; une précision ne sert d'exemple que pour la **même boîte** (sur une boîte partagée, « c'est pour moi » ne vaut pas pour les collègues) |
| `de`, `a`, `cc` | adresses nues ; `a` et `cc` joints par `, `, au plus `ia_destinataires_max`, puis ` et {n} autre(s)` |
| `objet` | objet décodé |
| `date_mail` | `AAAA-MM-JJ HH:MM` dans le fuseau de l'utilisateur |
| `pieces_jointes` | noms joints par `, `, ou `aucune` |
| `extrait` | extrait (voir `_semantique.extrait`), suivi de `ia_marque_troncature` s'il a été tronqué ; `(corps vide ou non texte)` si l'extrait est vide |

## Contrat de sortie

Tronc commun (`sortie.schema.json`) : `categorie`, `urgence`, `action_attendue`,
`tache`, `echeance`, `justification`, `resume`, `question`, `confiance`.
Extensions actives : leurs propriétés s'ajoutent (`action`, `chantier`, `evenement`,
`portee`). Le compagnon IAMail active toujours `evenement` et `portee`.

La **case du topo** (maintenant / cette semaine / ce mois) n'est pas demandée à
l'IA : elle se **déduit** de l'urgence, de l'échéance et d'`action_attendue`
(`regles.json` → `topo`), de la même façon partout.

## Ce que le destinataire a envoyé (dossier Envoyés, 2.4)

Proposé par GesEnt le 20/11/2026 (`docs/moteur-2.4-proposition.md`), écrit ici.

- **Dossier Envoyés** : attribut IMAP SPECIAL-USE `\Sent` (RFC 6154) d'abord ; sinon le
  premier nom trouvé parmi `Sent`, `Sent Items`, `Sent Messages`, `Éléments envoyés`,
  `Envoyés`, `INBOX.Sent`, `[Gmail]/Sent Mail`, `[Gmail]/Messages envoyés` ; un réglage
  de boîte peut l'imposer. Microsoft Graph : `sentitems` ; API Gmail : libellé `SENT`.
  Collecteurs : le dossier Envoyés du compte.
- **Rattachement d'un envoi à un fil** : par `In-Reply-To` puis `References` (un message
  reçu de la même boîte) ; sinon même clé de fil **et** l'expéditeur du fil parmi les
  destinataires (À, Cc) de l'envoi, dans `fil_fenetre_jours`.
- **Dédoublonnage** par Message-ID (une réponse envoyée par GesEnt figure aussi dans les
  Envoyés).
- Usages dans l'entrée IA : `memoire` (`dernier_envoi`), `profil` (`ecrits`), `relances`
  (comptées après la dernière réponse). Formats dans le tableau des variables.

## Bloc DESTINATAIRE (variable `utilisateur`, 2.2.1)

Décidé avec GesEnt le 17/11/2026. Deux sources, dans cet ordre de priorité :

- **U — le profil de l'utilisateur** : `{nom}, {fonction}. {profil libre}` (parties
  vides omises). IAMail : réglages du cockpit. GesEnt : profil par utilisateur,
  fortement encouragé (bandeau tant qu'il est vide), jamais bloquant.
- **B — la description de la boîte**, facultative. GesEnt : réglage par boîte.
  IAMail : clé `utilisateur.boite` de la config (une seule boîte).

Quelle source selon la boîte : boîte **personnelle** → U puis B ; boîte **partagée**
→ B seule (l'analyse est faite une fois pour tous les lecteurs).

Rendu, en caractères (points de code — `mb_*` en PHP) :

1. Chaque partie est **compactée** : espaces consécutives → une espace, puis espaces
   et points finaux retirés.
2. `U` = `{nom}, {fonction}` (virgule seulement si les deux existent), puis `. ` et
   `{profil}` s'il existe.
3. Si `U` fait 1500 caractères ou plus : `U[0:1499]`, espaces et points finaux
   retirés, puis `.` — B est omise.
4. Sinon, si B existe : séparateur `. Boîte : ` (ou `Boîte : ` si U est vide) ;
   place restante = 1500 − |U| − |séparateur| − 1. Si elle est **≥ 40** : B coupée à
   cette longueur, espaces et points finaux retirés, puis
   `U + séparateur + B + .` ; sinon B est omise.
5. Sinon `U + .` ; rien du tout si U et B sont vides.

Résultat : 1500 caractères au plus, point final compris. Les deux harnais doivent
reproduire **tous** les cas de `tests/entree/destinataire.json`.

**Changement de profil** : l'enregistrement n'appelle jamais l'IA. Un bandeau propose
de rejuger, avec le nombre de messages concernés et un avertissement sur les crédits ;
plusieurs modifications → un seul rejugement ; bouton manuel toujours disponible.
Limites : 14 jours, 300 messages, corrections humaines exclues.

## Topo rédigé par l'IA (`topo.txt`, 2.2)

Le topo n'est plus un collage de gabarits : une fois les messages jugés, un second
appel fait **rédiger** le texte parlé. Message système = `topo.txt` (bloc
`{{#utilisateur}}` comme dans `entree.txt`). Message utilisateur, construit par le
harnais :

```
DATE DU JOUR : lundi 16 novembre 2026 (2026-11-16)

ÉVÉNEMENTS D'AUJOURD'HUI ET DE DEMAIN :
- aujourd'hui à 14:30 — {tâche ou résumé} — lieu : {lieu} — annoncé par {nom}
(ou « (aucun) »)

MESSAGES À TRAITER, du plus pressant au moins pressant :
[MAINTENANT]
1. De : {nom} — reçu {ce matin|hier|…} — urgence {code} — {adressé à la personne|en copie|diffusion}
   Objet : …
   Tâche : …            (lignes omises si vides)
   Résumé : …
   Pourquoi : …
   Échéance : AAAA-MM-JJ ({aujourd'hui|demain|…})
   Question en suspens : …
   Réponse attendue : délégué à {à qui} le {date}   (2.3 ; omise si vide)
[CETTE SEMAINE] …
[CE MOIS] …

CHIFFRES : …
```

- Matière : messages **non traités**, hors suspects et hors masse non importante,
  reçus depuis `topo_fenetre_jours` (ou dont l'échéance n'est pas dépassée de plus
  de 7 jours) ; `topo_elements_max` au plus. La **case est recalculée à la date du
  jour** (`regles.topo`), jamais relue telle que stockée le jour du jugement.
- Ordre : case, puis urgence (critique d'abord), puis « à moi », puis échéance, puis
  le plus récent. Le harnais ne relève pas l'urgence d'une réponse attendue (décision
  du 17/11 : l'urgence se juge sur le contenu) ; c'est `topo.txt` qui la fait annoncer
  en tête.
- Réponse : `{"court": "...", "complet": "..."}`. Le harnais retire liens, adresses
  et symboles, vérifie les longueurs, et **retombe sur un gabarit local** (mêmes
  éléments, même ordre) en cas de panne, de réponse inutilisable ou sans IA.
- Seuls des résumés déjà produits par le tri partent chez le fournisseur IA ; la
  synthèse vocale reste locale.

## Règles de maintenance

1. Toute modification d'un fichier listé dans `manifest.json` → incrémenter
   `version` et recalculer les `sha256`.
   - **patch** : données seules (mots-clés, domaines, signaux, descriptions) — aucun harnais à modifier ;
   - **mineur** : changement de comportement ou ajout que chaque harnais doit implémenter
     (règle de l'algorithme, nouvelle clé, nouvelle extension, consigne de prompt) ; listé dans
     `manifest.json` → `harnais_requis` et prouvé par de nouveaux cas de test ;
   - **majeur** : rupture de format ou de contrat (un harnais existant ne peut plus lire
     les fichiers, ou la sortie IA change de forme).
2. Un bug corrigé = **un cas ajouté** dans `tests/` (valeurs écrites à la main).
3. Chaque harnais vérifie les empreintes au démarrage et inscrit la `version`
   du moteur dans chaque jugement.
4. Dépôt de référence : **GesEnt** (`resources/moteur/`). IAMail en garde une
   copie identique (vérifiée par les empreintes).

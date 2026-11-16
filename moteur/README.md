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
| `date_du_jour` | `{jour de la semaine} {J} {mois} {AAAA} (AAAA-MM-JJ)` en français, minuscules, `1er` pour le premier du mois, dans le fuseau de l'utilisateur — ex. `lundi 16 novembre 2026 (2026-11-16)` (2.2 : le jour de la semaine évite les erreurs sur « vendredi prochain ») |
| `utilisateur` | facultatif (2.2) : **pour qui l'on trie**. `{nom}, {fonction}. {profil libre}.` — parties vides omises, espaces compactées, 1500 caractères au plus. IAMail : réglages du cockpit. GesEnt : réglage par utilisateur (boîte personnelle) ou par boîte (boîte partagée). Vide → le bloc DESTINATAIRE disparaît et l'entrée est identique à la 2.1 |
| `niveaux` | une ligne par niveau, du plus urgent : `- {code} ({libelle}, à traiter sous {delai_heures} h) : {definition}` — sans `, à traiter…` si délai nul |
| `categories` | une ligne par catégorie **active**, dans l'ordre : `- {code} : {libelle}. {description} (urgence habituelle : {urgence_defaut})` |
| `expediteur` | en-tête From décodé, nom affiché et adresse : `Nom <adresse>` (adresse seule s'il n'y a pas de nom) |
| `expediteur_connu` | facultatif : GesEnt — contact ou fiche reconnus, ex. `client Dupont SRL` ; IAMail — `dans la liste des expéditeurs importants du destinataire` si c'est le cas |
| `profil` | facultatif : `{n} message(s) reçu(s), dont {recent} ces {fenetre} derniers jours, {pct} % au ton urgent` + `, CORRESPONDANT RÉCURRENT ET ACTIF` si actif — vide si n = 0 |
| `relances` | facultatif : `l'expéditeur a déjà écrit {k} fois ces {relance_fenetre_jours} derniers jours sans que ses messages soient traités` si k > 0. k = messages **antérieurs**, non traités, hors masse. GesEnt : tous ceux de l'expéditeur. IAMail : seulement ceux du **même fil** (sans le dossier Envoyés, « non traité » y est moins fiable) |
| `memoire` | facultatif : `fil « {sujet} » : {autres} autre(s) message(s) sur ce sujet, dont {chauds} déjà jugé(s) important(s) ou urgent(s)` (sans la fin si chauds = 0) — vide s'il n'y a aucun autre message. **2.2** : on ne compte que les AUTRES messages du fil (jamais celui que l'on juge), reçus depuis moins de `fil_fenetre_jours` ; « chaud » = `important` OU urgence retenue (IA, sinon règles) `critique`/`haute` |
| `pre_classement` | éléments joints par `, ` dans cet ordre : `à moi` / `en copie` / `diffusion` ; `expéditeur humain` / `expéditeur automatique` ; `courrier de masse` si bulk ; `marqué important` si important ; `soupçon d'hameçonnage (Reply-To d'un autre domaine)` si suspect |
| `signaux` | `aucun`, ou pour chaque signal : `« {expression} » ({niveau}, monte)` / `(…, baisse)` / `(…, monte — ignoré : expéditeur automatique ou masse)`, joints par `, ` |
| `exemples` | facultatif (apprentissage) : messages semblables déjà classés ou corrigés par une personne, `exemples_max` au plus, une entrée par exemple : `- De <{adresse}> — « {objet, 120 car.} » — {extrait, 200 car.}` puis, à la ligne, `  → categorie: {code}, urgence: {code}` (+ `, action_attendue: false (le destinataire a indiqué que ce n'était pas à lui d'agir)` pour une correction « pas pour moi ») |
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
[CETTE SEMAINE] …
[CE MOIS] …

CHIFFRES : …
```

- Matière : messages **non traités**, hors suspects et hors masse non importante,
  reçus depuis `topo_fenetre_jours` (ou dont l'échéance n'est pas dépassée de plus
  de 7 jours) ; `topo_elements_max` au plus. La **case est recalculée à la date du
  jour** (`regles.topo`), jamais relue telle que stockée le jour du jugement.
- Ordre : case, puis urgence (critique d'abord), puis « à moi », puis échéance, puis
  le plus récent.
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

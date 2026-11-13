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
| `extensions/*.json` | Champs supplémentaires facultatifs (`action`, `chantier`) : consigne, format, schéma. |
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
| `date_du_jour` | `AAAA-MM-JJ` dans le fuseau de l'utilisateur (Europe/Brussels par défaut) |
| `niveaux` | une ligne par niveau, du plus urgent : `- {code} ({libelle}, à traiter sous {delai_heures} h) : {definition}` — sans `, à traiter…` si délai nul |
| `categories` | une ligne par catégorie **active**, dans l'ordre : `- {code} : {libelle}. {description} (urgence habituelle : {urgence_defaut})` |
| `expediteur` | en-tête From décodé (nom + adresse) |
| `expediteur_connu` | facultatif (GesEnt) : contact ou fiche reconnus, ex. `client Dupont SRL` |
| `profil` | facultatif : `{n} message(s) reçu(s), dont {recent} ces {fenetre} derniers jours, {pct} % au ton urgent` + `, CORRESPONDANT RÉCURRENT ET ACTIF` si actif — vide si n = 0 |
| `relances` | facultatif : `l'expéditeur a déjà écrit {k} fois ces {relance_fenetre_jours} derniers jours sans que ses messages soient traités` si k > 0 |
| `memoire` | facultatif : `fil « {sujet} » : {n-1} autre(s) message(s) sur ce sujet, dont {chauds} déjà jugé(s) important(s) ou urgent(s)` (sans la fin si chauds = 0) — vide si le fil a moins de 2 messages |
| `pre_classement` | éléments joints par `, ` dans cet ordre : `à moi` / `en copie` / `diffusion` ; `expéditeur humain` / `expéditeur automatique` ; `courrier de masse` si bulk ; `marqué important` si important ; `soupçon d'hameçonnage (Reply-To d'un autre domaine)` si suspect |
| `signaux` | `aucun`, ou pour chaque signal : `« {expression} » ({niveau}, monte)` / `(…, baisse)` / `(…, monte — ignoré : expéditeur automatique ou masse)`, joints par `, ` |
| `exemples` | facultatif (GesEnt, apprentissage) : messages semblables déjà classés par une personne |
| `de`, `a`, `cc` | adresses nues ; `a` et `cc` joints par `, `, au plus `ia_destinataires_max`, puis ` et {n} autre(s)` |
| `objet` | objet décodé |
| `date_mail` | `AAAA-MM-JJ HH:MM` dans le fuseau de l'utilisateur |
| `pieces_jointes` | noms joints par `, `, ou `aucune` |
| `extrait` | extrait (voir `_semantique.extrait`), suivi de `ia_marque_troncature` s'il a été tronqué |

## Contrat de sortie

Tronc commun (`sortie.schema.json`) : `categorie`, `urgence`, `action_attendue`,
`tache`, `echeance`, `justification`, `resume`, `question`, `confiance`.
Extensions actives : leurs propriétés s'ajoutent (`action`, `chantier`).

La **case du topo** (maintenant / cette semaine / ce mois) n'est pas demandée à
l'IA : elle se **déduit** de l'urgence, de l'échéance et d'`action_attendue`
(`regles.json` → `topo`), de la même façon partout.

## Règles de maintenance

1. Toute modification d'un fichier listé dans `manifest.json` → incrémenter
   `version` et recalculer les `sha256`.
   - **patch** : mots-clés, domaines, signaux, descriptions ;
   - **mineur** : nouvelle clé facultative, nouvelle extension, consigne de prompt ;
   - **majeur** : contrat de sortie, algorithme, format d'entrée, sémantique.
2. Un bug corrigé = **un cas ajouté** dans `tests/` (valeurs écrites à la main).
3. Chaque harnais vérifie les empreintes au démarrage et inscrit la `version`
   du moteur dans chaque jugement.
4. Dépôt de référence : **GesEnt** (`resources/moteur/`). IAMail en garde une
   copie identique (vérifiée par les empreintes).

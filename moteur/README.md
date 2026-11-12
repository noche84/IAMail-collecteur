# moteur/ — données partagées du moteur de tri

Ces fichiers sont la **source de vérité unique** du moteur de tri. Ils sont lus
**tels quels** par chaque programme qui classe des emails :

- le compagnon Python d'IAMail (`compagnon/iamail_compagnon.py`) ;
- le module IAEmail de GesEnt (PHP/Laravel) ;
- tout futur harnais (agent Outlook, etc.).

Les collecteurs purs (extension Thunderbird, bouton cockpit) ne classent rien :
ils n'ont pas besoin de ces fichiers.

On modifie la logique **ici**, jamais dans le code d'un seul harnais.

| Fichier | Rôle |
|---|---|
| `manifest.json` | Identité du moteur : version semver, liste des fichiers et leur empreinte sha256. |
| `prompt.txt` | Le prompt IA, identique au mot près pour tous les harnais. |
| `regles.json` | Règles déterministes, **seuils** et **sémantique de comparaison** (clé `_semantique`). |
| `taxonomie.json` | Taxonomie par défaut (construction). GesEnt la surcharge par entreprise. |
| `sortie.schema.json` | Contrat de la réponse IA. Toute réponse est validée avant écriture en base. |
| `tests/` | Jeu de conformité commun (à constituer) : chaque harnais doit le passer. |

## Règles de maintenance

1. Toute modification d'un fichier listé dans `manifest.json` → incrémenter
   `version` (semver) et recalculer les empreintes `sha256`.
   - **patch** : ajout/retrait de mots-clés ou de domaines ;
   - **mineur** : nouvelle clé dans `regles.json`, changement de prompt, de seuil ;
   - **majeur** : changement du contrat de sortie ou d'une sémantique de comparaison.
2. Une nouvelle clé de `regles.json` n'a d'effet que lorsque **chaque** harnais
   l'implémente. Tant que ce n'est pas fait, les tests de conformité le montrent.
3. Les clés existantes restent au **format plat** (listes) : un harnais ancien
   doit pouvoir lire un `regles.json` récent sans planter.
4. Un harnais qui démarre vérifie les empreintes de sa copie locale ; en cas
   d'écart, il le signale (copie modifiée à la main ou incomplète).

Spécification complète : `docs/moteur-tri-partage.md` dans le dépôt GesEnt.

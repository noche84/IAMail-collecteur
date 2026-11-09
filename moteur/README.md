# moteur/ — données partagées du moteur de tri

Ces fichiers sont la **source de vérité unique** du moteur de tri, consommée par
les deux programmes (le compagnon Python d'IAMail, et à terme le module IAEmail
de GesEnt en PHP). On modifie la logique **ici**, pas dans le code.

| Fichier | Rôle |
|---|---|
| `prompt.txt` | Le prompt IA, identique au mot près dans les deux programmes. |
| `regles.json` | Les règles déterministes (mots-clés, expéditeurs automatiques, sujets importants, catégories). |
| `taxonomie.json` | Taxonomie par défaut (construction). GesEnt la surcharge par entreprise. |

Le harnais (Python ou PHP) ne fait que lire ces fichiers et les appliquer. Le
compagnon Python en embarque une copie locale pour tourner hors ligne ; GesEnt
sert la version de référence.

Spécification complète : `docs/moteur-tri-partage.md` dans le dépôt GesEnt.

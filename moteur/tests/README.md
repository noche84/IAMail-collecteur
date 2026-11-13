# moteur/tests — jeu de conformité partagé (moteur 2.x)

Garantit que **tous les harnais** (compagnon Python, GesEnt PHP, futurs) produisent
le même résultat à partir des mêmes fichiers `moteur/`. Un harnais n'est conforme
que s'il passe **tous** les cas.

```
contexte.json           # contexte commun : adresses « moi », importants, taxonomie, règles de comparaison
cas/NNN-nom.eml         # email RFC 822 brut
cas/NNN-nom.attendu.json
_generer.py             # régénère les cas à l'identique (attendus écrits À LA MAIN)
```

## Contexte (identique pour tous les cas)

- `mes_adresses` = `["moi@ccl.be"]`, `importants` = `[]` ;
- taxonomie = `../taxonomie.json`, toutes catégories actives, aucune extension ;
- `ia_extrait_max` = 4000.

## Comparaison

- **`extraction`** : clés `message_id`, `de`, `a`, `cc`, `cle_fil`,
  `extrait_debut` (l'extrait doit commencer par cette chaîne). Égalité stricte.
- **`deterministe`** : `categorie`, `pour_moi` (1/0/null), `expediteur_humain`,
  `bulk`, `important`, `suspect` (0/1), `urgence`, `signaux` (liste **triée par
  code point** des expressions qui ont matché, telles qu'écrites dans la
  taxonomie, qu'elles comptent ou non). Égalité stricte.
- `raison` et `confiance` ne sont pas comparées.

Un cas ne vérifie que les clés qu'il liste.

## Lancer

- PHP (GesEnt) : `php artisan test --filter=ConformiteMoteurTest`
- Python (IAMail) : `python3 -I run_conformance.py`

## Couverture (26 cas)

Humain en copie · newsletter `List-Unsubscribe` · alerte bancaire en masse
(importante, `finance`) · domaine composé `bnpparibasfortis` + label marketing
`info` · « urgent » humain (haute) vs robot (ignoré) · hameçonnage Reply-To ·
fil `Re: Re: Tr:` · `booking.com` non important · « Suivi de commande » ·
`setups` ≠ `ups` · apostrophe typographique · corps HTML seul · **sans
Message-ID** (identifiant synthétique) · **œ** conservé + `[étiquette]` ·
objet **RFC 2047 base64** · corps **ISO-8859-1** · freemail sans prénom.nom +
**chevrons** du Message-ID · **mot le plus long** (paiement > facture) ·
**signal critique dans le corps** · « danger » dans une pub (ignoré) · urgent
**en copie plafonné** · signal qui **baisse** · **réponse automatique** ·
**monter l'emporte** sur baisser · **catégorie par domaine** (dpd).

## Règles

- Aucun harnais ne modifie les cas pour « passer » : on corrige le harnais, ou on
  corrige `moteur/` (et la version) puis l'attendu, à la main.
- Tout bug corrigé ajoute un cas (non-régression partagée).

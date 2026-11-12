# moteur/tests — jeu de conformité partagé

But : **garantir que les deux harnais (compagnon Python, GesEnt PHP) produisent le
même résultat** à partir des mêmes fichiers `moteur/`. C'est le garde-fou contre la
dérive silencieuse entre implémentations (cf. TRANSMISSION §5).

## Structure

```
cas/
  NNN-nom.eml           # un email réel (RFC 822), lu par Python email / PHP mail-mime-parser
  NNN-nom.attendu.json  # résultat attendu, en deux étages
```

Chaque `.attendu.json` sépare volontairement deux étages, car le parsing d'un `.eml`
diffère un peu entre bibliothèques :

```json
{
  "extraction": {            // ce que le COLLECTEUR extrait de l'eml
    "de": "jean.dupont@entreprise.be",
    "cc": ["moi@ccl.be"],
    "cle_fil": "reunion de coordination herstal",
    "extrait_debut": "Bonjour,"      // le corps extrait commence par…
  },
  "deterministe": {          // ce que le MOTEUR déterministe produit (classer)
    "pour_moi": 0, "expediteur_humain": 1, "bulk": 0,
    "important": 1, "suspect": 0, "categorie": "rendez_vous", "urgence": "normale"
  }
}
```

Un cas ne vérifie que les clés qu'il liste (les autres sont ignorées), pour rester
robuste et lisible.

## Lancer (Python)

```bash
python3 run_conformance.py      # OK/ÉCHEC par cas, code retour ≠ 0 si échec
```

## Régénérer les cas

Les `.eml`/`.attendu.json` sont **statiques** (committés) pour que les deux harnais
lisent exactement la même chose. `_generer.py` permet de les reconstruire à
l'identique si besoin :

```bash
python3 _generer.py
```

## Côté GesEnt (PHP)

Écrire un `ConformiteTest` PHPUnit qui lit les mêmes `cas/*.eml` + `*.attendu.json`,
applique le harnais PHP, et compare les deux étages. **Aucun des deux harnais ne
doit modifier les fichiers de cas** : on modifie la logique dans `moteur/`, et on
ajoute un cas quand on corrige un bug (test de non-régression partagé).

## Cas couverts (v1.1.0)

humain en copie · newsletter `List-Unsubscribe` · alerte bancaire en masse (reste
important) · domaine composé `bnpparibasfortis.be` · « urgent » humain vs robot ·
hameçonnage (Reply-To ≠ From) · fil `Re: Re: Tr:` · `booking.com` pas important ·
« Suivi de commande » → livraison · « setups » ≠ `ups` · « aujourd'hui » (apostrophe)
· corps HTML seul.

À compléter (TRANSMISSION §5) : mail sans Message-ID, objet avec œ, RFC 2047,
corps ISO-8859-1, `prenom.nom` vs `info@`.

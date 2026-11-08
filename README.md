# IAMail — Collecteur Thunderbird (v0)

Extension Thunderbird (MailExtension, manifest v3, TB 128+) qui relève les
nouveaux messages et les archive en `.eml` + `.json` jumeau, nommés de façon
triable. **v0 : hors ligne, lecture seule.** Aucune écriture dans la boîte
(pas de déplacement, pas de marquage), aucune suppression. C'est la première
brique du collecteur décrit dans l'ADR 0041 de GesEnt.

## Ce que fait la v0
- Écoute l'arrivée de nouveaux messages → les archive automatiquement.
- Bouton « Relever la boîte de réception » pour un premier test sur l'existant.
- Écrit deux fichiers par message :
  - `<base>.eml` : le message MIME brut (pièces jointes incluses) ;
  - `<base>.json` : métadonnées (Message-ID, expéditeur, destinataires, copies,
    date, sujet, liste des pièces jointes) — l'index cherchable.
- Nommage : `AAAA-MM-JJ_HHMMSS__<expediteur>__<sujet>__<hash6>`, rangé en
  `<dossier>/AAAA/MM/`.
- Dédoublonnage par Message-ID.

## Limites assumées de la v0 (bac à sable Thunderbird)
- **Format `.eml`, pas `.msg`** : Thunderbird expose le MIME standard. Le vrai
  `.msg` viendra avec l'agent Outlook/COM.
- **Écriture sous le dossier Téléchargements** de Thunderbird uniquement (dans le
  sous-dossier réglé). Pour viser une part réseau quelconque + une base SQLite,
  il faudra un **compagnon natif** (étape suivante, qui portera aussi la synchro
  GesEnt).
- Métadonnées de suivi stockées dans le stockage local de l'extension.

## Installer pour tester (extension temporaire)
1. Thunderbird → menu ☰ → **Modules complémentaires et thèmes**.
2. Roue dentée → **Déboguer des modules complémentaires** (ou `about:debugging`).
3. **Ce Thunderbird** → **Charger un module temporaire…**
4. Choisir le fichier `src/manifest.json` (ou le `.xpi` fourni).
5. Régler le dossier de Téléchargements voulu dans Thunderbird
   (Paramètres → Général → Fichiers et pièces jointes).
6. Cliquer l'icône **IAMail** dans la barre d'outils → **Relever la boîte de
   réception**. Les fichiers apparaissent sous `Téléchargements/IAMail/AAAA/MM/`.

Un module temporaire disparaît au redémarrage de Thunderbird ; c'est voulu
pour la phase de test.

## Structure
```
src/
  manifest.json   déclaration de l'extension
  slug.js         nommage des fichiers d'archive
  archive.js      relève et écriture .eml/.json (lecture seule)
  background.js   écoute des nouveaux messages + commandes de la popup
  popup.html/js   bouton de relève manuelle + état
  options.html/js réglages (dossier, longueur sujet, auto on/off)
```

## Suite prévue
1. Compagnon natif : écriture vers un dossier/part réseau arbitraire + SQLite.
2. Synchronisation avec GesEnt (protocole d'ingestion, ADR 0041).
3. Topo du matin + audio en mode autonome.

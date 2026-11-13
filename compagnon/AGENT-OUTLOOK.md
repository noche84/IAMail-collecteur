# Agent Outlook (Windows) — collecteur pour les collègues

Pour les postes sous **Outlook classique**. Contrairement à Thunderbird (qui exige une
extension), Outlook s'automatise en **COM** : le compagnon **est** l'agent, en un seul
processus. Il fait tout, sans que le collègue voie Python ni le terminal :

- **Relève** : lit la Boîte de réception via Outlook (déjà authentifié localement),
  reconstruit chaque mail en `.eml` standard (+ garde le `.msg` natif comme coffre-fort),
  l'archive et l'indexe. Le classement (déterministe + IA) est **le même moteur** que
  partout ailleurs.
- **Retour-mail** (bidirectionnel, en direct) : « Traité » dans le cockpit → **catégorie
  « Traité IAMail » + lu** sur le mail dans Outlook ; et si le collègue pose/retire cette
  catégorie, ou supprime/déplace le mail dans Outlook, le cockpit suit. Lire un mail ne le
  traite jamais.

Aucun enregistrement d'application dans le tenant, aucun droit administrateur : COM agit
en tant que l'utilisateur sur son Outlook local (conforme à la contrainte GDPR / Bruxelles).

## Activer

Dans la config (`%LOCALAPPDATA%\IAMail\config.json`) :

```json
{
  "collecteur": "outlook",
  "outlook_dossiers": [],            // vide = Boîte de réception ; sinon ["Compte/Dossier", …]
  "dossier_destination": "C:/Users/<toi>/IAMail/Archive",
  "base_sqlite": "C:/Users/<toi>/IAMail/index.sqlite3",
  "mes_adresses": ["prenom.nom@embuild.be"],
  "ia": { "fournisseur": "mock" }    // ou "mistral" + MISTRAL_API_KEY
}
```

Puis l'installeur Windows (`empaquetage\installer-windows.ps1`) pose le tout en
démarrage automatique **caché**. Le compagnon relève, classe et applique le retour-mail
à chaque cycle.

## Construire

`empaquetage\construire-windows.ps1` installe `pyinstaller` + `pywin32` et produit
`compagnon\dist\iamail.exe` (agent Outlook inclus).

## Architecture (pour GesEnt / maintenance)

- `collecteur_outlook.py` : adaptateur COM **mince** (pywin32, importé seulement au
  besoin) + **cœur pur testable** (`construire_eml`, `message_id_depuis`,
  `decisions_retour`, `lire_mailitem`). Le cœur est testé sans Outlook (faux `MailItem`).
- Chaque mail Outlook devient un `.eml` standard → **tout le pipeline en aval est
  identique** à Thunderbird (analyse, extrait, IA, cockpit). Aucun code spécifique en aval.
- `source_type='outlook'`, `source_ref=EntryID` : pour retrouver l'item et faire le
  retour-mail. Message-ID réel (PR_INTERNET_MESSAGE_ID), `synth-…` si absent.
- Adresses SMTP réelles, y compris expéditeurs/destinataires **Exchange** (type `EX` →
  `GetExchangeUser().PrimarySmtpAddress`).

## À valider sur un poste réel

Le cœur logique est testé, mais le code **COM** (relève, catégories, lecture/écriture)
ne peut être exercé que sur une machine **Windows + Outlook**. Comme pour l'extension
Thunderbird à ses débuts, prévoir **un ou deux allers-retours** pour ajuster les détails
de l'Object Model (variantes de version d'Outlook, comptes Exchange vs IMAP). Points à
vérifier en premier : la relève remonte bien les mails (Message-ID, SMTP, To/Cc), la
catégorie « Traité IAMail » apparaît au clic « Traité », et la suppression/déplacement
dans Outlook sort le mail du cockpit.

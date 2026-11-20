# Agent Apple Mail (macOS) — collecteur pour les postes Mac

Pour les Mac qui utilisent l'app **Mail** d'Apple. Comme Outlook classique (COM), Mail se
pilote de l'extérieur — ici par les **Apple Events**, en JXA (JavaScript for Automation)
via `osascript`. Le compagnon **est** l'agent, en un seul processus, sans extension ni
droit administrateur :

- **Relève** : Mail fournit la **source brute** de chaque message ; elle est archivée telle
  quelle en `.eml` (+ `.json` jumeau), exactement comme un message lu dans un mbox
  Thunderbird. Tout le pipeline en aval (analyse déterministe, extrait, IA, cockpit) est
  identique, pièces jointes comprises.
- **Retour-mail** (bidirectionnel) : Mail n'a pas de catégories scriptables. « Traité »
  dans le cockpit → **drapeau vert + lu** sur le mail dans Mail. Poser ou retirer ce
  drapeau dans Mail fait suivre le cockpit. Lire un mail ne le traite jamais ; l'état
  lu / non lu de Mail est repris au cockpit.
- **Envoyés** : les en-têtes de la boîte « Envoyés » de Mail (moteur 2.4 : « as-tu déjà
  répondu ? », « à qui écris-tu ? »).

Tous les comptes configurés dans Mail (iCloud, Exchange, Gmail, IMAP…) passent par là :
Mail a déjà fait l'authentification.

## Activer

Dans la config (`~/Library/Application Support/IAMail/config.json`) :

```json
{
  "collecteur": "applemail",
  "applemail_boites": [],          // vide = Boîte de réception unifiée ; sinon ["Compte/Boîte", …]
  "applemail_jours": 60,           // fenêtre relue à chaque cycle (0 = toute la boîte)
  "applemail_drapeau": "vert",     // couleur qui veut dire « Traité IAMail »
  "applemail_lancer": false,       // Mail fermé : on attend (true = l'ouvrir)
  "dossier_destination": "/Users/<toi>/IAMail/Archive",
  "base_sqlite": "/Users/<toi>/IAMail/index.sqlite3",
  "mes_adresses": ["prenom.nom@embuild.be"]
}
```

Puis `bash installer-macos.sh` pose le tout en **LaunchAgent** (démarre à l'ouverture de
session, caché, relancé s'il s'arrête).

**Astuce** : dans la barre latérale de Mail, section « Avec drapeau », renomme le drapeau
vert en « Traité IAMail » (clic droit → Renommer). Si tu utilises déjà le vert pour autre
chose, choisis une autre couleur avec `applemail_drapeau`.

## L'autorisation macOS (une seule fois)

Au premier cycle, macOS affiche : **« iamail » souhaite contrôler « Mail »** → **OK**.
Si on a cliqué « Ne pas autoriser », le cockpit l'affiche en bandeau ; on rétablit dans
**Réglages Système → Confidentialité et sécurité → Automatisation → iamail → Mail**.

## Construire

`empaquetage/construire-macos.sh` (sur un Mac). L'agent Apple Mail est inclus dans
l'exécutable macOS ; il n'a aucune dépendance (osascript fait partie de macOS).

## Architecture (pour GesEnt / maintenance)

- `collecteur_applemail.py` : quatre petits scripts JXA (lister, lire les sources,
  retour-mail, envoyés) + **cœur pur testable** (`a_relever`, `etat_par_ref`,
  `envoi_depuis_entetes`, `drapeau`, `depuis_iso`). Les décisions de retour-mail sont
  **les mêmes** que pour Outlook (`collecteur_outlook.decisions_retour`).
- Les tests remplacent `osascript` par un faux Mail : relève, dédoublonnage, retour-mail
  dans les deux sens, Mail fermé.
- `source_type='applemail'`, `source_ref` = identifiant du message dans Mail. Message-ID
  réel, `synth-…` si absent (règle commune du moteur).
- Lecture en masse : un seul Apple Event par propriété (id, Message-ID, date, lu,
  drapeau) sur la fenêtre ; la source n'est lue que pour les messages nouveaux, par
  paquets de 25. Un mail arrivé entre deux appels est simplement repris au cycle suivant.

## À valider sur un Mac réel

Le cœur est testé, mais le JXA ne s'exécute que sur un Mac avec Mail. Comme pour
l'agent Outlook, prévoir un ou deux allers-retours. À vérifier en premier :

1. La demande d'autorisation apparaît bien pour « iamail » lancé par le LaunchAgent.
2. `iamail --config … import` remonte les mails (objet, expéditeur, pièces jointes).
3. « Traité » au cockpit pose le drapeau vert ; retirer le drapeau dans Mail rouvre le
   mail au cockpit.
4. Durée d'un cycle sur une grosse boîte (sinon réduire `applemail_jours`).

Pas encore géré (comme sous Outlook) : supprimer ou déplacer un mail dans Mail ne le
marque pas « traité » au cockpit.

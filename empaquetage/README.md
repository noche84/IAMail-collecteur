# Empaquetage — exécutable autonome + installeur

But : livrer IAMail à quelqu'un qui **ne doit jamais voir Python ni le terminal**.
On produit un **exécutable autonome** (Python + dépendances + `moteur/` embarqués),
puis un **installeur** qui le pose en **service auto-démarrant caché** avec un
raccourci « Cockpit ».

> PyInstaller ne compile pas d'un OS vers un autre : on **construit sur chaque OS
> cible**. L'installeur, lui, ne demande ni Python ni droits administrateur.

## 1. Construire l'exécutable (une fois par OS)

Prérequis : Python 3 + `pip install pyinstaller` sur la machine de build.

| OS | Commande | Sortie |
|----|----------|--------|
| Linux | `bash empaquetage/construire-linux.sh` | `compagnon/dist/iamail` |
| macOS | `bash empaquetage/construire-macos.sh` | `compagnon/dist/iamail` |
| Windows | `powershell -ExecutionPolicy Bypass -File empaquetage\construire-windows.ps1` | `compagnon\dist\iamail.exe` |

L'exécutable embarque le `moteur/` courant. Pour **mettre à jour le moteur sans
reconstruire**, il suffit de poser un dossier `moteur/` à côté de l'exécutable : il a
la priorité sur la copie embarquée (pratique quand GesEnt publie une nouvelle version).

## 2. Installer (poste de l'utilisateur, sans Python ni admin)

| OS | Commande |
|----|----------|
| Linux | `bash empaquetage/installer-linux.sh` |
| macOS | `bash empaquetage/installer-macos.sh` |
| Windows | `powershell -ExecutionPolicy Bypass -File empaquetage\installer-windows.ps1` |

L'installeur :
- copie l'exécutable (Linux : `~/.local/bin` ; macOS : `~/Library/Application
  Support/IAMail` ; Windows : `%LOCALAPPDATA%\IAMail`) ;
- crée une **config** à personnaliser (boîte, adresses, dossiers) et un fichier de
  **clé IA** privé (facultatif — sans clé, mode dégradé sans IA) ;
- installe le **démarrage automatique caché** (Linux : service utilisateur systemd ;
  macOS : LaunchAgent ; Windows : dossier Démarrage + lanceur `.vbs` sans fenêtre) ;
- ajoute un raccourci **« Cockpit IAMail »** (ouvre http://127.0.0.1:8740/).

**Mettre à jour un poste** : relancer le même installeur avec le nouvel exécutable posé à
côté. La config, la clé IA et l'archive sont gardées ; le compagnon en cours est arrêté
puis relancé (sous Windows, l'installeur l'arrête lui-même : un `.exe` qui tourne ne
peut pas être remplacé). Vérifier ensuite avec `iamail --version`.

Désinstallation : `desinstaller-linux.sh` / `desinstaller-macos.sh` /
`desinstaller-windows.ps1` (la config et
l'archive sont conservées).

## 3. État par plateforme

| Client mail | Poste | Source des mails | Retour-mail « Traité » |
|-------------|-------|------------------|------------------------|
| Thunderbird | Linux, macOS, Windows | lecture du mbox | étiquette, via l'extension `thunderbird-cockpit-button/` (`.xpi` joint aux releases) |
| Outlook classique | Windows | agent COM (`AGENT-OUTLOOK.md`) | catégorie « Traité IAMail » + lu |
| Apple Mail | macOS | agent JXA (`AGENT-APPLEMAIL.md`) | drapeau vert + lu |

Le choix se fait par `"collecteur"` dans la config (`thunderbird`, `outlook`,
`applemail`). L'installeur macOS règle d'office `applemail`.

## 4. Pour distribuer — construction automatique (GitHub Actions)

Le workflow `.github/workflows/executables.yml` construit et **vérifie** les quatre
exécutables sur des machines GitHub (Linux x64, Windows x64, Mac Apple, Mac Intel) :

- à chaque PR ou push qui touche `compagnon/`, `moteur/` ou `empaquetage/` : les
  exécutables sont téléchargeables dans l'onglet **Actions** (« artifacts », 30 jours) ;
- à la demande : **Actions → Exécutables → Run workflow**. Si on saisit une **version**
  (`v1.19.1`), le workflow crée lui-même le tag et la release sur la branche
  choisie : **un navigateur suffit**, sans clone du dépôt ;
- sur un **tag** `vX.Y.Z` (`git tag v1.19.1 && git push origin v1.19.1`) : une
  **release GitHub** est publiée avec les exécutables, les installeurs et
  `config.exemple.json`. Les installeurs trouvent seuls `iamail-linux-x64` /
  `iamail-macos-arm64` / `iamail-macos-x64` / `iamail-windows-x64.exe` posés à côté d'eux.

Sur les Mac de GitHub, les scripts Apple Mail (JXA) sont aussi **compilés** à chaque
construction : une faute de syntaxe y est arrêtée sans attendre un Mac réel.

> **Node.js** : le projet n'en utilise pas. L'avertissement « Node.js 20 is
> deprecated » visait les *actions* GitHub du workflow (checkout, setup-python,
> upload/download-artifact), du code JavaScript dont la version majeure fixe la
> version de Node. Elles sont maintenant en v7/v8, sur Node 24.

Chaque exécutable passe `empaquetage/verifier-executable.py` : le moteur embarqué est
celui du dépôt (`iamail --version` l'affiche), puis import d'un petit mbox, tri par les
règles, IA simulée, brief. Une construction qui échoue ne publie rien.

> **Mise à jour d'un poste** : un dossier `moteur/` posé à côté de l'exécutable
> l'emporte sur la copie embarquée. Après une mise à jour, vérifier avec
> `iamail --version` quel moteur est réellement chargé — et supprimer un ancien
> `moteur/` resté à côté s'il masque la nouvelle version.

## 5. Distribution manuelle

Une fois construit et testé sur un OS, on peut pousser l'exécutable comme **release
GitHub** (binaire), et les collègues lancent juste l'installeur. Les scripts, eux,
vivent dans le dépôt (`empaquetage/`).

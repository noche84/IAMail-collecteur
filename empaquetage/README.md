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
| Windows | `powershell -ExecutionPolicy Bypass -File empaquetage\installer-windows.ps1` |

L'installeur :
- copie l'exécutable (Linux : `~/.local/bin` ; Windows : `%LOCALAPPDATA%\IAMail`) ;
- crée une **config** à personnaliser (boîte, adresses, dossiers) et un fichier de
  **clé IA** privé (facultatif — sans clé, mode dégradé sans IA) ;
- installe le **démarrage automatique caché** (Linux : service utilisateur systemd ;
  Windows : dossier Démarrage + lanceur `.vbs` sans fenêtre) ;
- ajoute un raccourci **« Cockpit IAMail »** (ouvre http://127.0.0.1:8740/).

Désinstallation : `desinstaller-linux.sh` / `desinstaller-windows.ps1` (la config et
l'archive sont conservées).

## 3. État par plateforme

- **Linux / macOS (Thunderbird)** : complet — relève du mbox, cockpit, retour-mail via
  l'extension Thunderbird.
- **Windows** : l'exécutable et l'installeur fonctionnent ; la **source des mails** est
  aujourd'hui Thunderbird. Pour **Outlook classique** (la plupart des collègues),
  l'**agent Outlook** est le prochain jalon — il alimentera le même compagnon.

## 4. Pour distribuer

Une fois construit et testé sur un OS, on peut pousser l'exécutable comme **release
GitHub** (binaire), et les collègues lancent juste l'installeur. Les scripts, eux,
vivent dans le dépôt (`empaquetage/`).

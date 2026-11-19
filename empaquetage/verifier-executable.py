#!/usr/bin/env python3
"""Vérifie un exécutable IAMail construit, de bout en bout, sans réseau ni clé IA.

    python3 empaquetage/verifier-executable.py compagnon/dist/iamail[.exe]

1. --version : le moteur EMBARQUÉ est bien celui du dépôt (même version).
2. Chaîne complète dans un dossier temporaire : init → import d'un petit mbox →
   analyser (règles) → analyser-ia --mock → brief → stats.
Sort en erreur (code 1) au premier écart. Utilisé par la CI (.github/workflows).
"""
import json
import mailbox
import subprocess
import sys
import tempfile
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent


def lancer(exe, *args, cwd):
    r = subprocess.run([str(exe), *args], cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=180)
    sortie = (r.stdout or "") + (r.stderr or "")
    print(f"$ iamail {' '.join(args)}\n{sortie.strip()}\n")
    if r.returncode != 0:
        sys.exit(f"ÉCHEC (code {r.returncode}) : {' '.join(args)}")
    return sortie


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    exe = Path(sys.argv[1]).resolve()
    attendu = json.loads((RACINE / "moteur" / "manifest.json").read_text(encoding="utf-8"))["version"]

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)        # dossier vide : aucun moteur/ à côté ne peut masquer l'embarqué
        v = lancer(exe, "--version", cwd=tmp)
        if f"moteur {attendu}" not in v:
            sys.exit(f"ÉCHEC : moteur embarqué ≠ {attendu}")

        cfg = tmp / "config.json"
        lancer(exe, "--config", str(cfg), "init", cwd=tmp)
        c = json.loads(cfg.read_text(encoding="utf-8"))
        c.update({"dossier_staging": str(tmp / "in"), "dossier_destination": str(tmp / "arch"),
                  "base_sqlite": str(tmp / "i.sqlite3"), "mes_adresses": ["moi@exemple.be"],
                  "ia": {"fournisseur": "mock"}})
        cfg.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")

        mb = mailbox.mbox(str(tmp / "INBOX"))
        for i, (de, sujet, corps) in enumerate([
            ("Marc Petit <marc.petit@client.be>", "Serveur de fichiers inaccessible",
             "Bonjour, plus personne n'accède au serveur depuis ce matin. Urgent."),
            ("news@promo.exemple.com", "Nos promotions de la semaine", "Désabonnez-vous ici."),
            ("Julie Martin <julie.martin@client.be>", "Réunion de jeudi",
             "Peux-tu confirmer ta présence jeudi à 14h30 ?"),
        ]):
            extra = "List-Unsubscribe: <mailto:u@promo.exemple.com>\n" if "news@" in de else ""
            mb.add((f"From: {de}\nTo: moi@exemple.be\nSubject: {sujet}\n"
                   f"Message-ID: <verif{i}@exemple.be>\nDate: Mon, 16 Nov 2026 09:0{i}:00 +0100\n"
                   f"{extra}Content-Type: text/plain; charset=utf-8\nContent-Transfer-Encoding: 8bit\n\n"
                   f"{corps}\n").encode("utf-8"))
        mb.flush(); mb.close()

        lancer(exe, "--config", str(cfg), "import", "--mbox", str(tmp / "INBOX"), cwd=tmp)
        lancer(exe, "--config", str(cfg), "analyser", cwd=tmp)
        s = lancer(exe, "--config", str(cfg), "analyser-ia", "--mock", cwd=tmp)
        if "jugés : " not in s:
            sys.exit("ÉCHEC : analyser-ia n'a rien jugé")
        b = lancer(exe, "--config", str(cfg), "brief", cwd=tmp)
        if "Serveur de fichiers" not in b and "serveur" not in b.lower():
            sys.exit("ÉCHEC : le brief ne contient pas le message attendu")
        st = lancer(exe, "--config", str(cfg), "stats", cwd=tmp)
        if "3" not in st:
            sys.exit("ÉCHEC : stats inattendues")
    print(f"✅ Exécutable vérifié : {exe.name} — moteur {attendu}")


if __name__ == "__main__":
    main()

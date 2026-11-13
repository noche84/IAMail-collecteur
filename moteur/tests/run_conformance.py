#!/usr/bin/env python3
# Runner de conformité du harnais Python (moteur 2.x). Lit contexte.json + chaque
# cas/<n>.eml + <n>.attendu.json, passe le MÊME chemin que la production
# (extraction → extrait → classer) et compare 'extraction' et 'deterministe'.
# Le harnais PHP (GesEnt) lit exactement les mêmes fichiers.
#
#   python3 -I run_conformance.py
#
# Attendu : 26/26. Un écart = bug du harnais ou du moteur (le remonter avec le cas).
import email
import importlib.util
import json
import sys
from pathlib import Path

ICI = Path(__file__).resolve().parent
RACINE = ICI.parent.parent
_CANDIDATS = [RACINE / "iamail_compagnon.py", RACINE / "compagnon" / "iamail_compagnon.py"]
_MOD = next((p for p in _CANDIDATS if p.exists()), None)
if _MOD is None:
    print("iamail_compagnon.py introuvable.", file=sys.stderr)
    sys.exit(2)
spec = importlib.util.spec_from_file_location("iamail_compagnon", _MOD)
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)

CTX = json.loads((ICI / "contexte.json").read_text(encoding="utf-8"))
MES = CTX["mes_adresses"]
IMPORTANTS = CTX.get("importants", [])
MAX = int(CTX.get("ia_extrait_max", 4000))
# Charge le moteur (regles + taxonomie désignée par le contexte) et peuple les globals.
M.charger_moteur({"moteur_dir": str((ICI / CTX.get("taxonomie", "../taxonomie.json")).resolve().parent)})


def extraire(msg):
    d = lambda k: M._dec_entete(msg.get(k)) or ""
    entetes = {
        "from": d("From"), "to": d("To"), "cc": d("Cc"), "subject": d("Subject"),
        "reply_to": d("Reply-To"), "list_unsubscribe": d("List-Unsubscribe"),
        "list_id": d("List-Id"), "precedence": d("Precedence"),
    }
    return entetes, {
        "message_id": M._message_id(msg),
        "de": (M._adresses_spec(entetes["from"]) or [""])[0],
        "a": M._adresses_spec(entetes["to"]),
        "cc": M._adresses_spec(entetes["cc"]),
        "cle_fil": M._sujet_fil(entetes["subject"]),
        "extrait": M._corps_texte(msg, MAX),
    }


def verifier(cas):
    att = json.loads(cas.with_suffix(".attendu.json").read_text(encoding="utf-8"))
    with open(cas, "rb") as f:
        msg = email.message_from_binary_file(f)
    entetes, ex = extraire(msg)
    ecarts = []

    for k, attendu in att.get("extraction", {}).items():
        if k == "extrait_debut":
            if not ex["extrait"].startswith(attendu):
                ecarts.append(f"extrait: {ex['extrait'][:40]!r} ne commence pas par {attendu!r}")
        elif ex.get(k) != attendu:
            ecarts.append(f"{k}: {ex.get(k)!r} ≠ {attendu!r}")

    res = M.classer(entetes, ex["extrait"], MES, IMPORTANTS)
    for k, attendu in att.get("deterministe", {}).items():
        got = sorted(res["signaux"]) if k == "signaux" else res.get(k)
        if got != attendu:
            ecarts.append(f"{k}: {got!r} ≠ {attendu!r}")
    return ecarts


def main():
    cas = sorted((ICI / "cas").glob("*.eml"))
    echecs = 0
    for c in cas:
        ecarts = verifier(c)
        if ecarts:
            echecs += 1
            print(f"ÉCHEC {c.stem}")
            for e in ecarts:
                print(f"    - {e}")
        else:
            print(f"OK    {c.stem}")
    print(f"\n{len(cas) - echecs}/{len(cas)} cas conformes.")
    return 1 if echecs else 0


if __name__ == "__main__":
    sys.exit(main())

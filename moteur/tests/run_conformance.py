#!/usr/bin/env python3
# Runner de conformité (harnais Python). Lit chaque cas/<n>.eml + <n>.attendu.json,
# applique le moteur de tri du compagnon, compare le bloc 'deterministe' et vérifie
# l'extraction de base. Le harnais PHP (GesEnt) lira exactement les mêmes fichiers.
#
#   python3 run_conformance.py
#
# Sortie : OK / ÉCHEC par cas, code retour non nul si au moins un échec.
import email
import importlib.util
import json
import sys
from pathlib import Path

ICI = Path(__file__).resolve().parent
RACINE = ICI.parent.parent
spec = importlib.util.spec_from_file_location(
    "iamail_compagnon", RACINE / "compagnon" / "iamail_compagnon.py")
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)
M.charger_regles({})            # charge moteur/regles.json (donnée partagée)


def entetes(msg):
    """MessageHeader -> dict attendu par classer(), en-têtes DÉCODÉS comme le harnais."""
    g = lambda k: (M._dec_entete(msg.get(k)) or "")
    return {
        "from": g("From"), "to": g("To"), "cc": g("Cc"),
        "subject": g("Subject"), "reply_to": g("Reply-To"),
        "return_path": g("Return-Path"), "precedence": g("Precedence"),
        "auto_submitted": g("Auto-Submitted"),
        "list_unsubscribe": g("List-Unsubscribe"), "list_id": g("List-Id"),
    }


def verifier(cas):
    att = json.loads((cas.with_suffix(".attendu.json")).read_text(encoding="utf-8"))
    with open(cas, "rb") as f:
        msg = email.message_from_binary_file(f)
    ecarts = []

    # --- extraction ---
    ex = att.get("extraction", {})
    if "cle_fil" in ex:
        got = M._sujet_fil(M._dec_entete(msg.get("Subject")))
        if got != ex["cle_fil"]:
            ecarts.append(f"cle_fil: {got!r} ≠ {ex['cle_fil']!r}")
    if "extrait_debut" in ex:
        corps = M._corps_texte(msg, 2000)
        if not corps.startswith(ex["extrait_debut"]):
            ecarts.append(f"extrait_debut: {corps[:30]!r} ne commence pas par {ex['extrait_debut']!r}")

    # --- déterministe ---
    res = M.classer(entetes(msg), ["moi@ccl.be"])
    det = att.get("deterministe", {})
    # normaliser pour_moi : classer renvoie True/False/None
    norm = dict(res)
    pm = res["pour_moi"]
    norm["pour_moi"] = None if pm is None else (1 if pm else 0)
    norm["expediteur_humain"] = int(bool(res["expediteur_humain"]))
    norm["bulk"] = int(bool(res["bulk"]))
    norm["important"] = int(bool(res["important"]))
    norm["suspect"] = int(bool(res["suspect"]))
    for k, attendu in det.items():
        if norm.get(k) != attendu:
            ecarts.append(f"{k}: {norm.get(k)!r} ≠ {attendu!r}")
    return ecarts


def main():
    cas = sorted((ICI / "cas").glob("*.eml"))
    if not cas:
        print("Aucun cas. Lance d'abord : python3 _generer.py", file=sys.stderr)
        return 1
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

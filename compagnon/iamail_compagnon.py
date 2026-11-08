#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IAMail — compagnon natif (v1).

Rôle : surveiller le dossier où l'extension Thunderbird dépose les paires
.eml/.json, les ranger sur un dossier de destination réel (part serveur
comprise) selon l'arborescence AAAA/MM, et les indexer dans une base SQLite
en dédoublonnant par Message-ID.

Aucune dépendance hors bibliothèque standard. Lecture seule vis-à-vis de la
boîte : le compagnon ne touche jamais à la messagerie, seulement aux fichiers.

Sous-commandes :
  init            crée la base et un fichier de configuration d'exemple
  ingest          une passe : range et indexe tout ce qui est en attente
  watch           boucle : ingère à intervalle régulier
  stats           quelques chiffres sur l'archive
  search "texte"  recherche dans l'index (sujet / expéditeur)

Configuration (config.json) :
  dossier_staging : où l'extension dépose les fichiers (Téléchargements/IAMail)
  dossier_destination : où archiver pour de bon (part serveur, etc.)
  base_sqlite : chemin de l'index
  deplacer : true = déplace (supprime le staging), false = copie
"""

import argparse
import email
import email.header
import email.utils
import hashlib
import json
import mailbox
import os
import re
import shutil
import sqlite3
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

NOM = "iamail-compagnon"
VERSION = "1.1.0"

CONFIG_DEFAUT = {
    "dossier_staging": "",
    "dossier_destination": "",
    "base_sqlite": "iamail_index.sqlite3",
    "deplacer": True,
}


# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
def charger_config(chemin):
    p = Path(chemin)
    if not p.exists():
        raise SystemExit(
            f"Configuration introuvable : {p}\n"
            f"Lancez d'abord :  python3 {Path(__file__).name} init --config {p}"
        )
    cfg = dict(CONFIG_DEFAUT)
    cfg.update(json.loads(p.read_text(encoding="utf-8")))
    if not cfg["dossier_staging"] or not cfg["dossier_destination"]:
        raise SystemExit(
            "Renseignez 'dossier_staging' et 'dossier_destination' dans "
            f"{p} avant d'ingérer."
        )
    return cfg


def cmd_init(args):
    cfg_path = Path(args.config)
    if cfg_path.exists() and not args.force:
        print(f"{cfg_path} existe déjà (utilisez --force pour écraser).")
    else:
        exemple = dict(CONFIG_DEFAUT)
        exemple["dossier_staging"] = str(Path.home() / "Downloads" / "IAMail")
        exemple["dossier_destination"] = str(Path.home() / "ArchiveIAMail")
        exemple["base_sqlite"] = str(cfg_path.parent / "iamail_index.sqlite3")
        cfg_path.write_text(json.dumps(exemple, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Configuration d'exemple écrite : {cfg_path}")
        print("Ajustez les chemins, puis lancez :  ingest  ou  watch")
    # Crée aussi la base si on peut lire la config.
    try:
        cfg = charger_config(cfg_path)
        connexion(cfg["base_sqlite"]).close()
        print(f"Base prête : {cfg['base_sqlite']}")
    except SystemExit:
        pass


# ----------------------------------------------------------------------------
# Base SQLite
# ----------------------------------------------------------------------------
def connexion(chemin_db):
    Path(chemin_db).parent.mkdir(parents=True, exist_ok=True)
    cx = sqlite3.connect(chemin_db)
    cx.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            message_id   TEXT PRIMARY KEY,
            de_adresse   TEXT,
            destinataires TEXT,
            copies       TEXT,
            date_mail    TEXT,
            sujet        TEXT,
            fichier_eml  TEXT,
            fichier_json TEXT,
            pieces_jointes TEXT,
            dossier_src  TEXT,
            ingere_at    TEXT
        )
    """)
    cx.execute("CREATE INDEX IF NOT EXISTS idx_sujet ON messages(sujet)")
    cx.execute("CREATE INDEX IF NOT EXISTS idx_de ON messages(de_adresse)")
    cx.execute("CREATE INDEX IF NOT EXISTS idx_date ON messages(date_mail)")
    cx.commit()
    return cx


def existe(cx, message_id):
    return cx.execute(
        "SELECT 1 FROM messages WHERE message_id = ?", (message_id,)
    ).fetchone() is not None


# ----------------------------------------------------------------------------
# Ingestion
# ----------------------------------------------------------------------------
def cle_message(meta, base):
    mid = (meta.get("message_id") or "").strip()
    if mid:
        return mid
    # Pas de Message-ID : clé déterministe de repli.
    graine = (meta.get("date", "") + "|" + meta.get("sujet", "") + "|" + base)
    return "sansid:" + hashlib.sha1(graine.encode("utf-8")).hexdigest()[:16]


def partition(meta):
    """Sous-dossier AAAA/MM d'après la date du mail (repli : aujourd'hui)."""
    iso = meta.get("date")
    dt = None
    if iso:
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        except ValueError:
            dt = None
    if dt is None:
        dt = datetime.now(timezone.utc)
    return f"{dt.year:04d}/{dt.month:02d}"


def ecrire_atomique(src_bytes_or_path, dest):
    """Écrit dest de façon atomique (tmp + rename). src peut être un chemin."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    if isinstance(src_bytes_or_path, (bytes, bytearray)):
        tmp.write_bytes(src_bytes_or_path)
    else:
        shutil.copyfile(src_bytes_or_path, tmp)
    os.replace(tmp, dest)


def nom_libre(dossier, base, suffixe):
    """Évite d'écraser : base.eml, base-2.eml, base-3.eml…"""
    cand = Path(dossier) / (base + suffixe)
    i = 2
    while cand.exists():
        cand = Path(dossier) / (f"{base}-{i}{suffixe}")
        i += 1
    return cand


def ingest_un(cx, cfg, json_path):
    """Traite une paire. Renvoie 'archive', 'doublon', 'incomplet' ou 'erreur'."""
    base = json_path.stem  # nom sans .json
    eml_path = json_path.with_suffix(".eml")
    if not eml_path.exists():
        return "incomplet"  # le .eml n'est peut-être pas encore écrit

    try:
        meta = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception:
        return "erreur"

    mid = cle_message(meta, base)
    if existe(cx, mid):
        # Déjà indexé : on nettoie le staging si on est en mode déplacement.
        if cfg["deplacer"]:
            _retirer(json_path, eml_path)
        return "doublon"

    sous = partition(meta)
    dossier_dest = Path(cfg["dossier_destination"]) / sous
    dest_eml = nom_libre(dossier_dest, base, ".eml")
    dest_json = dest_eml.with_suffix(".json")

    try:
        ecrire_atomique(eml_path, dest_eml)
        ecrire_atomique(json_path, dest_json)
    except Exception as e:
        print(f"[{NOM}] échec écriture dest pour {base}: {e}", file=sys.stderr)
        return "erreur"

    pj = meta.get("pieces_jointes", [])
    cx.execute(
        """INSERT OR IGNORE INTO messages
           (message_id, de_adresse, destinataires, copies, date_mail, sujet,
            fichier_eml, fichier_json, pieces_jointes, dossier_src, ingere_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            mid,
            meta.get("de_adresse"),
            json.dumps(meta.get("destinataires", []), ensure_ascii=False),
            json.dumps(meta.get("copies", []), ensure_ascii=False),
            meta.get("date"),
            meta.get("sujet"),
            str(dest_eml),
            str(dest_json),
            json.dumps(pj, ensure_ascii=False),
            meta.get("dossier"),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    cx.commit()

    if cfg["deplacer"]:
        _retirer(json_path, eml_path)
    return "archive"


def _retirer(*chemins):
    for c in chemins:
        try:
            Path(c).unlink()
        except OSError:
            pass


def ingest(cfg, cx, silencieux=False):
    staging = Path(cfg["dossier_staging"])
    if not staging.exists():
        if not silencieux:
            print(f"[{NOM}] dossier de staging absent : {staging}")
        return {"archive": 0, "doublon": 0, "incomplet": 0, "erreur": 0}
    bilan = {"archive": 0, "doublon": 0, "incomplet": 0, "erreur": 0}
    for json_path in sorted(staging.rglob("*.json")):
        r = ingest_un(cx, cfg, json_path)
        bilan[r] = bilan.get(r, 0) + 1
    if not silencieux:
        print(f"[{NOM}] ingest : {bilan}")
    return bilan


def cmd_ingest(args):
    cfg = charger_config(args.config)
    cx = connexion(cfg["base_sqlite"])
    try:
        ingest(cfg, cx)
    finally:
        cx.close()


def cmd_watch(args):
    cfg = charger_config(args.config)
    cx = connexion(cfg["base_sqlite"])
    intervalle = max(2, args.intervalle)
    print(f"[{NOM}] surveillance de {cfg['dossier_staging']} toutes les {intervalle}s "
          f"(Ctrl+C pour arrêter)")
    try:
        while True:
            b = ingest(cfg, cx, silencieux=True)
            if b["archive"] or b["erreur"]:
                print(f"[{NOM}] {datetime.now().strftime('%H:%M:%S')} {b}")
            time.sleep(intervalle)
    except KeyboardInterrupt:
        print(f"\n[{NOM}] arrêt.")
    finally:
        cx.close()


def cmd_stats(args):
    cfg = charger_config(args.config)
    cx = connexion(cfg["base_sqlite"])
    try:
        total = cx.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        dernier = cx.execute(
            "SELECT date_mail, sujet FROM messages ORDER BY ingere_at DESC LIMIT 1"
        ).fetchone()
        print(f"Messages indexés : {total}")
        print(f"Destination : {cfg['dossier_destination']}")
        if dernier:
            print(f"Dernier ingéré : {dernier[0]} — {(dernier[1] or '')[:60]}")
    finally:
        cx.close()


def cmd_search(args):
    cfg = charger_config(args.config)
    cx = connexion(cfg["base_sqlite"])
    try:
        motif = f"%{args.texte}%"
        rows = cx.execute(
            """SELECT date_mail, de_adresse, sujet, fichier_eml
               FROM messages
               WHERE sujet LIKE ? OR de_adresse LIKE ?
               ORDER BY date_mail DESC LIMIT ?""",
            (motif, motif, args.limite),
        ).fetchall()
        if not rows:
            print("Aucun résultat.")
        for d, de, suj, fic in rows:
            print(f"{(d or '')[:19]}  {(de or '')[:32]:32}  {(suj or '')[:50]}")
            print(f"        {fic}")
    finally:
        cx.close()


# ----------------------------------------------------------------------------
# Lecture directe du stock Thunderbird (mbox) — contourne le bac à sable
# ----------------------------------------------------------------------------
def _dec_entete(valeur):
    """Décode un en-tête MIME (RFC 2047) en texte lisible."""
    if not valeur:
        return ""
    morceaux = []
    for txt, enc in email.header.decode_header(valeur):
        if isinstance(txt, bytes):
            try:
                morceaux.append(txt.decode(enc or "utf-8", "replace"))
            except (LookupError, TypeError):
                morceaux.append(txt.decode("utf-8", "replace"))
        else:
            morceaux.append(txt)
    return "".join(morceaux).replace("\r", " ").replace("\n", " ").strip()


def _adresses(valeur):
    """Liste d'adresses 'Nom <a@b>' d'un en-tête To/Cc."""
    if not valeur:
        return []
    out = []
    for nom, addr in email.utils.getaddresses([_dec_entete(valeur)]):
        out.append((nom + " <" + addr + ">").strip() if nom else addr)
    return [a for a in out if a]


def _date_iso(msg):
    try:
        dt = email.utils.parsedate_to_datetime(msg.get("Date"))
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
    except (TypeError, ValueError, IndexError):
        pass
    return None


def _sans_accents(s):
    return "".join(c for c in unicodedata.normalize("NFD", s or "")
                   if unicodedata.category(c) != "Mn")


def _slug(texte, maxlen=60):
    s = _sans_accents(texte or "").lower()
    s = re.sub(r'[\\/:*?"<>|]+', "-", s)
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"[^a-z0-9.\-]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-.")
    if len(s) > maxlen:
        s = s[:maxlen].rstrip("-")
    return s or "sans-objet"


def _slug_expediteur(de):
    m = re.search(r"<([^>]+)>", de or "")
    addr = (m.group(1) if m else (de or "")).strip()
    return _slug(addr.replace("@", "-at-"), 50) or "expediteur-inconnu"


def _hash6(cle):
    return hashlib.sha1((cle or "").encode("utf-8")).hexdigest()[:6]


def _pieces_jointes(msg):
    pj = []
    for part in msg.walk():
        if part.get_content_disposition() == "attachment":
            try:
                charge = part.get_payload(decode=True) or b""
                taille = len(charge)
            except Exception:
                taille = None
            pj.append({
                "nom": _dec_entete(part.get_filename()) or "(sans nom)",
                "type_mime": part.get_content_type(),
                "taille": taille,
            })
    return pj


def importer_mbox(cfg, cx, chemin_mbox, limite=None, dossier_logique="INBOX"):
    """Parcourt un mbox Thunderbird et archive chaque message. Lecture seule."""
    chemin_mbox = str(chemin_mbox)
    if not Path(chemin_mbox).exists():
        print(f"[{NOM}] mbox introuvable : {chemin_mbox}", file=sys.stderr)
        return {"lus": 0, "archive": 0, "doublon": 0, "erreur": 0}

    mbox = mailbox.mbox(chemin_mbox)  # ouverture read-only (pas de lock)
    bilan = {"lus": 0, "archive": 0, "doublon": 0, "erreur": 0}
    dest_root = Path(cfg["dossier_destination"])
    t0 = time.time()
    try:
        for cle in mbox.iterkeys():
            if limite and bilan["lus"] >= limite:
                break
            bilan["lus"] += 1
            try:
                msg = mbox.get_message(cle)
            except Exception:
                bilan["erreur"] += 1
                continue

            mid = (_dec_entete(msg.get("Message-ID")) or "").strip()
            if not mid:
                brut0 = mbox.get_bytes(cle)
                mid = "sansid:" + hashlib.sha1(brut0[:2000]).hexdigest()[:16]
            if existe(cx, mid):
                bilan["doublon"] += 1
                continue

            dt = _date_iso(msg)
            dt_part = dt or datetime.now(timezone.utc)
            sujet = _dec_entete(msg.get("Subject"))
            de = _dec_entete(msg.get("From"))

            base = (
                f"{dt_part.year:04d}-{dt_part.month:02d}-{dt_part.day:02d}_"
                f"{dt_part.hour:02d}{dt_part.minute:02d}{dt_part.second:02d}"
                f"__{_slug_expediteur(de)}__{_slug(sujet)}__{_hash6(mid)}"
            )
            sous = f"{dt_part.year:04d}/{dt_part.month:02d}"
            dossier_dest = dest_root / sous
            dest_eml = nom_libre(dossier_dest, base, ".eml")
            dest_json = dest_eml.with_suffix(".json")

            meta = {
                "message_id": mid if not mid.startswith("sansid:") else None,
                "de_adresse": de,
                "de_nom": de,
                "destinataires": _adresses(msg.get("To")),
                "copies": _adresses(msg.get("Cc")),
                "date": dt.isoformat() if dt else None,
                "sujet": sujet,
                "dossier": dossier_logique,
                "fichier_eml": str(dest_eml),
                "pieces_jointes": _pieces_jointes(msg),
                "collecte_at": datetime.now(timezone.utc).isoformat(),
                "collecteur": {"nom": NOM, "version": VERSION, "source": "mbox"},
            }

            try:
                ecrire_atomique(mbox.get_bytes(cle), dest_eml)
                ecrire_atomique(
                    json.dumps(meta, indent=2, ensure_ascii=False).encode("utf-8"),
                    dest_json,
                )
            except Exception as e:
                print(f"[{NOM}] échec écriture {base}: {e}", file=sys.stderr)
                bilan["erreur"] += 1
                continue

            cx.execute(
                """INSERT OR IGNORE INTO messages
                   (message_id, de_adresse, destinataires, copies, date_mail, sujet,
                    fichier_eml, fichier_json, pieces_jointes, dossier_src, ingere_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (mid, de,
                 json.dumps(meta["destinataires"], ensure_ascii=False),
                 json.dumps(meta["copies"], ensure_ascii=False),
                 meta["date"], sujet, str(dest_eml), str(dest_json),
                 json.dumps(meta["pieces_jointes"], ensure_ascii=False),
                 dossier_logique, meta["collecte_at"]),
            )
            bilan["archive"] += 1
            if bilan["archive"] % 200 == 0:
                cx.commit()
                print(f"[{NOM}] … {bilan['archive']} archivés "
                      f"({bilan['lus']} lus, {time.time()-t0:.0f}s)")
    finally:
        cx.commit()
        mbox.close()
    print(f"[{NOM}] import mbox : {bilan} en {time.time()-t0:.0f}s")
    return bilan


def cmd_import(args):
    cfg = charger_config(args.config)
    cx = connexion(cfg["base_sqlite"])
    sources = []
    if args.mbox:
        sources = [args.mbox]
    else:
        sources = cfg.get("mbox_sources", [])
    if not sources:
        raise SystemExit(
            "Indiquez un mbox : --mbox <chemin>  ou  'mbox_sources' dans la config."
        )
    try:
        for src in sources:
            importer_mbox(cfg, cx, src, limite=args.limite)
    finally:
        cx.close()


# ----------------------------------------------------------------------------
# Entrée
# ----------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(prog="iamail_compagnon",
                                 description=f"{NOM} v{VERSION}")
    ap.add_argument("--config", default="config.json", help="fichier de configuration")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="crée config + base")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("ingest", help="une passe d'ingestion (dossier relais)")
    s.set_defaults(func=cmd_ingest)

    s = sub.add_parser("import", help="importe un mbox Thunderbird (stock local)")
    s.add_argument("--mbox", help="chemin du fichier mbox (sinon 'mbox_sources' de la config)")
    s.add_argument("--limite", type=int, default=None, help="nb max de messages (test)")
    s.set_defaults(func=cmd_import)

    s = sub.add_parser("watch", help="surveillance continue")
    s.add_argument("--intervalle", type=int, default=10, help="secondes")
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser("stats", help="statistiques")
    s.set_defaults(func=cmd_stats)

    s = sub.add_parser("search", help="recherche dans l'index")
    s.add_argument("texte")
    s.add_argument("--limite", type=int, default=20)
    s.set_defaults(func=cmd_search)

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()

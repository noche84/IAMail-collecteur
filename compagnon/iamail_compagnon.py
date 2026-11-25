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
import html as _html_lib
import json
import mailbox
import os
import re
import shutil
import sqlite3
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

NOM = "iamail-compagnon"
VERSION = "1.24.0"

CONFIG_DEFAUT = {
    "dossier_staging": "",
    "dossier_destination": "",
    "base_sqlite": "iamail_index.sqlite3",
    "deplacer": True,
    "mes_adresses": [],
    "expediteurs_importants": [],
    "moteur_dir": "",
    "ia_jours": 0,
    "ia_extrait_max": 4000,
    # Pour QUI l'IA trie (moteur 2.2) : réglable depuis le cockpit (⚙ Réglages).
    "utilisateur": {"nom": "", "fonction": "", "profil": ""},
    "topo_ia": True,        # le topo est rédigé par l'IA (repli : gabarit local)
    "topo_jours": 0,        # 0 = regles.parametres.topo_fenetre_jours
    "tts": {"moteur": "espeak-ng", "voix": "fr", "vitesse": 150, "modele": ""},
    "ia": {
        "fournisseur": "mock",
        "endpoint": "https://api.mistral.ai/v1/chat/completions",
        "cle": "",
        "modele": "mistral-small-latest"
    },
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
    cfg["_chemin_config"] = str(p)
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
        exemple = json.loads(json.dumps(CONFIG_DEFAUT))
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
def _ouvrir_db(chemin_db):
    """Connexion SQLite partagée par le service, le cockpit et les tâches de fond :
    attente jusqu'à 30 s si un autre écrit (au lieu de « database is locked » après 5 s)
    et journal WAL (lire pendant qu'un autre écrit)."""
    cx = sqlite3.connect(chemin_db, timeout=30)
    try:
        cx.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    return cx


def connexion(chemin_db):
    Path(chemin_db).parent.mkdir(parents=True, exist_ok=True)
    cx = _ouvrir_db(chemin_db)
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
    # Contrat moteur (regles._semantique.message_id) : identifiant SANS chevrons.
    # Les archives faites avant la 1.18 les avaient gardés : on les retire une fois
    # (OR IGNORE : si la forme nue existe déjà, la ligne ancienne est laissée telle quelle).
    cx.execute("UPDATE OR IGNORE messages SET message_id = "
               "TRIM(REPLACE(REPLACE(message_id, '<', ''), '>', '')) "
               "WHERE message_id LIKE '<%' OR message_id LIKE '%>'")
    cx.execute("""
        CREATE TABLE IF NOT EXISTS corrections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT, de_adresse TEXT, domaine TEXT, sujet TEXT, mots TEXT,
            extrait TEXT, categorie TEXT, urgence TEXT, action_attendue INTEGER,
            concerne TEXT, avant TEXT, at TEXT
        )
    """)
    cx.execute("CREATE INDEX IF NOT EXISTS idx_corr_de ON corrections(de_adresse)")
    if "precision" not in {r[1] for r in cx.execute("PRAGMA table_info(corrections)")}:
        cx.execute("ALTER TABLE corrections ADD COLUMN precision TEXT")
    # Messages ENVOYÉS par l'utilisateur (dossier Envoyés) : index seulement, pour savoir
    # s'il a déjà répondu dans un fil et à qui il écrit (moteur 2.4).
    cx.execute("""
        CREATE TABLE IF NOT EXISTS envoyes (
            message_id TEXT PRIMARY KEY, date_envoi TEXT, a TEXT, sujet TEXT,
            cle_fil TEXT, in_reply_to TEXT, refs TEXT, source TEXT
        )
    """)
    cx.execute("CREATE INDEX IF NOT EXISTS idx_env_fil ON envoyes(cle_fil)")
    cx.execute("CREATE INDEX IF NOT EXISTS idx_env_date ON envoyes(date_envoi)")
    cx.execute("CREATE TABLE IF NOT EXISTS topo_cache "
               "(cle TEXT PRIMARY KEY, court TEXT, complet TEXT, at TEXT)")
    cx.commit()
    return cx


def existe(cx, message_id):
    """Vrai si le message est déjà indexé (forme nue, ou ancienne forme entre chevrons)."""
    nu = (message_id or "").strip().strip("<>")
    return cx.execute(
        "SELECT 1 FROM messages WHERE message_id IN (?,?,?)",
        (message_id, nu, "<" + nu + ">")
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
    try:
        with open(dest_eml, "rb") as f:
            msg_obj = email.message_from_binary_file(f)
        extrait = _corps_texte(msg_obj, _extrait_max(cfg))
    except Exception:
        extrait = ""
    cx.execute(
        """INSERT OR IGNORE INTO messages
           (message_id, de_adresse, destinataires, copies, date_mail, sujet,
            fichier_eml, fichier_json, pieces_jointes, dossier_src, ingere_at, extrait)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
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
            extrait,
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


def _decoder_part(part):
    try:
        charge = part.get_payload(decode=True)
        if charge is None:
            return ""
        cs = part.get_content_charset() or "utf-8"
        return charge.decode(cs, errors="replace")
    except Exception:
        return ""


def _html_vers_texte(h):
    h = re.sub(r'(?is)<(script|style).*?</\1>', ' ', h)
    h = re.sub(r'(?i)<br\s*/?>', '\n', h)
    h = re.sub(r'(?i)</(p|div|tr|li|h[1-6])>', '\n', h)
    h = re.sub(r'<[^>]+>', ' ', h)
    try:
        h = _html_lib.unescape(h)
    except Exception:
        pass
    return h


def _corps_texte(msg, maxlen=2000):
    """Corps du mail en texte : préfère text/plain, repli sur text/html nettoyé.
    Tronqué à maxlen (0 = pas de corps ; grand nombre ≈ corps complet)."""
    if not maxlen:
        return ""
    plain, html = [], []
    try:
        for part in msg.walk():
            if part.get_content_disposition() == "attachment":
                continue
            ct = part.get_content_type()
            if ct == "text/plain":
                plain.append(_decoder_part(part))
            elif ct == "text/html":
                html.append(_decoder_part(part))
    except Exception:
        pass
    texte = "\n".join(t for t in plain if t).strip()
    if not texte:
        texte = _html_vers_texte("\n".join(t for t in html if t))
    texte = texte.replace("\r\n", "\n").replace("\r", "\n")
    texte = re.sub(r'[ \t]+', ' ', texte)
    texte = re.sub(r'^ +| +$', '', texte, flags=re.M)   # strip de CHAQUE ligne
    texte = re.sub(r'\n{3,}', '\n\n', texte).strip()
    return texte[:maxlen]


def _extrait_max(cfg):
    """Longueur de l'extrait stocké : la même partout (relève, rattrapage, entrée IA)."""
    return int(cfg.get("ia_extrait_max") or PARAMS.get("ia_extrait_max", 4000))


def _message_id(msg):
    """Message-ID 2.0 : chevrons retirés, casse gardée ; si absent, 'synth-' +
    32 hex de sha256(Date brut trim | adresse From | objet décodé)."""
    brut = (msg.get("Message-ID") or "").replace("<", "").replace(">", "").strip()
    if brut:
        return brut
    date_brut = (msg.get("Date") or "").strip()
    de = (_adresses_spec(_dec_entete(msg.get("From"))) or [""])[0]
    sujet = _dec_entete(msg.get("Subject")) or ""
    empreinte = hashlib.sha256((date_brut + "|" + de + "|" + sujet).encode("utf-8")).hexdigest()
    return "synth-" + empreinte[:32]


def _archiver_message(cfg, cx, msg, brut, bilan, dossier_logique="INBOX"):
    """Archive UN message lu dans un mbox (.eml + .json + index). Lecture seule de la source."""
    # Identifiant du contrat moteur (le même que GesEnt et l'agent Outlook) :
    # chevrons retirés ; 'synth-…' si l'en-tête manque.
    mid = _message_id(msg)
    if existe(cx, mid):
        bilan["doublon"] += 1
        return
    if mid.startswith("synth-"):
        # Archives d'avant la 1.18 : ces messages portaient une clé 'sansid:'.
        if existe(cx, "sansid:" + hashlib.sha1(brut[:2000]).hexdigest()[:16]):
            bilan["doublon"] += 1
            return

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
    dossier_dest = Path(cfg["dossier_destination"]) / sous
    dest_eml = nom_libre(dossier_dest, base, ".eml")
    dest_json = dest_eml.with_suffix(".json")

    meta = {
        "message_id": mid if not mid.startswith("synth-") else None,
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
        ecrire_atomique(brut, dest_eml)
        ecrire_atomique(
            json.dumps(meta, indent=2, ensure_ascii=False).encode("utf-8"),
            dest_json,
        )
    except Exception as e:
        print(f"[{NOM}] échec écriture {base}: {e}", file=sys.stderr)
        bilan["erreur"] += 1
        return

    extrait = _corps_texte(msg, _extrait_max(cfg))
    st = msg.get("X-Mozilla-Status")
    try:
        non_lu = (0 if (int(st, 16) & 1) else 1) if st else None
    except ValueError:
        non_lu = None
    cx.execute(
        """INSERT OR IGNORE INTO messages
           (message_id, de_adresse, destinataires, copies, date_mail, sujet,
            fichier_eml, fichier_json, pieces_jointes, dossier_src, ingere_at,
            extrait)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (mid, de,
         json.dumps(meta["destinataires"], ensure_ascii=False),
         json.dumps(meta["copies"], ensure_ascii=False),
         meta["date"], sujet, str(dest_eml), str(dest_json),
         json.dumps(meta["pieces_jointes"], ensure_ascii=False),
         dossier_logique, meta["collecte_at"], extrait),
    )
    if non_lu is not None:
        cx.execute("UPDATE messages SET non_lu=? WHERE message_id=? AND non_lu IS NULL", (non_lu, mid))
    bilan["archive"] += 1


# État des fichiers mbox déjà lus (taille, date de modification) : un fichier qui n'a pas
# bougé n'est pas relu ; un fichier qui a seulement grandi (Thunderbird ajoute à la fin)
# n'est lu qu'à partir de l'ancienne fin. Un fichier réécrit (compactage) est relu en entier.
def _etat_source(cx, chemin):
    cx.execute("CREATE TABLE IF NOT EXISTS sources_etat (chemin TEXT PRIMARY KEY, "
               "taille INTEGER, mtime REAL, debut TEXT, at TEXT)")
    r = cx.execute("SELECT taille, mtime, debut FROM sources_etat WHERE chemin=?",
                   (str(chemin),)).fetchone()
    return {"taille": r[0], "mtime": r[1], "debut": r[2]} if r else None


def _debut_fichier(chemin, n=4096):
    with open(chemin, "rb") as f:
        return hashlib.sha1(f.read(n)).hexdigest()


def _noter_source(cx, chemin):
    st = Path(chemin).stat()
    cx.execute("CREATE TABLE IF NOT EXISTS sources_etat (chemin TEXT PRIMARY KEY, "
               "taille INTEGER, mtime REAL, debut TEXT, at TEXT)")
    cx.execute("INSERT OR REPLACE INTO sources_etat (chemin, taille, mtime, debut, at) "
               "VALUES (?,?,?,?,?)", (str(chemin), st.st_size, st.st_mtime,
                                      _debut_fichier(chemin, min(4096, st.st_size)),
                                      datetime.now(timezone.utc).isoformat()))
    cx.commit()


def _mode_lecture(cx, chemin):
    """'inchange', ('suite', octet de départ) ou 'complet'."""
    avant = _etat_source(cx, chemin)
    if not avant:
        return "complet"
    st = Path(chemin).stat()
    if st.st_size == avant["taille"] and abs(st.st_mtime - (avant["mtime"] or 0)) < 1e-6:
        return "inchange"
    if st.st_size > avant["taille"] and avant["debut"] == _debut_fichier(chemin, min(4096, avant["taille"])):
        with open(chemin, "rb") as f:
            f.seek(avant["taille"])
            bout = f.read(64).lstrip(b"\r\n")
        if bout.startswith(b"From "):
            return ("suite", avant["taille"])
    if st.st_size == avant["taille"]:
        return "inchange"       # même taille : statuts lus/non lus réécrits en place, rien de nouveau
    return "complet"


def _messages_depuis(chemin, depart):
    """Messages d'un mbox à partir de l'octet 'depart' (début d'un message) : (msg, brut)."""
    with open(chemin, "rb") as f:
        f.seek(depart)
        donnees = f.read()
    morceaux = re.split(rb"(?:^|\n)(?=From )", donnees)
    for m in morceaux:
        if not m.startswith(b"From "):
            continue
        nl = m.find(b"\n")
        brut = m[nl + 1:] if nl >= 0 else b""
        if not brut.strip():
            continue
        yield email.message_from_bytes(brut), brut


def importer_mbox(cfg, cx, chemin_mbox, limite=None, dossier_logique="INBOX", incremental=False):
    """Parcourt un mbox Thunderbird et archive chaque message. Lecture seule.
    incremental=True : saute un fichier inchangé, ne lit que la fin d'un fichier qui a grandi."""
    chemin_mbox = str(chemin_mbox)
    if not Path(chemin_mbox).exists():
        print(f"[{NOM}] mbox introuvable : {chemin_mbox}", file=sys.stderr)
        return {"lus": 0, "archive": 0, "doublon": 0, "erreur": 0, "introuvable": True}
    bilan = {"lus": 0, "archive": 0, "doublon": 0, "erreur": 0}
    t0 = time.time()
    mode = _mode_lecture(cx, chemin_mbox) if (incremental and not limite) else "complet"
    if mode == "inchange":
        return dict(bilan, inchange=True)
    if isinstance(mode, tuple):
        try:
            for msg, brut in _messages_depuis(chemin_mbox, mode[1]):
                bilan["lus"] += 1
                _archiver_message(cfg, cx, msg, brut, bilan, dossier_logique)
        finally:
            cx.commit()
        _noter_source(cx, chemin_mbox)
        print(f"[{NOM}] import mbox (suite) : {bilan} en {time.time()-t0:.0f}s")
        return bilan

    mbox = mailbox.mbox(chemin_mbox)  # ouverture read-only (pas de lock)
    try:
        for cle in mbox.iterkeys():
            if limite and bilan["lus"] >= limite:
                break
            bilan["lus"] += 1
            try:
                msg = mbox.get_message(cle)
                brut = mbox.get_bytes(cle)
            except Exception:
                bilan["erreur"] += 1
                continue
            _archiver_message(cfg, cx, msg, brut, bilan, dossier_logique)
            if bilan["archive"] and bilan["archive"] % 200 == 0:
                cx.commit()
                print(f"[{NOM}] … {bilan['archive']} archivés "
                      f"({bilan['lus']} lus, {time.time()-t0:.0f}s)")
    finally:
        cx.commit()
        mbox.close()
    if not limite:
        _noter_source(cx, chemin_mbox)
    print(f"[{NOM}] import mbox : {bilan} en {time.time()-t0:.0f}s")
    return bilan


# ── Dossier Envoyés (moteur 2.4) ────────────────────────────────────────────
# Noms usuels du dossier Envoyés (README du moteur, « Ce que le destinataire a envoyé »).
NOMS_ENVOYES = ("Sent", "Sent Items", "Sent Messages", "Sent Mail", "Éléments envoyés",
                "Envoyés", "Messages envoyés")


def _ids_entete(valeur):
    """Message-ID listés dans In-Reply-To / References, sans chevrons."""
    return [x.strip() for x in re.findall(r"<([^<>\s]+)>", str(valeur or ""))]


def envois_depuis_mbox(cfg):
    """Fichiers mbox du dossier Envoyés : 'envoyes_sources' de la config, sinon devinés
    à côté de chaque 'mbox_sources' (même dossier, ou un sous-dossier *.sbd, ex.
    [Gmail].sbd/Sent Mail)."""
    if cfg.get("envoyes_sources"):
        return [Path(x) for x in cfg["envoyes_sources"]]
    vus, out = set(), []
    for src in cfg.get("mbox_sources") or []:
        dossier = Path(src).parent
        candidats = [dossier] + [d for d in dossier.glob("*.sbd") if d.is_dir()]
        for d in candidats:
            for nom in NOMS_ENVOYES:
                f = d / nom
                if f.is_file() and f not in vus:
                    vus.add(f)
                    out.append(f)
    return out


def enregistrer_envoi(cx, message_id, date_iso, adresses, sujet, in_reply_to="",
                      refs=None, source="mbox"):
    """Indexe un message envoyé (dédoublonnage par Message-ID). Renvoie True si nouveau."""
    mid = (message_id or "").strip().strip("<>")
    if not mid:
        return False
    a = sorted({x.lower() for x in adresses if x and "@" in x})
    cur = cx.execute(
        "INSERT OR IGNORE INTO envoyes (message_id, date_envoi, a, sujet, cle_fil, "
        "in_reply_to, refs, source) VALUES (?,?,?,?,?,?,?,?)",
        (mid, date_iso, json.dumps(a), sujet or "", _sujet_fil(sujet),
         (in_reply_to or "").strip().strip("<>"), json.dumps(refs or []), source))
    return cur.rowcount == 1


def _indexer_envoi_mbox(cx, m):
    d = _dt_message(_dec_entete(m.get("Date")) or "")
    if d is not None and d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    adresses = []
    for h in ("To", "Cc"):
        for _, adr in email.utils.getaddresses([_dec_entete(m.get(h)) or ""]):
            adresses.append(adr)
    irt = _ids_entete(m.get("In-Reply-To"))
    return enregistrer_envoi(cx, _message_id(m), d.isoformat() if d else "", adresses,
                             _dec_entete(m.get("Subject")), irt[0] if irt else "",
                             _ids_entete(m.get("References")), "mbox")


def importer_envoyes_mbox(cx, chemin, incremental=False):
    """Indexe le dossier Envoyés d'un profil Thunderbird. Lecture seule ; aucun archivage
    (le coffre-fort concerne le courrier reçu). incremental : comme importer_mbox."""
    mode = _mode_lecture(cx, chemin) if incremental else "complet"
    if mode == "inchange":
        return 0
    n = 0
    if isinstance(mode, tuple):
        for m, _ in _messages_depuis(chemin, mode[1]):
            if _indexer_envoi_mbox(cx, m):
                n += 1
        cx.commit()
        _noter_source(cx, chemin)
        return n
    try:
        boite = mailbox.mbox(str(chemin))
    except Exception as e:
        print(f"[{NOM}] Envoyés illisibles ({chemin}) : {e}", file=sys.stderr)
        return 0
    try:
        for cle in boite.iterkeys():
            try:
                m = boite.get_message(cle)
            except Exception:
                continue
            if _indexer_envoi_mbox(cx, m):
                n += 1
        cx.commit()
    finally:
        boite.close()
    _noter_source(cx, chemin)
    return n


def index_envoyes(cx, maintenant=None):
    """Ce que l'utilisateur a envoyé, prêt à interroger (règle du moteur 2.4) :
    - fil_exact[cle_fil] = dates des envois rattachés par In-Reply-To / References à un
      message reçu de ce fil ;
    - fil_sujet[cle_fil] = [(date, {destinataires})] des autres envois (rattachés par la
      clé de fil, à confirmer par l'expéditeur parmi les destinataires) ;
    - ecrits[adresse] = envois à cette adresse depuis profil_ecrits_fenetre_jours."""
    now = maintenant or datetime.now(timezone.utc)
    f_ecrits = PARAMS.get("profil_ecrits_fenetre_jours", 90)
    try:
        lignes = cx.execute("SELECT date_envoi, a, cle_fil, in_reply_to, refs FROM envoyes").fetchall()
    except sqlite3.Error:
        lignes = []
    if not lignes:
        return {"fil_exact": {}, "fil_sujet": {}, "ecrits": {}}
    ids = set()
    for _, _, _, irt, refs in lignes:
        if irt:
            ids.add(irt)
        ids.update(json.loads(refs or "[]"))
    fil_de = {}
    ids = list(ids)
    for i in range(0, len(ids), 500):
        lot = ids[i:i + 500]
        for mid, sujet in cx.execute(
                f"SELECT message_id, sujet FROM messages WHERE message_id IN ({','.join('?' * len(lot))})", lot):
            fil_de[mid] = _sujet_fil(sujet)
    fil_exact, fil_sujet, ecrits = {}, {}, {}
    for date_envoi, a, cle_fil, irt, refs in lignes:
        d = _dt_message(date_envoi)
        if d is None:
            continue
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        dest = set(json.loads(a or "[]"))
        fils = {fil_de[x] for x in ([irt] if irt else []) + json.loads(refs or "[]") if x in fil_de}
        if fils:
            for k in fils:
                if k:
                    fil_exact.setdefault(k, []).append(d)
        elif cle_fil:
            fil_sujet.setdefault(cle_fil, []).append((d, dest))
        if (now - d).days <= f_ecrits:
            for adr in dest:
                ecrits[adr] = ecrits.get(adr, 0) + 1
    return {"fil_exact": fil_exact, "fil_sujet": fil_sujet, "ecrits": ecrits}


def _envois_du_fil(row, idx):
    """Dates des envois de l'utilisateur rattachés au fil de ce message (règle 2.4)."""
    fkey = _sujet_fil(row.get("sujet"))
    if not fkey or not idx:
        return []
    spec = (_adresses_spec(row.get("de_adresse") or row.get("de") or "") or [""])[0]
    out = list(idx["fil_exact"].get(fkey, []))
    out += [d for d, dest in idx["fil_sujet"].get(fkey, []) if spec and spec in dest]
    return out


def dernier_envoi(row, idx, maintenant=None):
    """Dernière réponse de l'utilisateur dans le fil, ANTÉRIEURE au message, dans
    fil_fenetre_jours ; ISO ou None."""
    d0 = _dt_message(row.get("date_mail") or row.get("date"))
    if d0 is None:
        return None
    if d0.tzinfo is None:
        d0 = d0.replace(tzinfo=timezone.utc)
    now = maintenant or datetime.now(timezone.utc)
    f = PARAMS.get("fil_fenetre_jours", 90)
    c = [d for d in _envois_du_fil(row, idx) if d < d0 and (now - d).days <= f]
    return max(c).isoformat() if c else None


def repondu_apres(row, idx):
    """Réponse de l'utilisateur POSTÉRIEURE à ce message (badge « Tu as répondu ») ; ISO ou None."""
    d0 = _dt_message(row.get("date_mail") or row.get("date"))
    if d0 is None:
        return None
    if d0.tzinfo is None:
        d0 = d0.replace(tzinfo=timezone.utc)
    c = [d for d in _envois_du_fil(row, idx) if d > d0]
    return max(c).isoformat() if c else None


def _collecteur(cfg):
    """Collecteur configuré : 'thunderbird' (défaut, lit le mbox), 'outlook' (COM,
    Windows) ou 'applemail' (Apple Events, macOS)."""
    c = (cfg.get("collecteur") or "thunderbird").lower().replace(" ", "").replace("-", "")
    return {"mail": "applemail", "applemail": "applemail", "mailmac": "applemail"}.get(c, c)


def cmd_import(args):
    cfg = charger_config(args.config)
    cx = connexion(cfg["base_sqlite"])
    _assurer_colonnes(cx)
    try:
        if _collecteur(cfg) == "outlook":
            import collecteur_outlook as outlook
            if not outlook.disponible():
                raise SystemExit("Collecteur Outlook : pywin32 introuvable "
                                 "(installe-le, ou lance sur un poste Windows avec Outlook).")
            faits, doublons = outlook.relever(cfg, cx, sys.modules[__name__],
                                              limite=args.limite)
            print(f"[{NOM}] Outlook : {faits} nouveau(x), {doublons} déjà connu(s).")
            try:
                n = outlook.relever_envoyes(cfg, cx, sys.modules[__name__])
                if n:
                    print(f"[{NOM}] Envoyés : {n} nouveau(x) message(s) indexé(s).")
            except Exception as e:
                print(f"[{NOM}] Envoyés Outlook illisibles : {e}", file=sys.stderr)
            return
        if _collecteur(cfg) == "applemail":
            import collecteur_applemail as applemail
            if not applemail.disponible():
                raise SystemExit("Collecteur Apple Mail : réservé à macOS (osascript introuvable).")
            try:
                faits, doublons = applemail.relever(cfg, cx, sys.modules[__name__],
                                                    limite=args.limite)
            except applemail.MailErreur as e:
                raise SystemExit(f"Apple Mail : {e}")
            print(f"[{NOM}] Apple Mail : {faits} nouveau(x), {doublons} déjà connu(s).")
            try:
                n = applemail.relever_envoyes(cfg, cx, sys.modules[__name__])
                if n:
                    print(f"[{NOM}] Envoyés : {n} nouveau(x) message(s) indexé(s).")
            except Exception as e:
                print(f"[{NOM}] Envoyés Apple Mail illisibles : {e}", file=sys.stderr)
            return
        sources = [args.mbox] if args.mbox else cfg.get("mbox_sources", [])
        if not sources:
            raise SystemExit(
                "Indiquez un mbox : --mbox <chemin>  ou  'mbox_sources' dans la config."
            )
        for src in sources:
            importer_mbox(cfg, cx, src, limite=args.limite, incremental=not args.mbox)
        if not args.mbox:
            n = sum(importer_envoyes_mbox(cx, f, incremental=True) for f in envois_depuis_mbox(cfg))
            if n:
                print(f"[{NOM}] Envoyés : {n} nouveau(x) message(s) indexé(s).")
    finally:
        cx.close()


# ----------------------------------------------------------------------------
# Le cerveau — couche déterministe (règles avant IA)
# ----------------------------------------------------------------------------
# Données éditables : c'est volontairement de la donnée, pas du code, pour
# pouvoir l'ajuster (et, demain, la partager entre le compagnon et GesEnt).

EXPEDITEURS_AUTO = {
    "no-reply", "noreply", "donotreply", "do-not-reply", "ne-pas-repondre",
    "nepasrepondre", "mailer-daemon", "postmaster", "bounce", "bounces",
    "notifications", "notification", "newsletter", "news", "mailing",
}

MOTS_URGENTS = [
    "urgent", "immediat", "asap", "au plus vite", "rappel", "relance",
    "derniere relance", "echeance", "expire", "aujourd hui", "avant demain",
    "delai", "impaye", "mise en demeure", "action requise", "a traiter",
]

# Domaines de messagerie « grand public » : un envoi depuis ces domaines est
# probablement une personne (et non une marque qui diffuse en masse).
FREEMAIL = {
    "gmail.com", "googlemail.com", "outlook.com", "outlook.be", "hotmail.com",
    "hotmail.be", "hotmail.fr", "live.com", "live.be", "live.fr", "msn.com",
    "yahoo.com", "yahoo.fr", "yahoo.be", "icloud.com", "me.com", "mac.com",
    "proton.me", "protonmail.com", "gmx.com", "gmx.net", "gmx.be",
    "skynet.be", "telenet.be", "proximus.be", "voo.be", "scarlet.be",
    "belgacom.net", "base.be", "orange.fr", "free.fr", "laposte.net", "wanadoo.fr",
}

# Étiquettes de domaine typiques des plateformes d'emailing de masse.
MARKETING_LABELS = {
    "mail", "email", "emails", "e", "m", "mg", "news", "newsletter", "mailing",
    "sendgrid", "mailchimp", "mailgun", "sparkpost", "amazonses", "mailjet",
    "sendinblue", "klaviyo", "s", "info", "promo", "promos",
}

# Mots dans la partie locale qui trahissent un envoi automatique / générique.
LOCAL_AUTO_INDICES = (
    "noreply", "no-reply", "donotreply", "do_not_reply", "do-not-reply",
    "nepasrepondre", "ne-pas-repondre", "mailer", "bounce", "notification",
    "newsletter", "mailing", "automated", "auto-", "-auto", "reply-",
)
# Parties locales génériques (non personnelles) même sur un domaine propre.
LOCAL_GENERIQUE = {
    "info", "contact", "team", "hello", "bonjour", "support", "service",
    "services", "shop", "boutique", "news", "alerts", "alert", "admin",
    "noreply", "no-reply", "sav", "client", "clients", "rewardclub", "club",
}


def _expediteur_humain(spec, local):
    """Heuristique : l'expéditeur ressemble-t-il à une personne ?"""
    if not spec:
        return False
    dom = _domaine(spec)
    if local in EXPEDITEURS_AUTO or local in LOCAL_GENERIQUE:
        return False
    if any(t in local for t in LOCAL_AUTO_INDICES):
        return False
    if dom in FREEMAIL:
        return True                      # personne sur une messagerie grand public
    # Domaine propre : humain seulement si la partie locale ressemble à prénom.nom
    return bool(re.match(REGEX_LOCAL_HUMAIN, local))


def _domaine_marketing(dom):
    if not dom or dom in FREEMAIL:
        return False
    labels = dom.split(".")
    if set(labels) & MARKETING_LABELS:
        return True
    return any(any(ind in l for ind in MARKETING_INDICES_LABEL) for l in labels)

# Sujets « à lire absolument », même venant d'un robot : argent, sécurité,
# transactions, contrats. Le déterministe ne doit JAMAIS les enterrer.
SUJETS_IMPORTANTS = [
    "compte", "virement", "transfert", "transfer", "iban", "retrait",
    "depot", "taux", "interet", "assurance", "resiliation", "resilier", "prime",
    "prelevement", "remboursement", "facture", "paiement", "impaye", "echeance",
    "crypto", "bitcoin", "wallet", "portefeuille", "securite", "mot de passe",
    "connexion", "verification", "authentification", "suspendu", "bloque",
    "fraude", "piratage", "commande", "livraison", "colis", "retour",
    "reservation", "confirmation", "contrat", "convocation", "rendez-vous",
]
# Fragments de domaines d'expéditeurs intrinsèquement importants.
DOMAINES_IMPORTANTS = (
    "binance", "coinbase", "kraken", "revolut", "paypal", "stripe", "wise",
    "kbc", "belfius", "ing", "bnpparibas", "bnpparibasfortis", "fortis",
    "hellobank", "santander", "argenta", "keytrade", "boursorama",
    "deutsche-bank", "deutschebank", "crelan", "beobank",
    "touring", "axa", "ethias", "baloise", "aginsurance", "p-v.be",
    "bpost", "dpd", "gls", "gls-group", "ups.com", "dhl", "fedex",
    "myminfin", "minfin",
)

# (catégorie, mots-clés cherchés dans sujet + expéditeur, dé-accentués)
REGLES_CATEGORIE = [
    ("facture",       ["facture", "invoice", "rappel de paiement", "note de credit", "impaye"]),
    ("devis",         ["devis", "offre de prix", "quote", "estimation", "chiffrage"]),
    ("commande",      ["bon de commande", "commande", "order", "confirmation de commande"]),
    ("livraison",     ["livraison", "expedition", "colis", "tracking", "suivi de commande",
                       "bpost", "dpd", "gls", "ups"]),
    ("rendez_vous",   ["rendez-vous", "reunion", "meeting", "convocation", "agenda",
                       "invitation", "rdv", "webinar", "webinaire"]),
    ("reclamation",   ["reclamation", "plainte", "remboursement", "insatisfait", "litige"]),
    ("reseau_social", ["facebook", "linkedin", "instagram", "twitter", "tiktok", "viadeo"]),
    ("chantier",      ["chantier", "travaux", "intervention", "depannage"]),
]

# --- Clés introduites par le moteur 1.1.0 (surchargées par regles.json) -------
# Ces valeurs par défaut doublent regles.json : si le fichier est absent, le
# compagnon reste fonctionnel. charger_regles() les remplace par la donnée.
REGEX_LOCAL_HUMAIN = r"^[a-z]+([._-][a-z]+)+$"
MARKETING_INDICES_LABEL = ("mail", "news", "mailing")
PRECEDENCE_BULK = ("bulk", "list", "junk")
CATEGORIES_IMPORTANTES = ("facture", "devis", "commande", "livraison",
                          "rendez_vous", "reclamation")
MOTS_SUSPECTS = ("paiement", "facture", "urgent", "compte", "mot de passe",
                 "iban", "virement", "verification")
DOMAINES_RESEAU_SOCIAL = ("facebook", "linkedin", "instagram", "twitter", "tiktok")
STRATEGIE_CATEGORIE = "mot_le_plus_long"
FIL_PREFIXES = ("re", "ref", "rep", "fwd", "fw", "tr", "aw", "wg")
FIL_MOTS_VIDES = {
    "pour", "avec", "votre", "notre", "dans", "vous", "nous", "email",
    "mail", "objet", "concernant", "suite", "projet", "demande", "reponse",
    "question", "info", "fwd", "les", "des", "une", "un", "la", "le", "du",
    "de", "et", "re", "tr",
}
PARAMS = {
    "fenetre_recent_jours": 30,
    "profil_actif_n_min": 5,
    "profil_actif_recent_min": 3,
    "regroupement_mot_longueur_min": 4,
    "regroupement_mots_partages_min": 2,
    "regroupement_jaccard_min": 0.34,
    "ia_extrait_max": 4000,
    "topo_intervenants_max": 5,
    "relance_fenetre_jours": 14,
    "fil_fenetre_jours": 90,
    "exemples_max": 3,
    "topo_fenetre_jours": 14,
    "topo_elements_max": 40,
    "ia_destinataires_max": 10,
    "profil_ecrits_fenetre_jours": 90,
    "precision_max": 500,
}


def _adresses_spec(valeur):
    """Liste d'adresses nues (a@b), minuscules, d'un en-tête To/Cc."""
    out = []
    for _, addr in email.utils.getaddresses([valeur or ""]):
        if addr and "@" in addr:
            out.append(addr.strip().lower())
    return out


def _domaine(adresse):
    return adresse.split("@", 1)[1].lower() if adresse and "@" in adresse else ""


def _norm_texte(s):
    """Normalisation _semantique (moteur 2.0) : minuscule, NFD sans marques
    combinantes, apostrophes ' ’ ‘ ` ´ → espace, suites d'espaces (sauts de ligne
    compris) réduites à une espace. Aucune translittération."""
    s = _sans_accents((s or "").lower())
    for ap in ("'", "’", "‘", "`", "´"):
        s = s.replace(ap, " ")
    return re.sub(r"\s+", " ", s)


def _contient_mot(texte, mots):
    """Vrai si l'un des mots/expressions apparaît comme MOT ENTIER dans texte
    (texte déjà normalisé par _norm_texte). Évite « ups » dans « setups »."""
    for m in mots:
        mm = _norm_texte(m).strip()
        if mm and re.search(r'(?<!\w)' + re.escape(mm) + r'(?!\w)', texte):
            return True
    return False


def _mot_matche(texte, mots):
    """Renvoie le mot-clé normalisé LE PLUS LONG qui matche en mot entier, ou None.
    Sert à la stratégie 'mot_le_plus_long' (« suivi de commande » > « commande »)."""
    meilleur = None
    for m in mots:
        mm = _norm_texte(m).strip()
        if mm and re.search(r'(?<!\w)' + re.escape(mm) + r'(?!\w)', texte):
            if meilleur is None or len(mm) > len(meilleur):
                meilleur = mm
    return meilleur


def _domaine_contient(dom, tokens):
    """Vrai si le domaine correspond à l'un des tokens sur une FRONTIÈRE DE LABEL
    (label complet, ou suffixe de domaine pour un token avec point).
    Évite « ing » dans « booking.com » (labels = [booking, com])."""
    if not dom:
        return False
    labels = dom.split(".")
    for t in tokens:
        t = _sans_accents(t.lower()).strip(". ")
        if not t:
            continue
        if "." in t:
            if dom == t or dom.endswith("." + t):
                return True
        elif t in labels:
            return True
    return False


# --- Moteur 2.0 : taxonomie chargée en global (niveaux, catégories, signaux) ---
NIVEAU_RANG = {"critique": 0, "haute": 1, "normale": 2, "basse": 3}
NIVEAUX = []                 # [{code,libelle,definition,delai_heures}, …]
CATEGORIES = []              # [{code,libelle,description,urgence_defaut}, …] (actives)
CAT_PAR_CODE = {}            # code -> dict catégorie
SIGNAUX = {"critique": [], "haute": [], "basse": []}
DOMAINES_CATEGORIE = []      # [[code, [tokens]], …]
SELECTION_IA = {"ignorer_masse_non_importante": True}
TOPO = {"jours_semaine": 7, "jours_mois": 31,
        "ordre": ["maintenant", "cette_semaine", "ce_mois", "aucune"]}


def _rang_vers_code(r):
    for code, rang in NIVEAU_RANG.items():
        if rang == r:
            return code
    return "normale"


def _humain(de_spec, local, dom):
    """_algorithme étape 1."""
    if not de_spec:
        return False
    if local in EXPEDITEURS_AUTO or local in LOCAL_GENERIQUE:
        return False
    if any(t in local for t in LOCAL_AUTO_INDICES):
        return False
    if dom in FREEMAIL:
        return True
    return re.search(REGEX_LOCAL_HUMAIN, local) is not None


def _marketing(dom, labels):
    if not dom or dom in FREEMAIL:
        return False
    if set(labels) & set(MARKETING_LABELS):
        return True
    return any(any(ind in l for ind in MARKETING_INDICES_LABEL) for l in labels)


def _est_important(texte_entete, categorie, de_spec, dom, importants):
    """_algorithme étape 7."""
    if categorie in CATEGORIES_IMPORTANTES:
        return True
    if de_spec and (de_spec in importants or dom in importants):
        return True
    if _domaine_contient(dom, DOMAINES_IMPORTANTS):
        return True
    return _mot_matche(texte_entete, SUJETS_IMPORTANTS) is not None


def classer(h, extrait="", mes_adresses=(), importants=()):
    """Couche déterministe 2.0 — applique regles._algorithme (étapes 1 à 10).
    Reçoit l'extrait du corps (les signaux d'urgence s'y cherchent aussi)."""
    de = h.get("from") or ""
    de_spec = (_adresses_spec(de) or [""])[0]
    if de_spec:
        local, _, dom = de_spec.partition("@")
    else:
        local, dom = "", ""
    dom = dom.lower()
    labels = dom.split(".") if dom else []
    texte_entete = _norm_texte((h.get("subject") or "") + " " + de)
    texte_complet = _norm_texte((h.get("subject") or "") + " " + (extrait or ""))

    # 1. humain
    humain = _humain(de_spec, local, dom)
    # ROBOT (2.1) : pas d'adresse, OU partie locale auto, OU indice auto en sous-chaîne.
    # Plus large que « non humain » : info@ / jean@entreprise.be ne sont pas des robots.
    robot = ((not de_spec) or local in EXPEDITEURS_AUTO
             or any(t in local for t in LOCAL_AUTO_INDICES))
    # 2-3. masse / automatique
    marketing = _marketing(dom, labels)
    bulk = bool((h.get("list_unsubscribe") or "").strip() or (h.get("list_id") or "").strip()
                or (h.get("precedence") or "").strip().lower() in PRECEDENCE_BULK
                or local in EXPEDITEURS_AUTO or marketing)
    automatique = (not humain) or bulk
    # 4. pour moi
    mes = {m.lower() for m in (mes_adresses or [])}
    to = set(_adresses_spec(h.get("to")))
    cc = set(_adresses_spec(h.get("cc")))
    pour_moi = 1 if (mes & to) else (0 if (mes & cc) else None)
    # 6. soupçon
    reply_to = (_adresses_spec(h.get("reply_to")) or [""])[0]
    dom_reply = _domaine(reply_to)
    suspect = bool(dom_reply and de_spec and dom_reply != dom
                   and _mot_matche(texte_entete, MOTS_SUSPECTS) is not None)
    # 5. catégorie
    categorie, confiance, meilleur = None, "moyenne", -1
    for code, mots in REGLES_CATEGORIE:
        if code not in CAT_PAR_CODE:
            continue
        for m in mots:
            if _contient_mot(texte_entete, [m]):
                long = len(_norm_texte(m).strip())
                if STRATEGIE_CATEGORIE != "mot_le_plus_long":
                    if categorie is None:
                        categorie = code
                elif long > meilleur:
                    categorie, meilleur = code, long
    if categorie is None:
        for code, tokens in DOMAINES_CATEGORIE:
            if code in CAT_PAR_CODE and _domaine_contient(dom, tokens):
                categorie = code
                break
    if (categorie is None and bulk and "newsletter" in CAT_PAR_CODE
            and not _est_important(texte_entete, "autre", de_spec, dom, importants)):
        categorie = "newsletter"
    if categorie is None:
        categorie, confiance = "autre", "faible"
    # 7. important
    important = _est_important(texte_entete, categorie, de_spec, dom, importants)
    # 8. signaux (objet + corps)
    signaux = []
    for niveau in ("critique", "haute", "basse"):
        for expr in SIGNAUX.get(niveau, []):
            if _contient_mot(texte_complet, [expr]):
                effet = "baisser" if niveau == "basse" else "monter"
                # 2.1 : un signal qui monte compte dès que l'expéditeur n'est pas un
                # ROBOT et que le courrier n'est pas de masse (avant : « humain »).
                compte = (effet == "baisser") or (not robot and not bulk)
                signaux.append({"expression": expr, "niveau": niveau,
                                "effet": effet, "compte": compte})
    # 9. urgence (4 niveaux)
    urgence = (CAT_PAR_CODE.get(categorie) or {}).get("urgence_defaut", "normale")
    montent = [s for s in signaux if s["effet"] == "monter" and s["compte"]]
    baissent = [s for s in signaux if s["effet"] == "baisser"]
    if montent:
        fort = min(NIVEAU_RANG[s["niveau"]] for s in montent)
        urgence = _rang_vers_code(min(NIVEAU_RANG[urgence], fort))
    elif baissent:
        faible = max(NIVEAU_RANG[s["niveau"]] for s in baissent)
        urgence = _rang_vers_code(max(NIVEAU_RANG[urgence], faible))
    if pour_moi != 1 and NIVEAU_RANG[urgence] < NIVEAU_RANG["normale"]:
        urgence = "normale"
    if bulk and not important:
        urgence = "basse"
    if important and urgence == "basse":
        urgence = "normale"

    expressions = sorted(s["expression"] for s in signaux)
    return {
        "categorie": categorie, "confiance": confiance, "pour_moi": pour_moi,
        "expediteur_humain": int(humain), "automatique": int(automatique),
        "bulk": int(bulk), "important": int(important), "suspect": int(suspect),
        "urgence": urgence, "signaux": expressions, "signaux_detail": signaux,
        "signal_urgent": int(bool(montent)),
        "raison": _raison_lisible(categorie, pour_moi, humain, important, bulk,
                                  suspect, urgence),
    }


def _raison_lisible(categorie, pour_moi, humain, important, bulk, suspect, urgence):
    bouts = ["adressé à toi" if pour_moi == 1 else
             ("en copie" if pour_moi == 0 else "diffusion")]
    bouts.append("humain" if humain else "automatique")
    if important:
        bouts.append("important")
    if bulk:
        bouts.append("masse")
    if urgence in ("critique", "haute"):
        bouts.append("urgence " + urgence)
    if suspect:
        bouts.append("⚠ Reply-To différent")
    return ", ".join(bouts)


COLONNES_ANALYSE = {
    "categorie": "TEXT", "urgence": "TEXT", "pour_moi": "INTEGER",
    "bulk": "INTEGER", "automatique": "INTEGER", "expediteur_humain": "INTEGER",
    "important": "INTEGER", "suspect": "INTEGER", "priorite": "TEXT",
    "raison": "TEXT", "confiance": "TEXT", "analyse_source": "TEXT", "analyse_at": "TEXT",
    "signaux": "TEXT", "signal_urgent": "INTEGER",   # moteur 2.0
    "ia_categorie": "TEXT", "ia_action": "INTEGER", "ia_tache": "TEXT",
    "ia_echeance": "TEXT", "ia_pression": "TEXT", "ia_priorite": "TEXT",
    "ia_urgence": "TEXT", "ia_justification": "TEXT", "case_topo": "TEXT",  # moteur 2.0
    "ia_resume": "TEXT", "ia_question": "TEXT", "ia_confiance": "TEXT",
    "ia_extensions": "TEXT",   # champs des extensions actives (JSON), moteur 2.0
    "ia_source": "TEXT", "ia_at": "TEXT",
    "non_lu": "INTEGER", "etat": "TEXT",
    "tb_sync": "TEXT",   # état déjà appliqué dans le client (retour-mail bidirectionnel)
    "extrait": "TEXT",   # corps du mail en texte, pour l'analyse IA
    "ia_moteur_version": "TEXT",   # version du moteur qui a produit le jugement
    "source_ref": "TEXT",   # réf. côté client (EntryID Outlook, id Mail) pour le retour-mail
    "source_type": "TEXT",  # collecteur d'origine : thunderbird | outlook | applemail
    "corrige_at": "TEXT",   # jugement corrigé à la main dans le cockpit : jamais rejugé d'office
    "precision": "TEXT",    # réponse de l'utilisateur à la question de l'IA (moteur 2.4)
    "precision_at": "TEXT",
}


def _assurer_colonnes(cx):
    cols = {r[1] for r in cx.execute("PRAGMA table_info(messages)")}
    for nom, typ in COLONNES_ANALYSE.items():
        if nom not in cols:
            cx.execute(f"ALTER TABLE messages ADD COLUMN {nom} {typ}")
    cx.commit()


def _entetes_du_eml(chemin):
    """Lit uniquement les en-têtes d'un .eml (rapide)."""
    from email.parser import BytesHeaderParser
    with open(chemin, "rb") as f:
        msg = BytesHeaderParser().parse(f)
    g = lambda k: _dec_entete(msg.get(k)) if k in ("Subject",) else (msg.get(k) or "")
    return {
        "from": _dec_entete(msg.get("From")),
        "to": msg.get("To") or "",
        "cc": msg.get("Cc") or "",
        "subject": _dec_entete(msg.get("Subject")),
        "list_unsubscribe": msg.get("List-Unsubscribe") or "",
        "list_id": msg.get("List-Id") or "",
        "precedence": msg.get("Precedence") or "",
        "auto_submitted": msg.get("Auto-Submitted") or "",
        "reply_to": msg.get("Reply-To") or "",
        "return_path": msg.get("Return-Path") or "",
    }


def cmd_analyser(args):
    cfg = charger_config(args.config)
    cx = connexion(cfg["base_sqlite"])
    _assurer_colonnes(cx)
    charger_moteur(cfg)
    mes = [a.lower() for a in cfg.get("mes_adresses", [])]
    importants = {a.lower() for a in cfg.get("expediteurs_importants", [])}
    if not mes:
        print("⚠ 'mes_adresses' est vide : le « à moi / juste en copie » ne pourra "
              "pas être déterminé.\n  Lance d'abord :  suggerer-adresses")

    where = "" if args.retout else " WHERE categorie IS NULL"
    rows = cx.execute(
        "SELECT message_id, fichier_eml, extrait FROM messages" + where
    ).fetchall()
    total = len(rows)
    print(f"[{NOM}] analyse de {total} message(s)…")

    faits, erreurs = 0, 0
    t0 = time.time()
    for mid, chemin, extrait in rows:
        if not chemin or not Path(chemin).exists():
            erreurs += 1
            continue
        try:
            h = _entetes_du_eml(chemin)
            c = classer(h, extrait or "", mes, importants)
        except Exception:
            erreurs += 1
            continue
        cx.execute(
            """UPDATE messages SET categorie=?, urgence=?, pour_moi=?, bulk=?,
               automatique=?, expediteur_humain=?, important=?, suspect=?,
               signaux=?, signal_urgent=?, raison=?, confiance=?,
               analyse_source=?, analyse_at=? WHERE message_id=?""",
            (c["categorie"], c["urgence"], c["pour_moi"], c["bulk"],
             c["automatique"], c["expediteur_humain"], c["important"], c["suspect"],
             json.dumps(c["signaux_detail"], ensure_ascii=False), c["signal_urgent"],
             c["raison"], c["confiance"], "regles",
             datetime.now(timezone.utc).isoformat(), mid),
        )
        faits += 1
        if faits % 2000 == 0:
            cx.commit()
    cx.commit()
    print(f"[{NOM}] analysés : {faits}, ignorés : {erreurs}, en {time.time()-t0:.0f}s")
    if not getattr(args, "silencieux", False):
        _resume_analyse(cx)
    cx.close()


def _resume_analyse(cx):
    def compter(req, params=()):
        return cx.execute(req, params).fetchall()
    print("\n=== Synthèse ===")
    tot = cx.execute("SELECT COUNT(*) FROM messages WHERE categorie IS NOT NULL").fetchone()[0]
    print(f"Classés : {tot}")
    print("\nPar urgence :")
    for p, n in compter("SELECT urgence, COUNT(*) AS n FROM messages WHERE categorie IS NOT NULL "
                        "GROUP BY urgence ORDER BY n DESC"):
        print(f"  {p or '—':10} {n}")
    print("\nÀ moi / copie / diffusion :")
    for v, lib in [(1, "adressés à moi"), (0, "juste en copie"), (None, "diffusion/liste")]:
        if v is None:
            n = cx.execute("SELECT COUNT(*) FROM messages WHERE pour_moi IS NULL AND categorie IS NOT NULL").fetchone()[0]
        else:
            n = cx.execute("SELECT COUNT(*) FROM messages WHERE pour_moi=?", (v,)).fetchone()[0]
        print(f"  {lib:18} {n}")
    nb_bulk = cx.execute("SELECT COUNT(*) FROM messages WHERE bulk=1").fetchone()[0]
    nb_susp = cx.execute("SELECT COUNT(*) FROM messages WHERE suspect=1").fetchone()[0]
    print(f"\nCourrier en masse : {nb_bulk}")
    print(f"Soupçons d'hameçonnage (à vérifier) : {nb_susp}")
    print("\nTop catégories :")
    for cat, n in compter("SELECT categorie, COUNT(*) FROM messages WHERE categorie IS NOT NULL "
                         "GROUP BY categorie ORDER BY COUNT(*) DESC LIMIT 10"):
        print(f"  {cat or '—':16} {n}")


def cmd_topo(args):
    """Aperçu priorisé — la graine du topo du matin."""
    cfg = charger_config(args.config)
    cx = connexion(cfg["base_sqlite"])
    n = args.nombre
    print("=== Ce qui semble demander ton attention ===\n")
    # On inclut : ce qui t'est adressé hors masse, ET tout ce qui est important
    # (argent, sécurité, commande, réservation…) même en courrier de masse.
    rows = cx.execute(
        """SELECT date_mail, de_adresse, sujet, priorite, important, raison
           FROM messages
           WHERE (pour_moi=1 AND bulk=0) OR important=1
           ORDER BY CASE priorite WHEN 'haute' THEN 0 WHEN 'normale' THEN 1 ELSE 2 END,
                    important DESC, date_mail DESC
           LIMIT ?""", (n,)).fetchall()
    if not rows:
        print("Rien ne ressort (as-tu renseigné 'mes_adresses' puis lancé 'analyser' ?).")
    for d, de, suj, prio, imp, rai in rows:
        marque = "‼" if prio == "haute" else ("★" if imp else "•")
        print(f"{marque} [{(d or '')[:10]}] {(suj or '(sans objet)')[:60]}")
        print(f"    de {(de or '')[:40]} — {rai}")
    cx.close()


def cmd_suggerer_adresses(args):
    cfg = charger_config(args.config)
    cx = connexion(cfg["base_sqlite"])
    from collections import Counter
    c = Counter()
    for (dest,) in cx.execute("SELECT destinataires FROM messages WHERE destinataires IS NOT NULL"):
        try:
            for a in json.loads(dest):
                for spec in _adresses_spec(a):
                    c[spec] += 1
        except Exception:
            pass
    print("Adresses les plus souvent en destinataire (candidates pour 'mes_adresses') :")
    for addr, n in c.most_common(12):
        print(f"  {n:6}  {addr}")
    print("\nAjoute la/les tiennes dans config.json, clé \"mes_adresses\": [ \"...\" ].")
    cx.close()


# ----------------------------------------------------------------------------
# Le cerveau — couche IA (jugement contextuel)
# ----------------------------------------------------------------------------
import urllib.request
import urllib.error


def _moteur_dir(cfg):
    d = cfg.get("moteur_dir") or ""
    if d:
        return Path(d)
    # Bases de recherche, dans l'ordre. En exécutable figé (PyInstaller), on
    # privilégie un moteur/ posé À CÔTÉ de l'exe (surchargeable sans reconstruire),
    # puis la copie embarquée dans le bundle (_MEIPASS).
    bases = []
    if getattr(sys, "frozen", False):
        bases.append(Path(sys.executable).resolve().parent)
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            bases.append(Path(meipass))
    else:
        ici = Path(__file__).resolve().parent
        bases += [ici, ici.parent]
    for base in bases:
        for cand in (base / "moteur", base):
            if (cand / "regles.json").exists():
                return cand
    return (bases[0] if bases else Path(".")) / "moteur"


def charger_regles(cfg):
    """Surcharge les règles déterministes depuis moteur/regles.json (donnée partagée)."""
    p = _moteur_dir(cfg) / "regles.json"
    if not p.exists():
        return False
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[{NOM}] regles.json illisible ({e}), règles internes utilisées.", file=sys.stderr)
        return False
    g = globals()
    corresp = {
        "expediteurs_auto": ("EXPEDITEURS_AUTO", set),
        "mots_urgents": ("MOTS_URGENTS", list),
        "freemail": ("FREEMAIL", set),
        "marketing_labels": ("MARKETING_LABELS", set),
        "local_auto_indices": ("LOCAL_AUTO_INDICES", tuple),
        "local_generique": ("LOCAL_GENERIQUE", set),
        "sujets_importants": ("SUJETS_IMPORTANTS", list),
        "domaines_importants": ("DOMAINES_IMPORTANTS", tuple),
        # --- clés introduites par le moteur 1.1.0 ---
        "regex_local_humain": ("REGEX_LOCAL_HUMAIN", str),
        "marketing_indices_label": ("MARKETING_INDICES_LABEL", tuple),
        "precedence_bulk": ("PRECEDENCE_BULK", tuple),
        "categories_importantes": ("CATEGORIES_IMPORTANTES", tuple),
        "mots_suspects": ("MOTS_SUSPECTS", tuple),
        "domaines_reseau_social": ("DOMAINES_RESEAU_SOCIAL", tuple),
        "strategie_categorie": ("STRATEGIE_CATEGORIE", str),
        "fil_prefixes": ("FIL_PREFIXES", tuple),
        "fil_mots_vides": ("FIL_MOTS_VIDES", set),
    }
    for cle, (nom_var, conv) in corresp.items():
        if cle in d:
            g[nom_var] = conv(d[cle])
    if "regles_categorie" in d:
        g["REGLES_CATEGORIE"] = [(x[0], list(x[1])) for x in d["regles_categorie"]]
    if "domaines_categorie" in d:                     # moteur 2.0
        g["DOMAINES_CATEGORIE"] = [(x[0], list(x[1])) for x in d["domaines_categorie"]]
    if isinstance(d.get("selection_ia"), dict):
        g["SELECTION_IA"] = d["selection_ia"]
    if isinstance(d.get("topo"), dict):
        g["TOPO"] = {**TOPO, **d["topo"]}
    if isinstance(d.get("parametres"), dict):
        g["PARAMS"] = {**PARAMS, **d["parametres"]}
    # Recompiler la regex de préfixes de fil si la liste a changé.
    g["_PREFIXE_FIL"] = _compiler_prefixe_fil(FIL_PREFIXES)
    return True


def charger_taxonomie(cfg):
    """Charge taxonomie.json (format 2) et peuple les globals du moteur 2.0
    (niveaux, catégories actives, signaux). Renvoie le dict complet."""
    p = _moteur_dir(cfg) / "taxonomie.json"
    taxo = None
    if p.exists():
        try:
            taxo = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            taxo = None
    if not taxo:
        taxo = {"niveaux": [{"code": "normale", "libelle": "Cette semaine",
                             "definition": "", "delai_heures": 72}],
                "categories": [{"code": "autre", "libelle": "Autre",
                                "description": "Tout le reste.", "urgence_defaut": "normale"}],
                "signaux": {"critique": [], "haute": [], "basse": []}}
    g = globals()
    g["NIVEAUX"] = taxo.get("niveaux", [])
    g["CATEGORIES"] = taxo.get("categories", [])
    g["CAT_PAR_CODE"] = {c["code"]: c for c in g["CATEGORIES"]}
    sig = taxo.get("signaux", {}) or {}
    g["SIGNAUX"] = {n: list(sig.get(n, []) or []) for n in ("critique", "haute", "basse")}
    return taxo


def charger_moteur(cfg):
    """Charge regles + taxonomie, vérifie les empreintes. Renvoie la version moteur."""
    charger_regles(cfg)
    charger_taxonomie(cfg)
    return verifier_moteur(cfg)


def charger_prompt(cfg):
    p = _moteur_dir(cfg) / "prompt.txt"
    if p.exists():
        return p.read_text(encoding="utf-8")
    return ("Tu classes un email. Réponds en JSON strict avec categorie, "
            "action_attendue, tache, echeance, pression, priorite, resume, "
            "question, confiance.")


def charger_manifest(cfg):
    p = _moteur_dir(cfg) / "manifest.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def verifier_moteur(cfg):
    """Vérifie les empreintes sha256 des fichiers moteur au démarrage ; avertit si
    écart (copie locale modifiée à la main ou incomplète). Renvoie la version."""
    man = charger_manifest(cfg)
    base = _moteur_dir(cfg)
    for f, h in (man.get("sha256") or {}).items():
        try:
            got = hashlib.sha256((base / f).read_bytes()).hexdigest()
        except Exception:
            print(f"[{NOM}] ⚠ moteur : {f} illisible", file=sys.stderr)
            continue
        if got != h:
            print(f"[{NOM}] ⚠ moteur : {f} diffère du manifest (sha256). "
                  f"Copie locale éditée à la main ou incomplète.", file=sys.stderr)
    return man.get("version", "?")


_RE_ECHEANCE = re.compile(r'^(aucune|\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01]))$')
_RE_CATEGORIE = re.compile(r'^[a-z_]{1,50}$')


def _valider_schema(v, schema):
    """Mini-validateur JSON-Schema (sous-ensemble : type, enum, min/maxLength,
    properties, required, additionalProperties:false). Pour les champs d'extension."""
    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        bon = any(
            (tt == "null" and v is None)
            or (tt == "string" and isinstance(v, str))
            or (tt == "boolean" and isinstance(v, bool))
            or (tt == "object" and isinstance(v, dict))
            or (tt == "array" and isinstance(v, list))
            or (tt == "integer" and isinstance(v, int) and not isinstance(v, bool))
            or (tt == "number" and isinstance(v, (int, float)) and not isinstance(v, bool))
            for tt in types)
        if not bon:
            return False, f"type attendu {types}"
    if "enum" in schema and v not in schema["enum"]:
        return False, "hors enum"
    if isinstance(v, str):
        if "maxLength" in schema and len(v) > schema["maxLength"]:
            return False, "trop long"
        if "minLength" in schema and len(v) < schema["minLength"]:
            return False, "trop court"
        if "pattern" in schema and not re.search(schema["pattern"], v):
            return False, "format"
    if isinstance(v, dict):
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for k in v:
                if k not in props:
                    return False, f"clé en trop : {k}"
        for k in schema.get("required", []):
            if k not in v:
                return False, f"clé manquante : {k}"
        for k, sub in props.items():
            if k in v:
                ok, e = _valider_schema(v[k], sub)
                if not ok:
                    return False, f"{k}.{e}"
    return True, None


def valider_sortie(res, taxo_noms, extensions=()):
    """Valide et normalise le jugement IA contre le tronc commun 2.0
    (sortie.schema.json) PLUS les champs des extensions actives. Ordre :
    pré-normalisation (clés inconnues retirées, ''→null) ; tronc ; extensions ;
    catégorie ∉ taxonomie → 'autre'. Renvoie (ok, res_normalise, erreur).
    Échec → ia_source='erreur', repli déterministe."""
    REQUIS = ["categorie", "urgence", "action_attendue", "tache", "echeance",
              "justification", "resume", "question", "confiance"]
    props_ext = {}
    for e in extensions:                          # champs ajoutés par les extensions actives
        for k in e.get("requis", []):
            if k not in REQUIS:
                REQUIS.append(k)
        props_ext.update(e.get("proprietes", {}))
    if not isinstance(res, dict):
        return False, None, "réponse non-objet"
    inconnues = [k for k in res if k not in REQUIS]
    if inconnues:
        print(f"[{NOM}] clés IA inconnues ignorées : {inconnues}", file=sys.stderr)
    r = {k: res.get(k) for k in REQUIS}
    for k in ("tache", "question"):               # "" → null
        if isinstance(r[k], str) and not r[k].strip():
            r[k] = None
    if r["urgence"] not in ("critique", "haute", "normale", "basse"):
        return False, None, "urgence invalide"
    if not isinstance(r["action_attendue"], bool):
        return False, None, "action_attendue non booléen"
    if r["confiance"] not in ("haute", "moyenne", "faible"):
        return False, None, "confiance invalide"
    if not isinstance(r["resume"], str) or not (1 <= len(r["resume"]) <= 300):
        return False, None, "resume invalide"
    if not isinstance(r["justification"], str) or not (1 <= len(r["justification"]) <= 500):
        return False, None, "justification invalide"
    if r["tache"] is not None and (not isinstance(r["tache"], str) or len(r["tache"]) > 300):
        return False, None, "tache invalide"
    if r["question"] is not None and (not isinstance(r["question"], str) or len(r["question"]) > 400):
        return False, None, "question invalide"
    if not isinstance(r["categorie"], str) or not _RE_CATEGORIE.match(r["categorie"]):
        return False, None, "categorie format"
    if not isinstance(r["echeance"], str) or not _RE_ECHEANCE.match(r["echeance"]):
        return False, None, "echeance format"
    for k, schema in props_ext.items():           # champs d'extension
        ok, e = _valider_schema(r.get(k), schema)
        if not ok:
            return False, None, f"extension {k} : {e}"
    if r["categorie"] not in taxo_noms:          # contrainte hors-schéma
        r["categorie_proposee"] = r["categorie"]
        r["categorie"] = "autre"
    return True, r, None


def deduire_case_topo(urgence, echeance, action_attendue, aujourdhui):
    """Déduit la case du topo (regles.topo) — jamais demandée à l'IA."""
    ordre = TOPO.get("ordre", ["maintenant", "cette_semaine", "ce_mois", "aucune"])
    rang = {c: i for i, c in enumerate(ordre)}
    case_u = {"critique": "maintenant", "haute": "maintenant",
              "normale": "cette_semaine"}.get(urgence,
              "ce_mois" if action_attendue else "aucune")
    case_e = "aucune"
    try:
        d = datetime.strptime((echeance or "").strip(), "%Y-%m-%d").date()
        auj = datetime.strptime(aujourdhui, "%Y-%m-%d").date()
        delta = (d - auj).days
        if delta <= 0:
            case_e = "maintenant"
        elif delta <= TOPO.get("jours_semaine", 7):
            case_e = "cette_semaine"
        elif delta <= TOPO.get("jours_mois", 31):
            case_e = "ce_mois"
    except Exception:
        case_e = "aucune"
    case = min(case_u, case_e, key=lambda c: rang.get(c, 99))
    # Information seule : pas d'action et urgence non critique → aucune.
    if not action_attendue and urgence != "critique":
        case = "aucune"
    return case


def _dt_message(valeur):
    """Parse une date de mail (ISO en base, repli en-tête RFC) ; None si illisible."""
    if not valeur:
        return None
    try:
        return datetime.fromisoformat(str(valeur).replace("Z", "+00:00"))
    except Exception:
        try:
            return email.utils.parsedate_to_datetime(valeur)
        except Exception:
            return None


def _compiler_prefixe_fil(prefixes):
    # Préfixes de fil (re|tr|fwd…) ; triés du plus long au plus court pour éviter
    # qu'un préfixe court (fw) morde sur un plus long (fwd).
    alt = "|".join(sorted((re.escape(p) for p in prefixes), key=len, reverse=True))
    return re.compile(r'^(?:\s*(?:' + alt + r')\s*:\s*)+', re.I)


_PREFIXE_FIL = _compiler_prefixe_fil(FIL_PREFIXES)


def _sujet_fil(sujet):
    """Clé de fil : objet NORMALISÉ (_semantique : minuscule, sans accents,
    apostrophes → espace), puis retrait des préfixes Re:/Tr:, des [tags], espaces compactées."""
    s = _norm_texte(sujet)
    s = _PREFIXE_FIL.sub("", s)
    s = re.sub(r'\[[^\]]*\]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def _jours_depuis(d, now=None):
    if not d:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return ((now or datetime.now(timezone.utc)) - d).days


def _local(d):
    """Date/heure dans le fuseau du poste (les mails sont stockés avec le fuseau de
    l'expéditeur ou en UTC : sans cela, « à 10 heures » est faux d'une ou deux heures)."""
    try:
        return d.astimezone() if (d and d.tzinfo) else d
    except Exception:
        return d


def _est_chaud(important, urgence):
    """Un message « chaud » pour la mémoire de fil : important, ou urgence retenue
    (IA, sinon règles) critique/haute. Même définition que GesEnt."""
    return important == 1 or urgence in ("critique", "haute")


def profils_expediteurs(cx):
    """Mémoire de l'historique. Renvoie (profils, fils, attente).

    profils[adresse] = volume, part de mails 'urgent', récence (30 j), premier/
    dernier contact, fil principal — pour ne pas traiter un habitué comme un inconnu.
    fils[clé] = messages du même sujet reçus depuis moins de fil_fenetre_jours, et
    combien sont 'chauds' — pour reconnaître un sujet suivi de près (un objet banal
    comme « Facture » ne devient plus un fil de 300 messages étalés sur des années).
    attente[(adresse, clé)] = dates des messages NON traités, hors masse, de cet
    expéditeur sur ce sujet (fenêtre relance_fenetre_jours) — pour repérer une relance.
    """
    profils, fils, attente = {}, {}, {}
    now = datetime.now(timezone.utc)
    f_fil = PARAMS.get("fil_fenetre_jours", 90)
    f_rel = PARAMS.get("relance_fenetre_jours", 14)

    rows = cx.execute(
        "SELECT de_adresse, sujet, date_mail, COALESCE(signal_urgent,0), "
        "COALESCE(NULLIF(ia_urgence,''), urgence, ''), COALESCE(important,0), "
        "COALESCE(bulk,0), etat FROM messages").fetchall()
    for de, sujet, dm, sig_urg, urg, imp, bulk, etat in rows:
        d = _dt_message(dm)
        if d is not None and d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        jd = _jours_depuis(d, now)
        recent = jd is not None and jd <= PARAMS["fenetre_recent_jours"]
        urgent_ton = bool(sig_urg)            # signal d'urgence déterministe retenu
        fkey = _sujet_fil(sujet)

        spec = (_adresses_spec(de or "") or [""])[0]
        if spec:
            p = profils.setdefault(spec, {"n": 0, "urgents": 0, "recent": 0,
                                          "premier": None, "dernier": None, "_fils": {}})
            p["n"] += 1
            if urgent_ton:
                p["urgents"] += 1
            if recent:
                p["recent"] += 1
            if d:
                if not p["premier"] or d < p["premier"]:
                    p["premier"] = d
                if not p["dernier"] or d > p["dernier"]:
                    p["dernier"] = d
            if fkey:
                p["_fils"][fkey] = p["_fils"].get(fkey, 0) + 1
            if (fkey and d and etat is None and not bulk
                    and jd is not None and jd <= f_rel):
                attente.setdefault((spec, fkey), []).append(d)

        if fkey and jd is not None and jd <= f_fil:
            f = fils.setdefault(fkey, {"n": 0, "chauds": 0, "sujet": sujet or fkey})
            f["n"] += 1
            if _est_chaud(imp, urg):
                f["chauds"] += 1

    ecrits = index_envoyes(cx, now)["ecrits"]
    for spec, p in profils.items():
        p["ecrits"] = ecrits.get(spec, 0)
        p["pct_urgent"] = round(p["urgents"] / p["n"], 2) if p["n"] else 0.0
        p["fil_principal"] = (max(p["_fils"].items(), key=lambda kv: kv[1])
                              if p["_fils"] else None)
        p["anciennete_j"] = _jours_depuis(p["premier"], now)
    return profils, fils, attente


def profil_txt(n, recent=0, urgents=0, ecrits=0):
    """Variable 'profil' de entree.txt — règle commune (README, vecteurs
    tests/entree/profil.json). n compte le message jugé ; vide si n = 0.
    Pourcentage arrondi au plus proche, .5 vers le haut, en entiers : Python arrondit
    « au pair » (12.5 → 12) quand PHP donne 13 — les deux harnais divergeaient."""
    n, recent, urgents, ecrits = (int(x or 0) for x in (n, recent, urgents, ecrits))
    if n <= 0:
        return ""
    pct = (200 * urgents + n) // (2 * n)
    s = (f"{n} message(s) reçu(s), dont {recent} ces "
         f"{PARAMS['fenetre_recent_jours']} derniers jours, {pct} % au ton urgent")
    if n >= PARAMS["profil_actif_n_min"] and recent >= PARAMS["profil_actif_recent_min"]:
        s += ", CORRESPONDANT RÉCURRENT ET ACTIF"
    if ecrits >= 1:
        s += (f", le destinataire lui a écrit {ecrits} fois ces "
              f"{PARAMS.get('profil_ecrits_fenetre_jours', 90)} derniers jours")
    return s


def _profil_en_clair(profil):
    """Phrase PROFIL pour l'IA. Le compagnon ne lit pas encore le dossier Envoyés :
    'ecrits' vaut 0 tant qu'il ne le fournit pas."""
    if not profil:
        return ""
    return profil_txt(profil.get("n", 0), profil.get("recent", 0),
                      profil.get("urgents", 0), profil.get("ecrits", 0))


def _date_courte(iso, aujourdhui=None):
    """'18 novembre', '1er novembre', '30 décembre 2025' (année si elle diffère de celle
    du jour) ; '' si illisible."""
    try:
        d = datetime.strptime(str(iso or "").strip()[:10], "%Y-%m-%d").date()
    except Exception:
        return ""
    try:
        an = datetime.strptime(str(aujourdhui)[:10], "%Y-%m-%d").year if aujourdhui else datetime.now().year
    except Exception:
        an = datetime.now().year
    txt = f"{'1er' if d.day == 1 else d.day} {_MOIS_ENTREE[d.month - 1]}"
    return txt + (f" {d.year}" if d.year != an else "")


def memoire_txt(sujet, n, chauds=0, dernier_envoi=None, aujourdhui=None):
    """Variable 'memoire' de entree.txt — règle commune 2.4 (README, vecteurs
    tests/entree/memoire.json). n = messages du fil, le message jugé compris ;
    chauds = parmi les AUTRES ; dernier_envoi = dernière réponse du destinataire."""
    n, chauds = int(n or 0), int(chauds or 0)
    tete = f"fil « {str(sujet or '')[:60]} » : "
    envoi = _date_courte(dernier_envoi, aujourdhui) if dernier_envoi else ""
    if n >= 2:
        txt = tete + f"{n - 1} autre(s) message(s) sur ce sujet"
        if chauds > 0:
            txt += f", dont {chauds} déjà jugé(s) important(s) ou urgent(s)"
        return txt + (f", dernière réponse du destinataire le {envoi}" if envoi else "")
    return (tete + f"dernière réponse du destinataire le {envoi}") if envoi else ""


def precision_txt(texte, maximum=None):
    """Variable 'precision' de entree.txt (2.4, vecteurs tests/entree/precision.json) :
    compactée, coupée à precision_max sans points de suspension, fin nettoyée."""
    m = int(maximum or PARAMS.get("precision_max", 500))
    t = re.sub(r"\s+", " ", str(texte or "")).strip()[:m]
    return t.rstrip(" .")


def relances_k(recu_le, anterieurs, dernier_envoi=None, aujourdhui=None, fenetre=None):
    """Nombre k de la variable 'relances' — règle commune 2.2.1 + 2.4 (vecteurs
    tests/entree/relances.json). anterieurs = messages du même expéditeur et du même
    fil : [{"date", "traite", "bulk"}]. Comptent ceux reçus avant le message jugé,
    depuis moins de 'fenetre' jours (à la date du jour), encore à traiter, hors masse,
    et — s'il y a une réponse du destinataire dans le fil — reçus APRÈS cette réponse."""
    f = int(fenetre or PARAMS.get("relance_fenetre_jours", 14))

    def jour(x):
        return datetime.fromisoformat(str(x).replace("Z", "+00:00")) if x else None

    recu = jour(recu_le)
    ref = jour(aujourdhui) if aujourdhui else datetime.now(timezone.utc)
    envoi = jour(dernier_envoi)
    norm = lambda d: d if (d is None or d.tzinfo) else d.replace(tzinfo=timezone.utc)
    recu, ref, envoi = norm(recu), norm(ref), norm(envoi)
    k = 0
    for m in anterieurs or []:
        d = norm(jour(m.get("date")))
        if (d is None or m.get("traite") or m.get("bulk") or (recu and d >= recu)
                or (ref - d).days > f or (envoi and d <= envoi)):
            continue
        k += 1
    return k


def relances_phrase(k):
    """Texte de la variable 'relances' pour k messages ; vide si k = 0."""
    k = int(k or 0)
    return (f"l'expéditeur a déjà écrit {k} fois ces {PARAMS.get('relance_fenetre_jours', 14)} "
            f"derniers jours sans que ses messages soient traités") if k > 0 else ""


def _memoire_fil(row, fils):
    """Phrase MÉMOIRE sur le fil (format entree.txt). Ne compte que les AUTRES
    messages du fil : le message jugé est retiré du total et des « chauds »
    (sinon un mail « important » se déclarait lui-même sujet brûlant)."""
    fkey = _sujet_fil(row.get("sujet"))
    f = fils.get(fkey) if fkey else None
    if not f:
        return ""
    jd = _jours_depuis(_dt_message(row.get("date_mail")))
    moi = jd is not None and jd <= PARAMS.get("fil_fenetre_jours", 90)
    autres = f["n"] - (1 if moi else 0)
    if autres < 1:
        return ""
    urg = row.get("ia_urgence") or row.get("urgence") or ""
    chauds = f["chauds"] - (1 if (moi and _est_chaud(row.get("important"), urg)) else 0)
    # Le compagnon ne lit pas encore le dossier Envoyés : pas de 'dernier_envoi'.
    return memoire_txt(f["sujet"], autres + 1, chauds, row.get("dernier_envoi"))


def _relances_txt(row, attente):
    """Phrase RELANCE (format entree.txt) : messages PRÉCÉDENTS du même expéditeur
    sur le même sujet, restés non traités. Volontairement prudent en autonome : sans
    le dossier Envoyés, « non traité » veut dire « pas marqué Traité au cockpit »."""
    spec = (_adresses_spec(row.get("de_adresse") or "") or [""])[0]
    fkey = _sujet_fil(row.get("sujet"))
    d = _dt_message(row.get("date_mail"))
    if not (spec and fkey and d and attente):
        return ""
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    envoi = _dt_message(row.get("dernier_envoi")) if row.get("dernier_envoi") else None
    if envoi is not None and envoi.tzinfo is None:
        envoi = envoi.replace(tzinfo=timezone.utc)
    return relances_phrase(sum(1 for x in attente.get((spec, fkey), [])
                               if x < d and (envoi is None or x > envoi)))


# ── Pour QUI l'on trie (moteur 2.2) ─────────────────────────────────────────
_JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
_MOIS_ENTREE = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
                "août", "septembre", "octobre", "novembre", "décembre"]


def _date_du_jour_txt(iso):
    """'lundi 16 novembre 2026 (2026-11-16)' : le jour de la semaine évite à l'IA
    de se tromper sur « vendredi prochain »."""
    try:
        d = datetime.strptime(iso, "%Y-%m-%d").date()
    except Exception:
        return iso
    jour = "1er" if d.day == 1 else str(d.day)
    return f"{_JOURS[d.weekday()]} {jour} {_MOIS_ENTREE[d.month - 1]} {d.year} ({iso})"


BLOC_DESTINATAIRE_MAX = 1500       # caractères (points de code), point final compris
BOITE_RESTE_MIN = 40               # en dessous, la description de la boîte est omise
_BOITE_PREFIXE = "Boîte : "


def _compacte(x):
    """Espaces compactées, espaces et points finaux retirés."""
    return re.sub(r"\s+", " ", str(x or "")).strip().rstrip(". ")


def bloc_destinataire(nom="", fonction="", profil="", boite=""):
    """Variable 'utilisateur' de entree.txt — règle commune moteur 2.2.1 (README) :

        partie U = « {nom}, {fonction}. {profil} »  (parties vides omises)
        partie B = description de la boîte
        bloc     = U + "." | U + ". Boîte : " + B + "." | "Boîte : " + B + "."

    1500 caractères au plus, point final compris. Le profil de l'utilisateur passe
    d'abord ; la description de la boîte prend la place restante (coupée), et elle
    est omise s'il reste moins de 40 caractères. Vecteurs : moteur/tests/entree/."""
    tete = ", ".join(x for x in (_compacte(nom), _compacte(fonction)) if x)
    u = ". ".join(x for x in (tete, _compacte(profil)) if x)
    b = _compacte(boite)
    if len(u) >= BLOC_DESTINATAIRE_MAX:
        u = u[:BLOC_DESTINATAIRE_MAX - 1].rstrip(" .")
        return u + "."
    if b:
        sep = ". " + _BOITE_PREFIXE if u else _BOITE_PREFIXE
        reste = BLOC_DESTINATAIRE_MAX - len(u) - len(sep) - 1
        if reste >= BOITE_RESTE_MIN:
            b = b[:reste].rstrip(" .")
            if b:
                return u + sep + b + "."
    return (u + ".") if u else ""


def _utilisateur_txt(cfg):
    """Bloc DESTINATAIRE pour le compagnon : une seule boîte, celle de l'utilisateur ;
    'utilisateur.boite' (facultatif, config seulement) la décrit."""
    u = cfg.get("utilisateur") or {}
    if isinstance(u, str):
        u = {"profil": u}
    return bloc_destinataire(u.get("nom"), u.get("fonction"), u.get("profil"), u.get("boite"))


def _date_longue(iso):
    """'12 novembre 2026' / '1er décembre 2026' depuis AAAA-MM-JJ ; '' si illisible."""
    try:
        d = datetime.strptime(str(iso or "").strip()[:10], "%Y-%m-%d").date()
    except Exception:
        return ""
    return f"{'1er' if d.day == 1 else d.day} {_MOIS_ENTREE[d.month - 1]} {d.year}"


def reponse_attendue_txt(a="", le="", suivi_le=None):
    """Variable 'reponse_attendue' de entree.txt (moteur 2.3) : le message répond à une
    affaire que le destinataire avait déléguée avec suivi. Format commun (README) :
    'délégué à {à qui} le {J} {mois} {AAAA}[, suivi prévu le {J} {mois} {AAAA}]'.
    Vide si l'un des deux éléments obligatoires manque. Le compagnon ne gère pas
    encore la délégation : la fonction sert au format commun et à ses vecteurs."""
    qui = _compacte(a)
    quand = _date_longue(le)
    if not (qui and quand):
        return ""
    txt = f"délégué à {qui} le {quand}"
    suivi = _date_longue(suivi_le) if suivi_le else ""
    return txt + (f", suivi prévu le {suivi}" if suivi else "")


def _expediteur_connu_txt(cfg, de):
    """En autonome, la seule chose « connue » est la liste expediteurs_importants."""
    spec = (_adresses_spec(de or "") or [""])[0]
    imp = {a.lower() for a in (cfg.get("expediteurs_importants") or [])}
    if spec and (spec in imp or _domaine(spec) in imp):
        return "dans la liste des expéditeurs importants du destinataire"
    return ""


# ── Jurisprudence locale : les corrections faites dans le cockpit ───────────
def _mots_cles(sujet):
    return " ".join(sorted(_mots_sujet(sujet)))


def exemples_pertinents(cx, row):
    """Corrections passées qui ressemblent à ce message (même expéditeur, même
    domaine d'entreprise, objet voisin). Même barème que GesEnt (ExemplesCourrier)."""
    spec = (_adresses_spec(row.get("de_adresse") or "") or [""])[0]
    dom = _domaine(spec)
    dom = dom if (dom and dom not in FREEMAIL) else ""
    mots = _mots_sujet(row.get("sujet"))
    try:
        lignes = cx.execute(
            "SELECT message_id, de_adresse, domaine, sujet, mots, extrait, categorie, "
            "urgence, action_attendue, concerne, precision FROM corrections "
            "ORDER BY id DESC LIMIT 300"
        ).fetchall()
    except sqlite3.Error:
        return []
    vus, notes = set(), []
    for mid, de, d2, sujet, m2, extrait, cat, urg, action, concerne, precision in lignes:
        if mid in vus or mid == row.get("message_id"):
            continue                      # la correction la plus récente d'un mail l'emporte
        vus.add(mid)
        score = 60 if (spec and de == spec) else (30 if (dom and d2 == dom) else 0)
        siens = set((m2 or "").split())
        if mots and siens:
            score += int(round(40 * len(mots & siens) / len(mots | siens)))
        if score >= 30:
            notes.append((score, {"de": de, "sujet": sujet, "extrait": extrait, "categorie": cat,
                                  "urgence": urg, "action": action, "concerne": concerne,
                                  "precision": precision}))
    notes.sort(key=lambda x: -x[0])
    return [e for _, e in notes[:int(PARAMS.get("exemples_max", 3))]]


def _exemples_txt(exs):
    """Bloc 'exemples' de entree.txt, au format de GesEnt (ExemplesCourrier::pourIa)."""
    out = []
    for e in exs:
        suite = f"categorie: {e['categorie'] or 'autre'}, urgence: {e['urgence'] or 'normale'}"
        if e.get("concerne") == "autre" or e.get("action") == 0:
            suite += ", action_attendue: false (le destinataire a indiqué que ce n'était pas à lui d'agir)"
        ligne = (f"- De <{e['de'] or ''}> — « {(e['sujet'] or '')[:120]} » — "
                 f"{(e['extrait'] or '')[:200]}\n  → {suite}")
        if e.get("precision"):            # moteur 2.4 : la réponse de la personne voyage avec
            ligne += f"\n  → précision du destinataire : « {e['precision'][:200]} »"
        out.append(ligne)
    return "\n".join(out)


# ── Rendu des gabarits moteur (prompt.txt, entree.txt) ──────────────────────
def _rendre_gabarit(tpl, vals):
    """Mustache minimal : {{#k}}…{{/k}} supprimé si vals[k] vide ; puis {{k}} remplacé."""
    def bloc(m):
        v = vals.get(m.group(1), "")
        return m.group(2) if (isinstance(v, str) and v.strip()) else ""
    tpl = re.sub(r"\{\{#(\w+)\}\}(.*?)\{\{/\1\}\}", bloc, tpl, flags=re.S)
    return re.sub(r"\{\{(\w+)\}\}", lambda m: str(vals.get(m.group(1), "")), tpl)


def _charger_gabarit(cfg, nom):
    p = _moteur_dir(cfg) / nom
    return p.read_text(encoding="utf-8") if p.exists() else ""


# Extensions dont le compagnon a besoin (topo, cockpit) : toujours actives.
EXTENSIONS_COMPAGNON = ("evenement", "portee")


def charger_extensions(cfg):
    """Extensions actives (config 'extensions': []), triées par nom."""
    base = _moteur_dir(cfg) / "extensions"
    out = []
    for nom in sorted(set(cfg.get("extensions", []) or []) | set(EXTENSIONS_COMPAGNON)):
        p = base / f"{nom}.json"
        if p.exists():
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                pass
    return out


def systeme_ia(cfg, exts):
    """Prompt système rendu avec les consignes/formats des extensions actives."""
    tpl = charger_prompt(cfg)
    if exts:
        consignes = "\n" + "\n\n".join(e["consigne"] for e in exts) + "\n"
        fmt = ",\n  " + ",\n  ".join(e["format"] for e in exts)
    else:
        consignes, fmt = "", ""
    return _rendre_gabarit(tpl, {"extensions_consignes": consignes,
                                 "extensions_format": fmt})


def _niveaux_txt():
    out = []
    for n in NIVEAUX:
        d = n.get("delai_heures")
        delai = f", à traiter sous {d} h" if d else ""
        out.append(f"- {n['code']} ({n.get('libelle', '')}{delai}) : {n.get('definition', '')}")
    return "\n".join(out)


def _categories_txt():
    return "\n".join(
        f"- {c['code']} : {c.get('libelle', '')}. {c.get('description', '')} "
        f"(urgence habituelle : {c.get('urgence_defaut', 'normale')})" for c in CATEGORIES)


def _signaux_txt(signaux_json):
    try:
        det = json.loads(signaux_json) if isinstance(signaux_json, str) else (signaux_json or [])
    except Exception:
        det = []
    if not det:
        return "aucun"
    bouts = []
    for s in det:
        if s["effet"] == "baisser":
            suff = "baisse"
        elif s.get("compte"):
            suff = "monte"
        else:
            suff = "monte — ignoré : expéditeur automatique ou masse"
        bouts.append(f"« {s['expression']} » ({s['niveau']}, {suff})")
    return ", ".join(bouts)


def _adresses_txt(valeur, maxi):
    try:
        arr = json.loads(valeur) if isinstance(valeur, str) else (valeur or [])
    except Exception:
        arr = []
    specs = []
    for x in arr:
        for s in _adresses_spec(x):
            if s not in specs:
                specs.append(s)
    if not specs:
        return ""
    if len(specs) > maxi:
        return ", ".join(specs[:maxi]) + f" et {len(specs) - maxi} autre(s)"
    return ", ".join(specs)


def _pj_txt(valeur):
    try:
        arr = json.loads(valeur) if isinstance(valeur, str) else (valeur or [])
    except Exception:
        arr = []
    noms = [str(a.get("nom") if isinstance(a, dict) else a) for a in arr if a]
    return ", ".join(noms) if noms else "aucune"


def _pre_classement_txt(row):
    parts = ["à moi" if row["pour_moi"] == 1 else
             ("en copie" if row["pour_moi"] == 0 else "diffusion"),
             "expéditeur humain" if row["expediteur_humain"] else "expéditeur automatique"]
    if row["bulk"]:
        parts.append("courrier de masse")
    if row["important"]:
        parts.append("marqué important")
    if row.get("suspect"):
        parts.append("soupçon d'hameçonnage (Reply-To d'un autre domaine)")
    return ", ".join(parts)


def construire_entree_ia(cfg, row, profil, fils, aujourdhui, attente=None, exemples=""):
    """Message utilisateur envoyé à l'IA : rend entree.txt avec les formats du README."""
    tpl = _charger_gabarit(cfg, "entree.txt")
    max_ex = _extrait_max(cfg)
    extrait = (row.get("extrait") or "").strip()
    if extrait and len(extrait) >= max_ex:
        extrait += PARAMS.get("ia_marque_troncature", "")
    dm = _local(_dt_message(row.get("date_mail")))
    maxi = PARAMS.get("ia_destinataires_max", 10)
    vals = {
        "date_du_jour": _date_du_jour_txt(aujourdhui),
        "utilisateur": _utilisateur_txt(cfg),
        "niveaux": _niveaux_txt(),
        "categories": _categories_txt(),
        "expediteur": row.get("de_adresse") or "",
        "expediteur_connu": _expediteur_connu_txt(cfg, row.get("de_adresse")),
        "profil": _profil_en_clair(profil),
        "relances": _relances_txt(row, attente),
        "memoire": _memoire_fil(row, fils),
        "reponse_attendue": row.get("reponse_attendue") or "",   # pas de délégation en autonome
        "precision": precision_txt(row.get("precision")),
        "pre_classement": _pre_classement_txt(row),
        "signaux": _signaux_txt(row.get("signaux")),
        "exemples": exemples or "",
        "de": (_adresses_spec(row.get("de_adresse")) or [row.get("de_adresse") or ""])[0],
        "a": _adresses_txt(row.get("destinataires"), maxi),
        "cc": _adresses_txt(row.get("copies"), maxi),
        "objet": row.get("sujet") or "",
        "date_mail": dm.strftime("%Y-%m-%d %H:%M") if dm else (row.get("date_mail") or ""),
        "pieces_jointes": _pj_txt(row.get("pieces_jointes")),
        "extrait": extrait or "(corps vide ou non texte)",
    }
    return _rendre_gabarit(tpl, vals)


def appeler_mistral(cfg_ia, systeme, utilisateur, modele=None, temperature=0):
    """Appel chat completions compatible OpenAI (Mistral). Renvoie le texte brut."""
    corps = json.dumps({
        "model": modele or cfg_ia.get("modele", "mistral-small-latest"),
        "temperature": temperature,
        "response_format": {"type": "json_object"},   # exige un objet JSON
        "messages": [
            {"role": "system", "content": systeme},
            {"role": "user", "content": utilisateur},
        ],
    }).encode("utf-8")
    req = urllib.request.Request(
        cfg_ia.get("endpoint", "https://api.mistral.ai/v1/chat/completions"),
        data=corps,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + cfg_ia.get("cle", ""),
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def _extraire_json(texte):
    """Récupère le premier objet JSON d'une réponse (robuste aux ``` et au bavardage)."""
    deb = texte.find("{")
    fin = texte.rfind("}")
    if deb == -1 or fin == -1:
        raise ValueError("pas de JSON dans la réponse")
    return json.loads(texte[deb:fin + 1])


def ia_bouchon(row, profil):
    """Modèle BOUCHONNÉ (contrat 2.0) pour tester le pipeline sans clé Mistral.
    Part de l'urgence déterministe + signaux ; ne remplace pas le vrai jugement IA."""
    cat = row.get("categorie") or "autre"
    urgence = row.get("urgence") or "normale"
    crie_au_loup = bool(profil and profil.get("n", 0) >= PARAMS["profil_actif_n_min"]
                        and profil.get("pct_urgent", 0) >= 0.5)
    texte = _norm_texte((row.get("sujet") or "") + " " + (row.get("de_adresse") or ""))
    notif = _contient_mot(texte,
                ("confirmation", "confirme", "reussi", "recu", "recue", "login",
                 "withdrawal", "deposit", "statement", "releve", "receipt",
                 "welcome", "bienvenue", "successful"))
    # Calibrage : un habitué qui crie urgence est redescendu d'un cran.
    if crie_au_loup and urgence in ("critique", "haute"):
        urgence = "normale"

    if cat == "hameconnage" or row.get("suspect"):
        action, tache, urgence = False, "Ne pas cliquer — vérifier par un autre canal", "haute"
    elif notif:
        action, tache, urgence = False, None, "basse"
    elif row.get("bulk") and not row.get("important"):
        action, tache, urgence = False, None, "basse"
    else:
        action = bool(row.get("pour_moi") == 1 or row.get("important"))
        tache = (row.get("sujet") or None) if action else None

    question = None
    if crie_au_loup:
        question = (f"« {(row.get('sujet') or '')[:40]} » vient d'un expéditeur qui "
                    f"écrit souvent « urgent » — vraiment prioritaire ?")
    return {
        "categorie": cat, "urgence": urgence, "action_attendue": action,
        "tache": tache, "echeance": "aucune",
        "justification": f"Mock : urgence déterministe {row.get('urgence') or 'normale'}, "
                         f"catégorie {cat}.",
        "resume": ((row.get("sujet") or "").strip() or "(sans objet)")[:300],
        "question": question, "confiance": "faible",
        "evenement": None, "concerne": "moi" if action else "collectif",
    }


def _juge_par_ia(source):
    """Vrai si le message porte un jugement utilisable (IA, bouchon ou correction humaine)."""
    return source not in (None, "", "erreur")


def case_du_jour(r, aujourdhui):
    """Case du topo recalculée AUJOURD'HUI (regles.topo). La case stockée le jour du
    jugement se périme : une échéance « dans 20 jours » doit remonter d'elle-même.
    Sans jugement IA (pas encore jugé, panne, réponse rejetée) : repli sur le tri
    déterministe — action_attendue = (à moi OU important), pas d'échéance."""
    if _juge_par_ia(r.get("ia_source")) and r.get("ia_urgence"):
        return deduire_case_topo(r["ia_urgence"], r.get("ia_echeance"),
                                 bool(r.get("ia_action")), aujourdhui)
    return deduire_case_topo(r.get("urgence") or "normale", None,
                             r.get("pour_moi") == 1 or r.get("important") == 1, aujourdhui)


def _config_ia(cfg):
    cfg_ia = dict(cfg.get("ia", {}))
    if not cfg_ia.get("cle"):
        cfg_ia["cle"] = os.environ.get("MISTRAL_API_KEY", "")
    return cfg_ia


COLS_SELECTION_IA = ["message_id", "de_adresse", "sujet", "date_mail", "pour_moi", "bulk",
                     "expediteur_humain", "important", "suspect", "categorie", "urgence",
                     "extrait", "destinataires", "copies", "pieces_jointes", "signaux",
                     "ia_urgence", "precision"]


def selection_ia(cx, retout=False, jours=0, limite=None, ids=None):
    """Messages qui seraient envoyés à l'IA : classés, jamais corrigés à la main,
    hors masse non importante (regles.selection_ia), pas encore jugés (sauf retout),
    reçus depuis 'jours' jours (0 = tous), du plus récent au plus ancien."""
    params = []
    filtre_date = ""
    if jours and jours > 0:
        filtre_date = " AND date_mail >= ?"
        params.append((datetime.now(timezone.utc) - timedelta(days=jours)).isoformat())
    cond_source = "" if retout else "ia_source IS NULL AND "
    filtre_masse = (" AND (bulk=0 OR important=1)"
                    if SELECTION_IA.get("ignorer_masse_non_importante", True) else "")
    filtre_ids = ""
    if ids:
        filtre_ids = f" AND message_id IN ({','.join('?' * len(ids))})"
        params += list(ids)
    sql = ("SELECT " + ", ".join(COLS_SELECTION_IA) + " FROM messages WHERE " + cond_source
           + "categorie IS NOT NULL AND corrige_at IS NULL"
           + filtre_masse + filtre_date + filtre_ids + " ORDER BY date_mail DESC")
    if limite:
        sql += f" LIMIT {int(limite)}"
    return cx.execute(sql, params).fetchall()


REJUGE_JOURS, REJUGE_MAX = 14, 300


def cmd_analyser_ia(args):
    cfg = charger_config(args.config)
    moteur_version = charger_moteur(cfg)
    cx = connexion(cfg["base_sqlite"])
    _assurer_colonnes(cx)
    cfg_ia = _config_ia(cfg)
    fournisseur = (args.mock and "mock") or cfg_ia.get("fournisseur", "mock")
    if fournisseur != "mock" and not cfg_ia.get("cle") and not getattr(args, "compter", False):
        raise SystemExit("Clé IA absente : mets-la dans la variable d'environnement "
                         "MISTRAL_API_KEY (recommandé, pas en clair dans config.json), "
                         "ou lance avec --mock.")

    exts = charger_extensions(cfg)
    if fournisseur == "mock":       # le bouchon ne connaît que les extensions du compagnon
        exts = [e for e in exts if e.get("nom") in EXTENSIONS_COMPAGNON]
    systeme = systeme_ia(cfg, exts)
    taxo_noms = set(CAT_PAR_CODE)
    aujourdhui = datetime.now().strftime("%Y-%m-%d")
    profils, fils, attente = profils_expediteurs(cx)
    if fournisseur != "mock" and not _utilisateur_txt(cfg) and not getattr(args, "compter", False):
        print(f"[{NOM}] ⚠ profil utilisateur vide : l'IA ne sait pas pour qui elle trie "
              f"(cockpit → ⚙ Réglages, ou clé \"utilisateur\" de la config).", file=sys.stderr)

    jours = args.jours if args.jours is not None else cfg.get("ia_jours", 0)
    cols = COLS_SELECTION_IA
    rows = selection_ia(cx, getattr(args, "retout", False), jours, args.limite,
                        getattr(args, "ids", None))
    if getattr(args, "compter", False):
        print(f"[{NOM}] {len(rows)} message(s) seraient envoyés à l'IA "
               f"({fournisseur}). Aucun appel effectué (--compter).")
        cx.close()
        return

    print(f"[{NOM}] IA ({fournisseur}) sur {len(rows)} message(s)…")
    progres = getattr(args, "progres", None)          # rappel (fait, total) du cockpit
    if progres:
        progres(0, len(rows))

    def repli(row, source):
        """Pas de jugement IA : le tri déterministe tient lieu de filet (jamais enterré).
        'erreur' = réponse rejetée (non rejugée d'office) ; None = panne, on réessaiera."""
        case = deduire_case_topo(row.get("urgence") or "normale", None,
                                 row.get("pour_moi") == 1 or row.get("important") == 1,
                                 aujourdhui)
        cx.execute("UPDATE messages SET ia_source=?, ia_at=?, case_topo=? WHERE message_id=?",
                   (source, datetime.now(timezone.utc).isoformat() if source else None,
                    case, row["message_id"]))

    faits, erreurs, questions, pannes = 0, 0, 0, 0
    t0 = time.time()
    envois = index_envoyes(cx)
    for n_vu, r in enumerate(rows):
        if progres:
            progres(n_vu, len(rows))
        if n_vu:
            cx.commit()           # un message à la fois : jamais de verrou tenu pendant les appels IA
        row = dict(zip(cols, r))
        row["dernier_envoi"] = dernier_envoi(row, envois)   # moteur 2.4 (dossier Envoyés)
        spec = (_adresses_spec(row["de_adresse"] or "") or [""])[0]
        profil = profils.get(spec)
        res, err = None, None
        try:
            if fournisseur == "mock":
                ok, res, err = valider_sortie(ia_bouchon(row, profil), taxo_noms, exts)
            else:
                entree = construire_entree_ia(
                    cfg, row, profil, fils, aujourdhui, attente,
                    _exemples_txt(exemples_pertinents(cx, row)))
                ok = False
                for essai in (1, 2):      # une seconde chance si la réponse est hors contrat
                    try:
                        brut = _extraire_json(appeler_mistral(cfg_ia, systeme, entree))
                    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
                        raise
                    except Exception as e:
                        ok, res, err = False, None, f"réponse illisible ({e})"
                        continue
                    # Validation stricte : une réponse non conforme (ou un mail piégé)
                    # ne pilote jamais le cockpit.
                    ok, res, err = valider_sortie(brut, taxo_noms, exts)
                    if ok:
                        break
            pannes = 0
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            erreurs += 1
            pannes += 1
            repli(row, None)
            if erreurs <= 3:
                print(f"[{NOM}] appel IA échoué : {e}", file=sys.stderr)
            if pannes >= 3:
                print(f"[{NOM}] IA injoignable : arrêt de la passe, le tri par les règles "
                      f"reste affiché ; nouvel essai au prochain cycle.", file=sys.stderr)
                break
            continue
        except Exception as e:
            ok, res, err = False, None, str(e)

        if not ok:
            erreurs += 1
            if erreurs <= 5:
                print(f"[{NOM}] jugement rejeté ({err}) : {row['message_id']}", file=sys.stderr)
            repli(row, "erreur")
            continue

        case = deduire_case_topo(res["urgence"], res["echeance"],
                                 bool(res["action_attendue"]), aujourdhui)
        # Champs des extensions actives, conservés en JSON (rien n'est perdu).
        cles_ext = [k for e in exts for k in e.get("requis", [])]
        ext_vals = {k: res.get(k) for k in cles_ext}
        cx.execute(
            """UPDATE messages SET ia_categorie=?, ia_urgence=?, ia_action=?, ia_tache=?,
               ia_echeance=?, ia_justification=?, case_topo=?, ia_resume=?, ia_question=?,
               ia_confiance=?, ia_extensions=?, ia_source=?, ia_moteur_version=?, ia_at=?
               WHERE message_id=?""",
            (res["categorie"], res["urgence"], int(bool(res["action_attendue"])),
             res["tache"], res["echeance"], res["justification"], case,
             res["resume"], res["question"], res["confiance"],
             json.dumps(ext_vals, ensure_ascii=False) if ext_vals else None,
             fournisseur, moteur_version,
             datetime.now(timezone.utc).isoformat(), row["message_id"]),
        )
        faits += 1
        if res.get("question"):
            questions += 1
    cx.commit()
    if progres:
        progres(len(rows), len(rows))
    print(f"[{NOM}] jugés : {faits}, questions soulevées : {questions}, "
          f"erreurs : {erreurs}, en {time.time()-t0:.0f}s")
    args.bilan = {"juges": faits, "questions": questions, "erreurs": erreurs}
    cx.close()


def cmd_reset_ia(args):
    """Efface les jugements IA pour pouvoir rejuger proprement."""
    cfg = charger_config(args.config)
    cx = connexion(cfg["base_sqlite"])
    _assurer_colonnes(cx)
    cols = [c for c in COLONNES_ANALYSE if c.startswith("ia_")]
    cx.execute("UPDATE messages SET " + ", ".join(f"{c}=NULL" for c in cols)
               + ", case_topo=NULL WHERE corrige_at IS NULL")
    cx.commit()
    print(f"[{NOM}] jugements IA effacés. Relance 'analyser-ia'.")
    cx.close()


def cmd_extraire_corps(args):
    """Rattrape l'extrait du corps pour les mails déjà archivés, en relisant leur
    .eml (lecture seule ; ne touche pas la boîte). À lancer une fois après mise à jour."""
    cfg = charger_config(args.config)
    cx = connexion(cfg["base_sqlite"])
    _assurer_colonnes(cx)
    maxlen = _extrait_max(cfg)
    cond = "extrait IS NULL OR extrait=''"
    if getattr(args, "retout", False):
        cond = "1=1"
    rows = cx.execute(
        f"SELECT message_id, fichier_eml FROM messages WHERE {cond}").fetchall()
    total = len(rows)
    print(f"[{NOM}] extraction du corps sur {total} message(s)…")
    faits, vides, erreurs = 0, 0, 0
    t0 = time.time()
    for i, (mid, chemin) in enumerate(rows, 1):
        try:
            if not chemin or not Path(chemin).exists():
                erreurs += 1
                continue
            with open(chemin, "rb") as f:
                msg = email.message_from_binary_file(f)
            extrait = _corps_texte(msg, maxlen)
            cx.execute("UPDATE messages SET extrait=? WHERE message_id=?", (extrait, mid))
            if extrait:
                faits += 1
            else:
                vides += 1
        except Exception:
            erreurs += 1
        if i % 500 == 0:
            cx.commit()
            print(f"[{NOM}] … {i}/{total} ({time.time()-t0:.0f}s)")
    cx.commit()
    cx.close()
    print(f"[{NOM}] extraits remplis : {faits}, vides : {vides}, "
          f"erreurs : {erreurs}, en {time.time()-t0:.0f}s")


def cmd_brief(args):
    """Topo du matin en cases (jugement IA, repli sur le déterministe), case du JOUR."""
    cfg = charger_config(args.config)
    charger_moteur(cfg)
    cx = connexion(cfg["base_sqlite"])
    _assurer_colonnes(cx)
    t = elements_topo(cx, cfg)
    titres = {"maintenant": "MAINTENANT", "cette_semaine": "CETTE SEMAINE",
              "ce_mois": "CE MOIS — À NE PAS OUBLIER"}
    print("=== Topo du matin ===\n")
    for ev in t["evenements"]:
        print(f"  📅 {ev['quand']} — {ev['quoi']} ({ev['nom']})")
    for cle, titre in titres.items():
        els = [e for e in t["elements"] if e["case"] == cle][:args.nombre]
        if not els:
            continue
        print(f"\n— {titre} —")
        for e in els:
            ligne = f"  • {(e['tache'] or e['resume'] or e['sujet'] or '(sans objet)')[:70]}"
            if e["echeance"]:
                ligne += f"  [échéance {e['echeance']}{' — EN RETARD' if e['retard'] else ''}]"
            print(ligne)
            print(f"      {e['nom'][:40]} — urgence {e['urgence']}"
                  + ("" if e["juge"] else " (tri par les règles, IA en attente)"))
            if e["question"]:
                print(f"      ❓ {e['question'][:90]}")
    if t["anciens"]:
        print(f"\n({t['anciens']} sujet(s) plus ancien(s) encore ouvert(s) — voir le cockpit.)")
    manque = cx.execute("SELECT COUNT(*) FROM messages WHERE ia_source IS NULL AND (bulk=0 OR important=1) AND categorie IS NOT NULL").fetchone()[0]
    if manque:
        print(f"\n({manque} message(s) pas encore jugés par l'IA — lance 'analyser-ia'.)")
    cx.close()


# ----------------------------------------------------------------------------
# Le cockpit — fenêtre de tri (serveur web local)
# ----------------------------------------------------------------------------
import http.server
import subprocess
import threading
import types
import webbrowser


def _ouvrir_fichier(chemin):
    """Ouvre un fichier avec l'application par défaut de l'OS (le client mail pour un .eml)."""
    if sys.platform.startswith("darwin"):
        subprocess.Popen(["open", chemin])
    elif os.name == "nt":
        os.startfile(chemin)  # noqa
    else:
        subprocess.Popen(["xdg-open", chemin])


def backfill_non_lu(cx):
    """Lit le statut lu/non-lu depuis l'archive (en-tête X-Mozilla-Status de Thunderbird)."""
    from email.parser import BytesHeaderParser
    rows = cx.execute("SELECT message_id, fichier_eml FROM messages "
                      "WHERE non_lu IS NULL AND fichier_eml IS NOT NULL").fetchall()
    n = 0
    for mid, chemin in rows:
        try:
            with open(chemin, "rb") as f:
                msg = BytesHeaderParser().parse(f)
            st = msg.get("X-Mozilla-Status")
            if st:
                nl = 0 if (int(st, 16) & 1) else 1
                cx.execute("UPDATE messages SET non_lu=? WHERE message_id=?", (nl, mid))
                n += 1
        except Exception:
            pass
    cx.commit()
    return n


CASES_COCKPIT = [("agenda", "Agenda — aujourd'hui et demain"),
                 ("attente", "Attend ta réponse"),
                 ("maintenant", "Maintenant"), ("cette_semaine", "Cette semaine"),
                 ("ce_mois", "Ce mois — à ne pas oublier"), ("aucune", "Pour info")]


_SQL_MASSE = ("(COALESCE(bulk,0)=1 AND COALESCE(important,0)=0 "
              "AND (ia_source IS NULL OR ia_source='erreur'))")


def _ext_json(v):
    try:
        d = json.loads(v) if isinstance(v, str) and v else (v or {})
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


COCKPIT_JOURS = 7          # « À traiter » : les derniers jours…
ATTENTE_JOURS = 14         # … plus les demandes restées sans réponse de cette période
ATTENTE_APRES_H = 20       # une demande devient « attend ta réponse » après ce délai


def cockpit_liste(db, vue="atraiter", non_lus=False, depuis=None, infos=None, jours=None):
    """Tout message classé reste accessible (zéro perte) : jugé par l'IA, ou — IA en panne,
    réponse rejetée, pas encore passée — rangé par le tri déterministe. Le courrier de
    masse non important a sa propre vue. La case est recalculée à la date du jour.

    « À traiter » (sans date choisie) montre l'ACTUALITÉ : les messages des derniers jours
    (COCKPIT_JOURS), les demandes restées sans réponse (ATTENTE_JOURS) et les échéances à
    venir ; un message auquel l'utilisateur a répondu ensuite (dossier Envoyés, quel que
    soit l'ordinateur d'où il a répondu) en sort. Le reste se retrouve en choisissant une
    date (« depuis »). 'infos' reçoit les nombres masqués."""
    infos = infos if infos is not None else {}
    actualite = vue == "atraiter" and not depuis
    cx = _ouvrir_db(db)
    profils, _, _ = profils_expediteurs(cx)   # mémoire : fréquence par expéditeur
    conds = ["categorie IS NOT NULL"]
    params = []
    if vue == "reporte":
        conds.append("etat='reporte'")
    elif vue == "masse":
        conds += ["etat IS NULL", _SQL_MASSE]
    else:
        conds += ["etat IS NULL", "NOT " + _SQL_MASSE]
        if depuis:
            conds.append("date_mail >= ?")
            params.append(depuis)
        elif non_lus:
            conds.append("(non_lu=1 OR non_lu IS NULL)")
        if actualite:
            maintenant_utc = datetime.now(timezone.utc)
            fen = (maintenant_utc - timedelta(days=jours or COCKPIT_JOURS)).isoformat()
            att = (maintenant_utc - timedelta(days=ATTENTE_JOURS)).isoformat()
            ech = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
            filtre = ("(date_mail >= ? OR (ia_action=1 AND date_mail >= ?) OR "
                      "(ia_echeance IS NOT NULL AND ia_echeance != 'aucune' AND ia_echeance >= ?))")
            infos["masques_anciens"] = cx.execute(
                f"SELECT COUNT(*) FROM messages WHERE {' AND '.join(conds)} AND NOT {filtre}",
                params + [fen, att, ech]).fetchone()[0]
            conds.append(filtre)
            params += [fen, att, ech]
    cols = ["id", "de", "sujet", "date", "resume", "tache", "echeance", "ia_urgence",
            "question", "non_lu", "ia_source", "urgence", "pour_moi", "important",
            "ia_action", "justification", "ia_extensions", "raison", "corrige_at",
            "precision"]
    rows = cx.execute(
        f"""SELECT message_id, de_adresse, sujet, date_mail, ia_resume, ia_tache,
            ia_echeance, ia_urgence, ia_question, non_lu, ia_source, urgence, pour_moi,
            important, ia_action, ia_justification, ia_extensions, raison, corrige_at,
            precision
            FROM messages WHERE {' AND '.join(conds)} ORDER BY date_mail DESC
            {'LIMIT 500' if vue == 'masse' else ''}""",
        params).fetchall()
    envois = index_envoyes(cx)                # dossier Envoyés : « tu as répondu »
    cx.close()
    aujourdhui = datetime.now().strftime("%Y-%m-%d")
    demain = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    _urg_prio = {"critique": "haute", "haute": "haute", "normale": "normale", "basse": "basse"}
    par_case = {c: [] for c, _ in CASES_COCKPIT}
    infos["masques_repondus"] = 0
    seuil_attente = datetime.now(timezone.utc) - timedelta(hours=ATTENTE_APRES_H)
    for r in rows:
        it = dict(zip(cols, r))
        juge = _juge_par_ia(it["ia_source"]) and bool(it["ia_urgence"])
        it["case_topo"] = ("aucune" if vue == "masse" else
                           case_du_jour({**it, "ia_echeance": it["echeance"]}, aujourdhui))
        it["urgence"] = it["ia_urgence"] if juge else (it["urgence"] or "normale")
        # D'où vient le classement : pour ne jamais faire passer une estimation pour un jugement.
        it["source"] = ("humain" if it["corrige_at"] else "ia" if juge else
                        "erreur" if it["ia_source"] == "erreur" else
                        "regles" if vue == "masse" else "attente")
        if not juge:
            it["resume"], it["tache"], it["question"], it["echeance"] = None, None, None, None
            it["justification"] = it["raison"]
        ext = _ext_json(it.pop("ia_extensions")) if juge else {}
        it.pop("ia_extensions", None)
        it["concerne"] = ext.get("concerne")
        it["evenement"] = ext.get("evenement") if isinstance(ext.get("evenement"), dict) else None
        ech = it["echeance"] if it["echeance"] not in (None, "", "aucune") else None
        it["retard"] = bool(ech and ech < aujourdhui)
        # Un événement d'aujourd'hui ou de demain ne reste pas enfoui dans « pour info »
        # sous prétexte qu'il n'appelle aucune action.
        ev = it["evenement"]
        if (it["case_topo"] == "aucune" and vue == "atraiter" and ev
                and ev.get("date") in (aujourdhui, demain) and not ev.get("annule")):
            it["case_topo"] = "agenda"
        d = _local(_dt_message(it["date"]))
        it["date_txt"] = d.strftime("%d/%m %H:%M") if d else ""
        # Une réponse de l'utilisateur APRÈS ce message : proposé comme traité, jamais
        # classé tout seul (il peut avoir répondu « je regarde et je reviens »).
        rep_ = repondu_apres({"sujet": it["sujet"], "de_adresse": it["de"], "date_mail": it["date"]}, envois)
        dr = _local(_dt_message(rep_)) if rep_ else None
        it["repondu"] = dr.strftime("%d/%m %H:%M") if dr else None
        if actualite and rep_:
            infos["masques_repondus"] += 1     # tu as répondu après : c'est réglé
            continue
        # Une demande qui t'est adressée, restée sans réponse : elle remonte, qu'elle soit
        # lue ou non (lu ≠ traité).
        dm = _dt_message(it["date"])
        if dm is not None and dm.tzinfo is None:
            dm = dm.replace(tzinfo=timezone.utc)
        if (vue == "atraiter" and juge and it["ia_action"] == 1 and not rep_
                and it["concerne"] in (None, "moi") and dm is not None and dm < seuil_attente
                and it["case_topo"] not in ("agenda",)):
            it["case_topo"] = "attente"
            it["attente_depuis"] = max(1, (datetime.now(timezone.utc) - dm).days)
        spec = (_adresses_spec(it["de"] or "") or [""])[0]
        p = profils.get(spec)
        it["fil"] = _sujet_fil(it["sujet"])          # regroupement par conversation
        it["priorite"] = _urg_prio.get(it["urgence"], "normale")   # couleur du cockpit
        it["freq"] = p["n"] if p else 0              # combien de mails de cet expéditeur
        it["actif"] = bool(p and p["n"] >= PARAMS["profil_actif_n_min"]
                           and p.get("recent", 0) >= PARAMS["profil_actif_recent_min"])
        for k in ("ia_urgence", "raison", "ia_action", "pour_moi", "important"):
            it.pop(k, None)
        par_case.get(it["case_topo"] or "aucune", par_case["aucune"]).append(it)
    for items in par_case.values():        # le plus urgent d'abord, puis le plus récent
        items.sort(key=lambda it: NIVEAU_RANG.get(it["urgence"], 2))
    return [{"cle": c, "titre": t, "items": par_case[c]} for c, t in CASES_COCKPIT]


def corriger_jugement(cx, mid, quoi):
    """Correction faite dans le cockpit : ▲ plus urgent, ▼ moins urgent, « pas pour moi »,
    « c'est pour moi ». Appliquée tout de suite, jamais rejugée d'office, et gardée
    comme jurisprudence : les prochains messages semblables la reçoivent en exemple."""
    r = cx.execute(
        "SELECT de_adresse, sujet, extrait, categorie, urgence, pour_moi, important, "
        "ia_source, ia_categorie, ia_urgence, ia_action, ia_extensions "
        "FROM messages WHERE message_id=?", (mid,)).fetchone()
    if not r:
        return False
    (de, sujet, extrait, cat_r, urg_r, pour_moi, important,
     src, cat_ia, urg_ia, action_ia, ext_json) = r
    juge = _juge_par_ia(src) and bool(urg_ia)
    urg = urg_ia if juge else (urg_r or "normale")
    action = bool(action_ia) if juge else (pour_moi == 1 or important == 1)
    cat = (cat_ia if juge else cat_r) or "autre"
    ext = _ext_json(ext_json)
    avant = {"urgence": urg, "action_attendue": action, "concerne": ext.get("concerne"),
             "source": src}
    rang = NIVEAU_RANG.get(urg, 2)
    if quoi == "plus":
        urg, action = _rang_vers_code(max(0, rang - 1)), True
    elif quoi == "moins":
        urg = _rang_vers_code(min(3, rang + 1))
    elif quoi == "pas_pour_moi":
        action, ext["concerne"] = False, "autre"
    elif quoi == "pour_moi":
        action, ext["concerne"] = True, "moi"
    else:
        return False
    ext.setdefault("evenement", None)
    ext.setdefault("concerne", "moi" if action else "autre")
    maintenant = datetime.now(timezone.utc).isoformat()
    ech = cx.execute("SELECT ia_echeance FROM messages WHERE message_id=?", (mid,)).fetchone()[0]
    case = deduire_case_topo(urg, ech, action, datetime.now().strftime("%Y-%m-%d"))
    cx.execute(
        "UPDATE messages SET ia_urgence=?, ia_action=?, ia_categorie=?, "
        "ia_echeance=COALESCE(ia_echeance,'aucune'), ia_extensions=?, case_topo=?, "
        "ia_source=?, corrige_at=? WHERE message_id=?",
        (urg, int(action), cat, json.dumps(ext, ensure_ascii=False), case,
         src if juge else "humain", maintenant, mid))
    spec = (_adresses_spec(de or "") or [""])[0]
    dom = _domaine(spec)
    cx.execute(
        "INSERT INTO corrections (message_id, de_adresse, domaine, sujet, mots, extrait, "
        "categorie, urgence, action_attendue, concerne, avant, at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (mid, spec, dom if dom not in FREEMAIL else "", sujet, _mots_cles(sujet),
         re.sub(r"\s+", " ", (extrait or ""))[:200], cat, urg, int(action),
         ext.get("concerne"), json.dumps(avant, ensure_ascii=False), maintenant))
    cx.commit()
    return True


def preciser(cx, mid, texte):
    """Réponse de l'utilisateur à la question de l'IA (moteur 2.4). Gardée sur le message,
    retenue comme exemple pour les messages semblables. Renvoie True si le message doit
    être rejugé avec elle : jamais un jugement corrigé à la main."""
    t = precision_txt(texte)
    if not t:
        return False
    r = cx.execute(
        "SELECT de_adresse, sujet, extrait, categorie, urgence, pour_moi, important, "
        "ia_source, ia_categorie, ia_urgence, ia_action, ia_extensions, corrige_at "
        "FROM messages WHERE message_id=?", (mid,)).fetchone()
    if not r:
        return False
    (de, sujet, extrait, cat_r, urg_r, pour_moi, important,
     src, cat_ia, urg_ia, action_ia, ext_json, corrige_at) = r
    maintenant = datetime.now(timezone.utc).isoformat()
    cx.execute("UPDATE messages SET precision=?, precision_at=? WHERE message_id=?",
               (t, maintenant, mid))
    juge = _juge_par_ia(src) and bool(urg_ia)
    spec = (_adresses_spec(de or "") or [""])[0]
    dom = _domaine(spec)
    ext = _ext_json(ext_json)
    cx.execute(
        "INSERT INTO corrections (message_id, de_adresse, domaine, sujet, mots, extrait, "
        "categorie, urgence, action_attendue, concerne, avant, at, precision) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (mid, spec, dom if dom not in FREEMAIL else "", sujet, _mots_cles(sujet),
         re.sub(r"\s+", " ", (extrait or ""))[:200],
         (cat_ia if juge else cat_r) or "autre", (urg_ia if juge else urg_r) or "normale",
         int(bool(action_ia)) if juge else int(pour_moi == 1 or important == 1),
         ext.get("concerne"), json.dumps({"origine": "precision"}), maintenant, t))
    cx.commit()
    return corrige_at is None


def enregistrer_reglages(cfg, donnees):
    """Écrit le profil utilisateur et les adresses dans le fichier de configuration
    (écriture atomique) et met à jour la config en mémoire. Renvoie True si
    'mes_adresses' a changé (le tri déterministe « à moi / en copie » est à refaire)."""
    chemin = Path(cfg.get("_chemin_config") or "config.json")
    brut = json.loads(chemin.read_text(encoding="utf-8")) if chemin.exists() else {}
    u = donnees.get("utilisateur") or {}
    util = {k: str(u.get(k) or "").strip()[:(1500 if k == "profil" else 120)]
            for k in ("nom", "fonction", "profil")}
    adresses = []
    for a in (donnees.get("mes_adresses") or []):
        a = str(a or "").strip().lower()
        if "@" in a and a not in adresses:
            adresses.append(a)
    change = sorted(adresses) != sorted(a.lower() for a in (cfg.get("mes_adresses") or []))
    ancien = cfg.get("utilisateur") or {}
    boite = ancien.get("boite") if isinstance(ancien, dict) else None
    if boite:
        util["boite"] = boite             # description de la boîte : config seulement
    if bloc_destinataire(**util) != _utilisateur_txt(cfg):
        # Enregistrer n'appelle jamais l'IA : on propose seulement de rejuger.
        brut["profil_a_rejuger"] = cfg["profil_a_rejuger"] = True
    brut["utilisateur"], brut["mes_adresses"] = util, adresses
    tmp = chemin.with_suffix(chemin.suffix + ".tmp")
    tmp.write_text(json.dumps(brut, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, chemin)
    cfg["utilisateur"], cfg["mes_adresses"] = util, adresses
    return change


def _effacer_profil_a_rejuger(cfg):
    cfg["profil_a_rejuger"] = False
    chemin = Path(cfg.get("_chemin_config") or "")
    if chemin.is_file():
        brut = json.loads(chemin.read_text(encoding="utf-8"))
        brut.pop("profil_a_rejuger", None)
        tmp = chemin.with_suffix(chemin.suffix + ".tmp")
        tmp.write_text(json.dumps(brut, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, chemin)


# ── Santé des sources Thunderbird ─────────────────────────────────────────────
def _date_dernier_message(chemin):
    """Date (ISO) du dernier message d'un mbox, en ne lisant que la fin du fichier
    (de plus en plus loin si le dernier message est gros : pièces jointes)."""
    for taille_lue in (512 * 1024, 8 * 1024 * 1024, 64 * 1024 * 1024):
        try:
            with open(chemin, "rb") as f:
                f.seek(0, 2)
                fin = f.tell()
                f.seek(max(0, fin - taille_lue))
                bout = f.read()
        except OSError:
            return None
        i = bout.rfind(b"\nFrom ")
        if i < 0 and bout.startswith(b"From "):
            i = -1
        elif i < 0:
            if fin <= taille_lue:
                return None
            continue
        entetes = bout[i + 1:].split(b"\n\n", 1)[0].split(b"\r\n\r\n", 1)[0]
        m = re.search(rb"^Date:[ \t]*(.+)$", entetes, re.M | re.I)
        d = _dt_message(m.group(1).decode("latin-1").strip()) if m else None
        return d.isoformat() if d else None
    return None


def _profils_thunderbird():
    """Dossiers de profils Thunderbird de ce poste (classique, Flatpak, Snap)."""
    h = Path.home()
    bases = [h / ".thunderbird", h / "snap" / "thunderbird" / "common" / ".thunderbird"]
    bases += list((h / ".var" / "app").glob("*/.thunderbird"))
    if sys.platform == "darwin":
        bases.append(h / "Library" / "Thunderbird" / "Profiles")
    if os.name == "nt" and os.environ.get("APPDATA"):
        bases.append(Path(os.environ["APPDATA"]) / "Thunderbird" / "Profiles")
    return [b for b in bases if b.is_dir()]


def candidats_boite_reception():
    """Fichiers « boîte de réception » trouvés dans tous les profils Thunderbird."""
    out = []
    for base in _profils_thunderbird():
        for motif in ("*/ImapMail/*/INBOX", "*/Mail/*/Inbox"):
            for f in base.glob(motif):
                if f.is_file():
                    out.append(f)
    return out


def etat_sources(cfg):
    """Ce que le compagnon lit vraiment (diagnostic + alertes) : pour chaque source, la
    date de modification et la date du dernier message ; et les autres boîtes de
    réception Thunderbird plus récentes que celles configurées."""
    def info(f, role):
        f = Path(f)
        d = {"chemin": str(f), "role": role, "existe": f.is_file()}
        if d["existe"]:
            st = f.stat()
            d["taille"] = st.st_size
            d["modifie"] = datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()
            d["dernier_message"] = _date_dernier_message(f)
        return d
    if _collecteur(cfg) != "thunderbird":
        return {"sources": [], "envoyes": [], "autres": []}
    sources = [info(f, "reception") for f in cfg.get("mbox_sources") or []]
    envoyes = [info(f, "envoyes") for f in envois_depuis_mbox(cfg)]
    plus_recent = max([Path(x["chemin"]).stat().st_mtime for x in sources if x["existe"]] or [0])
    connus = {str(Path(x["chemin"]).resolve()) for x in sources if x["existe"]}
    autres = []
    for f in candidats_boite_reception():
        try:
            if str(f.resolve()) not in connus and f.stat().st_mtime > plus_recent + 3600:
                autres.append(info(f, "autre"))
        except OSError:
            pass
    autres.sort(key=lambda x: x.get("modifie") or "", reverse=True)
    return {"sources": sources, "envoyes": envoyes, "autres": autres[:5]}


def alertes_sources(cfg, maintenant=None):
    """Bandeau du cockpit : la boîte lue est-elle vivante ? les Envoyés sont-ils trouvés ?"""
    try:
        e = etat_sources(cfg)
    except Exception:
        return []
    if _collecteur(cfg) != "thunderbird" or not (cfg.get("mbox_sources") or []):
        return []
    now = maintenant or datetime.now(timezone.utc)
    out = []
    for x in e["sources"]:
        if not x["existe"]:
            out.append(f"Boîte Thunderbird introuvable : {x['chemin']} — vérifie « mbox_sources » "
                       f"dans la configuration (commande : iamail diagnostic).")
            continue
        vu = _dt_message(x["modifie"])
        if vu and (now - vu) > timedelta(hours=48):
            out.append(f"La boîte lue n'a plus changé depuis le {_local(vu).strftime('%d/%m à %H:%M')} : "
                       f"les nouveaux mails n'arrivent pas ici. Thunderbird garde-t-il les messages "
                       f"de ce compte sur l'ordinateur (Paramètres du compte → Synchronisation) ?")
    if e["autres"]:
        a = e["autres"][0]
        out.append(f"Une autre boîte de réception Thunderbird est plus récente que celle lue : "
                   f"{a['chemin']} — à mettre dans « mbox_sources » ?")
    if not e["envoyes"]:
        out.append("Dossier Envoyés introuvable : je ne peux pas savoir à quoi tu as déjà répondu "
                   "(« envoyes_sources » dans la configuration ; commande : iamail diagnostic).")
    return out


def alertes_reglages(cfg):
    """Ce qui, dans les réglages, dégrade la pertinence — affiché en bandeau au cockpit."""
    out = []
    if not (cfg.get("mes_adresses") or []):
        out.append("Tes adresses ne sont pas renseignées : impossible de distinguer "
                   "« à moi » de « en copie », tout est plafonné à « normale ».")
    if not _utilisateur_txt(cfg):
        out.append("Ta fonction n'est pas décrite : l'IA ne sait pas pour qui elle trie.")
    elif cfg.get("profil_a_rejuger"):
        out.append("Ton profil a changé : les messages déjà jugés l'ont été avec l'ancien. "
                   "Rejuge les derniers jours depuis les réglages.")
    cfg_ia = _config_ia(cfg)
    if cfg_ia.get("fournisseur", "mock") == "mock":
        out.append("IA en mode bouchon (« mock ») : aucun vrai jugement, résumés = objets des mails.")
    elif not cfg_ia.get("cle"):
        out.append("Clé IA absente (MISTRAL_API_KEY) : le tri repose sur les seules règles.")
    am = sys.modules.get("collecteur_applemail")
    if _collecteur(cfg) == "applemail" and am is not None and am.DERNIERE_ERREUR:
        out.append("Apple Mail : " + am.DERNIERE_ERREUR)
    out += alertes_sources(cfg)
    return out


def alertes_bloquantes(cfg):
    """Ce qui empêche un tri utile : le cockpit ouvre directement les réglages."""
    out = []
    if not (cfg.get("mes_adresses") or []):
        out.append("tes adresses (sans elles, impossible de savoir ce qui t'est adressé)")
    if _config_ia(cfg).get("fournisseur", "mock") != "mock" and not _utilisateur_txt(cfg):
        out.append("ta fonction (sans elle, l'IA ne sait pas pour qui elle trie)")
    return out


# Étiquette partagée appliquée dans Thunderbird quand un mail est « Traité »
# dans le cockpit (retour-mail). La clé est l'identifiant stable côté Thunderbird.
ETIQUETTE_TRAITE = {"cle": "iamail_traite", "nom": "Traité IAMail", "couleur": "#12b76a"}


# ----------------------------------------------------------------------------
# Relié à GesEnt (extension Thunderbird, protocole v1) : GesEnt fait foi
# ----------------------------------------------------------------------------
def _chemin_relie(cfg):
    return Path(cfg["base_sqlite"]).with_name("relie.json")


def lire_relie(cfg):
    """État « relié à GesEnt » annoncé par l'extension : {relie, gesent, boites, le}.
    Relié : le cockpit local est en lecture seule et le compagnon n'appelle plus l'IA
    (GesEnt juge ; pas de double coût)."""
    try:
        d = json.loads(_chemin_relie(cfg).read_text(encoding="utf-8"))
        if isinstance(d, dict):
            return {"relie": bool(d.get("relie")), "gesent": d.get("gesent") or None,
                    "boites": [str(b) for b in (d.get("boites") or [])][:50], "le": d.get("le")}
    except Exception:
        pass
    return {"relie": False, "gesent": None, "boites": [], "le": None}


def enregistrer_relie(cfg, corps):
    gesent = corps.get("gesent")
    if gesent is not None and not (isinstance(gesent, str)
                                   and re.match(r"^https?://[^\s<>\"']{1,200}$", gesent)):
        gesent = None
    d = {"relie": bool(corps.get("relie")), "gesent": gesent if corps.get("relie") else None,
         "boites": [str(b)[:200] for b in (corps.get("boites") or []) if isinstance(b, str)][:50]
         if corps.get("relie") else [],
         "le": datetime.now(timezone.utc).isoformat()}
    try:
        _chemin_relie(cfg).write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[{NOM}] relie.json : {e}", file=sys.stderr)
    return d


def faire_cockpit_handler(cfg):
    db = cfg["base_sqlite"]
    etat_audio = {"chemin": None}
    etat_rejuge = {"en_cours": False, "fait": 0, "total": 0, "debut": None, "fin": None,
                   "bilan": None, "erreur": None}
    etat_relie = lire_relie(cfg)

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _origine_permise(self):
            """Origine autorisée à LIRE les réponses : l'extension Thunderbird
            (moz-extension://…). Le cockpit lui-même est en même origine."""
            o = (self.headers.get("Origin") or "").strip().lower()
            return o if o.startswith("moz-extension://") else None

        def _autorise(self):
            """Le serveur n'écoute que sur 127.0.0.1, mais n'importe quelle page web
            ouverte sur le poste peut tenter d'y envoyer des requêtes. On n'accepte que
            le cockpit (même origine) et l'extension : Host local (anti DNS-rebinding),
            et, si le navigateur annonce une origine, qu'elle soit l'une des nôtres."""
            port = self.server.server_address[1]
            if (self.headers.get("Host") or "").strip().lower() not in (
                    f"127.0.0.1:{port}", f"localhost:{port}"):
                return False
            o = (self.headers.get("Origin") or "").strip().lower()
            if not o:
                return True
            return (o in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")
                    or o.startswith("moz-extension://"))

        def _refuse(self):
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _cors(self):
            o = self._origine_permise()
            if o:
                self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin"))
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")

        def _json(self, obj, code=200):
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self._cors()
            self.end_headers()
            self.wfile.write(data)

        def do_OPTIONS(self):
            if not self._autorise():
                return self._refuse()
            self.send_response(204)
            self._cors()
            self.end_headers()

        def _corps(self):
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

        def do_GET(self):
            from urllib.parse import urlparse, parse_qs
            u = urlparse(self.path)
            if not self._autorise():
                return self._refuse()
            if u.path == "/":
                page = PAGE_COCKPIT.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
            elif u.path == "/api/liste":
                q = parse_qs(u.query)
                vue = q.get("vue", ["atraiter"])[0]
                non_lus = q.get("nonlus", ["0"])[0] == "1"
                depuis = (q.get("depuis", [""])[0] or "").strip() or None
                infos = {}
                cases = cockpit_liste(db, vue, non_lus, depuis, infos,
                                      jours=int(cfg.get("cockpit_jours") or COCKPIT_JOURS))
                self._json({"cases": cases, **infos,
                            "jours": int(cfg.get("cockpit_jours") or COCKPIT_JOURS)})
            elif u.path == "/api/reglages":
                cfg_ia = _config_ia(cfg)
                self._json({"utilisateur": cfg.get("utilisateur") or {},
                            "mes_adresses": cfg.get("mes_adresses") or [],
                            "alertes": alertes_reglages(cfg),
                            "ia": {"fournisseur": cfg_ia.get("fournisseur", "mock"),
                                   "modele": cfg_ia.get("modele", "")},
                            "rejuge_en_cours": etat_rejuge["en_cours"],
                            "rejuge": dict(etat_rejuge),
                            "bloquants": alertes_bloquantes(cfg),
                            "a_rejuger": bool(cfg.get("profil_a_rejuger")),
                            "relie": etat_relie})
            elif u.path == "/audio":
                ch = etat_audio["chemin"]
                if ch and Path(ch).exists():
                    data = Path(ch).read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "audio/mpeg" if ch.endswith(".mp3") else "audio/wav")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self._json({"erreur": "pas d'audio"}, 404)
            elif u.path == "/api/relie":
                self._json(etat_relie)
            elif u.path == "/api/progression":
                self._json({"rejuge": dict(etat_rejuge)})
            elif u.path == "/api/sync/pour-client" and etat_relie["relie"]:
                # Relié : c'est GesEnt qui pose l'étiquette, plus le cockpit local.
                self._json({"etiquette": ETIQUETTE_TRAITE, "a_marquer": [], "a_demarquer": []})
            elif u.path == "/api/sync/pour-client":
                # Ce que l'extension Thunderbird doit APPLIQUER dans la boîte.
                cx = _ouvrir_db(db)
                a_marquer = [r[0] for r in cx.execute(
                    "SELECT message_id FROM messages WHERE etat='traite' "
                    "AND (tb_sync IS NULL OR tb_sync!='traite') "
                    "AND message_id NOT LIKE 'sansid:%' "
                    "AND message_id NOT LIKE 'synth-%'").fetchall()]
                a_demarquer = [r[0] for r in cx.execute(
                    "SELECT message_id FROM messages WHERE tb_sync='traite' "
                    "AND (etat IS NULL OR etat!='traite') "
                    "AND message_id NOT LIKE 'sansid:%' "
                    "AND message_id NOT LIKE 'synth-%'").fetchall()]
                cx.close()
                self._json({"etiquette": ETIQUETTE_TRAITE,
                            "a_marquer": a_marquer, "a_demarquer": a_demarquer})
            else:
                self._json({"erreur": "inconnu"}, 404)

        def do_POST(self):
            if not self._autorise():
                return self._refuse()
            try:
                c = self._corps()
            except Exception:
                return self._json({"erreur": "JSON attendu"}, 400)
            if not isinstance(c, dict):
                return self._json({"erreur": "objet JSON attendu"}, 400)
            mid = c.get("id")
            if self.path == "/api/relie":
                etat_relie.clear()
                etat_relie.update(enregistrer_relie(cfg, c))
                return self._json({"ok": True, **etat_relie})
            ecritures = ("/api/preciser", "/api/corriger", "/api/reglages", "/api/action",
                         "/api/sync/confirme", "/api/sync/depuis-client")
            if etat_relie["relie"] and (self.path in ecritures
                                        or (self.path == "/api/rejuger" and not c.get("compter"))):
                # Relié à GesEnt : GesEnt fait foi, le cockpit local est en lecture seule.
                return self._json({"ok": False, "erreur": "relie", "gesent": etat_relie["gesent"],
                                   "message": "Ce poste est relié à GesEnt : agissez dans GesEnt."}, 409)
            if self.path == "/api/topo-audio":
                # POST (et non GET) : la génération peut appeler l'IA, donc coûter.
                try:
                    court, complet, source = construire_script_topo(
                        cfg, utiliser_ia=not c.get("sans_ia"))
                except Exception as e:
                    print(f"[{NOM}] topo : {e}", file=sys.stderr)
                    return self._json({"ok": False, "erreur": "topo", "message":
                                       "Le topo n'a pas pu être préparé (" + str(e) + "). "
                                       "Réessaie dans un instant."}, 500)
                sortie = str(Path(cfg.get("dossier_destination") or ".") / "topo.wav")
                chemin, err = generer_audio(complet, cfg, sortie)
                etat_audio["chemin"] = chemin
                self._json({"ok": bool(chemin), "court": court, "complet": complet,
                            "source": source,
                            "url": ("/audio?t=" + str(int(time.time()))) if chemin else None,
                            "erreur": err})
            elif self.path == "/api/preciser":
                # Réponse à la question de l'IA : gardée, servie en exemple, et le message
                # est rejugé avec elle (un seul appel IA) — sauf jugement corrigé à la main.
                cx = _ouvrir_db(db)
                rejuger = preciser(cx, mid, c.get("texte"))
                cx.close()
                cfg_ia = _config_ia(cfg)
                fournisseur = cfg_ia.get("fournisseur", "mock")
                rejuger = rejuger and cfg.get("_chemin_config") and (
                    fournisseur == "mock" or bool(cfg_ia.get("cle")))
                if rejuger:
                    threading.Thread(target=lambda: cmd_analyser_ia(types.SimpleNamespace(
                        config=cfg["_chemin_config"], mock=fournisseur == "mock", retout=True,
                        limite=1, jours=0, compter=False, ids=[mid])), daemon=True).start()
                self._json({"ok": True, "rejuge": bool(rejuger)})
            elif self.path == "/api/corriger":
                cx = _ouvrir_db(db)
                ok = corriger_jugement(cx, mid, c.get("quoi"))
                cx.close()
                self._json({"ok": ok}, 200 if ok else 400)
            elif self.path == "/api/reglages":
                change = enregistrer_reglages(cfg, c)
                if change and cfg.get("_chemin_config"):
                    # « à moi / en copie » dépend des adresses : on refait le tri par
                    # les règles (local, gratuit) en arrière-plan.
                    threading.Thread(target=lambda: cmd_analyser(types.SimpleNamespace(
                        config=cfg["_chemin_config"], retout=True, silencieux=True)),
                        daemon=True).start()
                self._json({"ok": True, "alertes": alertes_reglages(cfg)})
            elif self.path == "/api/rejuger":
                # Rejuge les derniers jours avec le profil courant : proposé, jamais
                # automatique (appels IA payants, bornés ; corrections humaines exclues).
                jours = max(1, min(60, int(c.get("jours") or REJUGE_JOURS)))
                if c.get("compter"):
                    cx = _ouvrir_db(db)
                    n = len(selection_ia(cx, True, jours, REJUGE_MAX))
                    cx.close()
                    return self._json({"ok": True, "jours": jours, "n": n, "max": REJUGE_MAX})
                if etat_rejuge["en_cours"]:
                    return self._json({"ok": False, "erreur": "en_cours", "rejuge": dict(etat_rejuge),
                                       "message": f"Un rejugement est déjà en cours "
                                                  f"({etat_rejuge['fait']}/{etat_rejuge['total']})."}, 409)
                if not cfg.get("_chemin_config"):
                    return self._json({"ok": False, "erreur": "config", "message": "Configuration introuvable."}, 409)
                _effacer_profil_a_rejuger(cfg)
                etat_rejuge.update(en_cours=True, fait=0, total=0, bilan=None, erreur=None,
                                   debut=datetime.now(timezone.utc).isoformat(), fin=None)

                def progres(fait, total):
                    etat_rejuge["fait"], etat_rejuge["total"] = fait, total

                def tache():
                    a = types.SimpleNamespace(config=cfg["_chemin_config"], mock=False, retout=True,
                                              limite=REJUGE_MAX, jours=jours, compter=False,
                                              progres=progres)
                    try:
                        cmd_analyser_ia(a)
                        etat_rejuge["bilan"] = getattr(a, "bilan", None)
                    except BaseException as e:
                        etat_rejuge["erreur"] = str(e) or e.__class__.__name__
                        print(f"[{NOM}] rejugement : {e}", file=sys.stderr)
                    finally:
                        etat_rejuge["en_cours"] = False
                        etat_rejuge["fin"] = datetime.now(timezone.utc).isoformat()
                threading.Thread(target=tache, daemon=True).start()
                self._json({"ok": True, "jours": jours, "rejuge": dict(etat_rejuge)})
            elif self.path == "/api/action":
                e = c.get("etat")
                e = None if e in (None, "", "a_traiter") else e
                cx = _ouvrir_db(db)
                cx.execute("UPDATE messages SET etat=? WHERE message_id=?", (e, mid))
                cx.commit()
                cx.close()
                self._json({"ok": True})
            elif self.path == "/api/ouvrir":
                cx = _ouvrir_db(db)
                row = cx.execute("SELECT fichier_eml FROM messages WHERE message_id=?",
                                 (mid,)).fetchone()
                cx.close()
                if row and row[0] and Path(row[0]).exists():
                    _ouvrir_fichier(row[0])
                    self._json({"ok": True})
                else:
                    self._json({"ok": False, "erreur": "fichier introuvable"}, 404)
            elif self.path == "/api/sync/confirme":
                # L'extension confirme ce qu'elle a appliqué dans Thunderbird.
                marques = c.get("marques", []) or []
                demarques = c.get("demarques", []) or []
                cx = _ouvrir_db(db)
                for i in marques:
                    cx.execute("UPDATE messages SET tb_sync='traite' WHERE message_id=?", (i,))
                for i in demarques:
                    cx.execute("UPDATE messages SET tb_sync=NULL WHERE message_id=?", (i,))
                cx.commit()
                cx.close()
                self._json({"ok": True})
            elif self.path == "/api/sync/depuis-client":
                # Sens inverse : l'utilisateur a posé/retiré l'étiquette dans Thunderbird.
                # L'étiquette est la vérité partagée ; LIRE un mail ne le traite jamais.
                traites = c.get("traites", []) or []
                non_traites = c.get("non_traites", []) or []

                def _variantes(i):
                    # Thunderbird envoie l'id nu ; en base il peut être entre chevrons.
                    nu = (i or "").strip().strip("<>")
                    return [nu, "<" + nu + ">", (i or "").strip()]

                cx = _ouvrir_db(db)
                maj = 0
                for i in traites:
                    v = _variantes(i)
                    cur = cx.execute(
                        "UPDATE messages SET etat='traite', tb_sync='traite' "
                        "WHERE message_id IN (?,?,?) AND (etat IS NULL OR etat!='traite')", v)
                    maj += cur.rowcount
                for i in non_traites:
                    # On ne rouvre que si Thunderbird AVAIT l'étiquette (évite d'annuler
                    # un « Traité » tout juste fait dans le cockpit et pas encore appliqué).
                    v = _variantes(i)
                    cur = cx.execute(
                        "UPDATE messages SET etat=NULL, tb_sync=NULL "
                        "WHERE message_id IN (?,?,?) AND tb_sync='traite'", v)
                    maj += cur.rowcount
                cx.commit()
                cx.close()
                self._json({"ok": True, "maj": maj})
            else:
                self._json({"erreur": "inconnu"}, 404)
    return H


PAGE_COCKPIT = """<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8"><title>IAMail — cockpit</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{--bg:#f6f7f9;--card:#fff;--tx:#1a1a1a;--mut:#666;--line:#e5e7eb;
--haute:#d92d20;--normale:#1570ef;--basse:#667085;--acc:#1570ef;}
@media(prefers-color-scheme:dark){:root{--bg:#16181d;--card:#20242b;--tx:#e8e8e8;
--mut:#9aa1ac;--line:#30353d;}}
*{box-sizing:border-box}body{font:14px system-ui,sans-serif;margin:0;background:var(--bg);color:var(--tx)}
header{position:sticky;top:0;z-index:5;background:var(--bg);padding:12px 18px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:12px;flex-wrap:wrap}
h1{font-size:17px;margin:0}main{max-width:840px;margin:0 auto;padding:16px}
.case{margin:20px 0 6px;font-size:13px;letter-spacing:.04em;text-transform:uppercase;color:var(--mut)}
.item{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:8px 0;border-left:4px solid var(--basse)}
.item.haute{border-left-color:var(--haute)}.item.normale{border-left-color:var(--normale)}
.tache{font-weight:600}.sujet{color:var(--mut);font-size:13px;margin:2px 0}
.meta{color:var(--mut);font-size:12px;margin-top:4px}
.ech{display:inline-block;background:var(--line);border-radius:5px;padding:1px 7px;font-size:12px;margin-left:6px}
.q{margin-top:6px;font-size:13px;color:var(--haute)}
.prec{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:6px}
.prec input{flex:1;min-width:180px;font:13px system-ui;padding:5px 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--tx)}
.rep{margin-top:6px;font-size:13px;color:var(--mut)}
.badge.ok{border-color:var(--basse);color:var(--basse)}
.badge{font-size:11px;border:1px solid var(--line);border-radius:5px;padding:0 6px;margin-left:6px;color:var(--mut)}
.btns{margin-top:10px;display:flex;gap:8px;flex-wrap:wrap}
button{font:13px system-ui;padding:6px 12px;border:1px solid var(--line);border-radius:7px;background:var(--card);color:var(--tx);cursor:pointer}
button:hover{filter:brightness(.97)}button.ok{border-color:#12b76a;color:#12b76a}button.pri{border-color:var(--normale);color:var(--normale)}
.group{background:var(--card);border:1px solid var(--line);border-radius:10px;margin:8px 0;border-left:4px solid var(--basse);overflow:hidden}
.group.haute{border-left-color:var(--haute)}.group.normale{border-left-color:var(--normale)}
.grphead{padding:11px 14px;font-weight:600;border-bottom:1px solid var(--line)}
.row{padding:9px 14px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.row:last-child{border-bottom:0}.row .who{flex:1;min-width:160px;color:var(--mut);font-size:13px}
.seg{display:flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.seg button{border:0;border-radius:0;padding:6px 12px}.seg button.on{background:var(--acc);color:#fff}
.ctl{font-size:13px;color:var(--mut);display:flex;align-items:center;gap:6px}
.spacer{margin-left:auto}#vide{color:var(--mut);padding:50px 0;text-align:center;font-size:15px}
input[type=date]{font:13px system-ui;padding:4px 6px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--tx)}
.resume{font-size:13px;margin:3px 0}.why{color:var(--mut);font-size:12px;margin-top:4px;font-style:italic}
.badge.warn{border-color:var(--haute);color:var(--haute)}.badge.ev{border-color:var(--acc);color:var(--acc)}
button.mini{padding:6px 9px;color:var(--mut)}
#alerte{display:none;max-width:840px;margin:10px auto 0;padding:0 16px}
#alerte div{background:var(--card);border:1px solid var(--haute);border-radius:10px;padding:10px 14px;font-size:13px}
dialog{border:1px solid var(--line);border-radius:12px;background:var(--card);color:var(--tx);width:min(640px,92vw);padding:18px}
dialog::backdrop{background:rgba(0,0,0,.45)}
dialog label{display:block;font-size:13px;color:var(--mut);margin:10px 0 4px}
dialog input[type=text],dialog textarea{width:100%;font:14px system-ui;padding:7px 9px;border:1px solid var(--line);border-radius:7px;background:var(--bg);color:var(--tx)}
dialog textarea{min-height:150px;resize:vertical}dialog .aide{font-size:12px;color:var(--mut);margin-top:4px}
#topotexte{white-space:pre-wrap;font-size:13px;margin-top:8px}
.badge.att{background:#fef0c7;color:#93370d}
.masques{max-width:840px;margin:14px auto;color:var(--mut);font-size:13px;text-align:center}
#progres{display:none;max-width:840px;margin:10px auto 0;padding:10px 14px;border:1px solid #b2ddff;background:#eff8ff;color:#175cd3;border-radius:10px;font-size:14px}
#progres .barre{height:6px;background:#d1e9ff;border-radius:3px;margin-top:6px;overflow:hidden}
#progresbarre{height:100%;width:0;background:#2e90fa;transition:width .4s}
#dlg .intro{background:#fffaeb;border:1px solid #fedf89;color:#93370d;border-radius:8px;padding:8px 10px;font-size:13px;margin-bottom:8px}
#relie{display:none;max-width:840px;margin:10px auto 0;padding:10px 14px;border:1px solid #abefc6;background:#ecfdf3;color:#085d3a;border-radius:10px;font-size:14px}
#relie a{color:inherit;font-weight:600}
body.relie #relie{display:block}
body.relie .btns button:not([data-a="ouvrir"]),body.relie .prec,body.relie #reglages{display:none}
</style></head><body>
<header>
  <h1>🛩️ IAMail</h1>
  <div class="seg"><button id="v-atraiter" class="on">À traiter</button><button id="v-reporte">Reportés</button><button id="v-masse" title="Newsletters et courrier de masse non important : rangés par les règles, jamais envoyés à l'IA">Masse</button></div>
  <button id="audio" class="pri">🔊 Topo audio</button>
  <span id="compte" class="badge"></span>
  <label class="ctl" id="l-nonlus"><input type="checkbox" id="nonlus"> non-lus seulement</label>
  <label class="ctl spacer">Retour d'absence depuis <input type="date" id="depuis"></label>
  <button id="reglages" title="Ta fonction, tes adresses">⚙ Réglages</button>
</header>
<div id="relie">🔗 Ce poste est <b>relié à GesEnt</b> : c'est là que l'on traite, délègue et corrige (GesEnt fait foi). Ce cockpit reste consultable, en lecture seule. <a id="relielien" target="_blank" rel="noopener">Ouvrir GesEnt</a></div>
<div id="alerte"><div id="alertetexte"></div></div>
<div id="progres"><div id="progrestexte"></div><div class="barre"><div id="progresbarre"></div></div></div>
<dialog id="dlg">
  <h1>Réglages — pour qui l'IA trie</h1>
  <div id="r-intro" class="intro" style="display:none"></div>
  <label for="r-nom">Ton nom</label><input type="text" id="r-nom" maxlength="120">
  <label for="r-fonction">Ta fonction</label><input type="text" id="r-fonction" maxlength="120" placeholder="Responsable informatique et communication d'une fédération d'entreprises">
  <label for="r-profil">Ce qui compte pour toi</label>
  <textarea id="r-profil" maxlength="1500" placeholder="Ce dont je suis responsable : …&#10;Urgent pour moi : une panne qui bloque plusieurs collègues, une demande de la direction, une échéance…&#10;Personnes ou organismes prioritaires : …&#10;Ce qui ne me concerne pas (même si je suis en copie) : …"></textarea>
  <div class="aide">Écris comme tu l'expliquerais à un nouvel assistant. Ce texte est joint à chaque mail envoyé à l'IA.</div>
  <label for="r-adresses">Tes adresses (une par ligne, alias compris)</label>
  <textarea id="r-adresses" style="min-height:60px"></textarea>
  <div class="btns"><button class="ok" id="r-ok">Enregistrer</button>
    <button id="r-rejuger" title="Appels IA payants, bornés à 300 messages ; le nombre exact est annoncé avant">Enregistrer et rejuger les 14 derniers jours…</button>
    <button id="r-annuler">Fermer</button></div>
  <div class="aide" id="r-change" style="display:none">Ton profil a changé : les messages déjà jugés l'ont été avec l'ancien. « Enregistrer et rejuger » les met à jour.</div>
  <div class="aide" id="r-etat"></div>
</dialog>
<div id="barreaudio" style="display:none;max-width:840px;margin:10px auto 0;padding:0 16px">
  <div style="background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px">
    <div id="audiotexte" style="font-size:13px;color:var(--mut);margin-bottom:6px"></div>
    <audio id="lecteur" controls style="width:100%"></audio>
    <details><summary style="font-size:13px;color:var(--mut);cursor:pointer">Lire le topo complet <span id="toposource" class="badge"></span></summary><div id="topotexte"></div></details></div></div>
<main id="main"></main>
<script>
const api=(p,b)=>fetch(p,{method:b?'POST':'GET',headers:{'Content-Type':'application/json'},body:b?JSON.stringify(b):undefined}).then(r=>r.json());
document.getElementById('audio').onclick=async()=>{
  const b=document.getElementById('audio');b.textContent='⏳ génération…';b.disabled=true;
  try{const r=await api('/api/topo-audio',{});
    if(r.erreur==='topo'){alert(r.message);return;}
    document.getElementById('audiotexte').textContent=r.court||'';
    document.getElementById('topotexte').textContent=r.complet||'';
    document.getElementById('toposource').textContent=r.source==='ia'?"rédigé par l'IA":'gabarit local (IA indisponible)';
    document.getElementById('barreaudio').style.display='';
    if(r.ok&&r.url){const l=document.getElementById('lecteur');l.src=r.url;l.play().catch(()=>{});}
    else{alert('Pas de voix : '+(r.erreur||'')+"\\nInstalle espeak-ng (sudo apt install espeak-ng). Le texte du topo reste lisible.");}
  }catch(e){alert('Erreur: '+e);} finally{b.textContent='🔊 Topo audio';b.disabled=false;}
};
const esc=s=>(s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
let state={vue:'atraiter',nonlus:false,depuis:''},total=0;
// On regroupe par CONVERSATION (fil = objet nettoyé des Re:/Tr:), pas par texte de tâche.
const cle=it=>(it.fil||it.tache||it.resume||it.sujet||'').trim().toLowerCase();
const freqBadge=it=> it.actif
  ? '<span class="badge" title="expéditeur fréquent et récent">✉️ écrit souvent</span>'
  : (it.freq>=5?`<span class="badge" title="${it.freq} messages reçus">✉️ ${it.freq} mails</span>`:'');

function btns(it){
  const rem=state.vue==='reporte'
    ? `<button data-a="remettre">Remettre à traiter</button>`
    : `<button data-a="reporte">Reporter</button>`;
  const corr=state.vue==='masse'?'':`<button class="mini" data-a="plus" title="Plus urgent que ça — l'IA s'en souviendra">▲</button>
     <button class="mini" data-a="moins" title="Moins urgent que ça — l'IA s'en souviendra">▼</button>
     ${it.concerne==='autre'?'<button class="mini" data-a="pour_moi" title="Si, c\\'est à moi d\\'agir">C\\'est pour moi</button>'
       :'<button class="mini" data-a="pas_pour_moi" title="Ce n\\'est pas à moi d\\'agir — l\\'IA s\\'en souviendra">Pas pour moi</button>'}`;
  return `<div class="btns">
     <button class="pri" data-a="ouvrir">Ouvrir l'email</button>
     <button data-a="deleguer">Déléguer…</button>
     <button class="ok" data-a="traite">Traité</button>${rem}${corr}</div>`;
}
function brancher(el,it){el.querySelectorAll('button').forEach(b=>b.onclick=()=>action(it,b.dataset.a,el,b));
  const champ=el.querySelector('.prec input');
  if(champ)champ.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();action(it,'preciser',el,{dataset:{}});}};}

const srcBadge=it=>({erreur:'<span class="badge warn" title="La réponse de l\\'IA a été rejetée : classement par les règles">tri par les règles</span>',
  attente:'<span class="badge" title="Pas encore jugé par l\\'IA : classement provisoire par les règles">IA en attente</span>',
  humain:'<span class="badge" title="Tu as corrigé ce classement">corrigé</span>'}[it.source]||'');
const evBadge=it=>{const e=it.evenement;if(!e||!e.date)return '';
  return `<span class="badge ev">${e.annule?'❌ annulé : ':'📅 '}${esc(e.date)}${e.heure?' '+esc(e.heure):''}${e.lieu?' — '+esc(e.lieu):''}</span>`;};
const quiBadge=it=>it.concerne==='autre'?'<span class="badge">pour info — pas à toi d\\'agir</span>':(it.concerne==='collectif'?'<span class="badge">collectif</span>':'');
function carte(it){
  const d=document.createElement('div');d.className='item '+(it.priorite||'basse');
  const ech=(it.echeance&&it.echeance!=='aucune')?`<span class="ech">${it.retard?'⚠ en retard : ':'⏱ '}${esc(it.echeance)}</span>`:'';
  const nl=it.non_lu===1?'<span class="badge">non lu</span>':'';
  const q=it.question?`<div class="q">❓ ${esc(it.question)}</div>`+(it.precision
      ?`<div class="rep">Ta réponse : « ${esc(it.precision)} »</div>`
      :`<div class="prec"><button class="mini" data-a="preciser" data-t="Oui, c’est pour moi">Oui, c’est pour moi</button>
        <button class="mini" data-a="preciser" data-t="Non, pour information">Non, pour information</button>
        <input type="text" maxlength="500" placeholder="Ou réponds en quelques mots…">
        <button class="mini" data-a="preciser">Répondre</button></div>`):'';
  const attBadge=it.attente_depuis?`<span class="badge att" title="Demande qui t’est adressée, sans réponse de ta part dans ton dossier Envoyés">⏳ sans réponse depuis ${it.attente_depuis} jour${it.attente_depuis>1?'s':''}</span>`:'';
  const repBadge=it.repondu?`<span class="badge ok" title="Trouvé dans ton dossier Envoyés : marque-le Traité si l’affaire est close">✓ tu as répondu le ${esc(it.repondu)}</span>`:'';
  const titre=it.tache||it.resume||it.sujet||'(sans objet)';
  const res=(it.resume&&it.resume!==titre)?`<div class="resume">${esc(it.resume)}</div>`:'';
  const why=it.justification?`<div class="why">${esc(it.justification)}</div>`:'';
  d.innerHTML=`<div class="tache">${esc(titre)}${ech}${evBadge(it)}${nl}</div>${res}
   ${it.sujet&&it.sujet!==titre?`<div class="sujet">${esc(it.sujet)}</div>`:''}
   <div class="meta">${esc(it.de)} · ${esc(it.date_txt)} ${freqBadge(it)}${quiBadge(it)}${attBadge}${repBadge}${srcBadge(it)}</div>${why}${q}${btns(it)}`;
  brancher(d,it);return d;
}
function groupe(items){
  const g=document.createElement('div');g.className='group '+(items[0].priorite||'basse');
  const head=document.createElement('div');head.className='grphead';
  const suj=items[0].sujet||items[0].tache||'(conversation)';
  const top=items.reduce((a,b)=>(b.actif||b.freq>a.freq)?b:a,items[0]);
  const rem=state.vue==='reporte'?'remettre':'reporte';
  const remLbl=state.vue==='reporte'?'Tout remettre':'Tout reporter';
  head.innerHTML=`<div>💬 ${esc(suj)} <span class="badge">×${items.length} dans ce fil</span> ${freqBadge(top)}</div>
    <div class="btns"><button class="ok" data-g="traite">Tout traiter</button>
      <button data-g="${rem}">${remLbl}</button></div>`;
  head.querySelectorAll('button').forEach(b=>b.onclick=()=>actionGroupe(items,b.dataset.g,g));
  g.appendChild(head);
  for(const it of items){
    const r=document.createElement('div');r.className='item row';r.style.border='0';r.style.margin='0';r.style.borderRadius='0';
    r.classList.remove('haute','normale');
    const nl=it.non_lu===1?'<span class="badge">non lu</span>':'';
    r.innerHTML=`<div class="who">${esc(it.de)} — ${esc(it.tache||it.resume||it.sujet)}${evBadge(it)}${nl} ${freqBadge(it)}${quiBadge(it)}${srcBadge(it)}</div>${btns(it)}`;
    brancher(r,it);g.appendChild(r);
  }
  return g;
}
async function actionGroupe(items,a,g){
  const etat=a==='remettre'?'a_traiter':a;
  for(const it of items.slice()){await api('/api/action',{id:it.id,etat});}
  g.remove();total-=items.length;maj();
}
async function action(it,a,el,bouton){
  if(['plus','moins','pas_pour_moi','pour_moi'].includes(a)){await api('/api/corriger',{id:it.id,quoi:a});charger();return;}
  if(a==='preciser'){
    const champ=el.querySelector('.prec input');
    const texte=((bouton&&bouton.dataset&&bouton.dataset.t)||(champ&&champ.value)||'').trim();
    if(!texte){if(champ)champ.focus();return;}
    const r=await api('/api/preciser',{id:it.id,texte});
    const zone=el.querySelector('.prec');
    if(zone)zone.outerHTML=`<div class="rep">Ta réponse : « ${esc(texte)} » — ${r.rejuge?'le message est rejugé avec elle, recharge dans un instant.':'gardée pour ce message et les messages semblables.'}</div>`;
    if(r.rejuge)setTimeout(charger,8000);
    return;}
  if(a==='ouvrir'||a==='deleguer'){await api('/api/ouvrir',{id:it.id});
     if(a==='deleguer')alert("L'email s'ouvre dans ton logiciel : fais « Transférer », ajoute le collègue et ta note.");
     return;}
  const etat=a==='remettre'?'a_traiter':a;
  await api('/api/action',{id:it.id,etat});
  const grp=el.closest('.group');el.remove();
  if(grp&&!grp.querySelector('.row'))grp.remove();
  total--;maj();
}
function maj(){document.getElementById('compte').textContent=total+(state.vue==='reporte'?' reporté(s)':(state.vue==='masse'?' en masse (500 max)':' à traiter'));
  if(total<=0)document.getElementById('main').innerHTML='<div id="vide">🎉 Rien ici.</div>';}
async function charger(){
  const p=new URLSearchParams({vue:state.vue,nonlus:state.nonlus?1:0,depuis:state.depuis});
  const r=await api('/api/liste?'+p.toString());
  const m=document.getElementById('main');m.innerHTML='';total=0;
  for(const cas of r.cases){if(!cas.items.length)continue;
    const h=document.createElement('div');h.className='case';h.textContent=cas.titre;m.appendChild(h);
    const groupes=new Map();
    for(const it of cas.items){const k=cle(it);(groupes.get(k)||groupes.set(k,[]).get(k)).push(it);}
    for(const items of groupes.values()){total+=items.length;
      m.appendChild(items.length>1?groupe(items):carte(items[0]));}
  }
  maj();
  const n=[];if(r.masques_repondus)n.push(r.masques_repondus+' auxquel(s) tu as déjà répondu');
  if(r.masques_anciens)n.push(r.masques_anciens+' de plus de '+r.jours+' jours sans demande en attente');
  if(n.length){const d=document.createElement('div');d.className='masques';
    d.textContent='Masqués : '+n.join(' · ')+'. Pour les revoir, choisis une date « depuis ».';m.appendChild(d);}
}
function setVue(v){state.vue=v;
  document.getElementById('v-atraiter').classList.toggle('on',v==='atraiter');
  document.getElementById('v-reporte').classList.toggle('on',v==='reporte');
  document.getElementById('v-masse').classList.toggle('on',v==='masse');
  document.getElementById('l-nonlus').style.display=(v==='atraiter'&&!state.depuis)?'':'none';
  charger();}
document.getElementById('v-atraiter').onclick=()=>setVue('atraiter');
document.getElementById('v-reporte').onclick=()=>setVue('reporte');
document.getElementById('v-masse').onclick=()=>setVue('masse');
// --- Réglages : pour qui l'IA trie ---
const $=id=>document.getElementById(id);
let charge={},aRejuger=false;
function alertes(l,rejuger){aRejuger=!!rejuger;$('alerte').style.display=(l&&l.length)?'block':'none';
  const lien=aRejuger?'<a href="#" id="al-rejuger">Rejuger maintenant</a> · <a href="#" id="al-regl">Réglages</a>':'<a href="#" id="al-regl">Ouvrir les réglages</a>';
  $('alertetexte').innerHTML=(l||[]).map(x=>'⚠ '+esc(x)).join('<br>')+' '+lien;
  const a=$('al-regl');if(a)a.onclick=e=>{e.preventDefault();ouvrirReglages();};
  const b=$('al-rejuger');if(b)b.onclick=e=>{e.preventDefault();lancerRejuge();};}
function valeurs(){return {utilisateur:{nom:$('r-nom').value.trim(),fonction:$('r-fonction').value.trim(),profil:$('r-profil').value.trim()},
  mes_adresses:$('r-adresses').value.split(/[\\s,;]+/).filter(Boolean)};}
const profilChange=()=>{const v=valeurs().utilisateur;return ['nom','fonction','profil'].some(k=>(v[k]||'')!==(charge[k]||''));};
function majChange(){$('r-change').style.display=(profilChange()&&$('r-fonction').value.trim())?'':'none';
  $('r-rejuger').classList.toggle('ok',profilChange());$('r-ok').classList.toggle('ok',!profilChange());}
['r-nom','r-fonction','r-profil'].forEach(id=>$(id).addEventListener('input',majChange));
async function ouvrirReglages(bloquants){const r=await api('/api/reglages');const u=r.utilisateur||{};
  charge={nom:u.nom||'',fonction:u.fonction||'',profil:u.profil||''};
  $('r-nom').value=charge.nom;$('r-fonction').value=charge.fonction;$('r-profil').value=charge.profil;
  $('r-adresses').value=(r.mes_adresses||[]).join('\\n');
  const bl=bloquants||r.bloquants||[];
  $('r-intro').style.display=bl.length?'':'none';
  $('r-intro').innerHTML=bl.length?'<b>Avant de trier, il me manque :</b><br>'+bl.map(x=>'• '+esc(x)).join('<br>'):'';
  $('r-etat').textContent=r.rejuge&&r.rejuge.en_cours?'Rejugement en cours…':('IA : '+r.ia.fournisseur+(r.ia.modele?' / '+r.ia.modele:''));
  majChange();$('dlg').showModal();
  const vide=['r-fonction','r-adresses'].find(id=>!$(id).value.trim());if(vide)$(vide).focus();}
async function sauver(){$('r-etat').textContent='Enregistrement…';
  const r=await api('/api/reglages',valeurs());charge=Object.assign({},valeurs().utilisateur);
  alertes(r.alertes,false);$('r-etat').textContent='Enregistré.';return r;}
// Rejuger : estimation, accord (appels payants), puis barre de progression visible partout.
async function lancerRejuge(){
  const est=await api('/api/rejuger',{jours:14,compter:true});
  if(!est.n){alert('Rien à rejuger sur les 14 derniers jours.');return false;}
  if(!confirm("Rejuger "+est.n+" message(s) des 14 derniers jours avec ce profil ?\\n\\nCela consomme des crédits IA ("+est.n+" appel(s), "+est.max+" au plus). Tes corrections sont conservées."))return false;
  const r=await fetch('/api/rejuger',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({jours:14})}).then(x=>x.json());
  if(!r.ok&&r.erreur!=='en_cours'){alert('Impossible : '+(r.message||r.erreur||''));return false;}
  if($('dlg').open)$('dlg').close();
  $('alerte').style.display='none';suivreRejuge();return true;}
let suivi=null;
function suivreRejuge(){if(suivi)return;
  const tour=async()=>{let p;try{p=(await api('/api/progression')).rejuge;}catch(e){return;}
    const bloc=$('progres');bloc.style.display='block';
    if(p.en_cours){const pct=p.total?Math.round(100*p.fait/p.total):0;
      $('progrestexte').textContent=p.total?('🧠 Rejugement avec ton profil : '+p.fait+' / '+p.total+' message(s)… tu peux continuer à travailler.'):'🧠 Rejugement : préparation…';
      $('progresbarre').style.width=pct+'%';return;}
    clearInterval(suivi);suivi=null;$('progresbarre').style.width='100%';
    $('progrestexte').textContent=p.erreur?('Rejugement interrompu : '+p.erreur):
      ('✅ Rejugement terminé'+(p.bilan?' : '+p.bilan.juges+' jugé(s)'+(p.bilan.erreurs?', '+p.bilan.erreurs+' en erreur':''):'')+'.');
    charger();setTimeout(()=>{bloc.style.display='none';},8000);};
  tour();suivi=setInterval(tour,2000);}
$('reglages').onclick=()=>ouvrirReglages();
$('r-annuler').onclick=()=>$('dlg').close();
$('r-ok').onclick=async()=>{const change=profilChange()&&$('r-fonction').value.trim();
  await sauver();
  if(change&&confirm("Ton profil a changé : les messages déjà jugés l'ont été avec l'ancien.\\n\\nLes rejuger maintenant avec le nouveau profil ? (recommandé — le nombre d'appels IA est annoncé juste après)")){
    if(await lancerRejuge())return;}
  $('dlg').close();charger();};
$('r-rejuger').onclick=async()=>{const b=$('r-rejuger');b.disabled=true;
  try{await sauver();await lancerRejuge();}finally{b.disabled=false;}};
api('/api/reglages').then(r=>{alertes(r.alertes,r.a_rejuger);
  if(r.rejuge&&r.rejuge.en_cours)suivreRejuge();
  else if(r.bloquants&&r.bloquants.length)ouvrirReglages(r.bloquants);   // bloquant : on y va directement
}).catch(()=>{});
document.getElementById('nonlus').onchange=e=>{state.nonlus=e.target.checked;charger();};
document.getElementById('depuis').onchange=e=>{state.depuis=e.target.value;
  document.getElementById('l-nonlus').style.display=(state.vue==='atraiter'&&!state.depuis)?'':'none';charger();};
api('/api/relie').then(r=>{if(r&&r.relie){document.body.classList.add('relie');
  const l=document.getElementById('relielien');if(r.gesent)l.href=r.gesent;else l.remove();}}).catch(()=>{});
charger();
</script></body></html>"""


def cmd_cockpit(args):
    cfg = charger_config(args.config)
    cx = connexion(cfg["base_sqlite"])
    _assurer_colonnes(cx)
    print(f"[{NOM}] lecture du statut lu/non-lu depuis l'archive…")
    n = backfill_non_lu(cx)
    if n:
        print(f"[{NOM}] {n} statuts lus.")
    cx.close()
    port = args.port
    srv = http.server.HTTPServer(("127.0.0.1", port), faire_cockpit_handler(cfg))
    url = f"http://127.0.0.1:{port}/"
    print(f"[{NOM}] cockpit ouvert : {url}  (Ctrl+C pour arrêter)")
    if not args.pas_de_navigateur:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print(f"\n[{NOM}] cockpit fermé.")
        srv.server_close()


# ----------------------------------------------------------------------------
# Le topo du matin — script parlé + synthèse vocale (local)
# ----------------------------------------------------------------------------
def _nom_affiche(de):
    """Nom lisible d'un expéditeur pour la voix (prénom/nom, sinon l'adresse)."""
    if not de:
        return "un expéditeur"
    m = re.match(r'\s*"?([^"<]+?)"?\s*<', de)
    if m and m.group(1).strip():
        return m.group(1).strip()
    spec = (_adresses_spec(de) or [de])[0]
    return spec.split("@", 1)[0].replace(".", " ")


_MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
         "août", "septembre", "octobre", "novembre", "décembre"]


def _echeance_parlee(ech):
    """'aujourd'hui' / 'demain' / 'le 15 novembre' depuis une échéance AAAA-MM-JJ, sinon None.
    Une échéance passée est dite telle quelle : 'en retard depuis le 3 novembre'."""
    try:
        d = datetime.strptime((ech or "").strip(), "%Y-%m-%d").date()
    except Exception:
        return None
    delta = (d - datetime.now().date()).days
    jour = "1er" if d.day == 1 else str(d.day)
    if delta < 0:
        return "hier, donc en retard" if delta == -1 else f"le {jour} {_MOIS[d.month - 1]}, donc en retard"
    if delta == 0:
        return "aujourd'hui"
    if delta == 1:
        return "demain"
    if delta < 7:
        return f"{_JOURS[d.weekday()]} {jour} {_MOIS[d.month - 1]}"
    return f"le {jour} {_MOIS[d.month - 1]}"


def _recu_parle(dm):
    """'ce matin', 'hier', 'vendredi', 'le 3 novembre' — en heure LOCALE, sans l'heure
    exacte (une voix qui lit « le 6 novembre à 10 heures 22 » à chaque phrase noie l'essentiel)."""
    d = _local(_dt_message(dm))
    if not d:
        return ""
    delta = (datetime.now().date() - d.date()).days
    if delta <= 0:
        return "ce matin" if d.hour < 12 else "cet après-midi"
    if delta == 1:
        return "hier"
    if delta < 7:
        return _JOURS[d.weekday()]
    jour = "1er" if d.day == 1 else str(d.day)
    return f"le {jour} {_MOIS[d.month - 1]}"


def _phrase_message(de, tache, res, ech, dm, annee=None, sujet=None, pourquoi=None,
                    sans_reponse=None):
    """Qui, quoi, pour quand — et pourquoi, pour ce qui presse."""
    nom = _nom_affiche(de)
    res = (res or "").strip().rstrip(".")
    tache = (tache or "").strip().rstrip(".")
    quand = _recu_parle(dm)
    tete = f"{nom}, {quand}" if quand else nom
    if res and tache and res.lower() != tache.lower():
        phrase = f"{tete} : {res}. À faire : {tache}"
    else:
        phrase = f"{tete} : {tache or res or _sujet_court(sujet) or 'un message à regarder'}"
    ep = _echeance_parlee(ech) if ech and ech not in ("aucune", None) else None
    if ep:
        phrase += f", pour {ep}"
    phrase += "."
    if sans_reponse:
        phrase += f" Toujours sans réponse de ta part, depuis {sans_reponse} jour{'s' if sans_reponse > 1 else ''}."
    if pourquoi:
        phrase += " " + pourquoi.strip().rstrip(".") + "."
    return phrase


def _sujet_court(sujet):
    """Objet lisible pour la voix : sans Re:/Tr: ni [tags], casse d'origine conservée."""
    s = _PREFIXE_FIL.sub("", (sujet or "").strip())
    s = re.sub(r'\[[^\]]*\]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s[:70]


def _mots_sujet(sujet):
    """Mots significatifs d'un objet (≥4 lettres, hors mots vides) — pour rapprocher deux fils."""
    s = _sujet_fil(sujet)
    nmin = PARAMS["regroupement_mot_longueur_min"]
    return {m for m in re.split(r'[^a-z0-9]+', s) if len(m) >= nmin and m not in FIL_MOTS_VIDES}


def _grouper_fils(rows):
    """Regroupe des messages par AFFAIRE : fils identiques, mais aussi fils voisins
    (même sujet au libellé un peu différent). rows = (de, tache, res, ech, dm, sujet),
    triés par date décroissante. Renvoie une liste de groupes {mots, rows}."""
    inter_min = PARAMS["regroupement_mots_partages_min"]
    jac_min = PARAMS["regroupement_jaccard_min"]
    groupes = []
    for r in rows:
        ms = _mots_sujet(r[5])
        cible, meilleur = None, 0.0
        if ms:
            for g in groupes:
                inter = len(ms & g["mots"])
                if not inter:
                    continue
                jac = inter / len(ms | g["mots"])
                sous = ms <= g["mots"] or g["mots"] <= ms
                if (inter >= inter_min or sous) and jac >= jac_min and jac > meilleur:
                    cible, meilleur = g, jac
        else:
            cle = _sujet_fil(r[5])
            for g in groupes:
                if not g["mots"] and g["cle"] == cle:
                    cible = g
                    break
        if cible:
            cible["rows"].append(r)
            cible["mots"] |= ms
        else:
            groupes.append({"mots": set(ms), "cle": _sujet_fil(r[5]), "rows": [r]})
    return groupes


def _lister_noms(noms, maxi=None):
    if maxi is None:
        maxi = PARAMS["topo_intervenants_max"]
    if len(noms) == 1:
        return noms[0]
    if len(noms) <= maxi:
        return ", ".join(noms[:-1]) + " et " + noms[-1]
    return ", ".join(noms[:maxi]) + f" et {len(noms) - maxi} autres"


def _echeance_proche(eches):
    """La plus proche échéance datée d'un groupe, dite en français ; None si aucune."""
    best = None
    for e in eches:
        try:
            d = datetime.strptime((e or "").strip(), "%Y-%m-%d").date()
        except Exception:
            continue
        if best is None or d < best:
            best = d
    return _echeance_parlee(best.strftime("%Y-%m-%d")) if best else None


def _phrase_groupe(rows):
    """Une seule annonce pour toute une conversation : sujet, intervenants, action commune."""
    noms = []
    for r in rows:
        n = _nom_affiche(r[0])
        if n not in noms:
            noms.append(n)
    suj = _sujet_court(rows[0][5]) or "le même sujet"
    action = ""
    for r in rows:
        action = (r[1] or r[2] or "").strip().rstrip(".")
        if action:
            break
    base = f"Sur « {suj} », {len(rows)} messages de {_lister_noms(noms)}"
    if action:
        base += f", pour l'essentiel : {action}"
    ep = _echeance_proche([r[3] for r in rows])
    if ep:
        base += f", pour {ep}"
    return base + "."


_CASES_TOPO = ("maintenant", "cette_semaine", "ce_mois")


def elements_topo(cx, cfg):
    """Matière du topo, IDENTIQUE pour la rédaction par l'IA et pour le gabarit local.

    - messages non traités de la fenêtre récente (topo_fenetre_jours), plus ceux, plus
      anciens, dont l'échéance n'est pas dépassée de plus d'une semaine ;
    - case recalculée à la date du jour ; tri : case, puis urgence, puis « à moi »,
      puis échéance, puis le plus récent ;
    - événements d'aujourd'hui et de demain (extension 'evenement'), quel que soit
      l'âge du mail qui les annonçait ;
    - jamais les messages suspects ni la masse non importante.
    """
    maintenant = datetime.now()
    auj = maintenant.strftime("%Y-%m-%d")
    demain = (maintenant + timedelta(days=1)).strftime("%Y-%m-%d")
    fen = int(cfg.get("topo_jours") or PARAMS.get("topo_fenetre_jours", 14))
    plancher = (datetime.now(timezone.utc) - timedelta(days=fen)).isoformat()
    ech_min = (maintenant - timedelta(days=7)).strftime("%Y-%m-%d")
    cols = ["message_id", "de_adresse", "sujet", "date_mail", "ia_source", "ia_urgence",
            "ia_echeance", "ia_action", "ia_tache", "ia_resume", "ia_justification",
            "ia_question", "ia_categorie", "ia_extensions", "urgence", "pour_moi",
            "important", "suspect"]
    base = ("SELECT " + ", ".join(cols) + " FROM messages WHERE etat IS NULL "
            "AND categorie IS NOT NULL AND NOT " + _SQL_MASSE)
    rows = cx.execute(
        base + " AND (date_mail >= ? OR (ia_echeance IS NOT NULL AND ia_echeance != 'aucune' "
               "AND ia_echeance >= ?) OR ia_extensions LIKE ? OR ia_extensions LIKE ?) "
               "ORDER BY date_mail DESC",
        (plancher, ech_min, f'%"date": "{auj}"%', f'%"date": "{demain}"%')).fetchall()

    elements, evenements, n_susp = [], [], 0
    envois = index_envoyes(cx)                     # tu as répondu ensuite → réglé
    seuil_attente = datetime.now(timezone.utc) - timedelta(hours=ATTENTE_APRES_H)
    for r in rows:
        m = dict(zip(cols, r))
        juge = _juge_par_ia(m["ia_source"]) and bool(m["ia_urgence"])
        if m["suspect"] == 1 or (juge and m["ia_categorie"] == "hameconnage"):
            n_susp += 1
            continue
        ext = _ext_json(m["ia_extensions"]) if juge else {}
        ech = m["ia_echeance"] if (juge and m["ia_echeance"] not in (None, "", "aucune")) else None
        e = {
            "id": m["message_id"], "de": m["de_adresse"], "nom": _nom_affiche(m["de_adresse"]),
            "sujet": m["sujet"] or "", "date": m["date_mail"], "juge": juge,
            "urgence": m["ia_urgence"] if juge else (m["urgence"] or "normale"),
            "tache": m["ia_tache"] if juge else None,
            "resume": m["ia_resume"] if juge else None,
            "pourquoi": m["ia_justification"] if juge else None,
            "question": m["ia_question"] if juge else None,
            "echeance": ech, "retard": bool(ech and ech < auj),
            "pour_moi": m["pour_moi"], "concerne": ext.get("concerne"),
            "case": case_du_jour(m, auj),
        }
        ev = ext.get("evenement")
        if isinstance(ev, dict) and ev.get("date") in (auj, demain) and not ev.get("annule"):
            jour = "aujourd'hui" if ev["date"] == auj else "demain"
            evenements.append({
                "jour": jour, "date": ev["date"], "heure": ev.get("heure") or "",
                "lieu": ev.get("lieu") or "", "nom": e["nom"],
                "quoi": e["tache"] or e["resume"] or _sujet_court(e["sujet"]),
                "quand": jour + (f" à {ev['heure']}" if ev.get("heure") else ""),
            })
        if repondu_apres(m, envois):
            continue
        dm = _dt_message(m["date_mail"] or "")
        if dm is not None and dm.tzinfo is None:
            dm = dm.replace(tzinfo=timezone.utc)
        if (juge and m["ia_action"] == 1 and e["concerne"] in (None, "moi")
                and dm is not None and dm < seuil_attente):
            # Demande restée sans réponse : elle passe devant, avec son ancienneté.
            e["case"] = "maintenant"
            e["sans_reponse_jours"] = max(1, (datetime.now(timezone.utc) - dm).days)
        recent = (m["date_mail"] or "") >= plancher
        if e["case"] in _CASES_TOPO and (recent or (ech and ech >= ech_min)):
            elements.append(e)

    rang_case = {c: i for i, c in enumerate(_CASES_TOPO)}

    def cle(e):
        d = _dt_message(e["date"])
        if d is not None and d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return (rang_case[e["case"]], NIVEAU_RANG.get(e["urgence"], 2),
                0 if e["pour_moi"] == 1 else 1, e["echeance"] or "9999-99-99",
                -(d.timestamp() if d else 0))
    elements.sort(key=cle)
    evenements.sort(key=lambda v: (v["date"], v["heure"] or "99:99"))
    anciens = cx.execute(
        "SELECT COUNT(*) FROM messages WHERE etat IS NULL AND date_mail < ? "
        "AND COALESCE(suspect,0)=0 AND ia_source IS NOT NULL AND ia_source != 'erreur' "
        "AND ia_action=1 AND ia_urgence IN ('critique','haute','normale')",
        (plancher,)).fetchone()[0]
    compte = {c: sum(1 for e in elements if e["case"] == c) for c in _CASES_TOPO}
    return {"elements": elements, "evenements": evenements, "compte": compte,
            "suspects": n_susp, "anciens": anciens, "aujourdhui": auj}


def _heure_dite(hhmm):
    try:
        h, mn = (int(x) for x in hhmm.split(":"))
    except Exception:
        return hhmm or ""
    base = "midi" if (h, mn) == (12, 0) else ("1 heure" if h == 1 else f"{h} heures")
    return base if mn == 0 or (h, mn) == (12, 0) else f"{base} {mn}"


def script_topo_gabarit(t, par_case=5):
    """Topo rédigé SANS IA (hors ligne, mode bouchon, panne) : mêmes éléments, même
    ordre, phrases construites par gabarit. Renvoie (court, complet)."""
    c = t["compte"]
    n_now, n_week, n_month = c["maintenant"], c["cette_semaine"], c["ce_mois"]
    if not (n_now or n_week or n_month):
        resume = "Bonjour. Rien ne demande votre attention dans les messages récents."
    else:
        resume = (f"Bonjour. Vous avez {n_now} sujet{'s' if n_now > 1 else ''} à traiter "
                  f"aujourd'hui, {n_week} pour cette semaine, et {n_month} à ne pas oublier "
                  f"d'ici la fin du mois.")
        tetes = [e for e in t["elements"] if e["case"] == "maintenant"][:2]
        if tetes:
            resume += " En priorité : " + " ; ".join(
                f"{e['nom']}, {(e['tache'] or e['resume'] or _sujet_court(e['sujet'])).rstrip('.')}"
                for e in tetes) + "."
    if t["suspects"]:
        n = t["suspects"]
        resume += (f" J'ai mis de côté {n} message{'s' if n > 1 else ''} "
                   f"suspect{'s' if n > 1 else ''}, à vérifier, que je ne lis pas.")
    lignes = [resume, ""]

    if t["evenements"]:
        lignes.append("D'abord, votre agenda.")
        for ev in t["evenements"]:
            quand = ev["jour"].capitalize() + (f" à {_heure_dite(ev['heure'])}" if ev["heure"] else "")
            ou = f", {ev['lieu']}" if ev["lieu"] else ""
            lignes.append(f"{quand} : {ev['quoi'].rstrip('.')}{ou}. Annoncé par {ev['nom']}.")
        lignes.append("")

    intros = {"maintenant": "Voici ce qui demande une action aujourd'hui.",
              "cette_semaine": "Pour cette semaine.",
              "ce_mois": "Et à garder en tête d'ici la fin du mois."}
    for case in _CASES_TOPO:
        els = [e for e in t["elements"] if e["case"] == case]
        if not els:
            continue
        rows = [(e["de"], e["tache"], e["resume"], e["echeance"], e["date"], e["sujet"], e)
                for e in els]
        groupes = _grouper_fils(rows)     # l'ordre d'entrée (le plus pressant d'abord) est gardé
        lignes.append(intros[case])
        maxg = max(par_case, 6)
        for g in groupes[:maxg]:
            gr = g["rows"]
            if len(gr) == 1:
                e = gr[0][6]
                lignes.append(_phrase_message(
                    e["de"], e["tache"], e["resume"], e["echeance"], e["date"],
                    sujet=e["sujet"], pourquoi=e["pourquoi"] if case == "maintenant" else None,
                    sans_reponse=e.get("sans_reponse_jours")))
            else:
                lignes.append(_phrase_groupe(gr))
        if len(groupes) > maxg:
            reste = len(groupes) - maxg
            lignes.append(f"Et {reste} autre sujet{'s' if reste > 1 else ''}, "
                          f"à voir dans le cockpit.")
        lignes.append("")
    if t["anciens"]:
        n = t["anciens"]
        lignes.append(f"Il reste aussi {n} sujet{'s' if n > 1 else ''} plus ancien"
                      f"{'s' if n > 1 else ''} encore ouvert{'s' if n > 1 else ''}, "
                      f"à voir dans le cockpit.")
    lignes.append("Voilà pour l'essentiel. Bonne journée.")
    return resume, "\n".join(lignes).strip()


def entree_topo_ia(t):
    """Message envoyé à l'IA pour rédiger le topo (format décrit dans moteur/README.md)."""
    lim = int(PARAMS.get("topo_elements_max", 40))
    L = ["DATE DU JOUR : " + _date_du_jour_txt(t["aujourdhui"]), "",
         "ÉVÉNEMENTS D'AUJOURD'HUI ET DE DEMAIN :"]
    if t["evenements"]:
        for ev in t["evenements"]:
            L.append(f"- {ev['quand']} — {ev['quoi']}"
                     + (f" — lieu : {ev['lieu']}" if ev["lieu"] else "")
                     + f" — annoncé par {ev['nom']}")
    else:
        L.append("(aucun)")
    L += ["", "MESSAGES À TRAITER, du plus pressant au moins pressant :"]
    titres = {"maintenant": "MAINTENANT", "cette_semaine": "CETTE SEMAINE", "ce_mois": "CE MOIS"}
    montres, case_courante = 0, None
    for e in t["elements"][:lim]:
        if e["case"] != case_courante:
            case_courante = e["case"]
            L.append(f"[{titres[case_courante]}]")
        montres += 1
        qui = {1: "adressé à la personne", 0: "en copie"}.get(e["pour_moi"], "diffusion")
        if e.get("concerne") == "autre":
            qui += ", action attendue de quelqu'un d'autre"
        L.append(f"{montres}. De : {e['nom']} — reçu {_recu_parle(e['date']) or '?'} — "
                 f"urgence {e['urgence']} — {qui}")
        L.append(f"   Objet : {_sujet_court(e['sujet'])}")
        if e["tache"]:
            L.append(f"   Tâche : {e['tache']}")
        if e["resume"]:
            L.append(f"   Résumé : {e['resume']}")
        if e["pourquoi"]:
            L.append(f"   Pourquoi : {e['pourquoi']}")
        if e["echeance"]:
            L.append(f"   Échéance : {e['echeance']} ({_echeance_parlee(e['echeance'])})")
        if e["question"]:
            L.append(f"   Question en suspens : {e['question']}")
        if e.get("reponse_attendue"):
            L.append(f"   Réponse attendue : {e['reponse_attendue']}")
        if e.get("sans_reponse_jours"):
            L.append(f"   Sans réponse de ta part depuis {e['sans_reponse_jours']} jour(s) (dossier Envoyés)")
        if not e["juge"]:
            L.append("   (pas encore jugé par l'IA : classé par les règles, seul l'objet est connu)")
    if not montres:
        L.append("(aucun)")
    c = t["compte"]
    L += ["", f"CHIFFRES : {c['maintenant']} à traiter maintenant, {c['cette_semaine']} cette "
              f"semaine, {c['ce_mois']} ce mois ; {max(0, len(t['elements']) - montres)} non "
              f"détaillés ci-dessus ; {t['suspects']} message(s) suspect(s) mis de côté ; "
              f"{t['anciens']} sujet(s) plus ancien(s) encore ouvert(s)."]
    return "\n".join(L)


def _nettoyer_parle(texte):
    """Retire ce qu'une voix lit mal : liens, adresses, symboles de mise en forme."""
    t = re.sub(r"https?://\S+|www\.\S+", "", texte or "")
    t = re.sub(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b", "", t)
    t = re.sub(r"[*_#`>|]+", "", t)
    t = re.sub(r"^\s*[-•]\s+", "", t, flags=re.M)
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def rediger_topo_ia(cfg, t, cx=None):
    """Fait RÉDIGER le topo par l'IA à partir des jugements (moteur/topo.txt).
    Renvoie (court, complet) ou None (pas d'IA, pas de matière, réponse inutilisable) :
    l'appelant retombe alors sur le gabarit local. Seuls des résumés déjà produits
    partent chez le fournisseur IA ; la voix reste locale. Mis en cache : réécouter le
    même topo ne coûte rien."""
    cfg_ia = _config_ia(cfg)
    if (not cfg.get("topo_ia", True) or cfg_ia.get("fournisseur", "mock") == "mock"
            or not cfg_ia.get("cle")):
        return None
    if not (t["elements"] or t["evenements"]):
        return None
    gabarit = _charger_gabarit(cfg, "topo.txt")
    if not gabarit:
        return None
    systeme = _rendre_gabarit(gabarit, {"utilisateur": _utilisateur_txt(cfg)})
    entree = entree_topo_ia(t)
    cle = hashlib.sha256((systeme + "\x00" + entree).encode("utf-8")).hexdigest()
    if cx is not None:
        r = cx.execute("SELECT court, complet FROM topo_cache WHERE cle=?", (cle,)).fetchone()
        if r:
            return r[0], r[1]
    try:
        res = _extraire_json(appeler_mistral(
            cfg_ia, systeme, entree, modele=cfg_ia.get("modele_topo") or None, temperature=0.3))
        court = _nettoyer_parle(res.get("court") if isinstance(res, dict) else "")
        complet = _nettoyer_parle(res.get("complet") if isinstance(res, dict) else "")
    except Exception as e:
        print(f"[{NOM}] topo IA indisponible ({e}) : gabarit local.", file=sys.stderr)
        return None
    if not (20 <= len(court) <= 1200 and 40 <= len(complet) <= 6000):
        print(f"[{NOM}] topo IA hors gabarit (longueurs {len(court)}/{len(complet)}) : "
              f"gabarit local.", file=sys.stderr)
        return None
    if t["suspects"] and "suspect" not in complet.lower():
        n = t["suspects"]
        complet += (f"\n\nJ'ai aussi mis de côté {n} message{'s' if n > 1 else ''} "
                    f"suspect{'s' if n > 1 else ''}, à vérifier.")
    if cx is not None:
        cx.execute("DELETE FROM topo_cache WHERE at < ?",
                   ((datetime.now(timezone.utc) - timedelta(days=3)).isoformat(),))
        cx.execute("INSERT OR REPLACE INTO topo_cache (cle, court, complet, at) VALUES (?,?,?,?)",
                   (cle, court, complet, datetime.now(timezone.utc).isoformat()))
        cx.commit()
    return court, complet


def construire_script_topo(cfg, par_case=5, utiliser_ia=True):
    """Renvoie (résumé 30 s, script complet, source) ; source = 'ia' ou 'gabarit'."""
    charger_moteur(cfg)
    cx = connexion(cfg["base_sqlite"])
    _assurer_colonnes(cx)
    try:
        t = elements_topo(cx, cfg)
        if utiliser_ia:
            redige = rediger_topo_ia(cfg, t, cx)
            if redige:
                return redige[0], redige[1], "ia"
        court, complet = script_topo_gabarit(t, par_case)
        return court, complet, "gabarit"
    finally:
        cx.close()


def generer_audio(texte, cfg, sortie):
    """Synthèse vocale locale (espeak-ng par défaut, piper si configuré). Renvoie (chemin, erreur)."""
    tts = cfg.get("tts", {})
    moteur = tts.get("moteur", "espeak-ng")
    wav = sortie if sortie.endswith(".wav") else sortie + ".wav"
    Path(wav).parent.mkdir(parents=True, exist_ok=True)
    try:
        if moteur == "piper" and tts.get("modele"):
            # 'piper' en commande, sinon via le module Python (si pas dans le PATH)
            base = ["piper"] if shutil.which("piper") else [sys.executable, "-m", "piper"]
            subprocess.run(base + ["-m", tts["modele"], "-f", wav],
                           input=texte.encode("utf-8"), check=True, capture_output=True)
        else:
            exe = shutil.which("espeak-ng") or shutil.which("espeak")
            if not exe:
                return None, "aucun moteur de synthèse (installe espeak-ng : sudo apt install espeak-ng)"
            subprocess.run([exe, "-v", tts.get("voix", "fr"),
                            "-s", str(tts.get("vitesse", 150)), "-w", wav, texte],
                           check=True, capture_output=True)
    except Exception as e:
        return None, str(e)
    # Convertit en mp3 (plus léger, pour le téléphone) si ffmpeg est là.
    if shutil.which("ffmpeg"):
        mp3 = wav[:-4] + ".mp3"
        try:
            subprocess.run(["ffmpeg", "-y", "-i", wav, "-codec:a", "libmp3lame",
                            "-q:a", "5", mp3], check=True, capture_output=True)
            return mp3, None
        except Exception:
            pass
    return wav, None


def cmd_topo_audio(args):
    cfg = charger_config(args.config)
    court, complet, source = construire_script_topo(
        cfg, args.par_case, utiliser_ia=not getattr(args, "sans_ia", False))
    print(f"[{NOM}] topo " + ("rédigé par l'IA" if source == "ia" else "construit par gabarit local"))
    print("=== Topo (30 secondes) ===\n" + court + "\n")
    print("=== Script complet ===\n" + complet + "\n")
    sortie = args.sortie or str(Path(cfg.get("dossier_destination") or ".") / "topo.wav")
    chemin, err = generer_audio(complet, cfg, sortie)
    if chemin:
        print(f"[{NOM}] audio généré : {chemin}")
    else:
        print(f"[{NOM}] script prêt, mais pas d'audio : {err}")


# ----------------------------------------------------------------------------
# Le service — relève + jugement en continu, et sert le cockpit
# ----------------------------------------------------------------------------
def pipeline_once(cfg_path, utiliser_ia=True, ia_max=20):
    """Un cycle : relève des mbox -> analyse déterministe -> jugement IA (du nouveau)."""
    try:
        cmd_import(types.SimpleNamespace(config=cfg_path, mbox=None, limite=None))
    except SystemExit as e:
        print(f"[{NOM}] (relève) {e}")
    except Exception as e:
        print(f"[{NOM}] relève: {e}")
    try:
        cmd_analyser(types.SimpleNamespace(config=cfg_path, retout=False, silencieux=True))
    except Exception as e:
        print(f"[{NOM}] analyse: {e}")
    if utiliser_ia and lire_relie(charger_config(cfg_path))["relie"]:
        print(f"[{NOM}] relié à GesEnt : jugement IA fait par GesEnt (pas d'appel IA local)")
        utiliser_ia = False
    if utiliser_ia:
        try:
            cmd_analyser_ia(types.SimpleNamespace(
                config=cfg_path, mock=False, retout=False, limite=ia_max, jours=None))
        except SystemExit as e:
            print(f"[{NOM}] (IA) {e}")
        except Exception as e:
            print(f"[{NOM}] IA: {e}")
    # Retour-mail Outlook / Apple Mail : l'agent applique « Traité » (étiquette ou
    # drapeau + lu) et relit les changements faits dans le client — en direct, même
    # processus, pas d'extension.
    try:
        cfg = charger_config(cfg_path)
        if _collecteur(cfg) == "outlook":
            import collecteur_outlook as outlook
            if outlook.disponible():
                cx = connexion(cfg["base_sqlite"])
                try:
                    outlook.appliquer_retour(cfg, cx, sys.modules[__name__])
                finally:
                    cx.close()
        elif _collecteur(cfg) == "applemail":
            import collecteur_applemail as applemail
            if applemail.disponible():
                cx = connexion(cfg["base_sqlite"])
                try:
                    applemail.appliquer_retour(cfg, cx, sys.modules[__name__])
                finally:
                    cx.close()
    except Exception as e:
        print(f"[{NOM}] retour-mail: {e}")


def cmd_service(args):
    cfg = charger_config(args.config)
    v = charger_moteur(cfg)
    print(f"[{NOM}] moteur de tri version {v}")
    cx = connexion(cfg["base_sqlite"])
    _assurer_colonnes(cx)
    backfill_non_lu(cx)
    cx.close()
    intervalle = max(60, args.intervalle)
    utiliser_ia = not args.sans_ia
    ia_max = cfg.get("ia_max_par_cycle", 20)

    arret = threading.Event()

    def boucle():
        while not arret.is_set():
            print(f"[{NOM}] — cycle de relève/jugement —")
            pipeline_once(args.config, utiliser_ia, ia_max)
            arret.wait(intervalle)

    threading.Thread(target=boucle, daemon=True).start()

    port = args.port
    srv = http.server.HTTPServer(("127.0.0.1", port), faire_cockpit_handler(cfg))
    url = f"http://127.0.0.1:{port}/"
    barre = "=" * 54
    print("\n" + barre)
    print("  IAMail — service actif")
    print(f"  Cockpit  : {url}")
    print(f"  Relève + jugement toutes les {intervalle // 60} min"
          + ("  (sans IA)" if not utiliser_ia else ""))
    print("  Ctrl+C pour arrêter")
    print(barre + "\n")
    if not args.pas_de_navigateur:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print(f"\n[{NOM}] arrêt du service.")
        arret.set()
        srv.server_close()


# ----------------------------------------------------------------------------
# Entrée
# ----------------------------------------------------------------------------
def _version_complete():
    """« IAMail-compagnon 1.19.0 — moteur 2.3.0 (…/moteur) » : dit aussi QUEL moteur est
    chargé — celui posé à côté de l'exécutable l'emporte sur la copie embarquée."""
    try:
        d = _moteur_dir({})
        m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        return f"{NOM} {VERSION} — moteur {m.get('version', '?')} ({d})"
    except Exception as e:
        return f"{NOM} {VERSION} — moteur introuvable ({e})"


def _sorties_sures():
    """Sous Windows, une sortie redirigée (service, fichier, CI) est en cp1252 : un émoji
    ou un caractère hors table faisait planter le programme. On remplace au lieu de
    planter ; l'encodage choisi par le système ne change pas.
    Ligne par ligne : sous systemd (sortie vers le journal, pas un terminal), Python
    gardait tout en mémoire tampon et `journalctl` restait vide pendant des heures."""
    for flux in (sys.stdout, sys.stderr):
        try:
            flux.reconfigure(errors="replace", line_buffering=True)
        except Exception:
            pass


def cmd_diagnostic(args):
    """Ce que le compagnon lit vraiment : sources, Envoyés, dernière arrivée en base."""
    cfg = charger_config(args.config)
    print(_version_complete())
    print(f"collecteur : {_collecteur(cfg)}")
    e = etat_sources(cfg)
    def ligne(x):
        if not x["existe"]:
            return f"  ✗ INTROUVABLE  {x['chemin']}"
        d = _dt_message(x.get("modifie"))
        m = _dt_message(x.get("dernier_message") or "")
        return (f"  • {x['chemin']}\n      modifié le {_local(d).strftime('%d/%m/%Y %H:%M') if d else '?'}"
                f" · {x['taille'] / 1048576:.0f} Mo · dernier message du "
                f"{_local(m).strftime('%d/%m/%Y %H:%M') if m else '?'}")
    print("\nBoîte(s) de réception lue(s) (mbox_sources) :")
    for x in e["sources"] or [{"chemin": "(aucune)", "existe": False}]:
        print(ligne(x))
    print("\nDossier(s) Envoyés :")
    for x in e["envoyes"] or [{"chemin": "(aucun trouvé : « envoyes_sources »)", "existe": False}]:
        print(ligne(x))
    if e["autres"]:
        print("\nAutres boîtes de réception Thunderbird, PLUS RÉCENTES que celle lue :")
        for x in e["autres"]:
            print(ligne(x))
    try:
        cx = _ouvrir_db(cfg["base_sqlite"])
        r = cx.execute("SELECT MAX(date_mail), COUNT(*) FROM messages").fetchone()
        rs = cx.execute("SELECT MAX(date_envoi), COUNT(*) FROM envoyes").fetchone()
        cx.close()
        d = _dt_message(r[0] or "")
        ds = _dt_message(rs[0] or "")
        print(f"\nBase : {r[1]} message(s) reçu(s), le plus récent du "
              f"{_local(d).strftime('%d/%m/%Y %H:%M') if d else '?'} ; {rs[1]} envoi(s) indexé(s), "
              f"le plus récent du {_local(ds).strftime('%d/%m/%Y %H:%M') if ds else '?'}")
    except Exception as x:
        print(f"\nBase illisible : {x}")
    for a in alertes_sources(cfg):
        print("⚠ " + a)


def main(argv=None):
    _sorties_sures()
    ap = argparse.ArgumentParser(prog="iamail_compagnon",
                                 description=f"{NOM} v{VERSION}")
    ap.add_argument("--config", default="config.json", help="fichier de configuration")
    ap.add_argument("--version", action="version", version=_version_complete(),
                    help="affiche la version du compagnon et du moteur utilisé")
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

    s = sub.add_parser("analyser", help="classe les messages (règles déterministes)")
    s.add_argument("--retout", action="store_true", help="ré-analyse tout, pas seulement le nouveau")
    s.set_defaults(func=cmd_analyser)

    s = sub.add_parser("topo", help="aperçu priorisé déterministe")
    s.add_argument("--nombre", type=int, default=20)
    s.set_defaults(func=cmd_topo)

    s = sub.add_parser("analyser-ia", help="jugement IA (Mistral, ou --mock pour tester)")
    s.add_argument("--mock", action="store_true", help="modèle bouchonné, sans clé")
    s.add_argument("--retout", action="store_true", help="rejuger même ce qui l'a déjà été")
    s.add_argument("--limite", type=int, default=None, help="nb max de messages")
    s.add_argument("--jours", type=int, default=None, help="limiter aux N derniers jours")
    s.add_argument("--compter", action="store_true",
                   help="affiche seulement combien de messages seraient envoyés (aucun coût)")
    s.set_defaults(func=cmd_analyser_ia)

    s = sub.add_parser("reset-ia", help="efface les jugements IA (pour rejuger)")
    s.set_defaults(func=cmd_reset_ia)

    s = sub.add_parser("extraire-corps",
                       help="rattrape l'extrait du corps des mails déjà archivés (relit les .eml)")
    s.add_argument("--retout", action="store_true",
                   help="ré-extrait même les mails qui ont déjà un extrait")
    s.set_defaults(func=cmd_extraire_corps)

    s = sub.add_parser("cockpit", help="ouvre la fenêtre de tri (serveur web local)")
    s.add_argument("--port", type=int, default=8740)
    s.add_argument("--pas-de-navigateur", action="store_true", dest="pas_de_navigateur")
    s.set_defaults(func=cmd_cockpit)

    s = sub.add_parser("service", help="relève + juge en continu ET sert le cockpit")
    s.add_argument("--port", type=int, default=8740)
    s.add_argument("--intervalle", type=int, default=300, help="secondes entre deux relèves")
    s.add_argument("--sans-ia", action="store_true", dest="sans_ia", help="relève+tri sans appeler l'IA (sans coût)")
    s.add_argument("--pas-de-navigateur", action="store_true", dest="pas_de_navigateur")
    s.set_defaults(func=cmd_service)

    s = sub.add_parser("brief", help="topo du matin en cases (d'après l'IA)")
    s.add_argument("--nombre", type=int, default=15)
    s.set_defaults(func=cmd_brief)

    s = sub.add_parser("diagnostic", help="ce que le compagnon lit vraiment (sources, Envoyés, base)")
    s.set_defaults(func=cmd_diagnostic)
    s = sub.add_parser("topo-audio", help="génère le topo du matin en audio (voix locale)")
    s.add_argument("--par-case", type=int, default=5, dest="par_case")
    s.add_argument("--sortie", default=None, help="chemin du fichier audio")
    s.add_argument("--sans-ia", action="store_true", dest="sans_ia",
                   help="ne pas faire rédiger le topo par l'IA (gabarit local, sans coût)")
    s.set_defaults(func=cmd_topo_audio)

    s = sub.add_parser("suggerer-adresses", help="propose tes adresses pour 'mes_adresses'")
    s.set_defaults(func=cmd_suggerer_adresses)

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

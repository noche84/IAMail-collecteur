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
from datetime import datetime, timezone
from pathlib import Path

NOM = "iamail-compagnon"
VERSION = "1.14.0"

CONFIG_DEFAUT = {
    "dossier_staging": "",
    "dossier_destination": "",
    "base_sqlite": "iamail_index.sqlite3",
    "deplacer": True,
    "mes_adresses": [],
    "expediteurs_importants": [],
    "moteur_dir": "",
    "ia_jours": 0,
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
    try:
        with open(dest_eml, "rb") as f:
            msg_obj = email.message_from_binary_file(f)
        extrait = _corps_texte(msg_obj, int(cfg.get("ia_extrait_max", PARAMS["ia_extrait_max_defaut"])))
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
    texte = re.sub(r'[ \t]+', ' ', texte)
    texte = re.sub(r'\n{3,}', '\n\n', texte).strip()
    return texte[:maxlen]


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

            extrait = _corps_texte(msg, int(cfg.get("ia_extrait_max", PARAMS["ia_extrait_max_defaut"])))
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
    "ia_extrait_max_defaut": 2000,
    "topo_intervenants_max": 5,
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
    """Minuscule, sans accents, apostrophes → espaces (pour un matching par mots)."""
    s = _sans_accents((s or "").lower())
    return s.replace("'", " ").replace("’", " ")


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


def _est_important(texte, categorie, de_spec, importants):
    """Touche à l'argent / la sécurité / une transaction / un contrat ?"""
    if categorie in CATEGORIES_IMPORTANTES:
        return True
    dom = _domaine(de_spec)
    if de_spec in importants or dom in importants:
        return True
    if _domaine_contient(dom, DOMAINES_IMPORTANTS):
        return True
    return _contient_mot(texte, SUJETS_IMPORTANTS)


def classer(h, mes_adresses, importants=()):
    """
    h : dict d'en-têtes décodés (from, to, cc, subject, list_unsubscribe,
        list_id, precedence, auto_submitted, reply_to, return_path).
    mes_adresses : liste d'adresses de l'utilisateur (minuscules).
    importants : adresses/domaines à toujours remonter (config).
    Renvoie un dict de classement.
    """
    de = (h.get("from") or "")
    de_spec = (_adresses_spec(de) or [""])[0]
    de_local = de_spec.split("@", 1)[0] if de_spec else ""
    sujet = h.get("subject") or ""
    texte = _norm_texte(sujet + " " + de)

    to = _adresses_spec(h.get("to"))
    cc = _adresses_spec(h.get("cc"))
    mes = set(mes_adresses or [])

    # --- Humain vs automatique / en masse ---------------------------------
    humain = _expediteur_humain(de_spec, de_local)
    bulk = bool(
        h.get("list_unsubscribe") or h.get("list_id")
        or (h.get("precedence", "").lower() in PRECEDENCE_BULK)
        or de_local in EXPEDITEURS_AUTO
        or _domaine_marketing(_domaine(de_spec))
    )
    automatique = (not humain) or bulk

    # --- À moi / juste en copie / liste -----------------------------------
    if mes & set(to):
        pour_moi = True        # je suis destinataire direct → action probable
    elif mes & set(cc):
        pour_moi = False       # seulement en copie → pour info
    else:
        pour_moi = None        # ni l'un ni l'autre → liste / diffusion

    # --- Catégorie ---------------------------------------------------------
    categorie, conf_cat = "autre", "faible"
    if STRATEGIE_CATEGORIE == "mot_le_plus_long":
        # Parmi les catégories qui matchent, garder celle dont le mot-clé matché
        # est le plus long (« suivi de commande » bat « commande ») ; 1re à égalité.
        meilleure, meilleur_len = None, -1
        for nom, mots in REGLES_CATEGORIE:
            mm = _mot_matche(texte, mots)
            if mm is not None and len(mm) > meilleur_len:
                meilleure, meilleur_len = nom, len(mm)
        if meilleure:
            categorie, conf_cat = meilleure, "moyenne"
    else:  # "premiere" : ancien comportement
        for nom, mots in REGLES_CATEGORIE:
            if _contient_mot(texte, mots):
                categorie, conf_cat = nom, "moyenne"
                break
    if categorie == "autre" and bulk:
        dom = _domaine(de_spec)
        categorie = ("reseau_social"
                     if any(r in dom for r in DOMAINES_RESEAU_SOCIAL)
                     else "newsletter")
        conf_cat = "moyenne"

    # --- Soupçon d'hameçonnage (léger, jamais d'action automatique) --------
    reply_to = (_adresses_spec(h.get("reply_to")) or [""])[0]
    suspect = bool(
        reply_to and _domaine(reply_to) and de_spec
        and _domaine(reply_to) != _domaine(de_spec)
        and _contient_mot(texte, MOTS_SUSPECTS)
    )

    # --- Urgence -----------------------------------------------------------
    # Le vocabulaire d'urgence ne compte que s'il vient d'un humain : sinon
    # c'est du marketing qui crie « urgent / rappel / échéance ».
    a_mot_urgent = _contient_mot(texte, MOTS_URGENTS)
    if bulk:
        urgence = "basse"
    elif a_mot_urgent and humain:
        urgence = "haute"
    elif a_mot_urgent:
        urgence = "normale"            # mot d'urgence mais expéditeur automatique
    else:
        urgence = "normale"

    # --- Important « à lire » (argent, sécurité, transaction, contrat) -----
    important = _est_important(texte, categorie, de_spec, importants)

    # --- Priorité (la synthèse qui sert le topo) --------------------------
    if pour_moi and not bulk and humain and urgence == "haute":
        priorite = "haute"
    elif pour_moi and not bulk and humain:
        priorite = "normale"
    elif pour_moi and not bulk:
        priorite = "normale"           # adressé à toi, émetteur auto (transactionnel)
    else:
        priorite = "basse"

    # FILET DE SÉCURITÉ : ce qui est important n'est JAMAIS enterré en "basse".
    if important and priorite == "basse":
        priorite = "normale"

    # --- Raison lisible ----------------------------------------------------
    bouts = []
    bouts.append("adressé à toi" if pour_moi else
                 ("juste en copie" if pour_moi is False else "diffusion/liste"))
    bouts.append("expéditeur humain" if humain else "expéditeur automatique")
    if important:
        bouts.append("⭐ à lire (" + categorie + ")" if categorie not in ("autre", "newsletter")
                     else "⭐ à lire (argent/sécurité)")
    if bulk:
        bouts.append("courrier en masse")
    if urgence == "haute":
        bouts.append("marqueur d'urgence")
    elif a_mot_urgent and not humain:
        bouts.append("urgence marketing (ignorée)")
    if suspect:
        bouts.append("⚠ expéditeur de réponse différent")
    raison = ", ".join(bouts)

    return {
        "categorie": categorie,
        "urgence": urgence,
        "pour_moi": None if pour_moi is None else int(pour_moi),
        "bulk": int(bulk),
        "automatique": int(automatique),
        "expediteur_humain": int(humain),
        "important": int(important),
        "suspect": int(suspect),
        "priorite": priorite,
        "raison": raison,
        "confiance": conf_cat,
    }


COLONNES_ANALYSE = {
    "categorie": "TEXT", "urgence": "TEXT", "pour_moi": "INTEGER",
    "bulk": "INTEGER", "automatique": "INTEGER", "expediteur_humain": "INTEGER",
    "important": "INTEGER", "suspect": "INTEGER", "priorite": "TEXT",
    "raison": "TEXT", "confiance": "TEXT", "analyse_source": "TEXT", "analyse_at": "TEXT",
    "ia_categorie": "TEXT", "ia_action": "INTEGER", "ia_tache": "TEXT",
    "ia_echeance": "TEXT", "ia_pression": "TEXT", "ia_priorite": "TEXT",
    "ia_resume": "TEXT", "ia_question": "TEXT", "ia_confiance": "TEXT",
    "ia_source": "TEXT", "ia_at": "TEXT",
    "non_lu": "INTEGER", "etat": "TEXT",
    "tb_sync": "TEXT",   # état déjà appliqué dans Thunderbird (retour-mail bidirectionnel)
    "extrait": "TEXT",   # corps du mail en texte, pour l'analyse IA
    "ia_moteur_version": "TEXT",   # version du moteur qui a produit le jugement
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
    charger_regles(cfg)
    mes = [a.lower() for a in cfg.get("mes_adresses", [])]
    importants = {a.lower() for a in cfg.get("expediteurs_importants", [])}
    if not mes:
        print("⚠ 'mes_adresses' est vide : le « à moi / juste en copie » ne pourra "
              "pas être déterminé.\n  Lance d'abord :  suggerer-adresses")

    where = "" if args.retout else " WHERE categorie IS NULL"
    rows = cx.execute(
        "SELECT message_id, fichier_eml FROM messages" + where
    ).fetchall()
    total = len(rows)
    print(f"[{NOM}] analyse de {total} message(s)…")

    faits, erreurs = 0, 0
    t0 = time.time()
    for mid, chemin in rows:
        if not chemin or not Path(chemin).exists():
            erreurs += 1
            continue
        try:
            h = _entetes_du_eml(chemin)
            c = classer(h, mes, importants)
        except Exception:
            erreurs += 1
            continue
        cx.execute(
            """UPDATE messages SET categorie=?, urgence=?, pour_moi=?, bulk=?,
               automatique=?, expediteur_humain=?, important=?, suspect=?, priorite=?,
               raison=?, confiance=?, analyse_source=?, analyse_at=? WHERE message_id=?""",
            (c["categorie"], c["urgence"], c["pour_moi"], c["bulk"],
             c["automatique"], c["expediteur_humain"], c["important"], c["suspect"],
             c["priorite"], c["raison"], c["confiance"], "regles",
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
    print("\nPar priorité :")
    for p, n in compter("SELECT priorite, COUNT(*) FROM messages WHERE categorie IS NOT NULL "
                        "GROUP BY priorite ORDER BY n DESC".replace("n DESC", "COUNT(*) DESC")):
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
    # Par défaut : dossier moteur/ à côté du script, puis ../moteur.
    ici = Path(__file__).resolve().parent
    for cand in (ici / "moteur", ici.parent / "moteur"):
        if cand.exists():
            return cand
    return ici / "moteur"


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
    if isinstance(d.get("parametres"), dict):
        g["PARAMS"] = {**PARAMS, **d["parametres"]}
    # Recompiler la regex de préfixes de fil si la liste a changé.
    g["_PREFIXE_FIL"] = _compiler_prefixe_fil(FIL_PREFIXES)
    return True


def charger_prompt(cfg):
    p = _moteur_dir(cfg) / "prompt.txt"
    if p.exists():
        return p.read_text(encoding="utf-8")
    return ("Tu classes un email. Réponds en JSON strict avec categorie, "
            "action_attendue, tache, echeance, pression, priorite, resume, "
            "question, confiance.")


def charger_taxonomie(cfg):
    p = _moteur_dir(cfg) / "taxonomie.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8")).get("categories", [])
        except Exception:
            pass
    return [{"nom": "autre", "description": "Tout le reste."}]


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
_RE_CATEGORIE = re.compile(r'^[a-z_]{1,40}$')


def valider_sortie(res, taxo_noms):
    """Valide et normalise le jugement IA contre le contrat (sortie.schema.json).
    Renvoie (ok, res_normalise, erreur). Échec → le harnais écrit ia_source='erreur'
    et retombe sur le déterministe (aucune valeur IA non validée en base)."""
    REQUIS = ("categorie", "action_attendue", "tache", "echeance", "pression",
              "priorite", "resume", "question", "confiance")
    if not isinstance(res, dict):
        return False, None, "réponse non-objet"
    inconnues = [k for k in res if k not in REQUIS]
    if inconnues:
        print(f"[{NOM}] clés IA inconnues ignorées : {inconnues}", file=sys.stderr)
    r = {k: res.get(k) for k in REQUIS}
    for k in ("tache", "question"):               # "" → null
        if isinstance(r[k], str) and not r[k].strip():
            r[k] = None
    if not isinstance(r["action_attendue"], bool):
        return False, None, "action_attendue non booléen"
    if r["pression"] not in ("maintenant", "cette_semaine", "ce_mois", "aucune"):
        return False, None, "pression invalide"
    if r["priorite"] not in ("haute", "normale", "basse"):
        return False, None, "priorite invalide"
    if r["confiance"] not in ("haute", "moyenne", "faible"):
        return False, None, "confiance invalide"
    if not isinstance(r["resume"], str) or not (1 <= len(r["resume"]) <= 400):
        return False, None, "resume invalide"
    if r["tache"] is not None and (not isinstance(r["tache"], str) or len(r["tache"]) > 300):
        return False, None, "tache invalide"
    if r["question"] is not None and (not isinstance(r["question"], str) or len(r["question"]) > 400):
        return False, None, "question invalide"
    if not isinstance(r["categorie"], str) or not _RE_CATEGORIE.match(r["categorie"]):
        return False, None, "categorie format"
    if not isinstance(r["echeance"], str) or not _RE_ECHEANCE.match(r["echeance"]):
        return False, None, "echeance format"
    if r["categorie"] not in taxo_noms:          # contrainte hors-schéma
        r["categorie"] = "autre"
    return True, r, None


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
    """Clé de fil de discussion : objet normalisé (sans Re:/Tr:, sans [tags], sans accents)."""
    s = _sans_accents((sujet or "").lower())
    s = _PREFIXE_FIL.sub("", s)
    s = re.sub(r'\[[^\]]*\]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def profils_expediteurs(cx):
    """Mémoire de l'historique. Renvoie (profils, fils).

    profils[adresse] = volume, part de mails 'urgent', récence (30 j), premier/
    dernier contact, fil principal — pour ne pas traiter un habitué comme un inconnu.
    fils[clé] = combien de messages partagent ce sujet et combien sont déjà 'chauds'
    (importants ou urgents) — pour reconnaître un sujet suivi de près.
    """
    profils, fils = {}, {}
    now = datetime.now(timezone.utc)

    def _jours_depuis(d):
        if not d:
            return None
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return (now - d).days

    rows = cx.execute(
        "SELECT de_adresse, sujet, date_mail, COALESCE(ia_priorite,''), "
        "COALESCE(ia_pression,''), COALESCE(important,0) FROM messages").fetchall()
    for de, sujet, dm, iaprio, iapress, imp in rows:
        d = _dt_message(dm)
        jd = _jours_depuis(d)
        recent = jd is not None and jd <= PARAMS["fenetre_recent_jours"]
        urgent_ton = _contient_mot(_norm_texte(sujet or ""), MOTS_URGENTS)
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

        if fkey:
            f = fils.setdefault(fkey, {"n": 0, "chauds": 0, "sujet": sujet or fkey})
            f["n"] += 1
            if iaprio == "haute" or iapress in ("maintenant", "cette_semaine") or imp == 1:
                f["chauds"] += 1

    for p in profils.values():
        p["pct_urgent"] = round(p["urgents"] / p["n"], 2) if p["n"] else 0.0
        p["fil_principal"] = (max(p["_fils"].items(), key=lambda kv: kv[1])
                              if p["_fils"] else None)
        jd = _jours_depuis(p["premier"])
        p["anciennete_j"] = jd
    return profils, fils


def _profil_en_clair(profil):
    """Résume le profil expéditeur en une phrase lisible pour l'IA."""
    if not profil:
        return "expéditeur inconnu (premier contact)"
    bits = [f"{profil['n']} message(s) reçus de cet expéditeur"]
    if profil.get("recent"):
        bits.append(f"dont {profil['recent']} sur les 30 derniers jours")
    bits.append(f"{int(profil['pct_urgent'] * 100)}% au ton « urgent »")
    if (profil["n"] >= PARAMS["profil_actif_n_min"]
            and profil.get("recent", 0) >= PARAMS["profil_actif_recent_min"]):
        bits.append("CORRESPONDANT RÉCURRENT ET ACTIF — ne le traite pas comme un inconnu")
    elif profil["n"] >= 5:
        bits.append("correspondant connu de longue date")
    return ", ".join(bits)


def _memoire_fil(sujet, fils):
    """Phrase 'mémoire' sur le fil de discussion de ce message, ou '' si rien de notable."""
    fkey = _sujet_fil(sujet)
    f = fils.get(fkey) if fkey else None
    if not f or f["n"] < 2:
        return ""
    autres = f["n"] - 1
    phrase = (f"fil « {f['sujet'][:60]} » : {autres} autre(s) message(s) sur ce même "
              f"sujet dans la boîte")
    if f["chauds"]:
        phrase += f", dont {f['chauds']} déjà jugé(s) important(s) ou urgent(s)"
    return phrase + ". Sujet suivi — tiens-en compte."


def _liste_adresses(valeur, maxi=10):
    """Formatte une liste d'adresses (stockée en JSON) pour l'IA, tronquée."""
    try:
        arr = json.loads(valeur) if isinstance(valeur, str) else (valeur or [])
    except Exception:
        arr = []
    arr = [str(a) for a in arr if a]
    if not arr:
        return ""
    if len(arr) > maxi:
        return ", ".join(arr[:maxi]) + f" (+{len(arr) - maxi})"
    return ", ".join(arr)


def construire_entree_ia(row, profil, fils, taxonomie, aujourdhui):
    """Texte compact envoyé à l'IA pour UN message (en plus du prompt système)."""
    cats = ", ".join(f"{c['nom']} ({c['description']})" for c in taxonomie)
    prof = _profil_en_clair(profil)
    memo = _memoire_fil(row.get("sujet"), fils)
    # Le corps est déjà tronqué à ia_extrait_max à l'EXTRACTION : pas de re-plafond ici.
    extrait = (row.get("extrait") or "").strip() or "(corps vide ou non texte)"
    a = _liste_adresses(row.get("destinataires"))
    cc = _liste_adresses(row.get("copies"))
    pre = []
    pre.append("destinataire: " + ("à moi" if row["pour_moi"] == 1 else
               ("en copie" if row["pour_moi"] == 0 else "diffusion")))
    pre.append("expéditeur " + ("humain" if row["expediteur_humain"] else "automatique"))
    if row["bulk"]:
        pre.append("courrier de masse")
    if row["important"]:
        pre.append("marqué important")
    if row.get("suspect"):
        pre.append("soupçon d'hameçonnage (Reply-To différent)")
    return (
        f"DATE DU JOUR: {aujourdhui}\n"
        f"CATÉGORIES POSSIBLES: {cats}\n"
        f"PROFIL EXPÉDITEUR: {prof}\n"
        + (f"MÉMOIRE: {memo}\n" if memo else "")
        + f"PRÉ-CLASSEMENT AUTOMATIQUE: {', '.join(pre)}\n"
        f"--- EMAIL ---\n"
        f"De: {row['de_adresse']}\n"
        + (f"À: {a}\n" if a else "")
        + (f"Cc: {cc}\n" if cc else "")
        + f"Objet: {row['sujet']}\n"
        f"Date: {row['date_mail']}\n"
        f"Extrait: {extrait}\n"
    )


def appeler_mistral(cfg_ia, systeme, utilisateur):
    """Appel chat completions compatible OpenAI (Mistral). Renvoie le texte brut."""
    corps = json.dumps({
        "model": cfg_ia.get("modele", "mistral-small-latest"),
        "temperature": 0,
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
    """Modèle BOUCHONNÉ pour tester tout le pipeline sans clé Mistral.
    Imite un jugement plausible à partir des signaux déterministes + profil."""
    # Aligné sur le vrai moteur : matching par MOT ENTIER (pas sous-chaîne).
    texte = _norm_texte((row["sujet"] or "") + " " + (row["de_adresse"] or ""))
    a_urgent = _contient_mot(texte, MOTS_URGENTS)
    crie_au_loup = bool(profil and profil["n"] >= PARAMS["profil_actif_n_min"]
                        and profil["pct_urgent"] >= 0.5)
    finance = bool(row["important"] and _contient_mot(texte,
              ("compte", "virement", "transfert", "transfer", "crypto", "bitcoin",
               "paiement", "facture", "securite", "fraude", "bloque", "suspendu")))
    # Notification transactionnelle (déjà faite) : information, pas une action.
    notif = _contient_mot(texte,
                ("successful", "reussi", "recu", "recue", "confirmation", "confirme",
                 "login", "connexion", "withdrawal", "deposit",
                 "statement", "releve", "receipt", "welcome", "bienvenue",
                 "invitation", "horoscope"))
    cat = row["categorie"] or "autre"

    if notif:
        prio, pression, action, tache = "basse", "aucune", False, None
    elif finance:
        prio, pression, action = "haute", "maintenant", True
        tache = "Vérifier ce message financier/sécurité"
    elif row["expediteur_humain"] and a_urgent and not crie_au_loup:
        prio, pression, action = "haute", "maintenant", True
        tache = row["sujet"]
    elif row["expediteur_humain"] and a_urgent and crie_au_loup:
        prio, pression, action = "normale", "ce_mois", True
        tache = row["sujet"]
    elif row["important"]:
        prio, pression, action = "normale", "cette_semaine", True
        tache = row["sujet"]
    elif row["bulk"]:
        prio, pression, action = "basse", "aucune", False
        tache = None
    else:
        prio, pression, action = "normale", "cette_semaine", bool(row["pour_moi"] == 1)
        tache = row["sujet"] if row["pour_moi"] == 1 else None

    question = None
    if crie_au_loup and a_urgent:
        question = f"« {(row['sujet'] or '')[:40]} » est marqué urgent, mais cet expéditeur l'écrit souvent — vraiment pour maintenant ?"
    return {
        "categorie": cat, "action_attendue": action, "tache": tache,
        "echeance": "aucune", "pression": pression, "priorite": prio,
        "resume": ((row["sujet"] or "").strip() or "(sans objet)")[:400],
        "question": question, "confiance": "moyenne",
    }


def cmd_analyser_ia(args):
    cfg = charger_config(args.config)
    charger_regles(cfg)
    cx = connexion(cfg["base_sqlite"])
    _assurer_colonnes(cx)
    cfg_ia = dict(cfg.get("ia", {}))
    if not cfg_ia.get("cle"):
        cfg_ia["cle"] = os.environ.get("MISTRAL_API_KEY", "")
    fournisseur = (args.mock and "mock") or cfg_ia.get("fournisseur", "mock")
    if fournisseur != "mock" and not cfg_ia.get("cle") and not getattr(args, "compter", False):
        raise SystemExit("Clé IA absente : mets-la dans la variable d'environnement "
                         "MISTRAL_API_KEY (recommandé, pas en clair dans config.json), "
                         "ou lance avec --mock.")

    systeme = charger_prompt(cfg)
    taxonomie = charger_taxonomie(cfg)
    taxo_noms = {c["nom"] for c in taxonomie}
    moteur_version = verifier_moteur(cfg)
    aujourdhui = datetime.now().strftime("%Y-%m-%d")
    profils, fils = profils_expediteurs(cx)

    # Sélection : ce qui mérite l'IA et n'a pas encore été jugé.
    jours = args.jours if args.jours is not None else cfg.get("ia_jours", 0)
    filtre_date = ""
    params = []
    if jours and jours > 0:
        limite_date = (datetime.now(timezone.utc)).timestamp() - jours * 86400
        filtre_date = " AND date_mail >= ?"
        params.append(datetime.fromtimestamp(limite_date, timezone.utc).isoformat())
    cond_source = "" if getattr(args, "retout", False) else "ia_source IS NULL AND "
    sql = ("SELECT message_id, de_adresse, sujet, date_mail, pour_moi, bulk, "
           "expediteur_humain, important, suspect, categorie, extrait, "
           "destinataires, copies "
           "FROM messages WHERE " + cond_source + "categorie IS NOT NULL "
           "AND (bulk=0 OR important=1)" + filtre_date +
           " ORDER BY date_mail DESC")
    if args.limite:
        sql += f" LIMIT {int(args.limite)}"
    rows = cx.execute(sql, params).fetchall()
    cols = ["message_id", "de_adresse", "sujet", "date_mail", "pour_moi", "bulk",
            "expediteur_humain", "important", "suspect", "categorie", "extrait",
            "destinataires", "copies"]
    if getattr(args, "compter", False):
        print(f"[{NOM}] {len(rows)} message(s) seraient envoyés à l'IA "
               f"({fournisseur}). Aucun appel effectué (--compter).")
        cx.close()
        return

    print(f"[{NOM}] IA ({fournisseur}) sur {len(rows)} message(s)…")

    faits, erreurs, questions = 0, 0, 0
    t0 = time.time()
    for r in rows:
        row = dict(zip(cols, r))
        spec = (_adresses_spec(row["de_adresse"] or "") or [""])[0]
        profil = profils.get(spec)
        try:
            if fournisseur == "mock":
                res = ia_bouchon(row, profil)
            else:
                entree = construire_entree_ia(row, profil, fils, taxonomie, aujourdhui)
                res = _extraire_json(appeler_mistral(cfg_ia, systeme, entree))
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            erreurs += 1
            if erreurs <= 3:
                print(f"[{NOM}] appel IA échoué : {e}", file=sys.stderr)
            continue
        except Exception as e:
            erreurs += 1
            cx.execute("UPDATE messages SET ia_source=?, ia_at=? WHERE message_id=?",
                       ("erreur", datetime.now(timezone.utc).isoformat(), row["message_id"]))
            continue

        # Validation stricte : une réponse non conforme (ou un mail piégé) ne pilote
        # jamais le cockpit → ia_source='erreur', repli sur le déterministe.
        ok, res, err = valider_sortie(res, taxo_noms)
        if not ok:
            erreurs += 1
            if erreurs <= 5:
                print(f"[{NOM}] jugement rejeté ({err}) : {row['message_id']}", file=sys.stderr)
            cx.execute("UPDATE messages SET ia_source=?, ia_at=? WHERE message_id=?",
                       ("erreur", datetime.now(timezone.utc).isoformat(), row["message_id"]))
            continue

        cx.execute(
            """UPDATE messages SET ia_categorie=?, ia_action=?, ia_tache=?, ia_echeance=?,
               ia_pression=?, ia_priorite=?, ia_resume=?, ia_question=?, ia_confiance=?,
               ia_source=?, ia_moteur_version=?, ia_at=? WHERE message_id=?""",
            (res["categorie"], int(bool(res["action_attendue"])),
             res["tache"], res["echeance"], res["pression"],
             res["priorite"], res["resume"], res["question"],
             res["confiance"], fournisseur, moteur_version,
             datetime.now(timezone.utc).isoformat(), row["message_id"]),
        )
        faits += 1
        if res.get("question"):
            questions += 1
        if faits % 50 == 0:
            cx.commit()
    cx.commit()
    print(f"[{NOM}] jugés : {faits}, questions soulevées : {questions}, "
          f"erreurs : {erreurs}, en {time.time()-t0:.0f}s")
    cx.close()


def cmd_reset_ia(args):
    """Efface les jugements IA pour pouvoir rejuger proprement."""
    cfg = charger_config(args.config)
    cx = connexion(cfg["base_sqlite"])
    _assurer_colonnes(cx)
    cols = [c for c in COLONNES_ANALYSE if c.startswith("ia_")]
    cx.execute("UPDATE messages SET " + ", ".join(f"{c}=NULL" for c in cols))
    cx.commit()
    print(f"[{NOM}] jugements IA effacés. Relance 'analyser-ia'.")
    cx.close()


def cmd_extraire_corps(args):
    """Rattrape l'extrait du corps pour les mails déjà archivés, en relisant leur
    .eml (lecture seule ; ne touche pas la boîte). À lancer une fois après mise à jour."""
    cfg = charger_config(args.config)
    cx = connexion(cfg["base_sqlite"])
    _assurer_colonnes(cx)
    maxlen = int(cfg.get("ia_extrait_max", PARAMS["ia_extrait_max_defaut"]))
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
    """Topo du matin en cases, d'après le jugement IA (repli sur le déterministe)."""
    cfg = charger_config(args.config)
    cx = connexion(cfg["base_sqlite"])
    cases = [("maintenant", "MAINTENANT"), ("cette_semaine", "CETTE SEMAINE"),
             ("ce_mois", "CE MOIS — À NE PAS OUBLIER")]
    print("=== Topo du matin ===\n")
    for cle, titre in cases:
        rows = cx.execute(
            """SELECT date_mail, de_adresse, COALESCE(ia_tache, sujet), ia_echeance, ia_question
               FROM messages WHERE ia_pression=? AND COALESCE(ia_action,1)=1
               ORDER BY date_mail DESC LIMIT ?""", (cle, args.nombre)).fetchall()
        if not rows:
            continue
        print(f"— {titre} —")
        for d, de, tache, ech, q in rows:
            ligne = f"  • {(tache or '(sans objet)')[:66]}"
            if ech and ech not in ("aucune", None):
                ligne += f"  [échéance {ech}]"
            print(ligne)
            print(f"      {(de or '')[:40]}")
            if q:
                print(f"      ❓ {q[:90]}")
        print()
    nb_q = cx.execute("SELECT COUNT(*) FROM messages WHERE ia_question IS NOT NULL AND ia_question<>''").fetchone()[0]
    if nb_q:
        print(f"({nb_q} question(s) en attente de ta réponse — voir ci-dessus.)")
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


CASES_COCKPIT = [("maintenant", "Maintenant"), ("cette_semaine", "Cette semaine"),
                 ("ce_mois", "Ce mois — à ne pas oublier"), ("aucune", "Pour info")]


def cockpit_liste(db, vue="atraiter", non_lus=False, depuis=None):
    cx = sqlite3.connect(db)
    profils, _ = profils_expediteurs(cx)   # mémoire : fréquence par expéditeur
    conds = ["ia_source IS NOT NULL"]
    params = []
    if vue == "reporte":
        conds.append("etat='reporte'")
    else:
        conds.append("etat IS NULL")
        if depuis:
            conds.append("date_mail >= ?")
            params.append(depuis)
        elif non_lus:
            conds.append("(non_lu=1 OR non_lu IS NULL)")
    rows = cx.execute(
        f"""SELECT message_id, de_adresse, sujet, date_mail, ia_resume, ia_tache,
            ia_echeance, ia_priorite, ia_pression, ia_question, non_lu
            FROM messages WHERE {' AND '.join(conds)} ORDER BY date_mail DESC""",
        params).fetchall()
    cx.close()
    cols = ["id", "de", "sujet", "date", "resume", "tache", "echeance",
            "priorite", "pression", "question", "non_lu"]
    par_case = {c: [] for c, _ in CASES_COCKPIT}
    for r in rows:
        it = dict(zip(cols, r))
        spec = (_adresses_spec(it["de"] or "") or [""])[0]
        p = profils.get(spec)
        it["fil"] = _sujet_fil(it["sujet"])          # regroupement par conversation
        it["freq"] = p["n"] if p else 0              # combien de mails de cet expéditeur
        it["actif"] = bool(p and p["n"] >= PARAMS["profil_actif_n_min"]
                           and p.get("recent", 0) >= PARAMS["profil_actif_recent_min"])
        par_case.get(it["pression"] or "aucune", par_case["aucune"]).append(it)
    return [{"cle": c, "titre": t, "items": par_case[c]} for c, t in CASES_COCKPIT]


# Étiquette partagée appliquée dans Thunderbird quand un mail est « Traité »
# dans le cockpit (retour-mail). La clé est l'identifiant stable côté Thunderbird.
ETIQUETTE_TRAITE = {"cle": "iamail_traite", "nom": "Traité IAMail", "couleur": "#12b76a"}


def faire_cockpit_handler(cfg):
    db = cfg["base_sqlite"]
    etat_audio = {"chemin": None}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _cors(self):
            # Serveur strictement local : l'extension Thunderbird appelle depuis
            # son origine moz-extension://… — on autorise l'accès.
            self.send_header("Access-Control-Allow-Origin", "*")
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
            self.send_response(204)
            self._cors()
            self.end_headers()

        def _corps(self):
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

        def do_GET(self):
            from urllib.parse import urlparse, parse_qs
            u = urlparse(self.path)
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
                self._json({"cases": cockpit_liste(db, vue, non_lus, depuis)})
            elif u.path == "/api/topo-audio":
                court, complet = construire_script_topo(db)
                sortie = str(Path(cfg.get("dossier_destination") or ".") / "topo.wav")
                chemin, err = generer_audio(complet, cfg, sortie)
                etat_audio["chemin"] = chemin
                self._json({"ok": bool(chemin), "court": court,
                            "url": ("/audio?t=" + str(int(time.time()))) if chemin else None,
                            "erreur": err})
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
            elif u.path == "/api/sync/pour-client":
                # Ce que l'extension Thunderbird doit APPLIQUER dans la boîte.
                cx = sqlite3.connect(db)
                a_marquer = [r[0] for r in cx.execute(
                    "SELECT message_id FROM messages WHERE etat='traite' "
                    "AND (tb_sync IS NULL OR tb_sync!='traite') "
                    "AND message_id NOT LIKE 'sansid:%'").fetchall()]
                a_demarquer = [r[0] for r in cx.execute(
                    "SELECT message_id FROM messages WHERE tb_sync='traite' "
                    "AND (etat IS NULL OR etat!='traite') "
                    "AND message_id NOT LIKE 'sansid:%'").fetchall()]
                cx.close()
                self._json({"etiquette": ETIQUETTE_TRAITE,
                            "a_marquer": a_marquer, "a_demarquer": a_demarquer})
            else:
                self._json({"erreur": "inconnu"}, 404)

        def do_POST(self):
            c = self._corps()
            mid = c.get("id")
            if self.path == "/api/action":
                e = c.get("etat")
                e = None if e in (None, "", "a_traiter") else e
                cx = sqlite3.connect(db)
                cx.execute("UPDATE messages SET etat=? WHERE message_id=?", (e, mid))
                cx.commit()
                cx.close()
                self._json({"ok": True})
            elif self.path == "/api/ouvrir":
                cx = sqlite3.connect(db)
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
                cx = sqlite3.connect(db)
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

                cx = sqlite3.connect(db)
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
</style></head><body>
<header>
  <h1>🛩️ IAMail</h1>
  <div class="seg"><button id="v-atraiter" class="on">À traiter</button><button id="v-reporte">Reportés</button></div>
  <button id="audio" class="pri">🔊 Topo audio</button>
  <span id="compte" class="badge"></span>
  <label class="ctl" id="l-nonlus"><input type="checkbox" id="nonlus"> non-lus seulement</label>
  <label class="ctl spacer">Retour d'absence depuis <input type="date" id="depuis"></label>
</header>
<div id="barreaudio" style="display:none;max-width:840px;margin:10px auto 0;padding:0 16px">
  <div style="background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px">
    <div id="audiotexte" style="font-size:13px;color:var(--mut);margin-bottom:6px"></div>
    <audio id="lecteur" controls style="width:100%"></audio></div></div>
<main id="main"></main>
<script>
const api=(p,b)=>fetch(p,{method:b?'POST':'GET',headers:{'Content-Type':'application/json'},body:b?JSON.stringify(b):undefined}).then(r=>r.json());
document.getElementById('audio').onclick=async()=>{
  const b=document.getElementById('audio');b.textContent='⏳ génération…';b.disabled=true;
  try{const r=await api('/api/topo-audio');
    document.getElementById('audiotexte').textContent=r.court||'';
    if(r.ok&&r.url){const l=document.getElementById('lecteur');l.src=r.url;
      document.getElementById('barreaudio').style.display='';l.play().catch(()=>{});}
    else{alert('Pas de voix : '+(r.erreur||'')+"\\nInstalle espeak-ng (sudo apt install espeak-ng).");}
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
  return `<div class="btns">
     <button class="pri" data-a="ouvrir">Ouvrir l'email</button>
     <button data-a="deleguer">Déléguer…</button>
     <button class="ok" data-a="traite">Traité</button>${rem}</div>`;
}
function brancher(el,it){el.querySelectorAll('button').forEach(b=>b.onclick=()=>action(it,b.dataset.a,el));}

function carte(it){
  const d=document.createElement('div');d.className='item '+(it.priorite||'basse');
  const ech=(it.echeance&&it.echeance!=='aucune')?`<span class="ech">⏱ ${esc(it.echeance)}</span>`:'';
  const nl=it.non_lu===1?'<span class="badge">non lu</span>':'';
  const q=it.question?`<div class="q">❓ ${esc(it.question)}</div>`:'';
  d.innerHTML=`<div class="tache">${esc(it.tache||it.resume||'(sans objet)')}${ech}${nl}</div>
   <div class="sujet">${esc(it.sujet)}</div><div class="meta">${esc(it.de)} ${freqBadge(it)}</div>${q}${btns(it)}`;
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
    r.innerHTML=`<div class="who">${esc(it.de)} — ${esc(it.sujet)}${nl} ${freqBadge(it)}</div>${btns(it)}`;
    brancher(r,it);g.appendChild(r);
  }
  return g;
}
async function actionGroupe(items,a,g){
  const etat=a==='remettre'?'a_traiter':a;
  for(const it of items.slice()){await api('/api/action',{id:it.id,etat});}
  g.remove();total-=items.length;maj();
}
async function action(it,a,el){
  if(a==='ouvrir'||a==='deleguer'){await api('/api/ouvrir',{id:it.id});
     if(a==='deleguer')alert("L'email s'ouvre dans ton logiciel : fais « Transférer », ajoute le collègue et ta note.");
     return;}
  const etat=a==='remettre'?'a_traiter':a;
  await api('/api/action',{id:it.id,etat});
  const grp=el.closest('.group');el.remove();
  if(grp&&!grp.querySelector('.row'))grp.remove();
  total--;maj();
}
function maj(){document.getElementById('compte').textContent=total+(state.vue==='reporte'?' reporté(s)':' à traiter');
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
}
function setVue(v){state.vue=v;
  document.getElementById('v-atraiter').classList.toggle('on',v==='atraiter');
  document.getElementById('v-reporte').classList.toggle('on',v==='reporte');
  document.getElementById('l-nonlus').style.display=(v==='atraiter'&&!state.depuis)?'':'none';
  charger();}
document.getElementById('v-atraiter').onclick=()=>setVue('atraiter');
document.getElementById('v-reporte').onclick=()=>setVue('reporte');
document.getElementById('nonlus').onchange=e=>{state.nonlus=e.target.checked;charger();};
document.getElementById('depuis').onchange=e=>{state.depuis=e.target.value;
  document.getElementById('l-nonlus').style.display=(state.vue==='atraiter'&&!state.depuis)?'':'none';charger();};
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


def _heure_parlee(d):
    """'9 heures', '10 heures 56' — pensé pour la synthèse vocale."""
    h, mn = d.hour, d.minute
    base = "1 heure" if h == 1 else f"{h} heures"
    if h == 0:
        base = "minuit"
    return base if mn == 0 else f"{base} {mn}"


def _date_parlee(d, annee_courante):
    """'le 9 novembre à 10 heures 56' ; ajoute l'année seulement si ce n'est pas l'année en cours."""
    jour = "1er" if d.day == 1 else str(d.day)
    an = "" if d.year == annee_courante else f" {d.year}"
    return f"le {jour} {_MOIS[d.month - 1]}{an} à {_heure_parlee(d)}"


def _echeance_parlee(ech):
    """'aujourd'hui' / 'demain' / 'le 15 novembre' depuis une échéance AAAA-MM-JJ, sinon None."""
    try:
        d = datetime.strptime((ech or "").strip(), "%Y-%m-%d").date()
    except Exception:
        return None
    delta = (d - datetime.now().date()).days
    if delta == 0:
        return "aujourd'hui"
    if delta == 1:
        return "demain"
    jour = "1er" if d.day == 1 else str(d.day)
    return f"le {jour} {_MOIS[d.month - 1]}"


def _phrase_message(de, tache, res, ech, dm, annee):
    """Une phrase fluide : quand, qui, quoi, pour quand."""
    quoi = (tache or res or "un message à regarder").strip().rstrip(".")
    nom = _nom_affiche(de)
    d = _dt_message(dm)
    if d:
        quand = _date_parlee(d, annee)
        phrase = f"{quand[0].upper()}{quand[1:]}, {nom} vous a écrit : {quoi}"
    else:
        phrase = f"{nom} vous a écrit : {quoi}"
    ep = _echeance_parlee(ech) if ech and ech not in ("aucune", None) else None
    if ep:
        phrase += f", à faire pour {ep}"
    return phrase + "."


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
    for de, tache, res, ech, dm, sujet in rows:
        action = (tache or res or "").strip().rstrip(".")
        if action:
            break
    base = f"Sur « {suj} », {len(rows)} messages de {_lister_noms(noms)}"
    if action:
        base += f", pour l'essentiel : {action}"
    ep = _echeance_proche([r[3] for r in rows])
    if ep:
        base += f", à traiter pour {ep}"
    return base + "."


def construire_script_topo(db, par_case=5):
    """Construit (résumé 30 s, script complet) du topo, en français parlé naturel."""
    cx = sqlite3.connect(db)
    annee = datetime.now().year
    aujourdhui = datetime.now().strftime("%Y-%m-%d")

    def cnt(pression):
        return cx.execute(
            "SELECT COUNT(*) FROM messages WHERE etat IS NULL AND ia_source IS NOT NULL "
            "AND ia_pression=? AND COALESCE(ia_action,1)=1 AND COALESCE(suspect,0)=0",
            (pression,)).fetchone()[0]

    n_now, n_week, n_month = cnt("maintenant"), cnt("cette_semaine"), cnt("ce_mois")
    n_susp = cx.execute("SELECT COUNT(*) FROM messages WHERE etat IS NULL AND suspect=1").fetchone()[0]

    resume = (f"Bonjour. Ce matin, vous avez {n_now} chose"
              f"{'s' if n_now > 1 else ''} à traiter tout de suite, "
              f"{n_week} pour cette semaine, et {n_month} à ne pas oublier d'ici la fin du mois.")
    if n_susp:
        resume += (f" J'ai aussi mis de côté {n_susp} message{'s' if n_susp > 1 else ''} "
                   f"suspect{'s' if n_susp > 1 else ''}, à vérifier, que je ne lis pas à voix haute.")

    lignes = [resume, ""]

    # Rendez-vous et échéances tombant aujourd'hui — annoncés en premier.
    rdv = cx.execute(
        "SELECT de_adresse, ia_tache, ia_resume, ia_echeance, date_mail FROM messages "
        "WHERE etat IS NULL AND ia_source IS NOT NULL AND COALESCE(suspect,0)=0 "
        "AND (ia_echeance=? OR ia_categorie IN ('rendez_vous','reunion','agenda')) "
        "ORDER BY date_mail DESC LIMIT ?", (aujourdhui, par_case)).fetchall()
    if rdv:
        lignes.append("Commençons par vos rendez-vous et vos échéances du jour.")
        for de, tache, res, ech, dm in rdv:
            quoi = (tache or res or "un rendez-vous").strip().rstrip(".")
            lignes.append(f"Avec {_nom_affiche(de)} : {quoi}.")
        lignes.append("")

    sections = [
        ("maintenant", "Voici ce qui demande une action maintenant."),
        ("cette_semaine", "Pour cette semaine, à présent."),
        ("ce_mois", "Et à garder en tête d'ici la fin du mois."),
    ]
    for pression, intro in sections:
        rows = cx.execute(
            "SELECT de_adresse, ia_tache, ia_resume, ia_echeance, date_mail, sujet FROM messages "
            "WHERE etat IS NULL AND ia_source IS NOT NULL AND ia_pression=? "
            "AND COALESCE(ia_action,1)=1 AND COALESCE(suspect,0)=0 "
            "ORDER BY date_mail DESC LIMIT 80", (pression,)).fetchall()
        if not rows:
            continue
        groupes = _grouper_fils(rows)
        lignes.append(intro)
        maxg = max(par_case, 6)
        for g in groupes[:maxg]:
            gr = g["rows"]
            if len(gr) == 1:
                de, tache, res, ech, dm, sujet = gr[0]
                lignes.append(_phrase_message(de, tache, res, ech, dm, annee))
            else:
                lignes.append(_phrase_groupe(gr))
        if len(groupes) > maxg:
            reste = len(groupes) - maxg
            lignes.append(f"Et {reste} autre sujet{'s' if reste > 1 else ''}, "
                          f"à voir dans le cockpit.")
        lignes.append("")
    lignes.append("Voilà pour l'essentiel. Je vous souhaite une bonne journée.")
    cx.close()
    return resume, "\n".join(lignes).strip()


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
    court, complet = construire_script_topo(cfg["base_sqlite"], args.par_case)
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
    if utiliser_ia:
        try:
            cmd_analyser_ia(types.SimpleNamespace(
                config=cfg_path, mock=False, retout=False, limite=ia_max, jours=None))
        except SystemExit as e:
            print(f"[{NOM}] (IA) {e}")
        except Exception as e:
            print(f"[{NOM}] IA: {e}")


def cmd_service(args):
    cfg = charger_config(args.config)
    v = verifier_moteur(cfg)
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

    s = sub.add_parser("topo-audio", help="génère le topo du matin en audio (voix locale)")
    s.add_argument("--par-case", type=int, default=5, dest="par_case")
    s.add_argument("--sortie", default=None, help="chemin du fichier audio")
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

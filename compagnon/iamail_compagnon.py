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
VERSION = "1.3.0"

CONFIG_DEFAUT = {
    "dossier_staging": "",
    "dossier_destination": "",
    "base_sqlite": "iamail_index.sqlite3",
    "deplacer": True,
    "mes_adresses": [],
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
    return bool(re.match(r"^[a-z]+([._-][a-z]+)+$", local))


def _domaine_marketing(dom):
    if not dom or dom in FREEMAIL:
        return False
    labels = dom.split(".")
    if set(labels) & MARKETING_LABELS:
        return True
    return any(("mail" in l or "news" in l or "mailing" in l) for l in labels)

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


def _adresses_spec(valeur):
    """Liste d'adresses nues (a@b), minuscules, d'un en-tête To/Cc."""
    out = []
    for _, addr in email.utils.getaddresses([valeur or ""]):
        if addr and "@" in addr:
            out.append(addr.strip().lower())
    return out


def _domaine(adresse):
    return adresse.split("@", 1)[1].lower() if adresse and "@" in adresse else ""


def classer(h, mes_adresses):
    """
    h : dict d'en-têtes décodés (from, to, cc, subject, list_unsubscribe,
        list_id, precedence, auto_submitted, reply_to, return_path).
    mes_adresses : liste d'adresses de l'utilisateur (minuscules).
    Renvoie un dict de classement.
    """
    de = (h.get("from") or "")
    de_spec = (_adresses_spec(de) or [""])[0]
    de_local = de_spec.split("@", 1)[0] if de_spec else ""
    sujet = h.get("subject") or ""
    texte = _sans_accents((sujet + " " + de).lower())

    to = _adresses_spec(h.get("to"))
    cc = _adresses_spec(h.get("cc"))
    mes = set(mes_adresses or [])

    # --- Humain vs automatique / en masse ---------------------------------
    humain = _expediteur_humain(de_spec, de_local)
    bulk = bool(
        h.get("list_unsubscribe") or h.get("list_id")
        or (h.get("precedence", "").lower() in ("bulk", "list", "junk"))
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
    for nom, mots in REGLES_CATEGORIE:
        if any(m in texte for m in mots):
            categorie, conf_cat = nom, "moyenne"
            break
    if categorie == "autre" and bulk:
        categorie = "reseau_social" if any(
            r in _domaine(de_spec) for r in ("facebook", "linkedin", "instagram", "twitter")
        ) else "newsletter"
        conf_cat = "moyenne"

    # --- Soupçon d'hameçonnage (léger, jamais d'action automatique) --------
    reply_to = (_adresses_spec(h.get("reply_to")) or [""])[0]
    suspect = bool(
        reply_to and _domaine(reply_to) and de_spec
        and _domaine(reply_to) != _domaine(de_spec)
        and any(m in texte for m in ("paiement", "facture", "urgent", "compte", "mot de passe", "iban"))
    )

    # --- Urgence -----------------------------------------------------------
    # Le vocabulaire d'urgence ne compte que s'il vient d'un humain : sinon
    # c'est du marketing qui crie « urgent / rappel / échéance ».
    a_mot_urgent = any(m in texte for m in MOTS_URGENTS)
    if bulk:
        urgence = "basse"
    elif a_mot_urgent and humain:
        urgence = "haute"
    elif a_mot_urgent:
        urgence = "normale"            # mot d'urgence mais expéditeur automatique
    else:
        urgence = "normale"

    # --- Priorité (la synthèse qui sert le topo) --------------------------
    if pour_moi and not bulk and humain and urgence == "haute":
        priorite = "haute"
    elif pour_moi and not bulk and humain:
        priorite = "normale"
    elif pour_moi and not bulk:
        priorite = "normale"           # adressé à toi, émetteur auto (transactionnel)
    else:
        priorite = "basse"

    # --- Raison lisible ----------------------------------------------------
    bouts = []
    bouts.append("adressé à toi" if pour_moi else
                 ("juste en copie" if pour_moi is False else "diffusion/liste"))
    bouts.append("expéditeur humain" if humain else "expéditeur automatique")
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
        "suspect": int(suspect),
        "priorite": priorite,
        "raison": raison,
        "confiance": conf_cat,
    }


COLONNES_ANALYSE = {
    "categorie": "TEXT", "urgence": "TEXT", "pour_moi": "INTEGER",
    "bulk": "INTEGER", "automatique": "INTEGER", "expediteur_humain": "INTEGER",
    "suspect": "INTEGER", "priorite": "TEXT", "raison": "TEXT", "confiance": "TEXT",
    "analyse_source": "TEXT", "analyse_at": "TEXT",
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
    mes = [a.lower() for a in cfg.get("mes_adresses", [])]
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
            c = classer(h, mes)
        except Exception:
            erreurs += 1
            continue
        cx.execute(
            """UPDATE messages SET categorie=?, urgence=?, pour_moi=?, bulk=?,
               automatique=?, expediteur_humain=?, suspect=?, priorite=?, raison=?,
               confiance=?, analyse_source=?, analyse_at=? WHERE message_id=?""",
            (c["categorie"], c["urgence"], c["pour_moi"], c["bulk"],
             c["automatique"], c["expediteur_humain"], c["suspect"], c["priorite"],
             c["raison"], c["confiance"], "regles",
             datetime.now(timezone.utc).isoformat(), mid),
        )
        faits += 1
        if faits % 2000 == 0:
            cx.commit()
    cx.commit()
    print(f"[{NOM}] analysés : {faits}, ignorés : {erreurs}, en {time.time()-t0:.0f}s")
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
    rows = cx.execute(
        """SELECT date_mail, de_adresse, sujet, priorite, raison
           FROM messages
           WHERE pour_moi=1 AND bulk=0
           ORDER BY CASE priorite WHEN 'haute' THEN 0 WHEN 'normale' THEN 1 ELSE 2 END,
                    date_mail DESC
           LIMIT ?""", (n,)).fetchall()
    if not rows:
        print("Rien ne ressort (as-tu renseigné 'mes_adresses' puis lancé 'analyser' ?).")
    for d, de, suj, prio, rai in rows:
        marque = "‼" if prio == "haute" else "•"
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

    s = sub.add_parser("topo", help="aperçu priorisé (graine du topo du matin)")
    s.add_argument("--nombre", type=int, default=20)
    s.set_defaults(func=cmd_topo)

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

"""Collecteur Outlook classique (Windows) via COM — pour les collègues sous Outlook.

Contrairement à Thunderbird (qui exige une extension car le compagnon ne peut pas le
piloter), Outlook classique s'automatise en COM : le compagnon EST l'agent, en un seul
processus — relève ET retour-mail (catégorie « Traité IAMail » + lu), par accès direct.

Principe : chaque mail Outlook est reconstruit en `.eml` standard (+ `.msg` natif gardé
comme coffre-fort), pour que TOUT le pipeline existant (analyse déterministe, extrait,
IA, cockpit) fonctionne à l'identique, sans code spécifique en aval.

Ce module ne s'importe qu'au besoin : pywin32 n'est requis que sur Windows. Les parties
COM sont des adaptateurs minces ; la logique (construction du .eml, Message-ID, décisions
de retour-mail) est pure et testée sans Outlook.
"""
import hashlib
import json
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate, parseaddr
from pathlib import Path

CATEGORIE_TRAITE = "Traité IAMail"

# Propriétés MAPI utiles (PropertyAccessor).
PR_INTERNET_MESSAGE_ID = "http://schemas.microsoft.com/mapi/proptag/0x1035001F"
PR_TRANSPORT_HEADERS = "http://schemas.microsoft.com/mapi/proptag/0x007D001F"
OL_MSG = 3            # olMSG : format .msg pour MailItem.SaveAs
OL_FOLDER_INBOX = 6   # olFolderInbox


# ───────────────────────── Logique pure (testable sans COM) ─────────────────────────
def message_id_depuis(brut, recu_iso, de_adresse, sujet):
    """Message-ID : chevrons retirés si présent ; sinon 'synth-' + 32 hex de
    sha256(date reçue | adresse | objet). Même esprit que le collecteur mbox."""
    mid = (brut or "").replace("<", "").replace(">", "").strip()
    if mid:
        return mid
    empreinte = hashlib.sha256(
        ((recu_iso or "") + "|" + (de_adresse or "") + "|" + (sujet or "")).encode("utf-8")
    ).hexdigest()
    return "synth-" + empreinte[:32]


def construire_eml(v):
    """Reconstruit un .eml standard (bytes) depuis les valeurs plates d'un mail Outlook.
    v : dict {de_nom, de_adresse, to[], cc[], sujet, recu_iso, corps, message_id}."""
    m = EmailMessage()
    exp = v.get("de_adresse") or ""
    nom = v.get("de_nom") or ""
    m["From"] = f"{nom} <{exp}>" if (nom and exp) else (exp or nom or "inconnu@invalide")
    if v.get("to"):
        m["To"] = ", ".join(v["to"])
    if v.get("cc"):
        m["Cc"] = ", ".join(v["cc"])
    m["Subject"] = v.get("sujet") or ""
    recu = v.get("recu_iso")
    try:
        dt = datetime.fromisoformat(recu) if recu else None
        m["Date"] = formatdate(dt.timestamp() if dt else None, localtime=bool(dt))
    except Exception:
        m["Date"] = formatdate(localtime=False)
    mid = v.get("message_id") or ""
    if mid and not mid.startswith("synth-"):
        m["Message-ID"] = "<" + mid + ">"
    m.set_content(v.get("corps") or "")
    return m.as_bytes()


def decisions_retour(rows, categorie=CATEGORIE_TRAITE):
    """À partir de lignes (message_id, source_ref, etat, tb_sync, a_categorie_dans_outlook),
    renvoie les actions de retour-mail, SANS COM — pour pouvoir les tester.
    Renvoie {marquer:[(mid,ref)], retirer:[(mid,ref)], rouvrir:[mid], traiter:[mid]}.
      - marquer : cockpit dit « traité », Outlook pas encore → poser étiquette + lu.
      - retirer : cockpit a rouvert, Outlook a encore l'étiquette → la retirer.
      - rouvrir : l'utilisateur a retiré l'étiquette dans Outlook → sortir du cockpit.
      - traiter : l'utilisateur a posé l'étiquette dans Outlook → traité au cockpit.
    """
    marquer, retirer, rouvrir, traiter = [], [], [], []
    for mid, ref, etat, tb, a_cat in rows:
        traite_cockpit = (etat == "traite")
        applique = (tb == "traite")
        if traite_cockpit and not applique and ref:
            marquer.append((mid, ref))
        elif applique and not traite_cockpit and ref:
            retirer.append((mid, ref))
        if applique and a_cat is False:          # l'utilisateur a enlevé l'étiquette
            rouvrir.append(mid)
        if (a_cat is True) and not traite_cockpit and not applique:
            traiter.append(mid)                   # l'utilisateur a posé l'étiquette
    return {"marquer": marquer, "retirer": retirer, "rouvrir": rouvrir, "traiter": traiter}


# ───────────────────────── Adaptateur COM (Windows seulement) ─────────────────────────
def disponible():
    try:
        import win32com.client  # noqa: F401
        return True
    except Exception:
        return False


def _namespace():
    import win32com.client
    return win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")


def _prop(item, tag, defaut=""):
    try:
        return item.PropertyAccessor.GetProperty(tag) or defaut
    except Exception:
        return defaut


def _smtp_expediteur(item):
    """Adresse SMTP réelle, y compris pour un expéditeur Exchange (type 'EX')."""
    try:
        if str(getattr(item, "SenderEmailType", "") or "").upper() == "EX":
            try:
                return item.Sender.GetExchangeUser().PrimarySmtpAddress or ""
            except Exception:
                return _prop(item, "http://schemas.microsoft.com/mapi/proptag/0x39FE001F",
                             str(getattr(item, "SenderEmailAddress", "") or ""))
        return str(getattr(item, "SenderEmailAddress", "") or "")
    except Exception:
        return ""


def _destinataires(item):
    """(to[], cc[]) en adresses SMTP, via la collection Recipients (Type 1=To, 2=Cc)."""
    to, cc = [], []
    try:
        for r in item.Recipients:
            adr = ""
            try:
                adr = (r.AddressEntry.GetExchangeUser().PrimarySmtpAddress
                       if (r.AddressEntry and str(r.Type) and
                           str(getattr(r.AddressEntry, "Type", "")).upper() == "EX")
                       else r.Address) or r.Address or ""
            except Exception:
                adr = getattr(r, "Address", "") or ""
            if not adr:
                continue
            (cc if int(getattr(r, "Type", 1)) == 2 else to).append(adr)
    except Exception:
        pass
    return to, cc


def _recu_iso(item):
    try:
        t = item.ReceivedTime
        if t:
            return datetime(t.year, t.month, t.day, t.hour, t.minute, t.second,
                            tzinfo=timezone.utc).isoformat()
    except Exception:
        pass
    return ""


def lire_mailitem(item):
    """Extrait les valeurs plates d'un MailItem Outlook (aucune reconstruction ici).
    Tout est en accès attribut/COM ; se teste avec un faux objet exposant les mêmes noms."""
    to, cc = _destinataires(item)
    de_adresse = _smtp_expediteur(item)
    sujet = str(getattr(item, "Subject", "") or "")
    recu = _recu_iso(item)
    mid = message_id_depuis(str(_prop(item, PR_INTERNET_MESSAGE_ID, "")), recu, de_adresse, sujet)
    pj = []
    try:
        for a in item.Attachments:
            pj.append(str(getattr(a, "FileName", "") or ""))
    except Exception:
        pass
    return {
        "de_nom": str(getattr(item, "SenderName", "") or ""),
        "de_adresse": de_adresse,
        "to": to, "cc": cc, "sujet": sujet, "recu_iso": recu,
        "corps": str(getattr(item, "Body", "") or ""),
        "pieces_jointes": pj,
        "message_id": mid,
        "entry_id": str(getattr(item, "EntryID", "") or ""),
    }


def _dossiers_a_relever(ns, cfg):
    """Dossiers à relever : Boîte de réception par défaut, ou liste configurée
    'outlook_dossiers' (chemins 'Compte/Dossier')."""
    noms = cfg.get("outlook_dossiers") or []
    if not noms:
        return [ns.GetDefaultFolder(OL_FOLDER_INBOX)]
    dossiers = []
    for chemin in noms:
        cur = None
        for i, part in enumerate(chemin.split("/")):
            cur = (ns.Folders(part) if i == 0 else cur.Folders(part))
        if cur:
            dossiers.append(cur)
    return dossiers


def relever(cfg, cx, C, limite=None):
    """Relève COM : archive chaque nouveau mail en .eml (+ .msg coffre-fort) et l'indexe.
    Le classement (déterministe + IA) reste assuré par les commandes existantes."""
    ns = _namespace()
    dest_root = Path(cfg["dossier_destination"])
    max_ex = int(cfg.get("ia_extrait_max", C.PARAMS.get("ia_extrait_max", 4000)))
    vus = {r[0] for r in cx.execute("SELECT message_id FROM messages").fetchall()}
    faits, doublons = 0, 0
    import email as _email
    for dossier in _dossiers_a_relever(ns, cfg):
        items = dossier.Items
        try:
            items.Sort("[ReceivedTime]", True)
        except Exception:
            pass
        n = 0
        for item in items:
            if str(getattr(item, "Class", 43)) not in ("43", "0"):   # 43 = olMail
                if getattr(item, "MessageClass", "IPM.Note") and \
                   not str(item.MessageClass).startswith("IPM.Note"):
                    continue
            try:
                v = lire_mailitem(item)
            except Exception:
                continue
            if v["message_id"] in vus:
                doublons += 1
                continue
            eml_bytes = construire_eml(v)
            dt = v["recu_iso"][:10].replace("-", "/") or datetime.now().strftime("%Y/%m")
            dossier_dest = dest_root / dt
            dossier_dest.mkdir(parents=True, exist_ok=True)
            base = C.nom_libre(dossier_dest,
                               f"{C._slug_expediteur(v['de_adresse'])}__{C._slug(v['sujet'])}"
                               f"__{C._hash6(v['message_id'])}", ".eml")
            base.write_bytes(eml_bytes)
            try:                                   # coffre-fort natif Outlook
                item.SaveAs(str(base.with_suffix(".msg")), OL_MSG)
            except Exception:
                pass
            msg = _email.message_from_bytes(eml_bytes)
            extrait = C._corps_texte(msg, max_ex)
            cx.execute(
                """INSERT OR IGNORE INTO messages
                   (message_id, de_adresse, destinataires, copies, date_mail, sujet,
                    fichier_eml, fichier_json, pieces_jointes, dossier_src, ingere_at,
                    extrait, source_ref, source_type)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (v["message_id"], f"{v['de_nom']} <{v['de_adresse']}>".strip(),
                 json.dumps(v["to"], ensure_ascii=False), json.dumps(v["cc"], ensure_ascii=False),
                 v["recu_iso"], v["sujet"], str(base), "",
                 json.dumps([{"nom": p} for p in v["pieces_jointes"]], ensure_ascii=False),
                 "Outlook", datetime.now(timezone.utc).isoformat(),
                 extrait, v["entry_id"], "outlook"),
            )
            vus.add(v["message_id"])
            faits += 1
            n += 1
            if limite and n >= limite:
                break
    cx.commit()
    return faits, doublons


def _assurer_categorie(ns):
    """Crée la catégorie « Traité IAMail » (verte) si absente."""
    try:
        cats = ns.Categories
        for c in cats:
            if c.Name == CATEGORIE_TRAITE:
                return
        cats.Add(CATEGORIE_TRAITE, 4)   # 4 ≈ vert (olCategoryColorGreen)
    except Exception:
        pass


def _a_categorie(item):
    try:
        return CATEGORIE_TRAITE in [c.strip() for c in (item.Categories or "").split(",")]
    except Exception:
        return None


def appliquer_retour(cfg, cx, C):
    """Retour-mail Outlook, en un seul processus (pas d'extension) :
    cockpit → Outlook (étiquette + lu) et Outlook → cockpit (étiquette posée/retirée)."""
    ns = _namespace()
    _assurer_categorie(ns)
    rows = cx.execute(
        "SELECT message_id, source_ref, etat, tb_sync FROM messages "
        "WHERE source_type='outlook' AND source_ref IS NOT NULL").fetchall()
    # État Outlook courant (étiquette présente ?) par item, pour les décisions.
    enrichies = []
    for mid, ref, etat, tb in rows:
        a_cat = None
        if ref:
            try:
                a_cat = _a_categorie(ns.GetItemFromID(ref))
            except Exception:
                a_cat = None
        enrichies.append((mid, ref, etat, tb, a_cat))
    d = decisions_retour(enrichies)

    for mid, ref in d["marquer"]:
        try:
            it = ns.GetItemFromID(ref)
            cats = [c.strip() for c in (it.Categories or "").split(",") if c.strip()]
            if CATEGORIE_TRAITE not in cats:
                cats.append(CATEGORIE_TRAITE)
            it.Categories = ", ".join(cats)
            it.UnRead = False
            it.Save()
            cx.execute("UPDATE messages SET tb_sync='traite' WHERE message_id=?", (mid,))
        except Exception:
            pass
    for mid, ref in d["retirer"]:
        try:
            it = ns.GetItemFromID(ref)
            cats = [c.strip() for c in (it.Categories or "").split(",")
                    if c.strip() and c.strip() != CATEGORIE_TRAITE]
            it.Categories = ", ".join(cats)
            it.Save()
            cx.execute("UPDATE messages SET tb_sync=NULL WHERE message_id=?", (mid,))
        except Exception:
            pass
    for mid in d["rouvrir"]:
        cx.execute("UPDATE messages SET etat=NULL, tb_sync=NULL "
                   "WHERE message_id=? AND tb_sync='traite'", (mid,))
    for mid in d["traiter"]:
        cx.execute("UPDATE messages SET etat='traite', tb_sync='traite' "
                   "WHERE message_id=? AND (etat IS NULL OR etat!='traite')", (mid,))
    cx.commit()
    return d

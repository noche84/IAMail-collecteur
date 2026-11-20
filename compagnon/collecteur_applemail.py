"""Collecteur Apple Mail (macOS) — pour les postes Mac qui utilisent Mail.

Même principe que l'agent Outlook : Mail se pilote par les Apple Events (ici en JXA,
JavaScript for Automation, via `osascript`), donc le compagnon EST l'agent, en un seul
processus — relève ET retour-mail, sans extension ni droit administrateur.

- Relève : Mail fournit la SOURCE brute de chaque message (RFC 822). Elle est archivée
  telle quelle en `.eml` (+ `.json` jumeau), exactement comme un message lu dans un mbox
  Thunderbird : tout le pipeline en aval (analyse, extrait, IA, cockpit) est identique.
- Retour-mail : Mail n'a pas de catégories scriptables ; « Traité IAMail » est donc un
  DRAPEAU de couleur (vert par défaut, réglable) + lu. Dans les deux sens : poser ou
  retirer ce drapeau dans Mail fait suivre le cockpit.
- Envoyés : les en-têtes de la boîte « Envoyés » (moteur 2.4 : « as-tu déjà répondu ? »).

macOS demande une fois l'autorisation « iamail souhaite contrôler Mail »
(Réglages Système → Confidentialité et sécurité → Automatisation).

Les parties JXA sont des adaptateurs minces ; la logique (fenêtre, filtrage des
nouveaux, décisions de retour-mail, lecture des envois) est pure et testée sans Mac.
"""
import email
import email.utils
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from email.parser import HeaderParser
from pathlib import Path

from collecteur_outlook import decisions_retour

SOURCE_TYPE = "applemail"
# Index des drapeaux de Mail (« flag index ») : 0 rouge … 6 gris ; -1 = pas de drapeau.
DRAPEAUX = {"rouge": 0, "orange": 1, "jaune": 2, "vert": 3, "bleu": 4, "violet": 5, "gris": 6}
DRAPEAU_DEFAUT = "vert"
JOURS_DEFAUT = 60          # fenêtre relue à chaque cycle (0 = toute la boîte)
JOURS_ENVOYES_MAX = 365    # les Envoyés ne sont jamais relus au-delà
PAQUET_SOURCES = 25        # sources lues par appel osascript


class MailErreur(RuntimeError):
    pass


# Dernière erreur rencontrée en parlant à Mail (affichée en bandeau au cockpit) ;
# effacée dès qu'un échange réussit.
DERNIERE_ERREUR = ""


# ───────────────────────── Logique pure (testable sans Mac) ─────────────────────────
def drapeau(cfg):
    """Index du drapeau « Traité » : nom de couleur ('vert') ou entier 0-6."""
    v = cfg.get("applemail_drapeau", DRAPEAU_DEFAUT)
    if isinstance(v, int) and 0 <= v <= 6:
        return v
    return DRAPEAUX.get(str(v).strip().lower(), DRAPEAUX[DRAPEAU_DEFAUT])


def depuis_iso(cfg, maintenant=None):
    """Début de la fenêtre relue (ISO UTC), ou None pour toute la boîte."""
    jours = int(cfg.get("applemail_jours", JOURS_DEFAUT) or 0)
    if jours <= 0:
        return None
    now = maintenant or datetime.now(timezone.utc)
    return (now - timedelta(days=jours)).isoformat()


def parametres(cfg, maintenant=None):
    """Ce que reçoivent les scripts JXA."""
    return {"boites": list(cfg.get("applemail_boites") or []),
            "depuis": depuis_iso(cfg, maintenant),
            "drapeau": drapeau(cfg),
            "lancer": bool(cfg.get("applemail_lancer", False))}


def _mid(x):
    return str(x or "").replace("<", "").replace(">", "").strip()


def a_relever(liste, refs_connues, mids_connus, limite=None):
    """Entrées de la liste Mail à lire en entier : ni leur id Mail ni leur Message-ID
    ne sont connus. Renvoie (à_lire, nb_doublons). Pur."""
    out, doublons, vus = [], 0, set()
    for e in liste:
        ref = str(e.get("id", ""))
        mid = _mid(e.get("mid"))
        if ref in refs_connues or (mid and mid in mids_connus) or (ref, mid) in vus:
            doublons += 1
            continue
        vus.add((ref, mid))
        out.append(e)
        if limite and len(out) >= limite:
            break
    return out, doublons


def etat_par_ref(liste, idx_drapeau):
    """{id Mail: (a_le_drapeau_traite, lu)} depuis la liste. Pur."""
    return {str(e.get("id", "")): (e.get("drapeau") == idx_drapeau, bool(e.get("lu")))
            for e in liste}


def envoi_depuis_entetes(texte, C):
    """Valeurs d'un message envoyé depuis ses en-têtes bruts (All Headers de Mail),
    dans la forme attendue par C.enregistrer_envoi. Pur (C fournit les décodeurs)."""
    m = HeaderParser().parsestr(texte or "")
    d = C._dt_message(C._dec_entete(m.get("Date")) or "")
    if d is not None and d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    adresses = []
    for h in ("To", "Cc"):
        for _, adr in email.utils.getaddresses([C._dec_entete(m.get(h)) or ""]):
            adresses.append(adr)
    irt = C._ids_entete(m.get("In-Reply-To"))
    return {"message_id": C._message_id(m), "date": d.isoformat() if d else "",
            "a": adresses, "sujet": C._dec_entete(m.get("Subject")),
            "in_reply_to": irt[0] if irt else "", "refs": C._ids_entete(m.get("References"))}


def message_erreur(err):
    """Traduit une erreur d'osascript en consigne lisible. Pur."""
    if "-1743" in err or "Not authorized" in err or "autoris" in err.lower():
        return ("macOS refuse à IAMail de piloter Mail. Autorise-le dans Réglages Système → "
                "Confidentialité et sécurité → Automatisation → iamail → Mail.")
    if "-600" in err or "-1728" in err:
        return "Mail ne répond pas ou la boîte configurée est introuvable (applemail_boites)."
    return f"osascript : {err}"


def source_en_octets(source):
    """La source arrive en texte (JSON) : on la ré-encode pour l'archive .eml."""
    return (source or "").encode("utf-8", "surrogateescape")


# ───────────────────────── Scripts JXA ─────────────────────────
# Prélude commun : lit les paramètres (fichier JSON), choisit les boîtes, applique la
# fenêtre. Sans 'boites' : la Boîte de réception unifiée de Mail (tous les comptes).
# 'Compte/Boîte' : une boîte précise ('Compte/INBOX', 'iCloud/Clients/2026'…).
_PRELUDE = r"""
ObjC.import('Foundation');
function lireParams(chemin) {
  return JSON.parse($.NSString.stringWithContentsOfFileEncodingError(
    chemin, $.NSUTF8StringEncoding, null).js);
}
function collections(Mail, p) {
  var out = [];
  var noms = p.boites || [];
  if (!noms.length) out.push({nom: '', spec: Mail.inbox});
  noms.forEach(function (chemin) {
    var i = chemin.indexOf('/');
    var compte = i < 0 ? chemin : chemin.slice(0, i), boite = i < 0 ? 'INBOX' : chemin.slice(i + 1);
    out.push({nom: chemin, spec: Mail.accounts.byName(compte).mailboxes.byName(boite)});
  });
  return out.map(function (c) {
    var m = c.spec.messages;
    if (p.depuis) m = m.whose({dateReceived: {_greaterThan: new Date(p.depuis)}});
    return {nom: c.nom, msgs: m};
  });
}
function ouvrirMail(p) {
  var Mail = Application('Mail');
  if (!Mail.running() && !p.lancer) return null;
  return Mail;
}
"""

# Liste légère (un Apple Event par propriété) : id, Message-ID, date, lu, drapeau.
JXA_LISTER = _PRELUDE + r"""
function run(argv) {
  var p = lireParams(argv[0]);
  var Mail = ouvrirMail(p);
  if (!Mail) return JSON.stringify({ferme: true});
  var out = [];
  collections(Mail, p).forEach(function (c) {
    var ids = c.msgs.id();
    if (!ids.length) return;
    var mids = c.msgs.messageId(), dates = c.msgs.dateReceived(),
        lus = c.msgs.readStatus(), dr = c.msgs.flagIndex();
    for (var i = 0; i < ids.length; i++) {
      out.push({boite: c.nom, i: i, id: String(ids[i]), mid: mids[i] || '',
                recu: dates[i] ? dates[i].toISOString() : '', lu: !!lus[i], drapeau: dr[i]});
    }
  });
  return JSON.stringify({messages: out});
}
"""

# Sources brutes des messages demandés. Chaque cible est vérifiée par son id Mail
# (l'index peut avoir glissé si un mail est arrivé entre-temps : elle est alors ignorée
# et sera reprise au cycle suivant).
JXA_SOURCES = _PRELUDE + r"""
function run(argv) {
  var p = lireParams(argv[0]);
  var Mail = ouvrirMail(p);
  if (!Mail) return JSON.stringify({ferme: true});
  var parNom = {};
  collections(Mail, p).forEach(function (c) { parNom[c.nom] = c.msgs; });
  var out = [];
  p.cibles.forEach(function (t) {
    try {
      var m = parNom[t.boite][t.i];
      if (String(m.id()) !== t.id) return;
      var b = m.mailbox();
      out.push({id: t.id, source: m.source(), lu: !!m.readStatus(),
                boite: b.name(), compte: b.account().name()});
    } catch (e) { /* message disparu : repris au cycle suivant */ }
  });
  return JSON.stringify({messages: out});
}
"""

# Retour-mail : pose / retire le drapeau « Traité » (+ lu), retrouvé par son id Mail.
JXA_RETOUR = _PRELUDE + r"""
function run(argv) {
  var p = lireParams(argv[0]);
  var Mail = ouvrirMail(p);
  if (!Mail) return JSON.stringify({ferme: true});
  var ou = {};
  collections(Mail, p).forEach(function (c) {
    var ids = c.msgs.id();
    for (var i = 0; i < ids.length; i++) ou[String(ids[i])] = {msgs: c.msgs, i: i};
  });
  var faits = [];
  p.actions.forEach(function (a) {
    var o = ou[a.id];
    if (!o) return;
    try {
      var m = o.msgs[o.i];
      if (String(m.id()) !== a.id) return;
      if (a.faire === 'marquer') {
        m.flagIndex = p.drapeau;
        m.flaggedStatus = true;
        m.readStatus = true;
      } else if (m.flagIndex() === p.drapeau) {
        m.flaggedStatus = false;
      }
      faits.push(a.id);
    } catch (e) {}
  });
  return JSON.stringify({faits: faits});
}
"""

# En-têtes des messages envoyés (boîte « Envoyés » unifiée de Mail).
JXA_ENVOYES = _PRELUDE + r"""
function run(argv) {
  var p = lireParams(argv[0]);
  var Mail = ouvrirMail(p);
  if (!Mail) return JSON.stringify({ferme: true});
  var m = Mail.sentMailbox.messages;
  if (p.depuis) m = m.whose({dateSent: {_greaterThan: new Date(p.depuis)}});
  return JSON.stringify({entetes: m.allHeaders()});
}
"""


# ───────────────────────── Adaptateur osascript (macOS seulement) ─────────────────────────
def disponible():
    return sys.platform == "darwin" and shutil.which("osascript") is not None


def _jxa(script, params, delai=600):
    """Exécute un script JXA avec ses paramètres (fichier JSON) ; renvoie le JSON produit.
    Lève MailErreur avec un message clair (autorisation refusée, Mail introuvable…)."""
    with tempfile.TemporaryDirectory(prefix="iamail-") as tmp:
        js = Path(tmp) / "script.js"
        pj = Path(tmp) / "params.json"
        js.write_text(script, encoding="utf-8")
        pj.write_text(json.dumps(params, ensure_ascii=False), encoding="utf-8")
        r = subprocess.run(["osascript", "-l", "JavaScript", str(js), str(pj)],
                           capture_output=True, timeout=delai)
    global DERNIERE_ERREUR
    err = r.stderr.decode("utf-8", "replace").strip()
    try:
        if r.returncode != 0:
            raise MailErreur(message_erreur(err or str(r.returncode)))
        try:
            res = json.loads(r.stdout.decode("utf-8", "surrogateescape") or "{}")
        except ValueError as e:
            raise MailErreur(f"réponse de Mail illisible : {e}")
    except MailErreur as e:
        DERNIERE_ERREUR = str(e)
        raise
    DERNIERE_ERREUR = ""
    return res


def _ferme(res):
    return bool(res.get("ferme"))


def archiver_source(cfg, cx, C, brut, ref, lu, dossier_logique):
    """Archive une source brute en .eml + .json et l'indexe — mêmes nom, métadonnées et
    colonnes que l'import mbox ; source_type='applemail', source_ref=id Mail.
    Renvoie 'archive', 'doublon' ou 'erreur'."""
    msg = email.message_from_bytes(brut)
    mid = C._message_id(msg)
    if C.existe(cx, mid):
        return "doublon"
    dt = C._date_iso(msg)
    dtp = dt or datetime.now(timezone.utc)
    sujet = C._dec_entete(msg.get("Subject"))
    de = C._dec_entete(msg.get("From"))
    base = (f"{dtp.year:04d}-{dtp.month:02d}-{dtp.day:02d}_"
            f"{dtp.hour:02d}{dtp.minute:02d}{dtp.second:02d}"
            f"__{C._slug_expediteur(de)}__{C._slug(sujet)}__{C._hash6(mid)}")
    dossier_dest = Path(cfg["dossier_destination"]) / f"{dtp.year:04d}/{dtp.month:02d}"
    dest_eml = C.nom_libre(dossier_dest, base, ".eml")
    dest_json = dest_eml.with_suffix(".json")
    meta = {
        "message_id": mid if not mid.startswith("synth-") else None,
        "de_adresse": de, "de_nom": de,
        "destinataires": C._adresses(msg.get("To")),
        "copies": C._adresses(msg.get("Cc")),
        "date": dt.isoformat() if dt else None,
        "sujet": sujet, "dossier": dossier_logique,
        "fichier_eml": str(dest_eml),
        "pieces_jointes": C._pieces_jointes(msg),
        "collecte_at": datetime.now(timezone.utc).isoformat(),
        "collecteur": {"nom": C.NOM, "version": C.VERSION, "source": SOURCE_TYPE},
    }
    try:
        C.ecrire_atomique(brut, dest_eml)
        C.ecrire_atomique(json.dumps(meta, indent=2, ensure_ascii=False).encode("utf-8"),
                          dest_json)
    except Exception as e:
        print(f"[{C.NOM}] échec écriture {base}: {e}", file=sys.stderr)
        return "erreur"
    cx.execute(
        """INSERT OR IGNORE INTO messages
           (message_id, de_adresse, destinataires, copies, date_mail, sujet,
            fichier_eml, fichier_json, pieces_jointes, dossier_src, ingere_at,
            extrait, source_ref, source_type, non_lu)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (mid, de, json.dumps(meta["destinataires"], ensure_ascii=False),
         json.dumps(meta["copies"], ensure_ascii=False), meta["date"], sujet,
         str(dest_eml), str(dest_json),
         json.dumps(meta["pieces_jointes"], ensure_ascii=False), dossier_logique,
         meta["collecte_at"], C._corps_texte(msg, C._extrait_max(cfg)),
         ref, SOURCE_TYPE, 0 if lu else 1))
    return "archive"


def relever(cfg, cx, C, limite=None):
    """Relève les nouveaux messages de Mail (fenêtre 'applemail_jours'). Renvoie
    (nouveaux, déjà connus). Mail fermé : rien (sauf 'applemail_lancer': true)."""
    p = parametres(cfg)
    res = _jxa(JXA_LISTER, p)
    if _ferme(res):
        print(f"[{C.NOM}] Mail est fermé : relève reportée au prochain cycle.")
        return 0, 0
    liste = res.get("messages") or []
    refs = {r[0] for r in cx.execute(
        "SELECT source_ref FROM messages WHERE source_type=? AND source_ref IS NOT NULL",
        (SOURCE_TYPE,))}
    mids = {r[0] for r in cx.execute("SELECT message_id FROM messages")}
    cibles, doublons = a_relever(liste, refs, mids, limite)
    faits = 0
    for k in range(0, len(cibles), PAQUET_SOURCES):
        paquet = [{"boite": e["boite"], "i": e["i"], "id": str(e["id"])}
                  for e in cibles[k:k + PAQUET_SOURCES]]
        lus = _jxa(JXA_SOURCES, dict(p, cibles=paquet))
        for m in lus.get("messages") or []:
            dossier = "/".join(x for x in (m.get("compte"), m.get("boite")) if x) or "Apple Mail"
            r = archiver_source(cfg, cx, C, source_en_octets(m.get("source")),
                                str(m["id"]), m.get("lu"), dossier)
            if r == "archive":
                faits += 1
            elif r == "doublon":
                doublons += 1
        cx.commit()
    return faits, doublons


def relever_envoyes(cfg, cx, C):
    """Indexe les envois récents (au plus JOURS_ENVOYES_MAX jours). Renvoie le nombre de
    nouveaux envois."""
    p = parametres(cfg)
    jours = int(cfg.get("applemail_jours", JOURS_DEFAUT) or 0)
    if jours <= 0 or jours > JOURS_ENVOYES_MAX:
        p["depuis"] = (datetime.now(timezone.utc) - timedelta(days=JOURS_ENVOYES_MAX)).isoformat()
    res = _jxa(JXA_ENVOYES, p)
    if _ferme(res):
        return 0
    n = 0
    for texte in res.get("entetes") or []:
        v = envoi_depuis_entetes(texte, C)
        if C.enregistrer_envoi(cx, v["message_id"], v["date"], v["a"], v["sujet"],
                               v["in_reply_to"], v["refs"], SOURCE_TYPE):
            n += 1
    cx.commit()
    return n


def appliquer_retour(cfg, cx, C):
    """Retour-mail Apple Mail : cockpit → Mail (drapeau « Traité » + lu) et Mail → cockpit
    (drapeau posé / retiré). Met aussi à jour lu / non lu. Seuls les messages de la
    fenêtre relue sont suivis ; au-delà, rien ne change."""
    p = parametres(cfg)
    res = _jxa(JXA_LISTER, p)
    if _ferme(res):
        return None
    etat = etat_par_ref(res.get("messages") or [], p["drapeau"])
    rows = cx.execute(
        "SELECT message_id, source_ref, etat, tb_sync FROM messages "
        "WHERE source_type=? AND source_ref IS NOT NULL", (SOURCE_TYPE,)).fetchall()
    enrichies = [(mid, ref, et, tb, etat[ref][0] if ref in etat else None)
                 for mid, ref, et, tb in rows]
    d = decisions_retour(enrichies)

    actions = ([{"id": ref, "faire": "marquer"} for _, ref in d["marquer"]]
               + [{"id": ref, "faire": "retirer"} for _, ref in d["retirer"]])
    faits = set()
    if actions:
        faits = set(_jxa(JXA_RETOUR, dict(p, actions=actions)).get("faits") or [])
    for mid, ref in d["marquer"]:
        if ref in faits:
            cx.execute("UPDATE messages SET tb_sync='traite', non_lu=0 WHERE message_id=?",
                       (mid,))
    for mid, ref in d["retirer"]:
        if ref in faits:
            cx.execute("UPDATE messages SET tb_sync=NULL WHERE message_id=?", (mid,))
    for mid in d["rouvrir"]:
        cx.execute("UPDATE messages SET etat=NULL, tb_sync=NULL "
                   "WHERE message_id=? AND tb_sync='traite'", (mid,))
    for mid in d["traiter"]:
        cx.execute("UPDATE messages SET etat='traite', tb_sync='traite' "
                   "WHERE message_id=? AND (etat IS NULL OR etat!='traite')", (mid,))
    # Lu / non lu tel que dans Mail (le cockpit filtre les non-lus).
    marques = {ref for _, ref in d["marquer"] if ref in faits}   # lus à l'instant
    for mid, ref, *_ in rows:
        if ref in etat and ref not in marques:
            cx.execute("UPDATE messages SET non_lu=? WHERE message_id=?",
                       (0 if etat[ref][1] else 1, mid))
    cx.commit()
    return d

#!/usr/bin/env python3
# Tests du compagnon 1.18 / moteur 2.2 (bibliothèque standard seule, aucun appel réseau :
# l'IA est remplacée par un faux). Lancer :  python3 -I compagnon/tests/test_pertinence.py
import http.client
import importlib.util
import json
import mailbox
import os
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ICI = Path(__file__).resolve().parent
sys.path.insert(0, str(ICI.parent))


def _charger(nom, fichier):
    spec = importlib.util.spec_from_file_location(nom, ICI.parent / fichier)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _charger("iamail_compagnon", "iamail_compagnon.py")
O = _charger("collecteur_outlook", "collecteur_outlook.py")
AUJ = datetime.now().strftime("%Y-%m-%d")


def jour(n):
    return (datetime.now() + timedelta(days=n)).strftime("%Y-%m-%d")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.cfg_path = d / "config.json"
        self.cfg_path.write_text(json.dumps({
            "dossier_staging": str(d / "in"), "dossier_destination": str(d / "arch"),
            "base_sqlite": str(d / "i.sqlite3"), "mes_adresses": ["moi@ccl.be"],
            "ia": {"fournisseur": "mistral", "cle": "x", "modele": "faux"},
        }), encoding="utf-8")
        self.cfg = M.charger_config(self.cfg_path)
        M.charger_moteur(self.cfg)
        self.cx = M.connexion(self.cfg["base_sqlite"])
        M._assurer_colonnes(self.cx)
        self._mistral = M.appeler_mistral

    def tearDown(self):
        M.appeler_mistral = self._mistral
        self.cx.close()
        self.tmp.cleanup()

    def ins(self, mid, de="Jean Dupont <jean.dupont@client.be>", sujet="Sujet", jours=0, **k):
        d = (datetime.now(timezone.utc) - timedelta(days=jours, minutes=len(mid))).isoformat()
        base = {"categorie": "autre", "urgence": "normale", "pour_moi": 1, "bulk": 0,
                "important": 0, "suspect": 0, "expediteur_humain": 1, "extrait": "Bonjour."}
        base.update(k)
        self.cx.execute("INSERT INTO messages (message_id,de_adresse,sujet,date_mail,"
                        "destinataires,copies,pieces_jointes) VALUES (?,?,?,?,?,?,?)",
                        (mid, de, sujet, d, '["moi@ccl.be"]', "[]", "[]"))
        for c, v in base.items():
            self.cx.execute(f"UPDATE messages SET {c}=? WHERE message_id=?", (v, mid))
        self.cx.commit()

    def juge(self, mid, urgence="normale", action=1, echeance="aucune", **k):
        v = {"ia_source": "mistral", "ia_urgence": urgence, "ia_action": action,
             "ia_echeance": echeance, "ia_categorie": "autre", "ia_tache": "Faire X",
             "ia_resume": "Résumé de X", "ia_justification": "Parce que."}
        v.update(k)
        for c, val in v.items():
            self.cx.execute(f"UPDATE messages SET {c}=? WHERE message_id=?", (val, mid))
        self.cx.commit()

    def cases(self, vue="atraiter"):
        return {c["cle"]: [i["id"] for i in c["items"]]
                for c in M.cockpit_liste(self.cfg["base_sqlite"], vue)}

    def analyser_ia(self, **k):
        a = dict(config=str(self.cfg_path), mock=False, retout=False, limite=None,
                 jours=None, compter=False)
        a.update(k)
        self.cx.commit()
        M.cmd_analyser_ia(types.SimpleNamespace(**a))


BON = {"categorie": "autre", "urgence": "haute", "action_attendue": True, "tache": "Rappeler",
       "echeance": "aucune", "justification": "Demande directe.", "resume": "Il demande un rappel.",
       "question": None, "confiance": "haute", "evenement": None, "concerne": "moi"}


class ZeroPerte(Base):
    def test_reponse_ia_rejetee_reste_visible_a_sa_place(self):
        self.ins("crit", sujet="Serveur inaccessible", urgence="haute", important=1)
        M.appeler_mistral = lambda *a, **k: '{"n importe": "quoi"}'
        self.analyser_ia()
        self.assertIn("crit", self.cases()["maintenant"])
        src = self.cx.execute("SELECT ia_source FROM messages").fetchone()[0]
        self.assertEqual(src, "erreur")

    def test_panne_reseau_reste_visible_et_sera_rejuge(self):
        import urllib.error
        self.ins("p1", urgence="haute")

        def panne(*a, **k):
            raise urllib.error.URLError("coupé")
        M.appeler_mistral = panne
        self.analyser_ia()
        self.assertIn("p1", self.cases()["maintenant"])
        self.assertIsNone(self.cx.execute("SELECT ia_source FROM messages").fetchone()[0])
        M.appeler_mistral = lambda *a, **k: json.dumps(BON)
        self.analyser_ia()
        self.assertEqual(self.cx.execute("SELECT ia_source FROM messages").fetchone()[0], "mistral")

    def test_seconde_chance_si_reponse_hors_contrat(self):
        self.ins("r1")
        rep = iter(["pas du json", json.dumps(BON)])
        M.appeler_mistral = lambda *a, **k: next(rep)
        self.analyser_ia()
        self.assertEqual(self.cx.execute("SELECT ia_urgence FROM messages").fetchone()[0], "haute")

    def test_masse_non_importante_a_sa_vue(self):
        self.ins("nl", de="news@mail.pub.com", bulk=1, pour_moi=None, urgence="basse",
                 categorie="newsletter")
        self.assertNotIn("nl", sum(self.cases().values(), []))
        self.assertIn("nl", sum(self.cases("masse").values(), []))


class Bouchon(Base):
    def test_mode_mock_de_bout_en_bout(self):
        self.ins("b1", sujet="Demande de devis")
        self.analyser_ia(mock=True)
        src, ext = self.cx.execute("SELECT ia_source, ia_extensions FROM messages").fetchone()
        self.assertEqual(src, "mock")
        self.assertEqual(json.loads(ext), {"evenement": None, "concerne": "moi"})
        self.assertIn("b1", self.cases()["cette_semaine"])
        self.cfg["ia"] = {"fournisseur": "mock"}
        self.assertEqual(M.construire_script_topo(self.cfg)[2], "gabarit")


class CaseDuJour(Base):
    def test_la_case_remonte_le_jour_de_l_echeance(self):
        self.ins("e1")
        self.juge("e1", urgence="basse", echeance=AUJ, case_topo="ce_mois")  # case périmée
        self.assertIn("e1", self.cases()["maintenant"])

    def test_tri_par_urgence_dans_la_case(self):
        self.ins("recent_haute", jours=0); self.juge("recent_haute", urgence="haute")
        self.ins("ancien_critique", jours=2); self.juge("ancien_critique", urgence="critique")
        self.assertEqual(self.cases()["maintenant"][0], "ancien_critique")


class Memoire(Base):
    def test_le_message_ne_se_compte_pas_lui_meme(self):
        self.ins("seul", sujet="Devis toiture", important=1)
        _, fils, _ = M.profils_expediteurs(self.cx)
        row = {"sujet": "Devis toiture", "date_mail": datetime.now(timezone.utc).isoformat(),
               "important": 1, "urgence": "normale"}
        self.assertEqual(M._memoire_fil(row, fils), "")
        self.ins("autre", sujet="Re: Devis toiture", important=0)
        _, fils, _ = M.profils_expediteurs(self.cx)
        txt = M._memoire_fil(row, fils)
        self.assertIn("1 autre(s) message(s)", txt)
        self.assertNotIn("déjà jugé", txt)

    def test_fenetre_de_fil(self):
        for i in range(5):
            self.ins(f"vieux{i}", sujet="Facture", jours=200 + i, important=1)
        self.ins("neuf", sujet="Facture", important=1)
        _, fils, _ = M.profils_expediteurs(self.cx)
        row = {"sujet": "Facture", "date_mail": datetime.now(timezone.utc).isoformat(),
               "important": 1, "urgence": "normale"}
        self.assertEqual(M._memoire_fil(row, fils), "")

    def test_relance_meme_expediteur_meme_fil(self):
        self.ins("m1", sujet="Accès VPN", jours=3)
        self.ins("m2", sujet="Re: Accès VPN", jours=0)
        _, _, attente = M.profils_expediteurs(self.cx)
        r = dict(zip(["de_adresse", "sujet", "date_mail"], self.cx.execute(
            "SELECT de_adresse, sujet, date_mail FROM messages WHERE message_id='m2'").fetchone()))
        self.assertIn("déjà écrit 1 fois", M._relances_txt(r, attente))
        self.cx.execute("UPDATE messages SET etat='traite' WHERE message_id='m1'")
        _, _, attente = M.profils_expediteurs(self.cx)
        self.assertEqual(M._relances_txt(r, attente), "")


class EntreeIa(Base):
    def test_profil_utilisateur_et_jour_de_la_semaine(self):
        row = {"message_id": "x", "de_adresse": "a@b.be", "sujet": "S", "pour_moi": 1,
               "expediteur_humain": 1, "bulk": 0, "important": 0,
               "date_mail": datetime.now(timezone.utc).isoformat(), "extrait": "Corps"}
        sans = M.construire_entree_ia(self.cfg, row, None, {}, "2026-11-16")
        self.assertNotIn("DESTINATAIRE", sans)
        self.assertIn("DATE DU JOUR : lundi 16 novembre 2026 (2026-11-16)\n\nNIVEAUX", sans)
        self.cfg["utilisateur"] = {"nom": "Arnaud", "fonction": "responsable informatique",
                                   "profil": "Urgent : panne qui bloque plusieurs collègues."}
        avec = M.construire_entree_ia(self.cfg, row, None, {}, "2026-11-16")
        self.assertIn("DESTINATAIRE (la personne pour qui tu tries) : Arnaud, responsable "
                      "informatique. Urgent : panne qui bloque plusieurs collègues.", avec)

    def test_extensions_du_compagnon_dans_le_prompt(self):
        exts = M.charger_extensions(self.cfg)
        self.assertEqual([e["nom"] for e in exts], ["evenement", "portee"])
        sysm = M.systeme_ia(self.cfg, exts)
        self.assertIn('"evenement":', sysm); self.assertIn('"concerne":', sysm)
        self.assertNotIn("{{", sysm)
        ok, _, err = M.valider_sortie(dict(BON, evenement={"date": "2026-11-17", "heure": "14:30",
                                                         "lieu": None, "annule": False}),
                                      set(M.CAT_PAR_CODE), exts)
        self.assertTrue(ok, err)
        ok, _, _ = M.valider_sortie(dict(BON, evenement={"date": "17/11", "heure": None,
                                                         "lieu": None, "annule": False}),
                                    set(M.CAT_PAR_CODE), exts)
        self.assertFalse(ok)


class VecteursCommuns(unittest.TestCase):
    """Les mêmes fichiers que GesEnt : moteur/tests/entree/*.json."""
    DOSSIER = ICI.parent.parent / "moteur" / "tests" / "entree"

    def test_bloc_destinataire(self):
        cas = json.loads((self.DOSSIER / "destinataire.json").read_text(encoding="utf-8"))["cas"]
        self.assertGreaterEqual(len(cas), 10)
        for c in cas:
            with self.subTest(c["cas"]):
                r = M.bloc_destinataire(**c["entree"])
                self.assertEqual(r, c["attendu"])
                self.assertLessEqual(len(r), 1500)

    def test_date_du_jour(self):
        for c in json.loads((self.DOSSIER / "date_du_jour.json").read_text(encoding="utf-8"))["cas"]:
            with self.subTest(c["date"]):
                self.assertEqual(M._date_du_jour_txt(c["date"]), c["attendu"])


class Rejugement(Base):
    def test_profil_modifie_propose_sans_appeler_l_ia(self):
        appels = []
        M.appeler_mistral = lambda *a, **k: appels.append(1) or json.dumps(BON)
        M.enregistrer_reglages(self.cfg, {"utilisateur": {"fonction": "Informatique"},
                                          "mes_adresses": ["moi@ccl.be"]})
        self.assertEqual(appels, [])
        self.assertTrue(any("profil a changé" in a for a in M.alertes_reglages(self.cfg)))
        M.enregistrer_reglages(self.cfg, {"utilisateur": {"fonction": "Informatique"},
                                          "mes_adresses": ["moi@ccl.be"]})
        M._effacer_profil_a_rejuger(self.cfg)
        self.assertFalse(any("profil a changé" in a for a in M.alertes_reglages(self.cfg)))
        self.assertNotIn("profil_a_rejuger", json.loads(self.cfg_path.read_text(encoding="utf-8")))

    def test_estimation_exclut_corrections_et_masse(self):
        self.ins("a1"); self.juge("a1")
        self.ins("a2"); self.juge("a2"); M.corriger_jugement(self.cx, "a2", "plus")
        self.ins("vieux", jours=30)
        self.ins("nl", bulk=1, important=0)
        ids = [r[0] for r in M.selection_ia(self.cx, True, 14, 300)]
        self.assertEqual(ids, ["a1"])


class Corrections(Base):
    def test_correction_appliquee_gardee_et_servie_en_exemple(self):
        self.ins("c1", sujet="Rapport hebdo ventes"); self.juge("c1", urgence="haute")
        self.assertTrue(M.corriger_jugement(self.cx, "c1", "pas_pour_moi"))
        self.assertIn("c1", self.cases()["aucune"])
        M.appeler_mistral = lambda *a, **k: json.dumps(BON)
        self.analyser_ia(retout=True)                       # ne rejuge pas une correction
        self.assertEqual(self.cx.execute(
            "SELECT ia_action FROM messages WHERE message_id='c1'").fetchone()[0], 0)
        self.ins("c2", sujet="Rapport hebdo ventes semaine 47")
        vu = {}
        M.appeler_mistral = lambda cfg_ia, sysm, ent, **k: (vu.setdefault("e", ent), json.dumps(BON))[1]
        self.analyser_ia()
        self.assertIn("jurisprudence", vu["e"])
        self.assertIn("action_attendue: false", vu["e"])


class Identifiants(Base):
    def test_migration_des_chevrons_et_pas_de_reimport(self):
        self.cx.execute("INSERT INTO messages (message_id) VALUES ('<abc@x.be>')")
        self.cx.commit(); self.cx.close()
        self.cx = M.connexion(self.cfg["base_sqlite"])
        self.assertEqual([r[0] for r in self.cx.execute("SELECT message_id FROM messages")], ["abc@x.be"])
        mb = Path(self.tmp.name) / "INBOX"
        box = mailbox.mbox(str(mb))
        box.add("From: a@b.be\nTo: moi@ccl.be\nSubject: Un\nMessage-ID: <abc@x.be>\n"
                "Date: Mon, 16 Nov 2026 09:00:00 +0100\n\nCorps\n")
        box.add("From: a@b.be\nTo: moi@ccl.be\nSubject: Deux\nMessage-ID: <def@x.be>\n"
                "Date: Mon, 16 Nov 2026 09:05:00 +0100\n\nCorps\n")
        box.flush(); box.close()
        b = M.importer_mbox(self.cfg, self.cx, mb)
        self.assertEqual((b["archive"], b["doublon"]), (1, 1))
        self.assertIn("def@x.be", [r[0] for r in self.cx.execute("SELECT message_id FROM messages")])


class Topo(Base):
    def remplir(self):
        self.ins("vieux_rdv", de="Julie Martin <julie.martin@x.be>", sujet="Réunion du 27 octobre", jours=20)
        self.juge("vieux_rdv", urgence="basse", action=0, ia_categorie="rendez_vous",
                  ia_extensions=json.dumps({"evenement": {"date": jour(-20), "heure": "10:00",
                                                          "lieu": None, "annule": False}, "concerne": "moi"}))
        self.ins("rdv", de="Paul Durand <paul.durand@y.be>", sujet="Comité IA", jours=9)
        self.juge("rdv", urgence="normale", action=0, ia_tache=None, ia_resume="Comité IA en salle 2",
                  ia_extensions=json.dumps({"evenement": {"date": AUJ, "heure": "14:30",
                                                          "lieu": "salle 2", "annule": False}, "concerne": "moi"}))
        self.ins("panne", de="Marc Petit <marc.petit@z.be>", sujet="Serveur de fichiers", jours=1)
        self.juge("panne", urgence="critique", ia_tache="Rétablir le serveur de fichiers",
                  ia_resume="Le secrétariat n'accède plus au serveur de fichiers")
        self.ins("recent", sujet="Question licence", jours=0); self.juge("recent", urgence="haute")
        self.ins("hame", sujet="Votre compte", suspect=1); self.juge("hame", urgence="haute")

    def test_matiere_et_gabarit(self):
        self.remplir()
        t = M.elements_topo(self.cx, self.cfg)
        self.assertEqual([e["id"] for e in t["elements"]][:2], ["panne", "recent"])   # critique d'abord
        self.assertEqual([v["quoi"] for v in t["evenements"]], ["Comité IA en salle 2"])
        self.assertEqual(t["suspects"], 1)
        court, complet = M.script_topo_gabarit(t)
        self.assertIn("14 heures 30", complet); self.assertIn("salle 2", complet)
        self.assertNotIn("27 octobre", complet)        # la vieille réunion n'est plus « du jour »
        self.assertNotIn("Votre compte", complet)      # un suspect n'est jamais lu
        self.assertLess(complet.index("serveur de fichiers"), complet.index("Faire X"))

    def test_redaction_ia_cache_et_repli(self):
        self.remplir()
        appels = []

        def faux(cfg_ia, sysm, ent, **k):
            appels.append((sysm, ent, k))
            return json.dumps({"court": "Bonjour. Le serveur de fichiers est en panne, c'est la priorité.",
                               "complet": "Bonjour. **Priorité** : le serveur de fichiers est en panne "
                                          "pour le secrétariat, voir https://exemple.be/x. Comité IA à 14 heures 30."})
        M.appeler_mistral = faux
        court, complet, source = M.construire_script_topo(self.cfg)
        self.assertEqual(source, "ia")
        self.assertNotIn("http", complet); self.assertNotIn("*", complet)
        self.assertIn("suspect", complet)              # ajouté si l'IA l'a omis
        sysm, ent, k = appels[0]
        self.assertIn("[MAINTENANT]", ent); self.assertIn("aujourd'hui à 14:30", ent)
        self.assertNotIn("Votre compte", ent)
        self.assertEqual(k.get("temperature"), 0.3)
        M.construire_script_topo(self.cfg)
        self.assertEqual(len(appels), 1)               # second topo identique : cache

        def casse(*a, **k):
            raise RuntimeError("panne")
        M.appeler_mistral = casse
        self.cx.execute("DELETE FROM topo_cache"); self.cx.commit()
        self.assertEqual(M.construire_script_topo(self.cfg)[2], "gabarit")


class Serveur(Base):
    def setUp(self):
        super().setUp()
        self.ins("s1"); self.juge("s1")
        self.srv = M.http.server.HTTPServer(("127.0.0.1", 0), M.faire_cockpit_handler(self.cfg))
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close()
        super().tearDown()

    def req(self, methode, chemin, corps=None, **entetes):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": f"127.0.0.1:{self.port}"}
        h.update(entetes)
        data = json.dumps(corps).encode() if corps is not None else None
        if data:
            h.setdefault("Content-Type", "application/json")
        c.request(methode, chemin, body=data, headers=h)
        r = c.getresponse()
        return r.status, dict(r.getheaders()), r.read()

    def test_origines(self):
        ici = f"http://127.0.0.1:{self.port}"
        self.assertEqual(self.req("GET", "/api/liste")[0], 200)
        self.assertEqual(self.req("GET", "/api/liste", Origin=ici)[0], 200)
        st, h, _ = self.req("GET", "/api/sync/pour-client", Origin="moz-extension://abc-123")
        self.assertEqual((st, h.get("Access-Control-Allow-Origin")), (200, "moz-extension://abc-123"))
        self.assertEqual(self.req("GET", "/api/liste", Origin="https://site-quelconque.example")[0], 403)
        self.assertEqual(self.req("POST", "/api/action", {"id": "s1", "etat": "traite"},
                                  Origin="https://site-quelconque.example")[0], 403)
        self.assertEqual(self.req("GET", "/api/liste", Host="attaquant.example:80")[0], 403)
        self.assertIsNone(self.cx.execute("SELECT etat FROM messages").fetchone()[0])
        self.assertEqual(self.req("POST", "/api/action", {"id": "s1", "etat": "traite"}, Origin=ici)[0], 200)

    def test_reglages_et_correction(self):
        st, _, b = self.req("POST", "/api/reglages", {"utilisateur": {
            "nom": "Arnaud", "fonction": "Informatique", "profil": "Pannes d'abord."},
            "mes_adresses": ["moi@ccl.be"]})
        self.assertEqual(st, 200)
        enregistre = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(enregistre["utilisateur"]["fonction"], "Informatique")
        self.assertNotIn("_chemin_config", enregistre)
        self.assertEqual(M._utilisateur_txt(self.cfg), "Arnaud, Informatique. Pannes d'abord.")
        self.assertEqual(self.req("POST", "/api/corriger", {"id": "s1", "quoi": "plus"})[0], 200)
        st, _, b = self.req("POST", "/api/rejuger", {"jours": 14, "compter": True})
        self.assertEqual((st, json.loads(b)["n"], json.loads(b)["max"]), (200, 0, 300))
        self.assertEqual(self.cx.execute("SELECT ia_urgence FROM messages").fetchone()[0], "haute")


class Outlook(unittest.TestCase):
    def test_classes_et_entetes(self):
        self.assertTrue(O.est_courrier("IPM.Note"))
        self.assertTrue(O.est_courrier("IPM.Schedule.Meeting.Request"))
        self.assertFalse(O.est_courrier("REPORT.IPM.Note.NDR"))
        h = O.entetes_tri_depuis("Received: x\r\nList-Unsubscribe: <mailto:u@x.be>\r\n"
                                 "Reply-To: autre@ailleurs.com\r\nSubject: s\r\n\r\n")
        self.assertEqual(h, {"Reply-To": "autre@ailleurs.com", "List-Unsubscribe": "<mailto:u@x.be>"})
        eml = O.construire_eml({"de_adresse": "a@b.be", "to": ["moi@ccl.be"], "sujet": "S",
                                "recu_iso": "2026-11-16T09:00:00+00:00", "corps": "c",
                                "message_id": "id@x", "entetes": h})
        self.assertIn(b"List-Unsubscribe: <mailto:u@x.be>", eml)
        self.assertIn(b"Reply-To: autre@ailleurs.com", eml)


if __name__ == "__main__":
    unittest.main(verbosity=1)

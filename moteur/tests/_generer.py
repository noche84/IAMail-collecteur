#!/usr/bin/env python3
# Génère le jeu de conformité du moteur de tri partagé 2.x (.eml + .attendu.json).
#
# Les fichiers produits sont STATIQUES et committés : les harnais (Python, PHP)
# lisent exactement les mêmes. Les valeurs attendues ci-dessous sont écrites À LA
# MAIN à partir de regles.json (_algorithme) et taxonomie.json — jamais recopiées
# depuis la sortie d'un harnais (sinon le test ne prouverait rien).
#
#   python3 -I _generer.py
import hashlib
import json
from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path

CAS = Path(__file__).resolve().parent / "cas"
CAS.mkdir(exist_ok=True)
for f in CAS.glob("*"):
    f.unlink()

DATE = "Tue, 10 Nov 2026 09:15:00 +0100"


def eml(headers, corps_txt=None, corps_html=None, mid="auto", charset="utf-8"):
    m = EmailMessage()
    for k, v in headers.items():
        m[k] = v
    if "Date" not in headers:
        m["Date"] = DATE
    if mid == "auto":
        mid = None  # posé par ecrire() avec le numéro du cas
    if mid is not None:
        m["Message-ID"] = mid
    if corps_html and not corps_txt:
        m.add_alternative(corps_html, subtype="html")
    elif charset != "utf-8":
        m.set_content(corps_txt, charset=charset, cte="8bit")
    else:
        m.set_content(corps_txt or "Bonjour.")
    return m


def ecrire(num, nom, msg, extraction, deterministe, mid_auto=True, entete_brut=None):
    base = f"{num:03d}-{nom}"
    if mid_auto and "Message-ID" not in msg:
        msg["Message-ID"] = f"<cas-{num:03d}@exemple.be>"
        extraction.setdefault("message_id", f"cas-{num:03d}@exemple.be")
    octets = msg.as_bytes(policy=SMTP)
    if entete_brut:  # en-tête écrit tel quel (la bibliothèque le réencoderait)
        octets = entete_brut.encode("ascii") + b"\r\n" + octets
    (CAS / f"{base}.eml").write_bytes(octets)
    (CAS / f"{base}.attendu.json").write_text(json.dumps(
        {"extraction": extraction, "deterministe": deterministe},
        indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def det(categorie, pour_moi, humain, bulk, important, suspect, urgence, signaux=()):
    return {"categorie": categorie, "pour_moi": pour_moi, "expediteur_humain": humain,
            "bulk": bulk, "important": important, "suspect": suspect,
            "urgence": urgence, "signaux": sorted(signaux)}


# ─── Cas repris du moteur 1.1.0 (attendus complétés pour 2.0) ────────────────

# 001 — humain seulement en copie ; « compte » rend important ; plafond copie
ecrire(1, "humain-en-copie",
       eml({"From": "Jean Dupont <jean.dupont@entreprise.be>",
            "To": "collegue@entreprise.be", "Cc": "moi@ccl.be",
            "Subject": "Compte rendu de la visite"},
           "Bonjour, voici le compte rendu."),
       {"de": "jean.dupont@entreprise.be", "a": ["collegue@entreprise.be"], "cc": ["moi@ccl.be"],
        "cle_fil": "compte rendu de la visite", "extrait_debut": "Bonjour, voici"},
       det("autre", 0, 1, 0, 1, 0, "normale"))

# 002 — newsletter avec List-Unsubscribe
ecrire(2, "newsletter-list-unsubscribe",
       eml({"From": "Promos <news@mg.boutique.com>", "To": "moi@ccl.be",
            "Subject": "Nos offres du mois",
            "List-Unsubscribe": "<mailto:stop@boutique.com>"},
           "Profitez de nos promotions."),
       {"de": "news@mg.boutique.com", "cle_fil": "nos offres du mois"},
       det("newsletter", 1, 0, 1, 0, 0, "basse"))

# 003 — alerte bancaire en masse : reste importante, catégorie finance
ecrire(3, "alerte-bancaire-masse",
       eml({"From": "Binance <no-reply@binance.com>", "To": "moi@ccl.be",
            "Subject": "Security alert on your account",
            "List-Unsubscribe": "<mailto:unsub@binance.com>"},
           "Unusual activity detected."),
       {"de": "no-reply@binance.com"},
       det("finance", 1, 0, 1, 1, 0, "normale"))

# 004 — domaine composé bnpparibasfortis ; label « info » = domaine marketing
ecrire(4, "domaine-bnpparibasfortis",
       eml({"From": "BNP <info@info.bnpparibasfortis.be>", "To": "moi@ccl.be",
            "Subject": "Mouvement sur votre compte"},
           "Un mouvement a été enregistré."),
       {"de": "info@info.bnpparibasfortis.be"},
       det("finance", 1, 0, 1, 1, 0, "normale"))

# 005 — « urgent » d'un humain adressé à moi → haute
ecrire(5, "urgent-humain",
       eml({"From": "Marie Martin <marie.martin@entreprise.be>", "To": "moi@ccl.be",
            "Subject": "URGENT : réponse attendue"},
           "Peux-tu me répondre vite ?"),
       {"de": "marie.martin@entreprise.be"},
       det("autre", 1, 1, 0, 0, 0, "haute", ["urgent"]))

# 006 — « urgent » d'un robot en masse : signaux listés mais ne comptent pas
ecrire(6, "urgent-robot",
       eml({"From": "Alertes <no-reply@mg.marque.com>", "To": "moi@ccl.be",
            "Subject": "URGENT : dernière relance",
            "List-Unsubscribe": "<mailto:x@marque.com>"},
           "Offre qui expire."),
       {"de": "no-reply@mg.marque.com"},
       det("newsletter", 1, 0, 1, 0, 0, "basse", ["urgent", "derniere relance"]))

# 007 — hameçonnage (Reply-To ≠ From + mot de passe)
ecrire(7, "hameconnage-replyto",
       eml({"From": "Support <support@mabanque.be>", "To": "moi@ccl.be",
            "Reply-To": "pirate@autre-domaine.ru",
            "Subject": "Vérification de votre mot de passe"},
           "Cliquez pour vérifier votre mot de passe."),
       {"de": "support@mabanque.be"},
       det("finance", 1, 0, 0, 1, 1, "normale"))

# 008 — fil Re: Re: Tr: (clé de fil propre)
ecrire(8, "fil-re-tr",
       eml({"From": "Paul <paul.nelis@entreprise.be>", "To": "moi@ccl.be",
            "Subject": "Re: Re: Tr: Réunion de coordination Herstal"},
           "On confirme la réunion."),
       {"de": "paul.nelis@entreprise.be", "cle_fil": "reunion de coordination herstal"},
       det("rendez_vous", 1, 1, 0, 1, 0, "normale"))

# 009 — booking.com n'est PAS important (« ing » ≠ booking)
ecrire(9, "booking-pas-important",
       eml({"From": "Booking <news@mail.booking.com>", "To": "moi@ccl.be",
            "Subject": "Vos prochaines vacances",
            "List-Unsubscribe": "<mailto:x@booking.com>"},
           "Des idées de séjour."),
       {"de": "news@mail.booking.com"},
       det("newsletter", 1, 0, 1, 0, 0, "basse"))

# 010 — « Suivi de commande » → commande_livraison
ecrire(10, "suivi-commande",
       eml({"From": "Shop <info@boutique.be>", "To": "moi@ccl.be",
            "Subject": "Suivi de commande 1234"},
           "Votre colis est en route."),
       {"de": "info@boutique.be"},
       det("commande_livraison", 1, 0, 0, 1, 0, "normale"))

# 011 — « setups » ne matche pas « ups »
ecrire(11, "setups-pas-ups",
       eml({"From": "Blog <info@tech.be>", "To": "moi@ccl.be",
            "Subject": "Nos meilleurs setups de bureau"},
           "Idées d'aménagement."),
       {"de": "info@tech.be"},
       det("autre", 1, 0, 0, 0, 0, "normale"))

# 012 — « attendue aujourd'hui » (apostrophe) : signal haute d'un humain
ecrire(12, "aujourdhui-apostrophe",
       eml({"From": "Chef <luc.henry@entreprise.be>", "To": "moi@ccl.be",
            "Subject": "Décision attendue aujourd’hui"},
           "Il faut trancher."),
       {"de": "luc.henry@entreprise.be"},
       det("autre", 1, 1, 0, 0, 0, "haute", ["attendue aujourd'hui"]))

# 013 — corps HTML seul → extrait texte, script retiré
ecrire(13, "corps-html-seul",
       eml({"From": "Info <contact@asbl.be>", "To": "moi@ccl.be",
            "Subject": "Invitation assemblée"},
           corps_html="<html><body><p>Bonjour,</p><p>Vous êtes invité.</p>"
                      "<script>x()</script></body></html>"),
       {"de": "contact@asbl.be", "extrait_debut": "Bonjour,\nVous êtes invité."},
       det("rendez_vous", 1, 0, 0, 1, 0, "normale"))

# ─── Nouveaux cas 2.0 ─────────────────────────────────────────────────────────

# 014 — sans Message-ID → identifiant synthétique
sujet14 = "Plan de la cuisine"
synth = "synth-" + hashlib.sha256(
    f"{DATE}|anne.lejeune@entreprise.be|{sujet14}".encode("utf-8")).hexdigest()[:32]
ecrire(14, "sans-message-id",
       eml({"From": "Anne Lejeune <anne.lejeune@entreprise.be>", "To": "moi@ccl.be",
            "Subject": sujet14}, "Voici le plan modifié.", mid=None),
       {"message_id": synth, "de": "anne.lejeune@entreprise.be"},
       det("autre", 1, 1, 0, 0, 0, "normale"), mid_auto=False)

# 015 — œ conservé (pas de translittération), [étiquette] et Tr: retirés
ecrire(15, "oe-et-etiquette",
       eml({"From": "Gilles Lambert <gilles.lambert@entreprise.be>", "To": "moi@ccl.be",
            "Subject": "TR : Nœud du problème [Projet X]"},
           "Voir ci-dessous."),
       {"cle_fil": "nœud du probleme"},
       det("autre", 1, 1, 0, 0, 0, "normale"))

# 016 — objet encodé RFC 2047 en base64
m16 = eml({"From": "Sophie Renard <sophie.renard@gmail.com>", "To": "moi@ccl.be"},
          "Bonjour, pourriez-vous passer pour un devis ?")
ecrire(16, "objet-rfc2047-base64", m16,
       {"de": "sophie.renard@gmail.com", "cle_fil": "demande de devis pour une veranda"},
       det("demande_devis", 1, 1, 0, 1, 0, "normale"),
       entete_brut="Subject: =?UTF-8?B?RGVtYW5kZSBkZSBkZXZpcyBwb3VyIHVuZSB2w6lyYW5kYQ==?=")

# 017 — corps en ISO-8859-1 ; « intervention » → suivi_chantier ; freemail humain
ecrire(17, "corps-iso-8859-1",
       eml({"From": "Garage Dumont <garage.dumont@skynet.be>", "To": "moi@ccl.be",
            "Subject": "Intervention terminée"},
           "Réparation effectuée hier, merci.", charset="iso-8859-1"),
       {"extrait_debut": "Réparation effectuée hier"},
       det("suivi_chantier", 1, 1, 0, 0, 0, "normale"))

# 018 — freemail sans forme prénom.nom = humain ; Message-ID : chevrons retirés, casse gardée
ecrire(18, "freemail-et-chevrons",
       eml({"From": "Jacques <jacques1960@hotmail.com>", "To": "moi@ccl.be",
            "Subject": "Question robinet"},
           "Le robinet de la cuisine goutte.", mid="<ABC.123@Exemple.be>"),
       {"message_id": "ABC.123@Exemple.be", "de": "jacques1960@hotmail.com"},
       det("autre", 1, 1, 0, 0, 0, "normale"), mid_auto=False)

# 019 — mot le plus long : « rappel de paiement » (paiement) > « facture »
ecrire(19, "mot-le-plus-long",
       eml({"From": "Comptabilité <comptabilite@fournisseur-sa.be>", "To": "moi@ccl.be",
            "Subject": "Rappel de paiement facture F-2026-114"},
           "Veuillez trouver notre rappel."),
       {"de": "comptabilite@fournisseur-sa.be"},
       det("paiement", 1, 0, 0, 1, 0, "normale"))

# 020 — signal CRITIQUE dans le CORPS d'un humain → critique
ecrire(20, "critique-dans-le-corps",
       eml({"From": "Claire Dubois <claire.dubois@gmail.com>", "To": "moi@ccl.be",
            "Subject": "Chaudière"},
           "Bonjour, nous n'avons plus de chauffage depuis hier soir et nous avons un bébé."),
       {"de": "claire.dubois@gmail.com"},
       det("autre", 1, 1, 0, 0, 0, "critique", ["plus de chauffage", "bebe"]))

# 021 — « danger » dans une publicité de masse : listé, ne compte pas
ecrire(21, "danger-dans-une-pub",
       eml({"From": "Pro Equip <news@mail.proequip.be>", "To": "moi@ccl.be",
            "Subject": "Nos nouveaux disjoncteurs",
            "List-Unsubscribe": "<mailto:stop@proequip.be>"},
           "Ne prenez aucun risque : danger d'électrocution évité."),
       {"de": "news@mail.proequip.be"},
       det("newsletter", 1, 0, 1, 0, 0, "basse", ["danger"]))

# 022 — « urgent » d'un humain mais je ne suis qu'en copie → plafonné à normale
ecrire(22, "urgent-en-copie-plafonne",
       eml({"From": "Marc Lambert <marc.lambert@entreprise.be>",
            "To": "chef@entreprise.be", "Cc": "moi@ccl.be",
            "Subject": "Urgent : plans à valider"},
           "Merci de valider les plans."),
       {"cc": ["moi@ccl.be"]},
       det("autre", 0, 1, 0, 0, 0, "normale", ["urgent"]))

# 023 — signal qui BAISSE : « rien ne presse » → basse
ecrire(23, "signal-qui-baisse",
       eml({"From": "Paul Gérard <paul.gerard@gmail.com>", "To": "moi@ccl.be",
            "Subject": "Question pour la salle de bain"},
           "Rien ne presse, c'est pour l'an prochain."),
       {},
       det("autre", 1, 1, 0, 0, 0, "basse", ["rien ne presse"]))

# 024 — réponse automatique d'un humain → reponse_auto, basse
ecrire(24, "reponse-automatique",
       eml({"From": "Julie Masson <julie.masson@entreprise.be>", "To": "moi@ccl.be",
            "Subject": "Réponse automatique : absente jusqu'au 20 novembre"},
           "Je suis absente jusqu'au 20 novembre."),
       {"cle_fil": "reponse automatique : absente jusqu au 20 novembre"},
       det("reponse_auto", 1, 1, 0, 0, 0, "basse"))

# 025 — un signal qui MONTE l'emporte sur un signal qui baisse
ecrire(25, "monter-l-emporte",
       eml({"From": "Anne Thiry <anne.thiry@gmail.com>", "To": "moi@ccl.be",
            "Subject": "Évier"},
           "Pas pressé mais il y a une fuite d’eau sous l’évier."),
       {},
       det("autre", 1, 1, 0, 0, 0, "critique", ["pas presse", "fuite d'eau"]))

# 026 — catégorie par le DOMAINE (dpd) ; masse mais importante
ecrire(26, "categorie-par-domaine",
       eml({"From": "DPD <notification@dpd.be>", "To": "moi@ccl.be",
            "Subject": "Votre envoi 0123 est en route"},
           "Suivez votre envoi."),
       {"de": "notification@dpd.be"},
       det("commande_livraison", 1, 0, 1, 1, 0, "normale"))

print(f"{len(list(CAS.glob('*.eml')))} cas générés dans {CAS}")

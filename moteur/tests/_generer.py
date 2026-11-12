#!/usr/bin/env python3
# Génère les cas de conformité (.eml + .attendu.json) du moteur de tri partagé.
# Fichiers statiques volontairement : les deux harnais (Python pytest, PHP PHPUnit)
# lisent les MÊMES .eml et le MÊME attendu. Lancer une fois : python3 _generer.py
import json
from email.message import EmailMessage
from pathlib import Path

CAS = Path(__file__).resolve().parent / "cas"
CAS.mkdir(exist_ok=True)


def eml(headers, corps_txt=None, corps_html=None, mid="<auto@exemple.be>"):
    m = EmailMessage()
    for k, v in headers.items():
        m[k] = v
    if mid is not None:
        m["Message-ID"] = mid
    if corps_html and not corps_txt:
        m.add_alternative(corps_html, subtype="html")
    else:
        m.set_content(corps_txt or "Bonjour.")
    return m


def ecrire(num, nom, msg, extraction, deterministe):
    base = f"{num:03d}-{nom}"
    (CAS / f"{base}.eml").write_bytes(bytes(msg))
    (CAS / f"{base}.attendu.json").write_text(json.dumps(
        {"extraction": extraction, "deterministe": deterministe},
        indent=2, ensure_ascii=False), encoding="utf-8")


# 001 — humain, seulement en copie
ecrire(1, "humain-en-copie",
       eml({"From": "Jean Dupont <jean.dupont@entreprise.be>",
            "To": "collegue@entreprise.be", "Cc": "moi@ccl.be",
            "Subject": "Compte rendu de la visite"},
           "Bonjour, voici le compte rendu."),
       {"de": "jean.dupont@entreprise.be", "cc": ["moi@ccl.be"],
        "cle_fil": "compte rendu de la visite", "extrait_debut": "Bonjour, voici"},
       {"pour_moi": 0, "expediteur_humain": 1, "bulk": 0, "suspect": 0})

# 002 — newsletter avec List-Unsubscribe
ecrire(2, "newsletter-list-unsubscribe",
       eml({"From": "Promos <news@mg.boutique.com>", "To": "moi@ccl.be",
            "Subject": "Nos offres du mois",
            "List-Unsubscribe": "<mailto:stop@boutique.com>"},
           "Profitez de nos promotions."),
       {"de": "news@mg.boutique.com", "cle_fil": "nos offres du mois"},
       {"expediteur_humain": 0, "bulk": 1, "important": 0, "categorie": "newsletter"})

# 003 — alerte bancaire en masse : reste important
ecrire(3, "alerte-bancaire-masse",
       eml({"From": "Binance <no-reply@binance.com>", "To": "moi@ccl.be",
            "Subject": "Security alert on your account",
            "List-Unsubscribe": "<mailto:unsub@binance.com>"},
           "Unusual activity detected."),
       {"de": "no-reply@binance.com"},
       {"bulk": 1, "important": 1})

# 004 — domaine composé bancaire
ecrire(4, "domaine-bnpparibasfortis",
       eml({"From": "BNP <info@info.bnpparibasfortis.be>", "To": "moi@ccl.be",
            "Subject": "Mouvement sur votre compte"},
           "Un mouvement a été enregistré."),
       {"de": "info@info.bnpparibasfortis.be"},
       {"important": 1})

# 005 — urgent d'un humain (compte) vs 006 urgent d'un robot
ecrire(5, "urgent-humain",
       eml({"From": "Marie Martin <marie.martin@entreprise.be>", "To": "moi@ccl.be",
            "Subject": "URGENT : réponse attendue"},
           "Peux-tu me répondre vite ?"),
       {"de": "marie.martin@entreprise.be"},
       {"expediteur_humain": 1, "bulk": 0, "urgence": "haute"})

ecrire(6, "urgent-robot",
       eml({"From": "Alertes <no-reply@mg.marque.com>", "To": "moi@ccl.be",
            "Subject": "URGENT : dernière relance",
            "List-Unsubscribe": "<mailto:x@marque.com>"},
           "Offre qui expire."),
       {"de": "no-reply@mg.marque.com"},
       {"expediteur_humain": 0, "bulk": 1, "urgence": "basse"})

# 007 — hameçonnage (Reply-To ≠ From + mot de passe)
ecrire(7, "hameconnage-replyto",
       eml({"From": "Support <support@mabanque.be>", "To": "moi@ccl.be",
            "Reply-To": "pirate@autre-domaine.ru",
            "Subject": "Vérification de votre mot de passe"},
           "Cliquez pour vérifier votre mot de passe."),
       {"de": "support@mabanque.be"},
       {"suspect": 1})

# 008 — fil Re: Re: Tr: (clé de fil propre, préfixes retirés)
ecrire(8, "fil-re-tr",
       eml({"From": "Paul <paul.nelis@entreprise.be>", "To": "moi@ccl.be",
            "Subject": "Re: Re: Tr: Réunion de coordination Herstal"},
           "On confirme la réunion."),
       {"de": "paul.nelis@entreprise.be",
        "cle_fil": "reunion de coordination herstal"},
       {"categorie": "rendez_vous", "expediteur_humain": 1})

# 009 — booking.com ne doit PAS être important
ecrire(9, "booking-pas-important",
       eml({"From": "Booking <news@mail.booking.com>", "To": "moi@ccl.be",
            "Subject": "Vos prochaines vacances",
            "List-Unsubscribe": "<mailto:x@booking.com>"},
           "Des idées de séjour."),
       {"de": "news@mail.booking.com"},
       {"important": 0, "bulk": 1})

# 010 — « Suivi de commande » → livraison (mot_le_plus_long)
ecrire(10, "suivi-commande-livraison",
       eml({"From": "Shop <info@boutique.be>", "To": "moi@ccl.be",
            "Subject": "Suivi de commande 1234"},
           "Votre colis est en route."),
       {"de": "info@boutique.be"},
       {"categorie": "livraison"})

# 011 — « setups » ne matche pas « ups »
ecrire(11, "setups-pas-ups",
       eml({"From": "Blog <info@tech.be>", "To": "moi@ccl.be",
            "Subject": "Nos meilleurs setups de bureau"},
           "Idées d'aménagement."),
       {"de": "info@tech.be"},
       {"categorie": "autre"})

# 012 — « aujourd'hui » (apostrophe) compte comme mot urgent (humain)
ecrire(12, "aujourdhui-apostrophe",
       eml({"From": "Chef <luc.henry@entreprise.be>", "To": "moi@ccl.be",
            "Subject": "Décision attendue aujourd'hui"},
           "Il faut trancher."),
       {"de": "luc.henry@entreprise.be"},
       {"expediteur_humain": 1, "urgence": "haute"})

# 013 — corps HTML seul → extrait non vide
ecrire(13, "corps-html-seul",
       eml({"From": "Info <contact@asbl.be>", "To": "moi@ccl.be",
            "Subject": "Invitation assemblée"},
           corps_html="<html><body><p>Bonjour,</p><p>Vous êtes invité.</p>"
                      "<script>x()</script></body></html>"),
       {"de": "contact@asbl.be", "extrait_debut": "Bonjour,"},
       {"categorie": "rendez_vous"})

print("Cas générés dans", CAS)

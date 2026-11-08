"use strict";
/*
 * archive.js — cœur de la relève (lecture seule).
 * Récupère un message, écrit son .eml (MIME brut) et son .json jumeau,
 * en évitant les doublons par Message-ID. N'écrit rien dans la boîte :
 * aucun déplacement, aucun marquage. Conforme à l'esprit "rien n'est supprimé".
 */

const COLLECTEUR = { nom: "iamail-collecteur-thunderbird", version: "0.1.0" };

const REGLAGES_DEFAUT = {
  dossierBase: "IAMail",   // sous le dossier Téléchargements de Thunderbird
  maxSujet: 60,
  archiver: true
};

async function lireReglages() {
  const r = await messenger.storage.local.get("reglages");
  return Object.assign({}, REGLAGES_DEFAUT, r.reglages || {});
}

async function lireEtat() {
  const r = await messenger.storage.local.get("etat");
  return Object.assign({ vus: {}, total: 0, dernier: null }, r.etat || {});
}

async function ecrireEtat(etat) {
  await messenger.storage.local.set({ etat });
}

// Écrit un contenu (string) comme fichier, via l'API de téléchargement.
// Le chemin est relatif au dossier Téléchargements ; les sous-dossiers sont créés.
async function ecrireFichier(cheminRelatif, contenu, typeMime) {
  const blob = new Blob([contenu], { type: typeMime || "application/octet-stream" });
  const url = URL.createObjectURL(blob);
  try {
    await messenger.downloads.download({
      url,
      filename: cheminRelatif,
      conflictAction: "uniquify",
      saveAs: false
    });
  } finally {
    // Laisse le temps au téléchargement de démarrer avant de révoquer l'URL.
    setTimeout(() => URL.revokeObjectURL(url), 15000);
  }
}

// Construit l'objet .json (métadonnées — l'index cherchable).
async function construireMetadonnees(entete, cheminEml) {
  let pieces = [];
  try {
    const liste = await messenger.messages.listAttachments(entete.id);
    pieces = (liste || []).map(p => ({
      nom: p.name,
      type_mime: p.contentType,
      taille: p.size,
      partie: p.partName
    }));
  } catch (e) {
    // Certains messages (brouillons, non téléchargés) n'exposent pas les pièces.
  }
  return {
    message_id: entete.headerMessageId || null,
    de_adresse: entete.author || null,
    de_nom: entete.author || null,
    destinataires: entete.recipients || [],
    copies: entete.ccList || [],
    date: entete.date ? new Date(entete.date).toISOString() : null,
    sujet: entete.subject || "",
    dossier: entete.folder ? entete.folder.path : null,
    fichier_eml: cheminEml,
    pieces_jointes: pieces,
    collecte_at: new Date().toISOString(),
    collecteur: COLLECTEUR
  };
}

// Archive un message à partir de son en-tête (objet MessageHeader).
// Renvoie "archive", "doublon" ou "erreur".
async function archiverMessage(entete) {
  const reglages = await lireReglages();
  if (!reglages.archiver) return "desactive";

  const cle = entete.headerMessageId || ("sansid:" + entete.id);
  const etat = await lireEtat();
  if (etat.vus[cle]) return "doublon";

  try {
    const brut = await messenger.messages.getRaw(entete.id); // MIME brut (.eml)
    const noms = construireNoms(entete, reglages.dossierBase, reglages.maxSujet);
    const meta = await construireMetadonnees(entete, noms.cheminEml);

    await ecrireFichier(noms.cheminEml, brut, "message/rfc822");
    await ecrireFichier(noms.cheminJson, JSON.stringify(meta, null, 2), "application/json");

    etat.vus[cle] = true;
    etat.total = (etat.total || 0) + 1;
    etat.dernier = { sujet: entete.subject || "", at: new Date().toISOString() };
    await ecrireEtat(etat);
    return "archive";
  } catch (e) {
    console.error("[IAMail] échec archivage", cle, e);
    return "erreur";
  }
}

// Relève tous les messages de la boîte de réception de chaque compte (premier test).
async function releverBoitesReception(limite) {
  const comptes = await messenger.accounts.list();
  let archives = 0, doublons = 0, erreurs = 0, examines = 0;
  const max = limite || 200;

  for (const compte of comptes) {
    const inbox = (compte.folders || []).find(f => f.type === "inbox");
    if (!inbox) continue;
    let page = await messenger.messages.list(inbox);
    let continuer = true;
    while (continuer) {
      for (const entete of page.messages) {
        if (examines >= max) { continuer = false; break; }
        examines++;
        const r = await archiverMessage(entete);
        if (r === "archive") archives++;
        else if (r === "doublon") doublons++;
        else if (r === "erreur") erreurs++;
      }
      if (continuer && page.id) page = await messenger.messages.continueList(page.id);
      else continuer = false;
      if (!page || !page.messages || page.messages.length === 0) continuer = false;
    }
  }
  return { examines, archives, doublons, erreurs };
}

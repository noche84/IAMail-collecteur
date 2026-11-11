"use strict";
// IAMail — bouton cockpit + synchro bidirectionnelle (retour-mail).
//
// Principe : le compagnon Python reste en LECTURE SEULE sur la boîte. C'est CETTE
// extension qui écrit dans Thunderbird, via l'API officielle (sûr, zéro risque de
// corruption). L'étiquette « Traité IAMail » est la vérité partagée.
//  - Cockpit « Traité »  -> on pose l'étiquette + on marque lu dans Thunderbird.
//  - Étiquette posée/retirée à la main dans Thunderbird -> on prévient le cockpit.
//  - LIRE un mail ne le marque jamais « traité » (l'état lu ne sert qu'à l'affichage).

const COCKPIT = "http://127.0.0.1:8740/";
const API = COCKPIT.replace(/\/$/, "");
const INTERVALLE_MS = 15000;          // synchro toutes les 15 secondes
const CLE_DEFAUT = "iamail_traite";
const sansChevrons = s => (s || "").replace(/^<|>$/g, "");

// --- Bouton : ouvre le cockpit dans le navigateur par défaut ---
messenger.action.onClicked.addListener(async () => {
  try { await messenger.windows.openDefaultBrowser(COCKPIT); }
  catch (e) { try { await messenger.tabs.create({ url: COCKPIT }); } catch (e2) {} }
});

// --- Compatibilité de l'API « tags » selon la version de Thunderbird ---
async function listerTags() {
  if (messenger.messages.tags && messenger.messages.tags.list) return messenger.messages.tags.list();
  if (messenger.messages.listTags) return messenger.messages.listTags();
  return [];
}
async function creerTag(cle, nom, couleur) {
  if (messenger.messages.tags && messenger.messages.tags.create) return messenger.messages.tags.create(cle, nom, couleur);
  if (messenger.messages.createTag) return messenger.messages.createTag(cle, nom, couleur);
}
async function assurerEtiquette(et) {
  try {
    const tags = await listerTags();
    if (!tags.some(t => t.key === et.cle)) await creerTag(et.cle, et.nom, et.couleur);
  } catch (e) { /* l'étiquette existe peut-être déjà */ }
}

// Retrouve le(s) message(s) Thunderbird correspondant à un Message-ID.
async function messagesParId(mid) {
  try {
    const r = await messenger.messages.query({ headerMessageId: sansChevrons(mid) });
    return (r && r.messages) || [];
  } catch (e) { return []; }
}

async function poserEtiquette(mid, cle) {
  let ok = false;
  for (const m of await messagesParId(mid)) {
    const tags = Array.from(new Set([...(m.tags || []), cle]));
    try { await messenger.messages.update(m.id, { read: true, tags }); ok = true; } catch (e) {}
  }
  return ok;
}
async function retirerEtiquette(mid, cle) {
  let ok = false;
  for (const m of await messagesParId(mid)) {
    const tags = (m.tags || []).filter(t => t !== cle);
    try { await messenger.messages.update(m.id, { tags }); ok = true; } catch (e) {}
  }
  return ok;
}

// --- Synchro cockpit -> Thunderbird ---
let enCours = false;
async function synchro() {
  if (enCours) return;
  enCours = true;
  try {
    const r = await fetch(API + "/api/sync/pour-client").then(x => x.json());
    const et = r.etiquette || { cle: CLE_DEFAUT, nom: "Traité IAMail", couleur: "#12b76a" };
    await assurerEtiquette(et);
    const marques = [], demarques = [];
    for (const mid of (r.a_marquer || []))   if (await poserEtiquette(mid, et.cle))   marques.push(mid);
    for (const mid of (r.a_demarquer || [])) if (await retirerEtiquette(mid, et.cle)) demarques.push(mid);
    if (marques.length || demarques.length) {
      await fetch(API + "/api/sync/confirme", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ marques, demarques }),
      });
    }
  } catch (e) { /* compagnon éteint : on retentera au prochain tour */ }
  finally { enCours = false; }
}
setInterval(synchro, INTERVALLE_MS);
synchro();

async function prevenirCockpit(corps) {
  try {
    await fetch(API + "/api/sync/depuis-client", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(corps),
    });
  } catch (e) {}
}

function idsDepuisListe(liste) {
  // liste de MessageHeader (ou {messages:[...]}) -> Message-IDs non vides
  const arr = Array.isArray(liste) ? liste : (liste && liste.messages) || [];
  return arr.map(m => m && m.headerMessageId).filter(Boolean);
}

// --- Sens inverse 1 : l'utilisateur pose/retire l'étiquette dans Thunderbird ---
messenger.messages.onUpdated.addListener(async (msg, changed) => {
  if (!changed || !("tags" in changed)) return;        // on ne réagit qu'aux étiquettes
  const mid = msg && msg.headerMessageId;
  if (!mid) return;
  const a = (changed.tags || []).includes(CLE_DEFAUT);
  await prevenirCockpit(a ? { traites: [mid], non_traites: [] }
                          : { traites: [], non_traites: [mid] });
});

// --- Sens inverse 2 : l'utilisateur SUPPRIME ou DÉPLACE un mail hors de sa boîte ---
// Il l'a traité à sa manière -> il doit sortir du cockpit. (Lire ne traite jamais.)
// L'archive du compagnon n'est pas touchée : le mail reste dans le coffre-fort.
messenger.messages.onDeleted.addListener(async (liste) => {
  const ids = idsDepuisListe(liste);
  if (ids.length) await prevenirCockpit({ traites: ids, non_traites: [] });
});
messenger.messages.onMoved.addListener(async (_origine, deplaces) => {
  const ids = idsDepuisListe(deplaces);
  if (ids.length) await prevenirCockpit({ traites: ids, non_traites: [] });
});

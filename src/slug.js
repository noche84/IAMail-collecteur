"use strict";
/*
 * slug.js — nommage des fichiers d'archive.
 * Chargé comme script classique : les fonctions sont globales (pas de module).
 * Produit un nom triable et sans collision, conforme à l'ADR 0041 :
 *   AAAA-MM-JJ_HHMMSS__<expediteur>__<sujet-tronque>__<hash6>
 */

// Retire les accents (NFD puis suppression des diacritiques).
function sansAccents(s) {
  return (s || "").normalize("NFD").replace(/[̀-ͯ]/g, "");
}

// Transforme un texte en fragment de nom de fichier sûr (Windows inclus).
// Caractères interdits \ / : * ? " < > | et espaces -> tirets ; pas de point final.
function slug(texte, maxLen) {
  let s = sansAccents(texte || "").toLowerCase();
  s = s.replace(/[\\/:*?"<>|]+/g, "-");   // interdits Windows
  s = s.replace(/[\s_]+/g, "-");           // espaces et soulignés
  s = s.replace(/[^a-z0-9.\-]+/g, "-");    // tout le reste
  s = s.replace(/-+/g, "-");               // tirets multiples
  s = s.replace(/^[-.]+|[-.]+$/g, "");     // pas de tiret/point en bord
  if (maxLen && s.length > maxLen) s = s.slice(0, maxLen).replace(/-+$/g, "");
  return s || "sans-objet";
}

// Expéditeur : on garde l'adresse, @ -> -at- (lisible et sans caractère interdit).
function slugExpediteur(adresse) {
  const m = (adresse || "").match(/<([^>]+)>/);
  const addr = (m ? m[1] : adresse || "").trim();
  return slug(addr.replace(/@/g, "-at-"), 50) || "expediteur-inconnu";
}

// Hash court et déterministe (djb2 -> base36, 6 caractères) à partir du Message-ID.
function hash6(cle) {
  let h = 5381;
  const s = cle || "";
  for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) >>> 0;
  return h.toString(36).padStart(6, "0").slice(-6);
}

// Deux chiffres.
function p2(n) { return String(n).padStart(2, "0"); }

// Préfixe date-heure local AAAA-MM-JJ_HHMMSS à partir d'un objet Date.
function horodatage(date) {
  const d = (date instanceof Date && !isNaN(date)) ? date : new Date();
  return (
    d.getFullYear() + "-" + p2(d.getMonth() + 1) + "-" + p2(d.getDate()) +
    "_" + p2(d.getHours()) + p2(d.getMinutes()) + p2(d.getSeconds())
  );
}

// Construit le chemin relatif (sous le dossier de base) : <base>/AAAA/MM/<nom>
// Renvoie { baseName, cheminEml, cheminJson }.
function construireNoms(entete, dossierBase, maxSujet) {
  const d = (entete.date instanceof Date && !isNaN(entete.date)) ? entete.date : new Date();
  const base =
    horodatage(d) +
    "__" + slugExpediteur(entete.author) +
    "__" + slug(entete.subject, maxSujet || 60) +
    "__" + hash6(entete.headerMessageId || entete.subject || String(d.getTime()));
  const dossier = (dossierBase || "IAMail") + "/" + d.getFullYear() + "/" + p2(d.getMonth() + 1);
  return {
    baseName: base,
    cheminEml: dossier + "/" + base + ".eml",
    cheminJson: dossier + "/" + base + ".json"
  };
}

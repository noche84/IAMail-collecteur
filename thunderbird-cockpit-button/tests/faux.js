"use strict";
// Faux GesEnt (contrat v1, schémas validés) et faux Thunderbird, partagés par les tests.
const fs = require("fs");
const path = require("path");
const zlib = require("zlib");
const crypto = require("crypto");
const MOTEUR = path.join(__dirname, "..", "..", "moteur");
const { valider } = require("./schema.js");
const lireJson = p => JSON.parse(fs.readFileSync(p, "utf8"));
const S = { lot: lireJson(path.join(__dirname, "contrat/lot.schema.json")), rep: lireJson(path.join(__dirname, "contrat/reponse-lot.schema.json")),
  sync: lireJson(path.join(__dirname, "contrat/sync.schema.json")), err: lireJson(path.join(__dirname, "contrat/erreur.schema.json")) };
const def = nom => ({ $ref: "#/$defs/" + nom, $defs: S.sync.$defs });
const sha = b => crypto.createHash("sha256").update(b).digest("hex");

// ------------------------------------------------------------------------------------------
// Faux GesEnt
// ------------------------------------------------------------------------------------------
function fauxGesEnt() {
  const fichiers = { "regles.json": fs.readFileSync(path.join(MOTEUR, "regles.json"), "utf8"),
    "taxonomie.json": fs.readFileSync(path.join(MOTEUR, "taxonomie.json"), "utf8") };
  const version = lireJson(path.join(MOTEUR, "manifest.json")).version;
  const g = {
    version, fichiers, jeton: "iae_1_secret", codes: { "K7Q2M9XD": true }, revoque: false,
    boites: [{ boite_id: 7, adresse: "info@entreprise.be", libelle: "Accueil", type: "partagee", relie: true, plus_ancien_recu: null },
             { boite_id: 9, adresse: "perso@entreprise.be", libelle: "Perso", type: "personnelle", relie: false, plus_ancien_recu: null }],
    etiquette: { cle: "traite_iamail", nom: "Traité IAMail", couleur: "#2E7D32" },
    lots: new Map(), messages: new Map(), envois: new Map(), introuvables: [], sources: new Map(), gestes: [], etats: [], confirmes: [],
    pourClient: { a_marquer: [], a_demarquer: [] }, pannes: [], journal: [], erreursSchema: [],
  };
  const repondre = (status, corps, entetes) => new Response(status === 204 || status === 304 ? null : (typeof corps === "string" ? corps : JSON.stringify(corps)),
    { status, headers: Object.assign({ "Content-Type": "application/json" }, entetes || {}) });
  const erreur = (status, code, message, details) => {
    const c = { erreur: { code, message, details: details || {} } };
    const e = valider(S.err, c); if (e.length) g.erreursSchema.push(...e);
    return repondre(status, c);
  };
  const schema = (s, v, quoi) => { const e = valider(s, v); if (e.length) g.erreursSchema.push(quoi + " : " + e.join(" ; ")); return e; };

  g.fetch = async (url, init) => {
    const u = new URL(url);
    const chemin = u.pathname.replace(/^\/api\/iaemail\/v1/, "");
    const methode = init.method || "GET";
    const h = init.headers || {};
    g.journal.push(methode + " " + chemin);
    if (!/^IAMail-thunderbird\/\d+\.\d+\.\d+$/.test(h["User-Agent"] || "")) g.erreursSchema.push("User-Agent absent ou mal formé : " + h["User-Agent"]);
    const panne = g.pannes.length && (g.pannes[0].quand === undefined || g.pannes[0].quand === methode + " " + chemin) ? g.pannes.shift() : null;
    if (panne && panne.reseau) throw new TypeError("fetch failed");
    if (panne) {
      const code = panne.code || (panne.status === 429 ? "trop_de_requetes" : "indisponible");
      return repondre(panne.status, { erreur: { code, message: panne.message || code, details: panne.details || {} } },
        panne.retryAfter ? { "Retry-After": String(panne.retryAfter) } : {});
    }

    if (chemin === "/appairage") {
      const c = JSON.parse(init.body);
      const code = String(c.code).replace(/-/g, "");
      if (!g.codes[code]) return erreur(404, "code_inconnu", "Code inconnu.");
      delete g.codes[code];
      if (!c.collecteur || c.collecteur.type !== "thunderbird" || !Array.isArray(c.comptes_locaux)) return erreur(400, "requete_invalide", "corps");
      g.appairage = c;
      return repondre(201, { collecteur_id: 42, jeton: g.jeton, protocole: 1, boites: g.boites.map(({ relie, plus_ancien_recu, ...b }) => b), moteur: { version: g.version }, etiquette: g.etiquette });
    }
    if (h["Authorization"] !== "Bearer " + g.jeton) return erreur(401, "jeton_invalide", "Ce jeton n'est pas reconnu.");
    if (g.revoque) return erreur(401, "jeton_revoque", "Ce poste a été délié.");

    if (chemin === "/moi") return repondre(200, { collecteur_id: 42, boites: g.boites, moteur: { version: g.version }, etiquette: g.etiquette });
    if (chemin === "/moteur") {
      const man = { version: g.version, fichiers: Object.fromEntries(Object.entries(g.fichiers).map(([k, v]) => [k, sha(Buffer.from(v, "utf8"))])) };
      if (g.manifesteFaux) man.fichiers["regles.json"] = "0".repeat(64);
      const etag = '"' + sha(JSON.stringify(man)) + '"';
      if (h["If-None-Match"] === etag) return repondre(304, null, { ETag: etag });
      return repondre(200, man, { ETag: etag });
    }
    if (chemin.startsWith("/moteur/fichiers/")) {
      const nom = decodeURIComponent(chemin.slice(17));
      if (!(nom in g.fichiers)) return erreur(404, "introuvable", "fichier");
      return repondre(200, g.fichiers[nom], { "Content-Type": "application/json; charset=utf-8", ETag: '"' + sha(Buffer.from(g.fichiers[nom])) + '"' });
    }
    if (chemin === "/lots") {
      const lot = JSON.parse(init.body);
      const brut = Buffer.byteLength(init.body);
      if (brut > 25 * 1024 * 1024 || g.forcer413) { g.forcer413 = Math.max(0, (g.forcer413 || 0) - 1); return erreur(413, "trop_volumineux", "Lot trop gros."); }
      if (schema(S.lot, lot, "lot").length) return erreur(400, "requete_invalide", "schéma");
      if ((lot.messages || []).length > 100) g.erreursSchema.push("lot de plus de 100 messages");
      if (lot.moteur_version.split(".")[0] !== g.version.split(".")[0]) return erreur(409, "moteur_incompatible", "majeure", { version_serveur: g.version });
      if (g.lots.has(lot.lot_id)) return repondre(200, Object.assign({}, g.lots.get(lot.lot_id).reponse, { deja_recu: true }));
      const rep = { lot_id: lot.lot_id, deja_recu: false, messages: { nouveaux: 0, deja_connus: 0, a_televerser: 0, envoyes: 0 }, gestes: { appliques: 0, en_attente: 0 }, rejets: [], moteur_version_serveur: g.version };
      (lot.messages || []).forEach((m, i) => {
        if (!g.boites.some(b => b.boite_id === m.boite_id)) { rep.rejets.push({ type: "message", index: i, code: "hors_portee", message: "boîte" }); return; }
        if (g.rejeterId && m.message_id === g.rejeterId) { rep.rejets.push({ type: "message", index: i, code: "source_corrompue", message: "test" }); return; }
        if (!m.source.a_televerser) {
          const b = zlib.gunzipSync(Buffer.from(m.source.donnees, "base64"));
          if (sha(b) !== m.source.sha256 || b.length !== m.source.taille) { rep.rejets.push({ type: "message", index: i, code: "source_corrompue", message: "sha" }); return; }
        }
        const cle = m.boite_id + "|" + m.message_id;
        if (m.sens === "envoye") { rep.messages.envoyes++; g.envois.set(cle, m); return; }   // index des envois, rien d'archivé
        if (g.messages.has(cle)) rep.messages.deja_connus++;
        else { rep.messages.nouveaux++; g.messages.set(cle, Object.assign({ mode: lot.mode || "arrivee" }, m)); }
        if (m.source.a_televerser) { rep.messages.a_televerser++; if (!g.sources.has(cle)) g.sources.set(cle, { sha256: m.source.sha256, recu: null }); }
        const b = g.boites.find(x => x.boite_id === m.boite_id);
        if (!b.plus_ancien_recu || Date.parse(m.recu_le) < Date.parse(b.plus_ancien_recu)) b.plus_ancien_recu = m.recu_le;
      });
      for (const ge of lot.gestes || []) { g.gestes.push(ge); rep.gestes.appliques++; }
      if (schema(S.rep, rep, "réponse lot").length) return erreur(500, "indisponible", "réponse");
      g.lots.set(lot.lot_id, { lot, reponse: rep });
      return repondre(200, rep);
    }
    if (chemin === "/sources") {
      const cle = u.searchParams.get("boite_id") + "|" + u.searchParams.get("message_id");
      const s = g.sources.get(cle);
      const corps = Buffer.from(init.body);
      if (!["message/rfc822", "application/vnd.ms-outlook"].includes(h["Content-Type"])) g.erreursSchema.push("PUT /sources : Content-Type " + h["Content-Type"]);
      if (!s) return erreur(404, "introuvable", "Aucun message annoncé.");
      if (sha(corps) !== String(h["Content-SHA256"]).toLowerCase()) return erreur(422, "source_corrompue", "empreinte");
      if (s.recu) return sha(corps) === s.sha256 ? repondre(200, { statut: "deja_recu" }) : erreur(409, "source_differente", "autre");
      if (sha(corps) !== s.sha256) return erreur(422, "source_corrompue", "pas celui annoncé");
      s.recu = corps.length; g.putSources = (g.putSources || 0) + 1;
      return repondre(201, { statut: "recu" });
    }
    if (chemin === "/sync/pour-client") {
      const c = { etiquette: g.etiquette, a_marquer: g.pourClient.a_marquer.slice(0, 500), a_demarquer: g.pourClient.a_demarquer.slice(0, 500) };
      schema(def("pour_client"), c, "pour-client");
      return repondre(200, c);
    }
    if (chemin === "/sync/confirme") {
      const c = JSON.parse(init.body);
      schema(def("confirme"), c, "confirme");
      g.confirmes.push(c);
      const k = r => r.boite_id + "|" + r.message_id;
      g.introuvables.push(...(c.introuvables || []));
      const m = new Set((c.marques || []).map(k).concat((c.introuvables || []).map(k))), d = new Set((c.demarques || []).map(k).concat((c.introuvables || []).map(k)));
      g.pourClient.a_marquer = g.pourClient.a_marquer.filter(r => !m.has(k(r)));
      g.pourClient.a_demarquer = g.pourClient.a_demarquer.filter(r => !d.has(k(r)));
      return repondre(204, null);
    }
    if (chemin === "/sync/depuis-client") {
      const c = JSON.parse(init.body);
      schema(def("depuis_client"), c, "depuis-client");
      g.etats.push(c);
      return repondre(200, { appliques: (c.traites || []).length + (c.non_traites || []).length + (c.ranges || []).length, ignores: [] });
    }
    return erreur(404, "introuvable", chemin);
  };
  return g;
}

// ------------------------------------------------------------------------------------------
// Faux Thunderbird
// ------------------------------------------------------------------------------------------
function fauxThunderbird() {
  const t = {
    comptesListe: [{ id: "acc1", identites: [{ email: "info@entreprise.be", nom: "Accueil" }, { email: "moi@ccl.be" }] },
                   { id: "acc2", identites: [{ email: "perso@entreprise.be" }] },
                   { id: "acc3", identites: [{ email: "prive@gmail.com" }] }],
    messages: new Map(), etiquettes: [], suivant: 1,
  };
  t.ajouter = (compte, brut, date, extra) => {
    const id = t.suivant++;
    const mid = (/^Message-ID:\s*<?([^>\r\n]+)>?/im.exec(brut.toString("latin1")) || [])[1];
    const e = Object.assign({ id, compte, date, dossier: "INBOX", lu: false, tags: [], messageIdTb: mid ? mid.trim() : "tb-" + id + "@thunderbird" }, extra || {});
    t.messages.set(id, { entete: e, brut: new Uint8Array(brut) });
    return e;
  };
  t.adaptateur = {
    comptes: async () => t.comptesListe,
    lireBrut: async id => (t.messages.get(id) || {}).brut || null,
    chercherParMessageId: async (mid, comptes) => [...t.messages.values()].map(x => x.entete).filter(e => e.messageIdTb === mid && comptes.includes(e.compte)),
    poserEtiquette: async (id, cle, poser) => {
      const m = t.messages.get(id); if (!m) return false;
      m.entete.tags = poser ? [...new Set([...m.entete.tags, cle])] : m.entete.tags.filter(x => x !== cle);
      if (poser) m.entete.lu = true;
      return true;
    },
    assurerEtiquette: async et => { if (!t.etiquettes.some(x => x.cle === et.cle)) t.etiquettes.push(et); },
    messagesEntre: async (comptes, debut, fin) => [...t.messages.values()].map(x => x.entete)
      .filter(e => comptes.includes(e.compte) && e.date >= debut && e.date <= fin),
    machine: async () => "PC-ACCUEIL-02", utilisateurOs: async () => "jdupont",
  };
  return t;
}

function mail({ de = "Jean Dupont <jean.dupont@client.be>", a = "info@entreprise.be", objet = "Bonjour", corps = "Texte.", id, date = "Fri, 20 Nov 2026 08:13:40 +0100", extra = "" } = {}) {
  return Buffer.from((id ? `Message-ID: <${id}>\r\n` : "") + `From: ${de}\r\nTo: ${a}\r\nSubject: ${objet}\r\nDate: ${date}\r\n${extra}MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n${corps}\r\n`, "utf8");
}

module.exports = { fauxGesEnt, fauxThunderbird, mail, sha };

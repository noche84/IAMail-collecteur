"use strict";
// IAMail — cockpit, synchro « Traité » et client GesEnt.
//
// Deux modes, boîte par boîte :
//  - AUTONOME (aucun GesEnt, ou boîte non reliée) : le compagnon Python local fait foi. Le
//    compagnon reste en LECTURE SEULE sur la boîte ; c'est CETTE extension qui écrit dans
//    Thunderbird (étiquette « Traité IAMail » + lu), dans les deux sens, via l'API officielle.
//  - RELIÉ À GESENT (boîte `relie` dans GET /moi) : GesEnt fait foi. Chaque mail est envoyé à
//    GesEnt dès son arrivée (puis l'existant, en rattrapage), l'étiquette est synchronisée avec
//    GesEnt seulement, et le cockpit local passe en lecture seule (il renvoie vers GesEnt).
// LIRE un mail ne le marque jamais « traité ».

const COCKPIT = "http://127.0.0.1:8740/";
const API = COCKPIT.replace(/\/$/, "");
const INTERVALLE_MS = 15000;          // synchro toutes les 15 secondes (tant que l'extension est éveillée)
const CLE_DEFAUT = "iamail_traite";
const VERSION = messenger.runtime.getManifest().version;
const sansChevrons = s => (s || "").replace(/^<|>$/g, "");

// --- Adaptateur Thunderbird pour le client GesEnt (gesent.js) ----------------------------------
const SPECIAUX_EXCLUS = ["sent", "drafts", "trash", "junk", "templates", "outbox", "archives"];
function usagesDossier(f) {
  if (!f) return [];
  if (Array.isArray(f.specialUse)) return f.specialUse;
  return f.type ? [f.type] : [];
}
const dossierCollecte = f => !usagesDossier(f).some(u => SPECIAUX_EXCLUS.includes(u));
const estCorbeille = f => usagesDossier(f).includes("trash");
const estEnvoyes = f => usagesDossier(f).includes("sent");
const estBoiteReception = f => usagesDossier(f).includes("inbox");

function entete(m) {
  return { id: m.id, compte: m.folder && m.folder.accountId, date: m.date ? new Date(m.date) : null,
    dossier: (m.folder && (m.folder.path || m.folder.name)) || "INBOX", lu: !!m.read, tags: m.tags || [],
    messageIdTb: m.headerMessageId || "", envoye: estEnvoyes(m.folder) };
}
async function* pages(liste) {
  let l = liste;
  while (l) {
    for (const m of l.messages || []) yield m;
    l = l.id ? await messenger.messages.continueList(l.id) : null;
  }
}
async function tous(liste) { const out = []; for await (const m of pages(liste)) out.push(m); return out; }

let cacheComptes = { t: 0, v: [] };
const adaptateur = {
  async comptes() {
    if (Date.now() - cacheComptes.t < 60000) return cacheComptes.v;
    const comptes = await messenger.accounts.list(false);
    cacheComptes = { t: Date.now(), v: comptes.map(a => ({ id: a.id, nom: a.name,
      identites: (a.identities || []).filter(i => i.email).map(i => ({ email: i.email, nom: i.name || null })) })) };
    return cacheComptes.v;
  },
  async lireBrut(id) {
    try {
      const r = await messenger.messages.getRaw(id, { data_format: "File" });
      if (typeof r === "string") return IAMailMime.binaireVersOctets(r);
      return new Uint8Array(await r.arrayBuffer());
    } catch (e) { return null; }
  },
  async chercherParMessageId(mid, comptes) {
    try {
      const r = await tous(await messenger.messages.query({ headerMessageId: sansChevrons(mid) }));
      return r.filter(m => m.folder && comptes.includes(m.folder.accountId)).map(entete);
    } catch (e) { return []; }
  },
  async poserEtiquette(id, cle, poser) {
    try {
      const m = await messenger.messages.get(id);
      const tags = poser ? Array.from(new Set([...(m.tags || []), cle])) : (m.tags || []).filter(t => t !== cle);
      await messenger.messages.update(id, poser ? { read: true, tags } : { tags });
      return true;
    } catch (e) { return false; }
  },
  async assurerEtiquette(et) { await assurerEtiquette(et); },
  async messagesEntre(comptes, debut, fin) {
    let liste;
    try { liste = await messenger.messages.query({ accountId: comptes, fromDate: debut, toDate: fin, messagesPerPage: 500 }); }
    catch (e) { liste = await messenger.messages.query({ fromDate: debut, toDate: fin }); }   // Thunderbird sans accountId
    // Dossiers collectés + Envoyés (envoyés à GesEnt avec sens « envoye », en-têtes seulement).
    return (await tous(liste)).filter(m => m.folder && comptes.includes(m.folder.accountId)
      && (dossierCollecte(m.folder) || estEnvoyes(m.folder))).map(entete);
  },
  async machine() { const r = await messenger.storage.local.get("poste"); return r.poste || undefined; },
  async utilisateurOs() { return undefined; },     // rien de personnel par défaut (CCT n° 81)
};

const client = new IAMailGesEnt.ClientGesEnt({
  fetch: (u, i) => fetch(u, i), tb: adaptateur, type: "thunderbird", version: VERSION,
  stockage: { get: k => messenger.storage.local.get(k), set: o => messenger.storage.local.set(o), remove: k => messenger.storage.local.remove(k) },
});
const pret = client.charger().then(async () => {
  if (client.etat.jeton) { await client.rafraichir(); await client.chargerMoteur(false); }
  await prevenirCompagnonRelie();
  client.pomper().catch(() => {});
});

// Boîte de ce compte reliée à GesEnt (GesEnt fait foi) ?
async function relieePourCompte(accountId) {
  if (!client.boitesReliees().length) return false;
  const b = await client.boitePourCompte(accountId);
  return !!(b && b.relie);
}

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

// Message(s) Thunderbird d'un Message-ID, hors boîtes reliées à GesEnt (le compagnon n'y agit plus).
async function messagesParIdAutonomes(mid) {
  try {
    const r = await tous(await messenger.messages.query({ headerMessageId: sansChevrons(mid) }));
    const out = [];
    for (const m of r) if (!(await relieePourCompte(m.folder && m.folder.accountId))) out.push(m);
    return out;
  } catch (e) { return []; }
}
async function poserEtiquette(mid, cle) {
  let ok = false;
  for (const m of await messagesParIdAutonomes(mid)) if (await adaptateur.poserEtiquette(m.id, cle, true)) ok = true;
  return ok;
}
async function retirerEtiquette(mid, cle) {
  let ok = false;
  for (const m of await messagesParIdAutonomes(mid)) if (await adaptateur.poserEtiquette(m.id, cle, false)) ok = true;
  return ok;
}

// --- Synchro cockpit local -> Thunderbird (boîtes non reliées) ---
let enCours = false;
async function synchroLocale() {
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

async function prevenirCockpit(corps) {
  try {
    await fetch(API + "/api/sync/depuis-client", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(corps),
    });
  } catch (e) {}
}

// Le compagnon passe son cockpit en lecture seule tant qu'une boîte est reliée à GesEnt.
let dernierAvis = null;
async function prevenirCompagnonRelie() {
  const reliees = client.boitesReliees();
  const corps = { relie: reliees.length > 0, gesent: reliees.length ? client.etat.instance : null,
    boites: reliees.map(b => b.adresse) };
  const s = JSON.stringify(corps);
  if (s === dernierAvis) return;
  try {
    const r = await fetch(API + "/api/relie", { method: "POST", headers: { "Content-Type": "application/json" }, body: s });
    if (r.ok) dernierAvis = s;
  } catch (e) { /* compagnon absent : rien à prévenir */ }
}

// --- Cycle : cockpit local, GesEnt (file, rattrapage, synchro), avis au compagnon ---
let tour = 0;
async function cycle() {
  await pret;
  tour++;
  await synchroLocale();
  if (client.estRelie()) {
    if (tour % 20 === 1) { await client.rafraichir(); await client.chargerMoteur(false); dernierAvis = null; }   // ≈ 5 min (le compagnon a pu redémarrer)
    if (tour % 4 === 1) await client.balayer();                                            // ≈ 1 min
    await client.pomper();
    await client.synchroTraite();
  }
  await prevenirCompagnonRelie();
}
setInterval(() => { cycle().catch(() => {}); }, INTERVALLE_MS);
// L'arrière-plan peut être mis en veille : une alarme le réveille, la file est persistante.
try {
  messenger.alarms.create("iamail-cycle", { periodInMinutes: 1 });
  messenger.alarms.onAlarm.addListener(a => { if (a.name === "iamail-cycle") cycle().catch(() => {}); });
} catch (e) {}
cycle().catch(() => {});

// --- Arrivée de nouveaux messages : envoyés à GesEnt (boîtes de la portée) ---
messenger.messages.onNewMailReceived.addListener(async (dossier, liste) => {
  await pret;
  if (!client.estRelie() || !(dossierCollecte(dossier) || estEnvoyes(dossier))) return;
  for await (const m of pages(liste)) {
    try { await client.surArrivee(entete(m)); } catch (e) { client.noter("erreur", "arrivée : " + e); }
  }
});

function idsDepuisListe(liste) {
  const arr = Array.isArray(liste) ? liste : (liste && liste.messages) || [];
  return arr.filter(Boolean);
}

// --- Sens inverse 1 : l'utilisateur pose/retire une étiquette dans Thunderbird ---
messenger.messages.onUpdated.addListener(async (msg, changed) => {
  if (!changed || !("tags" in changed)) return;        // on ne réagit qu'aux étiquettes
  const mid = msg && msg.headerMessageId;
  if (!mid) return;
  await pret;
  const e = Object.assign(entete(msg), { tags: changed.tags || [] });
  if (e.envoye) return;                                              // un envoi n'a pas d'état « Traité »
  const reliee = await relieePourCompte(e.compte);
  if (client.estRelie()) await client.surEtiquette(e, CLE_DEFAUT);   // GesEnt (état) ou journal (geste)
  if (reliee) return;                                                // GesEnt fait foi : pas le cockpit local
  const a = (changed.tags || []).includes(CLE_DEFAUT);
  await prevenirCockpit(a ? { traites: [mid], non_traites: [] } : { traites: [], non_traites: [mid] });
});

// --- Sens inverse 2 : l'utilisateur SUPPRIME ou DÉPLACE un mail hors de sa boîte ---
// Il l'a traité à sa manière -> il sort du cockpit (ou GesEnt le range). Lire ne traite jamais.
// L'archive (compagnon ou GesEnt) n'est pas touchée : le mail reste dans le coffre-fort.
async function surRange(m, options) {
  await pret;
  const e = entete(m);
  if (e.envoye) return;                                              // ranger un envoi n'est pas traiter
  if (client.estRelie()) await client.surRangement(e, options);
  if (!(await relieePourCompte(e.compte)) && e.messageIdTb) await prevenirCockpit({ traites: [e.messageIdTb], non_traites: [] });
}
messenger.messages.onDeleted.addListener(async (liste) => {
  for (const m of idsDepuisListe(liste)) await surRange(m, { supprime: true });
});
messenger.messages.onMoved.addListener(async (origine, deplaces) => {
  const avant = idsDepuisListe(origine), apres = idsDepuisListe(deplaces);
  for (let i = 0; i < apres.length; i++) {
    const o = avant[i] || apres[i], d = apres[i];
    // Un filtre qui range un mail tout juste arrivé n'est pas un geste de l'utilisateur.
    if (o && !o.read && o.date && Date.now() - new Date(o.date).getTime() < 120000) continue;
    if (o && o.folder && !dossierCollecte(o.folder)) continue;          // sortie de la corbeille, des envoyés…
    await surRange(d, estCorbeille(d.folder) ? { supprime: true } : { vers: (d.folder && (d.folder.path || d.folder.name)) || "" });
  }
});

// --- Page d'options : appairage et état ---
messenger.runtime.onMessage.addListener((msg) => {
  return (async () => {
    await pret;
    try {
      if (msg.cmd === "etat") {
        const e = client.etat;
        return { ok: true, version: VERSION, statut: e.statut, instance: e.instance, boites: e.boites,
          moteur: e.moteur && e.moteur.version, file: e.file.length + (e.lotEnCours ? e.lotEnCours.items.length : 0),
          attente: e.attenteJusqua > Date.now() ? new Date(e.attenteJusqua).toLocaleTimeString() : null,
          rattrapage: e.rattrapage, journal: e.journal.slice(0, 25), rejets: e.rejets.slice(0, 15), dernierContact: e.dernierContact,
          poste: (await messenger.storage.local.get("poste")).poste || "" };
      }
      if (msg.cmd === "appairer") {
        if (msg.poste != null) await messenger.storage.local.set({ poste: String(msg.poste).slice(0, 100) });
        const comptes = await adaptateur.comptes();
        const vus = new Set(), locaux = [];
        for (const c of comptes) for (const i of c.identites) {
          const a = i.email.toLowerCase();
          if (!vus.has(a)) { vus.add(a); locaux.push({ adresse: a, nom: i.nom || c.nom || null }); }
        }
        await client.appairer(msg.instance, msg.code, locaux);
        if (client.etat.etiquette) await assurerEtiquette(client.etat.etiquette);
        dernierAvis = null; await prevenirCompagnonRelie();
        client.pomper().catch(() => {});
        return { ok: true };
      }
      if (msg.cmd === "delier") { await client.delier(); dernierAvis = null; await prevenirCompagnonRelie(); return { ok: true }; }
      if (msg.cmd === "envoyer") {
        client.etat.attenteJusqua = 0;
        await client.rafraichir(); await client.pomper(); await client.synchroTraite();
        return { ok: true };
      }
      return { ok: false, erreur: "commande inconnue" };
    } catch (e) { return { ok: false, erreur: String(e && e.message || e) }; }
  })();
});

"use strict";
// L'extension entière (background.js) dans un faux Thunderbird (API `messenger` simulée),
// face au faux GesEnt et à un faux compagnon local : appairage par la page d'options, arrivée
// d'un mail, gestes de l'utilisateur, partage des rôles cockpit local / GesEnt.
//   node thunderbird-cockpit-button/tests/extension.test.js
const path = require("path");
const EXT = path.join(__dirname, "..");
const { fauxGesEnt, fauxThunderbird, mail } = require("./faux.js");

let ok = 0, ko = 0;
function verif(cond, texte) { if (cond) { ok++; console.log("  ✓ " + texte); } else { ko++; console.log("  ✗ " + texte); } }
const attendre = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const g = fauxGesEnt(), t = fauxThunderbird();
  const compagnon = { appels: [], relie: null };
  const ecouteurs = {};
  const ecoute = nom => ({ addListener: f => { ecouteurs[nom] = f; } });
  const magasin = {};
  const enteteTb = id => {
    const x = t.messages.get(id); if (!x) return null; const e = x.entete;
    return { id: e.id, headerMessageId: e.messageIdTb, date: e.date, read: e.lu, tags: e.tags.slice(),
      folder: { accountId: e.compte, path: e.dossierChemin || "/INBOX", specialUse: e.usage || ["inbox"] } };
  };
  const listes = new Map(); let nListe = 0;
  const pagine = arr => { const id = "l" + (++nListe); listes.set(id, arr.slice(50)); return { id: arr.length > 50 ? id : null, messages: arr.slice(0, 50) }; };

  global.messenger = {
    runtime: { getManifest: () => require(path.join(EXT, "manifest.json")), onMessage: ecoute("onMessage") },
    accounts: { list: async () => t.comptesListe.map(c => ({ id: c.id, name: c.id, identities: c.identites.map(i => ({ email: i.email, name: i.nom })) })) },
    storage: { local: { get: async k => (typeof k === "string" ? (k in magasin ? { [k]: JSON.parse(JSON.stringify(magasin[k])) } : {}) : {}), set: async o => { for (const k in o) magasin[k] = JSON.parse(JSON.stringify(o[k])); }, remove: async ks => { for (const k of [].concat(ks)) delete magasin[k]; } } },
    alarms: { create: () => {}, onAlarm: ecoute("onAlarm") },
    action: { onClicked: ecoute("onClicked") },
    windows: { openDefaultBrowser: async () => {} }, tabs: { create: async () => {} },
    messages: {
      onNewMailReceived: ecoute("onNewMailReceived"), onUpdated: ecoute("onUpdated"), onDeleted: ecoute("onDeleted"), onMoved: ecoute("onMoved"),
      tags: { list: async () => t.etiquettes.map(e => ({ key: e.cle })), create: async (cle, nom, couleur) => { t.etiquettes.push({ cle, nom, couleur }); } },
      query: async q => {
        let r = [...t.messages.keys()].map(enteteTb);
        if (q.headerMessageId) r = r.filter(m => m.headerMessageId === q.headerMessageId);
        if (q.accountId) r = r.filter(m => [].concat(q.accountId).includes(m.folder.accountId));
        if (q.fromDate) r = r.filter(m => m.date >= q.fromDate);
        if (q.toDate) r = r.filter(m => m.date <= q.toDate);
        return pagine(r);
      },
      continueList: async id => { const a = listes.get(id) || []; return pagine(a); },
      getRaw: async id => { const x = t.messages.get(id); if (!x) throw new Error("introuvable"); return new Blob([x.brut]); },
      get: async id => enteteTb(id),
      update: async (id, p) => { const e = t.messages.get(id).entete; if (p.tags) e.tags = p.tags; if (p.read !== undefined) e.lu = p.read; },
    },
  };
  global.fetch = async (url, init = {}) => {
    if (url.startsWith("http://127.0.0.1:8740")) {
      const chemin = new URL(url).pathname;
      compagnon.appels.push([chemin, init.body ? JSON.parse(init.body) : null]);
      if (chemin === "/api/relie") compagnon.relie = JSON.parse(init.body);
      if (chemin === "/api/sync/pour-client") return new Response(JSON.stringify({ etiquette: { cle: "iamail_traite", nom: "Traité IAMail", couleur: "#12b76a" }, a_marquer: [], a_demarquer: [] }));
      return new Response("{}");
    }
    return g.fetch(url, init);
  };
  const intervalles = [];
  global.setInterval = (f, ms) => { intervalles.push(f); return 0; };

  for (const f of ["mime.js", "tri.js", "gesent.js", "background.js"]) require(path.join(EXT, f));
  await attendre(50);

  console.log("0. Statut lu / non lu envoyé au compagnon (compte IMAP : le mbox ne le dit pas)");
  {
    const nl = compagnon.appels.find(a => a[0] === "/api/sync/non-lus");
    verif(nl && Array.isArray(nl[1].non_lus) && nl[1].depuis, "au démarrage : POST /api/sync/non-lus (fenêtre de 14 jours)");
  }

  console.log("1. Page d'options : appairage");
  const etat0 = await ecouteurs.onMessage({ cmd: "etat" });
  const versionExt = require(path.join(EXT, "manifest.json")).version;
  verif(etat0.ok && etat0.statut === "non_relie" && etat0.version === versionExt, `état initial : non relié, extension ${versionExt}`);
  const r = await ecouteurs.onMessage({ cmd: "appairer", instance: "gesent.test", code: "K7Q2-M9XD", poste: "PC-ACCUEIL-02" });
  verif(r.ok, "appairage accepté" + (r.ok ? "" : " — " + r.erreur));
  verif(g.appairage.comptes_locaux.some(c => c.adresse === "info@entreprise.be") && g.appairage.comptes_locaux.length === 4, "comptes locaux = identités des comptes Thunderbird (dédoublonnées)");
  verif(g.appairage.collecteur.machine === "PC-ACCUEIL-02" && !("utilisateur_os" in g.appairage.collecteur && g.appairage.collecteur.utilisateur_os), "nom de poste choisi, aucun compte système envoyé");
  await attendre(20);
  verif(compagnon.relie && compagnon.relie.relie === true && compagnon.relie.boites[0] === "info@entreprise.be", "compagnon prévenu : cockpit en lecture seule pour info@");
  verif(t.etiquettes.some(e => e.cle === "traite_iamail"), "étiquette de GesEnt créée");

  console.log("2. Arrivée d'un mail");
  const e1 = t.ajouter("acc1", mail({ id: "ext1@exemple.be", objet: "Commande urgente" }), new Date());
  const e2 = t.ajouter("acc1", mail({ id: "brouillon@exemple.be" }), new Date());
  await ecouteurs.onNewMailReceived({ accountId: "acc1", specialUse: ["inbox"] }, { id: null, messages: [enteteTb(e1.id)] });
  await ecouteurs.onNewMailReceived({ accountId: "acc1", specialUse: ["drafts"] }, { id: null, messages: [enteteTb(e2.id)] });
  await ecouteurs.onMessage({ cmd: "envoyer" });
  verif(g.messages.has("7|ext1@exemple.be") && !g.messages.has("7|brouillon@exemple.be"), "mail de la boîte de réception envoyé, brouillon ignoré");
  const env1 = t.ajouter("acc1", mail({ id: "envoi@entreprise.be", de: "info@entreprise.be", a: "client@x.be" }), new Date(), { usage: ["sent"], dossierChemin: "/Sent" });
  await ecouteurs.onNewMailReceived({ accountId: "acc1", specialUse: ["sent"] }, { id: null, messages: [enteteTb(env1.id)] });
  await ecouteurs.onMessage({ cmd: "envoyer" });
  verif(g.envois.has("7|envoi@entreprise.be") && !g.messages.has("7|envoi@entreprise.be"), "dossier Envoyés → sens « envoye »");
  await ecouteurs.onUpdated(Object.assign(enteteTb(env1.id), { tags: ["traite_iamail"] }), { tags: ["traite_iamail"] });
  await ecouteurs.onMessage({ cmd: "envoyer" });
  verif(!g.etats.some(x => (x.traites || []).some(r => r.message_id === "envoi@entreprise.be")), "étiquette sur un envoi → ignorée");
  verif(g.erreursSchema.length === 0, "conforme au contrat" + (g.erreursSchema.length ? " — " + g.erreursSchema.join(" | ") : ""));

  {
    const nonLu = t.ajouter("acc1", mail({ id: "pas-lu@exemple.be" }), new Date());
    const lu = t.ajouter("acc1", mail({ id: "deja-lu@exemple.be" }), new Date()); t.messages.get(lu.id).entete.lu = true;
    compagnon.appels.length = 0;
    for (let k = 0; k < 8; k++) for (const f of intervalles) f();
    await attendre(150);
    const nl = compagnon.appels.filter(a => a[0] === "/api/sync/non-lus").pop();
    verif(nl && nl[1].non_lus.includes("pas-lu@exemple.be") && !nl[1].non_lus.includes("deja-lu@exemple.be"), "liste des non lus : le mail lu n'y est pas, le non lu oui");
  }

  console.log("3. Gestes dans Thunderbird");
  compagnon.appels.length = 0;
  await ecouteurs.onUpdated(Object.assign(enteteTb(e1.id), { tags: ["traite_iamail"] }), { tags: ["traite_iamail"] });
  await ecouteurs.onMessage({ cmd: "envoyer" });
  verif(g.etats.some(x => x.traites.some(r => r.message_id === "ext1@exemple.be")), "étiquette posée sur une boîte reliée → GesEnt (depuis-client)");
  verif(!compagnon.appels.some(a => a[0] === "/api/sync/depuis-client"), "… et pas le cockpit local");
  const p = t.ajouter("acc2", mail({ id: "perso@exemple.be", a: "perso@entreprise.be" }), new Date(Date.now() - 3600e3));
  await ecouteurs.onUpdated(Object.assign(enteteTb(p.id), { tags: ["iamail_traite"] }), { tags: ["iamail_traite"] });
  verif(compagnon.appels.some(a => a[0] === "/api/sync/depuis-client" && a[1].traites[0] === "perso@exemple.be"), "boîte non reliée → cockpit local, comme avant");
  await ecouteurs.onMessage({ cmd: "envoyer" });
  verif(g.gestes.some(x => x.message_id === "perso@exemple.be" && x.geste === "traite"), "… et geste « traite » au journal GesEnt");
  const avant = enteteTb(e1.id);
  t.messages.get(e1.id).entete.dossierChemin = "/Corbeille"; t.messages.get(e1.id).entete.usage = ["trash"];
  avant.read = true;
  await ecouteurs.onMoved({ messages: [avant] }, { messages: [enteteTb(e1.id)] });
  await ecouteurs.onMessage({ cmd: "envoyer" });
  verif(g.etats.some(x => x.ranges.some(r => r.message_id === "ext1@exemple.be")), "mis à la corbeille → rangé dans GesEnt");
  const neuf = t.ajouter("acc1", mail({ id: "filtre@exemple.be" }), new Date());
  const nEtats = g.etats.length;
  await ecouteurs.onMoved({ messages: [enteteTb(neuf.id)] }, { messages: [Object.assign(enteteTb(neuf.id), { folder: { accountId: "acc1", path: "/Fournisseurs", specialUse: [] } })] });
  await ecouteurs.onMessage({ cmd: "envoyer" });
  verif(g.etats.length === nEtats, "mail non lu rangé par un filtre à l'arrivée → pas un geste");

  console.log("4. Synchro depuis GesEnt");
  const e3 = t.ajouter("acc1", mail({ id: "gesent-traite@exemple.be" }), new Date());
  g.pourClient.a_marquer.push({ boite_id: 7, message_id: "gesent-traite@exemple.be" });
  for (const f of intervalles) f();
  await attendre(100);
  verif(t.messages.get(e3.id).entete.tags.includes("traite_iamail") && g.confirmes.length > 0, "cycle : étiquette posée depuis GesEnt et confirmée");

  console.log("5. Délier");
  await ecouteurs.onMessage({ cmd: "delier" });
  await attendre(20);
  verif(compagnon.relie && compagnon.relie.relie === false, "compagnon prévenu : cockpit local de nouveau actif");
  const e4 = t.ajouter("acc1", mail({ id: "apres@exemple.be" }), new Date());
  await ecouteurs.onNewMailReceived({ accountId: "acc1", specialUse: ["inbox"] }, { id: null, messages: [enteteTb(e4.id)] });
  verif(!g.messages.has("7|apres@exemple.be") && (await ecouteurs.onMessage({ cmd: "etat" })).file === 0, "plus rien n'est envoyé à GesEnt");

  console.log(`\n${ok} vérification(s) réussie(s), ${ko} en échec`);
  process.exit(ko ? 1 : 0);
})().catch(e => { console.error(e); process.exit(2); });

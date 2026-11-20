"use strict";
// Recette du client GesEnt (gesent.js) contre un FAUX GesEnt qui applique le contrat v1 :
// chaque requête est validée par les schémas du contrat (copiés de GesEnt, tests/contrat/),
// les sources sont décompressées et leurs empreintes vérifiées, l'idempotence est tenue par
// lot_id et par clé (boite_id, message_id). Thunderbird est simulé (comptes, messages, étiquettes).
//   node thunderbird-cockpit-button/tests/protocole.test.js
const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const EXT = path.join(__dirname, "..");
const MOTEUR = path.join(EXT, "..", "moteur");
for (const f of ["mime.js", "tri.js", "gesent.js"]) require(path.join(EXT, f));
const { fauxGesEnt, fauxThunderbird, mail } = require("./faux.js");
const lireJson = p => JSON.parse(fs.readFileSync(p, "utf8"));

let ok = 0, ko = 0;
function verif(cond, texte) { if (cond) { ok++; console.log("  ✓ " + texte); } else { ko++; console.log("  ✗ " + texte); } }

function environnement(g, t, stockage) {
  const horloge = { t: Date.parse("2026-11-20T08:00:00Z") };
  const magasin = stockage || {};
  return { horloge, magasin, env: {
    fetch: (u, i) => g.fetch(u, i), tb: t.adaptateur, type: "thunderbird", version: "2.0.0", sansMinuterie: true,
    maintenant: () => new Date(horloge.t), dormir: async ms => { horloge.t += ms; },
    stockage: { get: async k => (k in magasin ? { [k]: JSON.parse(JSON.stringify(magasin[k])) } : {}), set: async o => { for (const k in o) magasin[k] = JSON.parse(JSON.stringify(o[k])); }, remove: async ks => { for (const k of [].concat(ks)) delete magasin[k]; } },
  } };
}
const avancer = (h, ms) => { h.t += ms; };

// ------------------------------------------------------------------------------------------
(async () => {
  const { ClientGesEnt } = IAMailGesEnt;

  console.log("1. Appairage, portée, moteur");
  let g = fauxGesEnt(), t = fauxThunderbird();
  let { env, horloge, magasin } = environnement(g, t);
  let c = new ClientGesEnt(env); await c.charger();
  let refus = null;
  try { await c.appairer("gesent.test", "XXXX-XXXX", []); } catch (e) { refus = e.message; }
  verif(refus === "Code inconnu.", "code inconnu → message clair");
  await c.appairer("gesent.test", "k7q2-m9xd", [{ adresse: "info@entreprise.be", nom: "Accueil" }]);
  verif(c.etat.jeton === "iae_1_secret" && c.estRelie(), "jeton gardé, statut actif");
  verif(c.etat.instance === "https://gesent.test", "adresse complétée en https://");
  verif(g.appairage.collecteur.machine === "PC-ACCUEIL-02" && g.appairage.collecteur.version === "2.0.0", "corps d'appairage (collecteur, comptes_locaux)");
  verif(c.etat.boites.find(b => b.boite_id === 7).relie === true, "GET /moi : relie et plus_ancien_recu rafraîchis");
  verif(c.moteur && c.etat.moteur.version === g.version, `moteur ${g.version} chargé (empreintes vérifiées)`);
  const n304 = g.journal.length; await c.chargerMoteur(false);
  verif(g.journal.length === n304 + 1, "moteur inchangé → 304, aucun fichier retéléchargé");
  g.manifesteFaux = true; g.version = "2.9.9";
  verif(!(await c.chargerMoteur(true)) && c.etat.moteur.version !== "2.9.9", "fichier dont l'empreinte diffère du manifeste → refusé, ancien moteur gardé");
  g.manifesteFaux = false; g.version = c.etat.moteur.version;

  console.log("2. Arrivées : lot conforme, clé synth-, gros message téléversé");
  const casEml = fs.readFileSync(path.join(MOTEUR, "tests/cas/001-humain-en-copie.eml"));
  const att = lireJson(path.join(MOTEUR, "tests/cas/001-humain-en-copie.attendu.json")).deterministe;
  const e1 = t.ajouter("acc1", mail({ id: "AB12@exemple.be", objet: "Fuite urgente", corps: "Il y a une fuite d'eau, c'est urgent." }), new Date("2026-11-20T08:05:00Z"));
  const e2 = t.ajouter("acc1", mail({ objet: "Sans identifiant" }), new Date("2026-11-20T08:06:00Z"));
  const gros = crypto.randomBytes(6 * 1024 * 1024).toString("base64");
  const e3 = t.ajouter("acc1", mail({ id: "gros@exemple.be", objet: "Plans de la phase 2", corps: gros }), new Date("2026-11-20T08:07:00Z"));
  const e4 = t.ajouter("acc3", mail({ id: "prive@gmail.com", a: "prive@gmail.com" }), new Date("2026-11-20T08:07:30Z"));
  const e5 = t.ajouter("acc1", casEml, new Date("2026-11-20T08:08:00Z"));
  for (const e of [e1, e2, e3, e4, e5]) await c.surArrivee(e);
  verif(c.etat.file.filter(x => x.type === "message").length === 4, "compte sans boîte GesEnt ignoré (4 messages en file)");
  verif(JSON.stringify(magasin.gesent.file).length < 10000 && Object.keys(magasin).some(k => k.startsWith("gesent_src_")),
    "les sources sont stockées à part : la file persistante reste légère");
  await c.pomper();
  const lots = [...g.lots.values()];
  verif(lots.length === 1 && lots[0].lot.mode === "arrivee" && lots[0].lot.messages.length === 4, "un seul lot « arrivee » de 4 messages");
  verif(g.erreursSchema.length === 0, "lot, réponses et en-têtes conformes aux schémas du contrat" + (g.erreursSchema.length ? " — " + g.erreursSchema.join(" | ") : ""));
  const recus = [...g.messages.values()];
  verif(recus.some(m => /^synth-[0-9a-f]{32}$/.test(m.message_id)), "Message-ID absent → clé synth- du moteur");
  verif(recus.find(m => m.message_id === "AB12@exemple.be").tri_local.urgence === "critique", "tri_local : « fuite d’eau » d’un humain qui écrit à la boîte → critique");
  const t5 = recus.find(m => m.message_id === "cas-001@exemple.be").tri_local;
  verif(Object.keys(att).every(k => JSON.stringify(t5[k]) === JSON.stringify(att[k])), "tri_local identique à l'attendu du cas de conformité 001");
  const mg = recus.find(m => m.message_id === "gros@exemple.be");
  verif(mg.source.a_televerser === true && !mg.source.donnees && mg.entetes && mg.entetes.objet === "Plans de la phase 2", "gros message annoncé (a_televerser + entetes, sans donnees)");
  verif(g.putSources === 1 && g.sources.get("7|gros@exemple.be").recu > 6e6, "PUT /sources : fichier brut reçu, empreinte vérifiée");
  verif(c.etat.file.length === 0 && c.etat.lotEnCours === null, "file vidée");
  verif(!Object.keys(magasin).some(k => k.startsWith("gesent_src_")), "sources effacées du stockage une fois reçues");
  verif(recus[0].recu_le.endsWith("+00:00") || /[+-]\d\d:\d\d$/.test(recus[0].recu_le), "recu_le avec décalage horaire");

  console.log("3. Pannes : 503, 429, coupure, lot rejoué avec le même lot_id");
  const e6 = t.ajouter("acc1", mail({ id: "panne@exemple.be" }), new Date("2026-11-20T08:09:00Z"));
  await c.surArrivee(e6);
  g.pannes.push({ quand: "POST /lots", status: 503 });
  await c.pomper();
  const lotId = c.etat.lotEnCours && c.etat.lotEnCours.lot_id;
  verif(lotId && c.etat.attenteJusqua - horloge.t === 30000, "503 → attente 30 s, lot gardé");
  await c.pomper();
  verif(g.lots.size === 1, "pas de nouvel essai avant l'échéance");
  avancer(horloge, 30001);
  g.pannes.push({ quand: "POST /lots", status: 429, retryAfter: 7 });
  await c.pomper();
  verif(c.etat.attenteJusqua - horloge.t === 7000 && c.etat.lotEnCours.lot_id === lotId, "429 → Retry-After respecté (7 s), même lot_id");
  avancer(horloge, 7001);
  // Thunderbird fermé pendant l'envoi : le serveur a reçu le lot, la réponse est perdue.
  const fetchReel = g.fetch;
  g.fetch = async (u, i) => { const r = await fetchReel(u, i); if (u.endsWith("/lots")) throw new TypeError("connexion coupée"); return r; };
  await c.pomper();
  g.fetch = fetchReel;
  verif(g.lots.has(lotId) && c.etat.lotEnCours && c.etat.lotEnCours.lot_id === lotId, "réponse perdue → lot toujours en cours, même lot_id");
  const c2 = new ClientGesEnt(env); await c2.charger();             // redémarrage de Thunderbird
  avancer(horloge, 900001);
  const avant = g.messages.size;
  await c2.pomper();
  verif(c2.etat.lotEnCours === null && g.messages.size === avant, "redémarrage : lot rejoué, GesEnt répond deja_recu, aucun doublon");
  verif(c2.etat.journal.some(j => /déjà reçu/.test(j.texte)), "journal : « déjà reçu »");
  c = c2;

  console.log("4. Moteur d'une autre majeure, lot trop gros, rejets");
  const e7 = t.ajouter("acc1", mail({ id: "moteur@exemple.be" }), new Date("2026-11-20T08:10:00Z"));
  await c.surArrivee(e7);
  c.etat.moteur.version = "1.9.0";                                   // collecteur resté en 1.x
  await c.pomper();
  verif(g.messages.has("7|moteur@exemple.be") && c.etat.moteur.version === g.version, "409 moteur_incompatible → moteur téléchargé, lot renvoyé");
  for (let i = 0; i < 6; i++) await c.surArrivee(t.ajouter("acc1", mail({ id: `lot413-${i}@exemple.be` }), new Date("2026-11-20T08:11:00Z")));
  g.forcer413 = 1;
  await c.pomper();
  verif([0, 1, 2, 3, 4, 5].every(i => g.messages.has(`7|lot413-${i}@exemple.be`)), "413 → lot découpé, tout finit par passer");
  g.rejeterId = "rejete@exemple.be";
  await c.surArrivee(t.ajouter("acc1", mail({ id: "rejete@exemple.be" }), new Date("2026-11-20T08:12:00Z")));
  await c.surArrivee(t.ajouter("acc1", mail({ id: "accepte@exemple.be" }), new Date("2026-11-20T08:12:00Z")));
  await c.pomper();
  verif(c.etat.file.length === 0 && c.etat.rejets[0].code === "source_corrompue" && g.messages.has("7|accepte@exemple.be"), "rejet 422 par élément : journalisé, retiré de la file, le reste passe");
  verif(g.erreursSchema.length === 0, "toujours conforme aux schémas" + (g.erreursSchema.length ? " — " + g.erreursSchema.join(" | ") : ""));

  // Démarrage sans moteur (GesEnt injoignable) : l'arrivée est gardée, préparée plus tard.
  c.moteur = null; c.etat.moteur = null;
  const sansMoteur = t.ajouter("acc1", mail({ id: "sans-moteur@exemple.be" }), new Date("2026-11-20T08:13:00Z"));
  verif((await c.surArrivee(sansMoteur)) === true && c.etat.file[0].type === "a_preparer", "arrivée sans moteur chargé → gardée en file");
  await c.pomper();
  verif(g.messages.has("7|sans-moteur@exemple.be") && c.moteur, "moteur téléchargé, message préparé et envoyé");

  console.log("5. Rattrapage : après les arrivées, du plus récent au plus ancien, repris");
  const anciens = [];
  for (let i = 0; i < 250; i++) anciens.push(t.ajouter("acc1", mail({ id: `ancien-${i}@exemple.be`, objet: "Ancien " + i }), new Date(Date.parse("2026-10-01T00:00:00Z") - i * 3 * 3600e3)));
  // Le poste a été relié alors que la boîte contenait déjà cet historique : les pompes des
  // étapes précédentes l'ont parcourue vide ; on repart de l'état « rien de rattrapé ».
  delete c.etat.rattrapage[7];
  const tresAncien = t.ajouter("acc1", mail({ id: "tres-ancien@exemple.be" }), new Date("2019-05-01T10:00:00Z"));
  await c.surArrivee(t.ajouter("acc1", mail({ id: "arrivee-pendant@exemple.be" }), new Date("2026-11-20T08:20:00Z")));
  await c.pomper();                                                 // arrivées d'abord, puis un lot de rattrapage
  const modes = [...g.lots.values()].slice(-2).map(x => x.lot.mode);
  verif(modes[0] === "arrivee" && modes[1] === "rattrapage", "l'arrivée part avant le rattrapage");
  for (let k = 0; k < 3; k++) { avancer(horloge, 3000); await c.pomper(); }
  const ratt = [...g.lots.values()].filter(x => x.lot.mode === "rattrapage").flatMap(x => x.lot.messages.map(m => Date.parse(m.recu_le)));
  verif([...g.lots.values()].some(x => x.lot.mode === "rattrapage" && x.lot.messages.length === 100)
    && ratt.every((d, i) => i === 0 || d <= ratt[i - 1]), "lots de 100 au plus, du plus récent au plus ancien");
  // Interruption : nouvel état local perdu, reprise à plus_ancien_recu (GET /moi).
  const magasin2 = {}; const env2 = Object.assign({}, env, { stockage: { get: async k => (k in magasin2 ? { [k]: JSON.parse(JSON.stringify(magasin2[k])) } : {}), set: async o => { for (const k in o) magasin2[k] = JSON.parse(JSON.stringify(o[k])); }, remove: async ks => { for (const k of [].concat(ks)) delete magasin2[k]; } } });
  const c3 = new ClientGesEnt(env2); await c3.charger();
  Object.assign(c3.etat, { instance: c.etat.instance, jeton: c.etat.jeton, statut: "actif", moteur: c.etat.moteur }); c3._monterMoteur();
  await c3.rafraichir();
  const lotsAvant = g.lots.size;
  for (let k = 0; k < 40; k++) { await c3.pomper(); avancer(horloge, 3000); }
  const rattrapes = [...g.lots.values()].slice(lotsAvant).filter(x => x.lot.mode === "rattrapage");
  const doublons = rattrapes.reduce((n, x) => n + x.reponse.messages.deja_connus, 0);
  verif(anciens.every(e => g.messages.has(`7|ancien-${anciens.indexOf(e)}@exemple.be`)) && g.messages.has("7|tres-ancien@exemple.be"), "les 251 anciens messages sont reçus (dont un de 2019)");
  verif(doublons <= 2, `reprise à plus_ancien_recu : ${doublons} déjà connu(s) seulement`);
  verif(c3.etat.rattrapage[7].fini === true, "rattrapage marqué terminé");
  verif([...g.messages.values()].filter(m => m.message_id.startsWith("ancien-")).every(m => m.mode === "rattrapage"), "mode « rattrapage » sur l'existant");

  console.log("6. Synchro « Traité » : GesEnt fait foi sur la boîte reliée");
  g.pourClient.a_marquer = [{ boite_id: 7, message_id: "AB12@exemple.be" }];
  g.pourClient.a_demarquer = [{ boite_id: 7, message_id: "panne@exemple.be" }];
  t.messages.get(e6.id).entete.tags = ["traite_iamail"];
  c.etat.etiquetes["7\u0000panne@exemple.be"] = 1;
  const bilan = await c.synchroTraite();
  verif(t.messages.get(e1.id).entete.tags.includes("traite_iamail") && t.messages.get(e1.id).entete.lu, "a_marquer → étiquette posée + lu dans Thunderbird");
  verif(!t.messages.get(e6.id).entete.tags.includes("traite_iamail"), "a_demarquer → étiquette retirée");
  verif(bilan.marques === 1 && bilan.demarques === 1 && g.pourClient.a_marquer.length === 0, "POST /sync/confirme avec ce qui a été réellement appliqué");
  verif(t.etiquettes.some(x => x.cle === "traite_iamail"), "étiquette de GesEnt créée dans Thunderbird");
  verif((await c.surEtiquette(t.messages.get(e1.id).entete)) === null, "notre propre pose n'est pas renvoyée (pas d'écho)");
  // Liste pleine : le collecteur rappelle.
  const plein = []; for (let i = 0; i < 500; i++) plein.push({ boite_id: 7, message_id: `ancien-${i % 250}@exemple.be` });
  g.pourClient.a_marquer = plein.concat([{ boite_id: 7, message_id: "accepte@exemple.be" }]);
  const nAvant = g.journal.filter(x => x === "GET /sync/pour-client").length;
  await c.synchroTraite();
  verif(g.journal.filter(x => x === "GET /sync/pour-client").length - nAvant >= 2 && g.pourClient.a_marquer.length === 0, "liste de 500 → rappel jusqu'à épuisement");
  // Gestes de l'utilisateur sur la boîte reliée → état (depuis-client), pas de geste.
  avancer(horloge, 20000);
  const m1 = t.messages.get(e1.id).entete; m1.tags = [];
  verif((await c.surEtiquette(m1)) === "non_traite", "étiquette retirée à la main → non_traite");
  const m5 = t.messages.get(e5.id).entete; m5.tags = ["traite_iamail"];
  await c.surEtiquette(m5);
  m5.tags = ["traite_iamail", "$label1"];
  verif((await c.surEtiquette(m5)) === null, "autre étiquette modifiée → rien");
  await c.surRangement(t.messages.get(e2.id).entete, { supprime: true });
  await c.pomper();
  const dc = g.etats.pop();
  verif(dc && dc.non_traites.length === 1 && dc.traites.length === 1 && dc.ranges.length === 1 && dc.ranges[0].message_id.startsWith("synth-"), "POST /sync/depuis-client : traites, non_traites, ranges (clé synth- retrouvée)");
  // Boîte non reliée → gestes au journal.
  const p1 = t.ajouter("acc2", mail({ id: "perso1@exemple.be", a: "perso@entreprise.be" }), new Date("2026-11-20T08:30:00Z"));
  await c.surArrivee(p1);
  await c.surRangement(p1, { vers: "Clients/Dupont" });
  p1.tags = ["traite_iamail"]; await c.surEtiquette(p1);
  await c.pomper();
  const gestes = g.gestes.filter(x => x.message_id === "perso1@exemple.be");
  verif(gestes.length === 2 && gestes[0].geste === "deplace" && gestes[0].details.vers === "Clients/Dupont" && gestes[1].geste === "traite" && gestes[0].utilisateur === "jdupont", "boîte non reliée : gestes deplace + traite au journal");
  verif(g.messages.has("9|perso1@exemple.be") && g.lots.size > 0, "le message arrive avant ses gestes (même lot, messages puis gestes)");
  verif(g.erreursSchema.length === 0, "synchro conforme aux schémas" + (g.erreursSchema.length ? " — " + g.erreursSchema.join(" | ") : ""));

  console.log("6 bis. Balayage : arrivées non signalées par Thunderbird");
  const silencieux = [0, 1, 2].map(i => t.ajouter("acc1", mail({ id: `silence-${i}@exemple.be` }), new Date(horloge.t - (i + 1) * 60e3)));
  const nLots = g.lots.size;
  const nb1 = await c.balayer();
  verif(nb1 === 3, "3 mails relevés sans signal (lus sur un téléphone…) → mis en file");
  await c.pomper();
  verif(silencieux.every((e, i) => g.messages.has(`7|silence-${i}@exemple.be`)) && [...g.lots.values()].slice(nLots).every(x => x.lot.mode === "arrivee"), "envoyés comme des arrivées");
  verif((await c.balayer()) === 0, "balayage suivant : rien n'est renvoyé");

  console.log("7. Portée retirée, jeton révoqué");
  g.boites = g.boites.filter(b => b.boite_id !== 9);
  g.pannes.push({ quand: "POST /lots", status: 403, code: "hors_portee", message: "La boîte 9 n'est pas reliée à ce collecteur." });
  await c.surArrivee(t.ajouter("acc1", mail({ id: "portee@exemple.be" }), new Date("2026-11-20T08:40:00Z")));
  await c.pomper();
  verif(!c.etat.boites.some(b => b.boite_id === 9), "403 → GET /moi : la boîte retirée disparaît de la portée");
  verif((await c.surArrivee(t.ajouter("acc2", mail({ id: "perso2@exemple.be" }), new Date()))) === false, "un mail de la boîte retirée n'est plus collecté");
  g.revoque = true;
  await c.surArrivee(t.ajouter("acc1", mail({ id: "apres-revocation@exemple.be" }), new Date("2026-11-20T08:41:00Z")));
  await c.pomper();
  verif(c.etat.statut === "revoque" && !c.estRelie() && c.etat.lotEnCours, "401 → arrêt, appairage demandé, le lot reste en file");
  verif(c.boitesReliees().length === 0, "révoqué → plus aucune boîte reliée : retour au cockpit local");

  console.log(`\n${ok} vérification(s) réussie(s), ${ko} en échec`);
  process.exit(ko ? 1 : 0);
})().catch(e => { console.error(e); process.exit(2); });

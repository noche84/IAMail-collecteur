"use strict";
// Conformité du tri JavaScript (tri.js) sur les cas du moteur : mêmes .eml, mêmes attendus
// que les harnais Python (moteur/tests/run_conformance.py) et PHP (GesEnt).
//   node thunderbird-cockpit-button/tests/conformite-tri.test.js
const fs = require("fs");
const path = require("path");
const EXT = path.join(__dirname, "..");
const MOTEUR = path.join(EXT, "..", "moteur");
require(path.join(EXT, "mime.js"));
require(path.join(EXT, "tri.js"));

(async () => {
  const ctx = JSON.parse(fs.readFileSync(path.join(MOTEUR, "tests", "contexte.json"), "utf8"));
  const regles = JSON.parse(fs.readFileSync(path.join(MOTEUR, "regles.json"), "utf8"));
  const taxo = JSON.parse(fs.readFileSync(path.join(MOTEUR, "tests", ctx.taxonomie || "../taxonomie.json"), "utf8"));
  regles.parametres = Object.assign({}, regles.parametres, { ia_extrait_max: ctx.ia_extrait_max || 4000 });
  const moteur = IAMailTri.creerMoteur(regles, taxo, "test");
  const dossier = path.join(MOTEUR, "tests", "cas");
  const cas = fs.readdirSync(dossier).filter(f => f.endsWith(".eml")).sort();
  let echecs = 0;
  for (const f of cas) {
    const att = JSON.parse(fs.readFileSync(path.join(dossier, f.replace(/\.eml$/, ".attendu.json")), "utf8"));
    const brut = new Uint8Array(fs.readFileSync(path.join(dossier, f)));
    const r = await moteur.lire(brut, ctx.mes_adresses, ctx.importants);
    const ecarts = [];
    const ex = Object.assign({ message_id: r.message_id }, r.extraction);
    for (const [k, v] of Object.entries(att.extraction || {})) {
      if (k === "extrait_debut") { if (!ex.extrait.startsWith(v)) ecarts.push(`extrait: ${JSON.stringify(ex.extrait.slice(0, 40))} ≠ ${JSON.stringify(v)}…`); }
      else if (JSON.stringify(ex[k]) !== JSON.stringify(v)) ecarts.push(`${k}: ${JSON.stringify(ex[k])} ≠ ${JSON.stringify(v)}`);
    }
    for (const [k, v] of Object.entries(att.deterministe || {})) {
      if (JSON.stringify(r.tri_local[k]) !== JSON.stringify(v)) ecarts.push(`${k}: ${JSON.stringify(r.tri_local[k])} ≠ ${JSON.stringify(v)}`);
    }
    if (ecarts.length) { echecs++; console.log(`✗ ${f}\n    ` + ecarts.join("\n    ")); }
    else console.log(`✓ ${f}`);
  }
  console.log(`\n${cas.length - echecs}/${cas.length} cas conformes (tri JavaScript)`);
  process.exit(echecs ? 1 : 0);
})();

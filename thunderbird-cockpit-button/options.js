"use strict";
// Page d'options : appairage avec GesEnt et état du client (file, rattrapage, journal).
const $ = id => document.getElementById(id);
const envoyer = msg => messenger.runtime.sendMessage(msg);
const texte = (el, t, classe) => { el.textContent = t || ""; el.className = classe || ""; };

function ligne(parent, cellules) {
  const tr = document.createElement("tr");
  for (const c of cellules) { const td = document.createElement("td"); if (c instanceof Node) td.appendChild(c); else td.textContent = c; tr.appendChild(td); }
  parent.appendChild(tr);
}
function pastille(oui, t) { const s = document.createElement("span"); s.className = "pastille " + (oui ? "oui" : "non"); s.textContent = t; return s; }

async function rafraichir() {
  const e = await envoyer({ cmd: "etat" });
  if (!e || !e.ok) return;
  const relie = e.statut === "actif" || e.statut === "revoque" || e.statut === "protocole";
  $("relier").hidden = e.statut === "actif" || e.statut === "protocole";
  $("etat").hidden = !relie;
  if ($("poste").value === "" && e.poste) $("poste").value = e.poste;
  if (!relie) return;
  $("instance-affichee").textContent = e.instance;
  const st = { actif: ["Relié", true], revoque: ["Délié dans GesEnt : reliez de nouveau", false], protocole: ["Extension à mettre à jour", false] }[e.statut];
  texte($("statut"), st[0], "pastille " + (st[1] ? "oui" : "non"));
  const bouts = [`moteur ${e.moteur || "—"}`, `${e.file} élément(s) en file`];
  if (e.attente) bouts.push(`prochain essai à ${e.attente}`);
  if (e.dernierContact) bouts.push(`dernier contact ${e.dernierContact.replace("T", " ").slice(0, 16)}`);
  $("resume").textContent = bouts.join(" · ") + ` · extension ${e.version}`;
  const tb = $("boites"); tb.textContent = "";
  for (const b of e.boites) {
    const r = e.rattrapage[b.boite_id] || {};
    const hist = r.fini ? "terminé" : (r.curseur ? "en cours (jusqu'au " + r.curseur.slice(0, 10) + ")" : "à venir");
    ligne(tb, [`${b.libelle || ""} — ${b.adresse}`, pastille(!!b.relie, b.relie ? "oui" : "non (journal seulement)"), hist]);
  }
  const j = $("journal"); j.textContent = "";
  for (const x of e.journal) {
    const li = document.createElement("li");
    const q = document.createElement("span"); q.className = "quand"; q.textContent = x.le.slice(11, 16);
    li.appendChild(q); li.appendChild(document.createTextNode(x.texte));
    if (x.niveau === "erreur") li.className = "erreur";
    j.appendChild(li);
  }
  $("bloc-rejets").hidden = !e.rejets.length;
  const rj = $("rejets"); rj.textContent = "";
  for (const x of e.rejets) { const li = document.createElement("li"); li.textContent = `${x.le.slice(0, 16).replace("T", " ")} — ${x.type} ${x.message_id || ""} : ${x.code}`; rj.appendChild(li); }
}

$("btn-relier").addEventListener("click", async () => {
  let instance = $("instance").value.trim().replace(/\/+$/, "");
  if (!instance) return texte($("message"), "Indiquez l'adresse de GesEnt.", "erreur");
  if (!/^https?:\/\//.test(instance)) instance = "https://" + instance;
  let origine;
  try { origine = new URL(instance).origin; } catch (e) { return texte($("message"), "Adresse invalide.", "erreur"); }
  // Permission d'hôte demandée ici (geste de l'utilisateur) : GesEnt n'envoie pas d'en-têtes CORS.
  const accorde = await messenger.permissions.request({ origins: [origine + "/*"] });
  if (!accorde) return texte($("message"), "Thunderbird doit pouvoir joindre " + origine + " : autorisation refusée.", "erreur");
  texte($("message"), "Liaison en cours…");
  const r = await envoyer({ cmd: "appairer", instance, code: $("code").value, poste: $("poste").value.trim() });
  if (r && r.ok) { texte($("message"), "Poste relié.", "ok"); $("code").value = ""; }
  else texte($("message"), (r && r.erreur) || "Échec de la liaison.", "erreur");
  rafraichir();
});
$("btn-delier").addEventListener("click", async () => {
  if (!confirm("Délier ce poste ? Les mails ne seront plus envoyés à GesEnt et le cockpit local reprend la main. (Pensez aussi à révoquer le poste dans GesEnt.)")) return;
  await envoyer({ cmd: "delier" });
  rafraichir();
});
$("btn-envoyer").addEventListener("click", async () => {
  texte($("message2"), "Envoi…");
  const r = await envoyer({ cmd: "envoyer" });
  texte($("message2"), r && r.ok ? "Fait." : (r && r.erreur) || "Échec.", r && r.ok ? "ok" : "erreur");
  rafraichir();
});

rafraichir();
setInterval(rafraichir, 5000);

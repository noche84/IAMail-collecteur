"use strict";

const DEFAUT = { dossierBase: "IAMail", maxSujet: 60, archiver: true };

async function charger() {
  const r = await messenger.storage.local.get("reglages");
  const g = Object.assign({}, DEFAUT, r.reglages || {});
  document.getElementById("dossierBase").value = g.dossierBase;
  document.getElementById("maxSujet").value = g.maxSujet;
  document.getElementById("archiver").checked = !!g.archiver;
}

document.getElementById("enregistrer").addEventListener("click", async () => {
  let base = document.getElementById("dossierBase").value.trim() || "IAMail";
  // Nettoie : pas de barre de début/fin, pas de "..".
  base = base.replace(/^[\/\\]+|[\/\\]+$/g, "").replace(/\.\./g, "");
  let maxSujet = parseInt(document.getElementById("maxSujet").value, 10);
  if (isNaN(maxSujet) || maxSujet < 20) maxSujet = 60;
  if (maxSujet > 120) maxSujet = 120;
  const reglages = {
    dossierBase: base || "IAMail",
    maxSujet,
    archiver: document.getElementById("archiver").checked
  };
  await messenger.storage.local.set({ reglages });
  const ok = document.getElementById("ok");
  ok.textContent = "Enregistré.";
  setTimeout(() => (ok.textContent = ""), 2000);
});

charger();

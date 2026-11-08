"use strict";

async function rafraichir() {
  const r = await messenger.runtime.sendMessage({ cmd: "etat" });
  if (!r || !r.ok) return;
  document.getElementById("total").textContent = r.etat.total || 0;
  document.getElementById("dossier").textContent = (r.reglages && r.reglages.dossierBase) || "IAMail";
  const d = r.etat.dernier;
  document.getElementById("dernier").textContent =
    d ? ("Dernier : " + d.sujet.slice(0, 40)) : "";
}

document.getElementById("relever").addEventListener("click", async () => {
  const btn = document.getElementById("relever");
  const bilan = document.getElementById("bilan");
  btn.disabled = true;
  bilan.textContent = "Relève en cours…";
  try {
    const r = await messenger.runtime.sendMessage({ cmd: "relever-inbox", limite: 200 });
    if (r && r.ok) {
      const b = r.bilan;
      bilan.textContent =
        "Comptes : " + b.comptes + " · inbox : " + b.inboxTrouvees +
        "\nExaminés : " + b.examines + "\nArchivés : " + b.archives +
        "\nDéjà vus : " + b.doublons + (b.erreurs ? "\nErreurs : " + b.erreurs : "");
      await rafraichir();
    } else {
      bilan.textContent = "Erreur : " + (r && r.erreur);
    }
  } catch (e) {
    bilan.textContent = "Erreur : " + e;
  } finally {
    btn.disabled = false;
  }
});

document.getElementById("options").addEventListener("click", (e) => {
  e.preventDefault();
  messenger.runtime.openOptionsPage();
});

rafraichir();

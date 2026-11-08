"use strict";
/*
 * background.js — point d'entrée.
 * 1) Écoute l'arrivée de nouveaux messages et les archive automatiquement.
 * 2) Répond aux commandes de la fenêtre popup (relève manuelle, état).
 */

// Arrivée de nouveaux messages : messages est une liste paginée.
messenger.messages.onNewMailReceived.addListener(async (folder, messages) => {
  try {
    for (const entete of messages.messages) {
      await archiverMessage(entete);
    }
  } catch (e) {
    console.error("[IAMail] onNewMailReceived", e);
  }
});

// Canal de commandes depuis la popup.
messenger.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    try {
      if (msg && msg.cmd === "relever-inbox") {
        const bilan = await releverBoitesReception(msg.limite || 200);
        sendResponse({ ok: true, bilan });
      } else if (msg && msg.cmd === "etat") {
        const etat = await lireEtat();
        const reglages = await lireReglages();
        sendResponse({ ok: true, etat, reglages });
      } else {
        sendResponse({ ok: false, erreur: "commande inconnue" });
      }
    } catch (e) {
      sendResponse({ ok: false, erreur: String(e) });
    }
  })();
  return true; // réponse asynchrone
});

console.log("[IAMail] collecteur v" + COLLECTEUR.version + " prêt");

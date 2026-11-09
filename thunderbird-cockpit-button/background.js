"use strict";
// Un seul rôle : ouvrir le cockpit IAMail (servi en local par le compagnon)
// dans le navigateur par défaut, au clic sur le bouton.
const COCKPIT = "http://127.0.0.1:8740/";

messenger.action.onClicked.addListener(async () => {
  try {
    await messenger.windows.openDefaultBrowser(COCKPIT);
  } catch (e) {
    // Repli : ouvrir dans un onglet Thunderbird si le navigateur externe échoue.
    try { await messenger.tabs.create({ url: COCKPIT }); } catch (e2) {}
  }
});

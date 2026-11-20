"use strict";
/*
 * tri.js — couche DÉTERMINISTE du moteur de tri IAMail, en JavaScript.
 *
 * Troisième harnais du moteur (après le compagnon Python et GesEnt en PHP) : il lit les
 * mêmes données (regles.json + taxonomie.json, servies par GesEnt et vérifiées par sha256)
 * et applique regles._algorithme. Sert à remplir `tri_local`, le TÉMOIN joint à chaque
 * message envoyé à GesEnt (contrat d'ingestion v1 § 4) : GesEnt rejoue tri et IA.
 *
 * Vérifié sur les cas de conformité du moteur (moteur/tests/cas/*.eml) par
 * tests/conformite-tri.test.js — mêmes attendus que les harnais Python et PHP.
 */
(function (racine) {
  const MIME = racine.IAMailMime;

  const NIVEAU_RANG = { critique: 0, haute: 1, normale: 2, basse: 3 };
  const rangVersCode = r => Object.keys(NIVEAU_RANG).find(k => NIVEAU_RANG[k] === r) || "normale";
  const MOT = "[\\p{L}\\p{N}_]";            // \w de Python (Unicode)
  const echapper = s => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

  function sansAccents(s) { return (s || "").normalize("NFD").replace(/\p{Mn}/gu, ""); }
  function normTexte(s) {                   // _semantique : minuscule, NFD sans marques, apostrophes → espace
    let t = sansAccents((s || "").toLowerCase());
    t = t.replace(/['’‘`´]/g, " ");
    return t.replace(/\s+/g, " ");
  }

  const cacheRe = new Map();
  function reMot(mm) {
    let r = cacheRe.get(mm);
    if (!r) { r = new RegExp("(?<!" + MOT + ")" + echapper(mm) + "(?!" + MOT + ")", "u"); cacheRe.set(mm, r); }
    return r;
  }
  function contientMot(texte, mots) {
    for (const m of mots) { const mm = normTexte(m).trim(); if (mm && reMot(mm).test(texte)) return true; }
    return false;
  }
  function motMatche(texte, mots) {
    let meilleur = null;
    for (const m of mots) {
      const mm = normTexte(m).trim();
      if (mm && reMot(mm).test(texte) && (meilleur === null || mm.length > meilleur.length)) meilleur = mm;
    }
    return meilleur;
  }
  function domaineContient(dom, tokens) {
    if (!dom) return false;
    const labels = dom.split(".");
    for (let t of tokens) {
      t = sansAccents(String(t).toLowerCase()).replace(/^[. ]+|[. ]+$/g, "");
      if (!t) continue;
      if (t.includes(".")) { if (dom === t || dom.endsWith("." + t)) return true; }
      else if (labels.includes(t)) return true;
    }
    return false;
  }
  function adressesSpec(valeur) {           // adresses nues, minuscules
    return MIME.adresses(valeur).map(a => a.adresse).filter(a => a && a.includes("@")).map(a => a.trim().toLowerCase());
  }

  // --- texte du corps (comme _corps_texte du compagnon) ------------------------------------
  const ENTITES = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " ", eacute: "é", egrave: "è",
    ecirc: "ê", euml: "ë", agrave: "à", acirc: "â", auml: "ä", ccedil: "ç", icirc: "î", iuml: "ï", ocirc: "ô",
    ouml: "ö", ugrave: "ù", ucirc: "û", uuml: "ü", Eacute: "É", Egrave: "È", Ecirc: "Ê", Agrave: "À", Ccedil: "Ç",
    oelig: "œ", OElig: "Œ", aelig: "æ", euro: "€", laquo: "«", raquo: "»", rsquo: "’", lsquo: "‘", ldquo: "“",
    rdquo: "”", hellip: "…", ndash: "–", mdash: "—", bull: "•", middot: "·", copy: "©", reg: "®", trade: "™",
    deg: "°", sect: "§", shy: "­", zwnj: "‌", zwj: "‍", thinsp: " ", ensp: " ", emsp: " " };
  function deEntites(h) {
    return h.replace(/&(#x[0-9a-fA-F]+|#\d+|[A-Za-z][A-Za-z0-9]*);?/g, (tout, e) => {
      if (e[0] === "#") {
        const n = e[1] === "x" || e[1] === "X" ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10);
        try { return n > 0 && n < 0x110000 ? String.fromCodePoint(n) : "�"; } catch (x) { return "�"; }
      }
      return Object.prototype.hasOwnProperty.call(ENTITES, e) ? ENTITES[e] : tout;
    });
  }
  function htmlVersTexte(h) {
    h = h.replace(/<(script|style)[\s\S]*?<\/\1>/gi, " ");
    h = h.replace(/<br\s*\/?>/gi, "\n");
    h = h.replace(/<\/(p|div|tr|li|h[1-6])>/gi, "\n");
    h = h.replace(/<[^>]+>/g, " ");
    return deEntites(h);
  }
  function corpsTexte(msg, maxlen) {
    if (!maxlen) return "";
    const plain = [], html = [];
    for (const p of MIME.parcourir(msg)) {
      if (p.disposition === "attachment") continue;
      if (p.parties.length) continue;
      if (p.type === "text/plain") plain.push(MIME.texteDePartie(p));
      else if (p.type === "text/html") html.push(MIME.texteDePartie(p));
    }
    let t = plain.filter(Boolean).join("\n").trim();
    if (!t) t = htmlVersTexte(html.filter(Boolean).join("\n"));
    t = t.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
    t = t.replace(/[ \t]+/g, " ");
    t = t.replace(/^ +| +$/gm, "");
    t = t.replace(/\n{3,}/g, "\n\n").trim();
    return Array.from(t).slice(0, maxlen).join("");
  }

  async function sha256Hex(texte) {
    const h = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(texte));
    return Array.from(new Uint8Array(h), b => b.toString(16).padStart(2, "0")).join("");
  }

  // --- moteur ------------------------------------------------------------------------------
  function creerMoteur(regles, taxonomie, version) {
    const r = regles || {}, tx = taxonomie || {};
    const set = k => new Set(r[k] || []);
    const C = {
      EXPEDITEURS_AUTO: set("expediteurs_auto"), FREEMAIL: set("freemail"),
      MARKETING_LABELS: set("marketing_labels"), LOCAL_AUTO_INDICES: r.local_auto_indices || [],
      LOCAL_GENERIQUE: set("local_generique"), SUJETS_IMPORTANTS: r.sujets_importants || [],
      DOMAINES_IMPORTANTS: r.domaines_importants || [],
      REGEX_LOCAL_HUMAIN: new RegExp(r.regex_local_humain || "^[a-z]+([._-][a-z]+)+$"),
      MARKETING_INDICES_LABEL: r.marketing_indices_label || [], PRECEDENCE_BULK: r.precedence_bulk || [],
      CATEGORIES_IMPORTANTES: r.categories_importantes || [], MOTS_SUSPECTS: r.mots_suspects || [],
      STRATEGIE_CATEGORIE: r.strategie_categorie || "mot_le_plus_long",
      REGLES_CATEGORIE: (r.regles_categorie || []).map(x => [x[0], x[1]]),
      DOMAINES_CATEGORIE: (r.domaines_categorie || []).map(x => [x[0], x[1]]),
      FIL_PREFIXES: r.fil_prefixes || ["re", "ref", "rep", "fwd", "fw", "tr", "aw", "wg"],
      EXTRAIT_MAX: parseInt((r.parametres || {}).ia_extrait_max || 4000, 10),
    };
    const CAT = {};
    for (const c of tx.categories || []) CAT[c.code] = c;
    const SIGNAUX = {};
    for (const n of ["critique", "haute", "basse"]) SIGNAUX[n] = ((tx.signaux || {})[n] || []).slice();
    const alt = C.FIL_PREFIXES.map(echapper).sort((a, b) => b.length - a.length).join("|");
    const PREFIXE_FIL = new RegExp("^(?:\\s*(?:" + alt + ")\\s*:\\s*)+", "i");

    function sujetFil(sujet) {
      let s = normTexte(sujet);
      s = s.replace(PREFIXE_FIL, "");
      s = s.replace(/\[[^\]]*\]/g, " ");
      return s.replace(/\s+/g, " ").trim();
    }
    function humain(deSpec, local, dom) {
      if (!deSpec) return false;
      if (C.EXPEDITEURS_AUTO.has(local) || C.LOCAL_GENERIQUE.has(local)) return false;
      if (C.LOCAL_AUTO_INDICES.some(t => local.includes(t))) return false;
      if (C.FREEMAIL.has(dom)) return true;
      return C.REGEX_LOCAL_HUMAIN.test(local);
    }
    function marketing(dom, labels) {
      if (!dom || C.FREEMAIL.has(dom)) return false;
      if (labels.some(l => C.MARKETING_LABELS.has(l))) return true;
      return labels.some(l => C.MARKETING_INDICES_LABEL.some(ind => l.includes(ind)));
    }
    function estImportant(texteEntete, categorie, deSpec, dom, importants) {
      if (C.CATEGORIES_IMPORTANTES.includes(categorie)) return true;
      if (deSpec && (importants.has(deSpec) || importants.has(dom))) return true;
      if (domaineContient(dom, C.DOMAINES_IMPORTANTS)) return true;
      return motMatche(texteEntete, C.SUJETS_IMPORTANTS) !== null;
    }

    // regles._algorithme, étapes 1 à 10 (même lecture que classer() du compagnon).
    function classer(h, extrait, mesAdresses, importantsListe) {
      const de = h.from || "";
      const deSpec = adressesSpec(de)[0] || "";
      let local = "", dom = "";
      if (deSpec) { const i = deSpec.indexOf("@"); local = deSpec.slice(0, i); dom = deSpec.slice(i + 1).toLowerCase(); }
      const labels = dom ? dom.split(".") : [];
      const texteEntete = normTexte((h.subject || "") + " " + de);
      const texteComplet = normTexte((h.subject || "") + " " + (extrait || ""));
      const importants = new Set((importantsListe || []).map(x => String(x).toLowerCase()));

      const estHumain = humain(deSpec, local, dom);
      const robot = !deSpec || C.EXPEDITEURS_AUTO.has(local) || C.LOCAL_AUTO_INDICES.some(t => local.includes(t));
      const estMarketing = marketing(dom, labels);
      const bulk = !!((h.list_unsubscribe || "").trim() || (h.list_id || "").trim()
        || C.PRECEDENCE_BULK.includes((h.precedence || "").trim().toLowerCase())
        || C.EXPEDITEURS_AUTO.has(local) || estMarketing);
      const mes = new Set((mesAdresses || []).map(m => String(m).toLowerCase()));
      const to = adressesSpec(h.to), cc = adressesSpec(h.cc);
      const pourMoi = to.some(a => mes.has(a)) ? 1 : (cc.some(a => mes.has(a)) ? 0 : null);
      const replyTo = adressesSpec(h.reply_to)[0] || "";
      const domReply = replyTo.includes("@") ? replyTo.split("@")[1].toLowerCase() : "";
      const suspect = !!(domReply && deSpec && domReply !== dom && motMatche(texteEntete, C.MOTS_SUSPECTS) !== null);

      let categorie = null, meilleur = -1;
      for (const [code, mots] of C.REGLES_CATEGORIE) {
        if (!CAT[code]) continue;
        for (const m of mots) {
          if (contientMot(texteEntete, [m])) {
            const long = normTexte(m).trim().length;
            if (C.STRATEGIE_CATEGORIE !== "mot_le_plus_long") { if (categorie === null) categorie = code; }
            else if (long > meilleur) { categorie = code; meilleur = long; }
          }
        }
      }
      if (categorie === null) {
        for (const [code, tokens] of C.DOMAINES_CATEGORIE) {
          if (CAT[code] && domaineContient(dom, tokens)) { categorie = code; break; }
        }
      }
      if (categorie === null && bulk && CAT.newsletter && !estImportant(texteEntete, "autre", deSpec, dom, importants)) categorie = "newsletter";
      if (categorie === null) categorie = "autre";
      const important = estImportant(texteEntete, categorie, deSpec, dom, importants);

      const signaux = [];
      for (const niveau of ["critique", "haute", "basse"]) {
        for (const expr of SIGNAUX[niveau] || []) {
          if (contientMot(texteComplet, [expr])) {
            const effet = niveau === "basse" ? "baisser" : "monter";
            signaux.push({ expression: expr, niveau, effet, compte: effet === "baisser" || (!robot && !bulk) });
          }
        }
      }
      let urgence = (CAT[categorie] || {}).urgence_defaut || "normale";
      const montent = signaux.filter(s => s.effet === "monter" && s.compte);
      const baissent = signaux.filter(s => s.effet === "baisser");
      if (montent.length) urgence = rangVersCode(Math.min(NIVEAU_RANG[urgence], ...montent.map(s => NIVEAU_RANG[s.niveau])));
      else if (baissent.length) urgence = rangVersCode(Math.max(NIVEAU_RANG[urgence], ...baissent.map(s => NIVEAU_RANG[s.niveau])));
      if (pourMoi !== 1 && NIVEAU_RANG[urgence] < NIVEAU_RANG.normale) urgence = "normale";
      if (bulk && !important) urgence = "basse";
      if (important && urgence === "basse") urgence = "normale";

      return {
        categorie, pour_moi: pourMoi, expediteur_humain: estHumain ? 1 : 0, bulk: bulk ? 1 : 0,
        important: important ? 1 : 0, suspect: suspect ? 1 : 0, urgence,
        signaux: signaux.map(s => s.expression).sort((a, b) => (a < b ? -1 : a > b ? 1 : 0)),
      };
    }

    // Message-ID du moteur (_semantique.message_id).
    async function messageId(msg) {
      const brut = (msg.get("message-id") || "").replace(/[<>]/g, "").trim();
      if (brut) return brut;
      const dateBrut = (msg.get("date") || "").trim();
      const de = adressesSpec(MIME.decoderEntete(msg.get("from")))[0] || "";
      const sujet = MIME.decoderEntete(msg.get("subject")) || "";
      return "synth-" + (await sha256Hex(dateBrut + "|" + de + "|" + sujet)).slice(0, 32);
    }

    // Lecture complète d'un message brut : clé, en-têtes, extraction, tri_local.
    async function lire(brut, mesAdresses, importants) {
      const msg = MIME.analyser(brut);
      const d = k => MIME.decoderEntete(msg.get(k)) || "";
      const h = { from: d("from"), to: d("to"), cc: d("cc"), subject: d("subject"), reply_to: d("reply-to"),
        list_unsubscribe: d("list-unsubscribe"), list_id: d("list-id"), precedence: d("precedence") };
      const extrait = corpsTexte(msg, C.EXTRAIT_MAX);
      const tri = classer(h, extrait, mesAdresses, importants);
      const cle_fil = sujetFil(h.subject);
      const nomEtAdr = v => MIME.adresses(v).filter(a => a.adresse.includes("@"))
        .map(a => (a.nom ? a.nom + " <" + a.adresse + ">" : a.adresse));
      return {
        message_id: await messageId(msg),
        extraction: { de: adressesSpec(h.from)[0] || "", a: adressesSpec(h.to), cc: adressesSpec(h.cc), cle_fil, extrait },
        entetes: { de: h.from, a: nomEtAdr(h.to), cc: nomEtAdr(h.cc), objet: h.subject, date: (msg.get("date") || "").trim() },
        tri_local: Object.assign({}, tri, { cle_fil }),
        date: msg.get("date"),
      };
    }

    return { version: version || "?", classer, lire, sujetFil, messageId };
  }

  racine.IAMailTri = { creerMoteur, normTexte, sansAccents };
})(typeof globalThis !== "undefined" ? globalThis : this);

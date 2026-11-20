"use strict";
/*
 * mime.js — lecture d'un message MIME brut (octets), sans dépendance.
 *
 * Sert au tri local (tri.js) : en-têtes décodés (RFC 2047), adresses, texte du corps.
 * Suit le comportement du harnais Python du compagnon (module email) sur les points qui
 * comptent pour le moteur : dépliage des en-têtes, mots encodés, parcours des parties
 * (pièces jointes sautées, message/rfc822 parcouru), base64 / quoted-printable, jeux de
 * caractères. Tourne tel quel dans Thunderbird et sous Node (tests).
 */
(function (racine) {

  const enc = new TextEncoder();

  function octetsVersBinaire(o) {          // octets -> chaîne « binaire » (1 caractère = 1 octet)
    let s = "";
    for (let i = 0; i < o.length; i += 0x8000) s += String.fromCharCode.apply(null, o.subarray(i, i + 0x8000));
    return s;
  }
  function binaireVersOctets(s) {
    const o = new Uint8Array(s.length);
    for (let i = 0; i < s.length; i++) o[i] = s.charCodeAt(i) & 0xff;
    return o;
  }

  // Décode des octets dans un jeu de caractères ; null si le jeu est inconnu (comme Python,
  // où un LookupError fait tomber la partie).
  function decoder(octets, charset) {
    let cs = (charset || "utf-8").trim().toLowerCase().replace(/^"|"$/g, "");
    if (cs === "unknown-8bit" || cs === "x-unknown" || cs === "8bit") cs = "utf-8";
    try { return new TextDecoder(cs, { fatal: false }).decode(octets); }
    catch (e) { return null; }
  }

  // Texte d'un en-tête brut (octets non ASCII éventuels) : UTF-8 si valide, sinon latin-1.
  function texteEnteteBrut(bin) {
    if (!/[\x80-\xff]/.test(bin)) return bin;
    try { return new TextDecoder("utf-8", { fatal: true }).decode(binaireVersOctets(bin)); }
    catch (e) { return bin; }
  }

  // --- base64 / quoted-printable ---------------------------------------------------------
  const B64 = (() => {
    const t = new Int16Array(256).fill(-1);
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/".split("")
      .forEach((c, i) => { t[c.charCodeAt(0)] = i; });
    t["-".charCodeAt(0)] = 62; t["_".charCodeAt(0)] = 63;
    return t;
  })();
  function base64Octets(bin) {             // tolérant : ignore tout ce qui n'est pas base64
    const out = [];
    let acc = 0, n = 0;
    for (let i = 0; i < bin.length; i++) {
      const c = bin.charCodeAt(i);
      if (c === 61) break;                  // '='
      const v = c < 256 ? B64[c] : -1;
      if (v < 0) continue;
      acc = (acc << 6) | v; n += 6;
      if (n >= 8) { n -= 8; out.push((acc >> n) & 0xff); }
    }
    return Uint8Array.from(out);
  }
  function qpOctets(bin, entete) {
    if (entete) bin = bin.replace(/_/g, " ");
    const out = [];
    for (let i = 0; i < bin.length; i++) {
      const c = bin[i];
      if (c === "=") {
        if (bin[i + 1] === "\r" && bin[i + 2] === "\n") { i += 2; continue; }
        if (bin[i + 1] === "\n") { i += 1; continue; }
        const h = bin.substr(i + 1, 2);
        if (/^[0-9A-Fa-f]{2}$/.test(h)) { out.push(parseInt(h, 16)); i += 2; continue; }
        out.push(61);
      } else out.push(bin.charCodeAt(i) & 0xff);
    }
    return Uint8Array.from(out);
  }

  // --- en-têtes ----------------------------------------------------------------------------
  // RFC 2047 : =?charset?B|Q?texte?= ; l'espace entre deux mots encodés est retiré.
  const MOT_ENCODE = /=\?([^?\s]+)\?([BbQq])\?([^?]*)\?=/g;
  function decoderEntete(valeur) {
    if (!valeur) return "";
    let s = String(valeur);
    if (s.indexOf("=?") >= 0) {
      s = s.replace(/(=\?[^?\s]+\?[BbQq]\?[^?]*\?=)\s+(?==\?[^?\s]+\?[BbQq]\?[^?]*\?=)/g, "$1");
      s = s.replace(MOT_ENCODE, (tout, cs, mode, txt) => {
        const octets = mode.toUpperCase() === "B" ? base64Octets(txt) : qpOctets(txt, true);
        const r = decoder(octets, cs.split("*")[0]);
        return r === null ? decoder(octets, "utf-8") : r;
      });
    }
    return s.replace(/\r/g, " ").replace(/\n/g, " ").trim();
  }

  function lireEntetes(bin) {
    // Dépliage : CRLF/LF suivi d'un blanc = continuation (le blanc est gardé).
    const lignes = bin.split(/\r?\n/);
    const entetes = [];
    for (const l of lignes) {
      if (/^[ \t]/.test(l) && entetes.length) { entetes[entetes.length - 1][1] += "\n" + l; continue; }
      const p = l.indexOf(":");
      if (p <= 0) continue;
      entetes.push([l.slice(0, p).trim().toLowerCase(), l.slice(p + 1).replace(/^[ \t]+/, "")]);
    }
    for (const e of entetes) e[1] = texteEnteteBrut(e[1]).replace(/[ \t\r\n]+$/, "");
    return entetes;
  }

  // Paramètres d'un en-tête structuré (Content-Type, Content-Disposition), RFC 2231 simple.
  function parametres(valeur) {
    const v = valeur || "";
    const i = v.indexOf(";");
    const principal = (i < 0 ? v : v.slice(0, i)).trim().toLowerCase();
    const params = {};
    const re = /;\s*([^=\s;]+)\s*=\s*("(?:[^"\\]|\\.)*"|[^;]*)/g;
    let m;
    while ((m = re.exec(v))) {
      let cle = m[1].toLowerCase(), val = m[2].trim();
      if (val.startsWith('"')) val = val.slice(1, -1).replace(/\\(.)/g, "$1");
      const etoile = cle.match(/^([^*]+)\*(\d+)?\*?$/);
      if (etoile) {
        cle = etoile[1];
        const m2 = val.match(/^([^']*)'[^']*'(.*)$/);
        if (m2) val = decoder(binaireVersOctets(decodeURIComponentBinaire(m2[2])), m2[1] || "utf-8") || m2[2];
        params[cle] = (params[cle] || "") + val;
      } else if (!(cle in params)) params[cle] = val;
    }
    return { valeur: principal, params };
  }
  function decodeURIComponentBinaire(s) {
    return s.replace(/%([0-9A-Fa-f]{2})/g, (_, h) => String.fromCharCode(parseInt(h, 16)));
  }

  // --- message -----------------------------------------------------------------------------
  function analyser(entree, typeParDefaut) {
    const bin = typeof entree === "string" ? entree : octetsVersBinaire(entree);
    const m = bin.match(/\r?\n\r?\n/);
    const brutEntetes = m ? bin.slice(0, m.index) : bin;
    const corps = m ? bin.slice(m.index + m[0].length) : "";
    const entetes = lireEntetes(brutEntetes);
    const msg = {
      entetes,
      get(nom) { const n = nom.toLowerCase(); const e = entetes.find(x => x[0] === n); return e ? e[1] : null; },
      corpsBinaire: corps,
      parties: [],
    };
    const ct = parametres(msg.get("content-type") || "");
    msg.type = ct.valeur && ct.valeur.indexOf("/") > 0 ? ct.valeur : (typeParDefaut || "text/plain");
    msg.charset = ct.params.charset || null;
    const cd = parametres(msg.get("content-disposition") || "");
    msg.disposition = cd.valeur || null;
    msg.nomFichier = cd.params.filename || ct.params.name || null;

    if (msg.type.startsWith("multipart/") && ct.params.boundary) {
      const defaut = msg.type === "multipart/digest" ? "message/rfc822" : "text/plain";
      for (const p of decouper(corps, ct.params.boundary)) msg.parties.push(analyser(p, defaut));
    } else if (msg.type === "message/rfc822") {
      msg.parties.push(analyser(corps));
    }
    return msg;
  }

  function decouper(corps, frontiere) {
    const lignes = corps.split(/(\r?\n)/);
    const parts = [];
    let courant = null;
    const debut = "--" + frontiere, fin = debut + "--";
    for (let i = 0; i < lignes.length; i += 2) {
      const ligne = lignes[i], sep = lignes[i + 1] || "";
      const nette = ligne.replace(/[ \t]+$/, "");
      if (nette === fin) { if (courant !== null) parts.push(courant); courant = null; break; }
      if (nette === debut) { if (courant !== null) parts.push(courant); courant = []; continue; }
      if (courant !== null) courant.push(ligne + sep);
    }
    if (courant !== null) parts.push(courant);
    // Le saut de ligne qui précède une frontière lui appartient.
    return parts.map(p => p.join("").replace(/\r?\n$/, ""));
  }

  function* parcourir(msg) {               // équivalent de Message.walk()
    yield msg;
    for (const p of msg.parties) yield* parcourir(p);
  }

  function chargeOctets(part) {            // get_payload(decode=True)
    const cte = (part.get("content-transfer-encoding") || "").trim().toLowerCase();
    if (cte === "base64") return base64Octets(part.corpsBinaire);
    if (cte === "quoted-printable") return qpOctets(part.corpsBinaire, false);
    return binaireVersOctets(part.corpsBinaire);
  }

  function texteDePartie(part) {
    try {
      const r = decoder(chargeOctets(part), part.charset || "utf-8");
      return r === null ? "" : r;
    } catch (e) { return ""; }
  }

  // --- adresses (équivalent pragmatique de email.utils.getaddresses) ------------------------
  function adresses(valeur) {
    const s = String(valeur || "");
    const morceaux = [];
    let cur = "", guillemets = false, chevrons = 0, parent = 0;
    for (let i = 0; i < s.length; i++) {
      const c = s[i];
      if (c === "\\" && (guillemets || parent)) { cur += c + (s[i + 1] || ""); i++; continue; }
      if (c === '"' && !parent) guillemets = !guillemets;
      else if (!guillemets) {
        if (c === "(") parent++;
        else if (c === ")" && parent) parent--;
        else if (c === "<" && !parent) chevrons++;
        else if (c === ">" && !parent && chevrons) chevrons--;
        else if ((c === "," || c === ";") && !parent && !chevrons) { morceaux.push(cur); cur = ""; continue; }
        else if (c === ":" && !parent && !chevrons && !/@/.test(cur)) { cur = ""; continue; } // nom de groupe
      }
      cur += c;
    }
    morceaux.push(cur);
    const out = [];
    for (let m of morceaux) {
      let nom = "";
      const commentaires = [];
      m = m.replace(/\((?:[^()\\]|\\.)*\)/g, x => { commentaires.push(x.slice(1, -1)); return " "; });
      const a = m.match(/<([^>]*)>/);
      let adr;
      if (a) {
        adr = a[1].replace(/^@[^:]*:/, "");
        nom = m.slice(0, a.index).trim().replace(/^"|"$/g, "").replace(/\\(.)/g, "$1");
      } else {
        adr = m.replace(/"(?:[^"\\]|\\.)*"/g, x => x).trim();
        nom = commentaires.join(" ").trim();
      }
      adr = adr.replace(/\s+/g, "");
      if (adr || nom) out.push({ nom, adresse: adr });
    }
    return out;
  }

  racine.IAMailMime = { analyser, parcourir, texteDePartie, decoderEntete, adresses, parametres,
    octetsVersBinaire, binaireVersOctets, base64Octets, enc };
})(typeof globalThis !== "undefined" ? globalThis : this);

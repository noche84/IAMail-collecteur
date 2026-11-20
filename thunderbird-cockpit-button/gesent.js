"use strict";
/*
 * gesent.js — client du protocole d'ingestion GesEnt v1 (docs/iaemail-contrat-ingestion-v1.md
 * côté GesEnt, ADR 0051). Indépendant de Thunderbird : tout ce qui touche au client mail passe
 * par l'adaptateur `tb`, le réseau par `fetch`, la persistance par `stockage` ; ainsi le même
 * code tourne dans l'extension et dans les tests (faux GesEnt, faux Thunderbird).
 *
 * Ce que fait le client :
 *  - appairage (code à 8 caractères) → jeton, portée (boîtes), étiquette ;
 *  - GET /moi au démarrage et régulièrement (portée, relie, plus_ancien_recu) ;
 *  - moteur servi par GesEnt (GET /moteur + fichiers, empreintes sha256 vérifiées) ;
 *  - file PERSISTANTE : chaque mail est préparé dès son arrivée (source brute gzip+base64,
 *    sha256, tri_local) et envoyé en lot (≤ 100 messages, ≤ 25 Mo, regroupés ≤ 30 s), dans
 *    l'ordre d'arrivée ; un lot interrompu est renvoyé avec le MÊME lot_id (idempotent) ;
 *  - gros messages (> 5 Mo compressés) : annoncés (a_televerser) puis PUT /sources ;
 *  - rattrapage de l'existant APRÈS les arrivées, du plus récent au plus ancien, repris à
 *    plus_ancien_recu ;
 *  - synchro « Traité » avec GesEnt pour les boîtes reliées (pour-client / confirme /
 *    depuis-client) ; gestes (supprime, deplace, traite, non_traite) au journal sinon ;
 *  - politique d'erreurs du contrat § 6 (5xx : 30 s → 15 min ; 429 : Retry-After ; 401 :
 *    arrêt et nouvel appairage ; 403 : rafraîchir la portée ; 409 moteur : télécharger).
 */
(function (racine) {
  const PROTOCOLE = 1;
  const MAX_MESSAGES = 100;
  const MAX_LOT_OCTETS = 24 * 1024 * 1024;           // marge sous les 25 Mo du contrat
  const SEUIL_TELEVERSER = 5 * 1024 * 1024;          // > 5 Mo compressé → PUT /sources
  const MAX_SOURCE = 150 * 1024 * 1024;
  const REGROUPEMENT_MS = 10 * 1000;                  // « au plus 30 s » : on envoie après 10 s
  const DELAIS_MS = [30e3, 60e3, 120e3, 300e3, 600e3, 900e3];
  const SYNC_MAX = 500;
  const FICHIERS_MOTEUR = ["regles.json", "taxonomie.json"];
  const MIME = racine.IAMailMime, TRI = racine.IAMailTri;

  // --- utilitaires --------------------------------------------------------------------------
  async function sha256Hex(octets) {
    const h = await crypto.subtle.digest("SHA-256", octets);
    return Array.from(new Uint8Array(h), b => b.toString(16).padStart(2, "0")).join("");
  }
  async function gzip(octets) {
    const cs = new CompressionStream("gzip");
    const w = cs.writable.getWriter();
    w.write(octets); w.close();
    return new Uint8Array(await new Response(cs.readable).arrayBuffer());
  }
  async function gunzip(octets) {
    const ds = new DecompressionStream("gzip");
    const w = ds.writable.getWriter();
    w.write(octets); w.close();
    return new Uint8Array(await new Response(ds.readable).arrayBuffer());
  }
  const enBase64 = o => btoa(MIME.octetsVersBinaire(o));
  const deBase64 = s => MIME.binaireVersOctets(atob(s));
  function isoLocal(d) {                          // 2026-11-20T08:13:58+01:00
    const p = n => String(Math.abs(n)).padStart(2, "0");
    const o = -d.getTimezoneOffset();
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) + "T" + p(d.getHours()) + ":"
      + p(d.getMinutes()) + ":" + p(d.getSeconds()) + (o >= 0 ? "+" : "-") + p(Math.trunc(o / 60)) + ":" + p(o % 60);
  }
  const cleRef = (b, m) => b + "\u0000" + m;
  const sansChevrons = s => String(s || "").replace(/[<>\s]/g, "");

  const ETAT_VIDE = () => ({
    instance: null, jeton: null, collecteur_id: null, statut: "non_relie",   // non_relie | actif | revoque | protocole
    boites: [], etiquette: null, moteur: null,
    file: [], lotEnCours: null, attenteJusqua: 0, essais: 0,
    rattrapage: {}, etiquetes: {}, synth: {}, journal: [], rejets: [], dernierContact: null,
    envoyes: {}, balayage: null,
  });

  class ClientGesEnt {
    constructor(env) {
      this.env = env;                  // { fetch, stockage, tb, maintenant, version, type }
      this.etat = ETAT_VIDE();
      this.moteur = null;              // moteur de tri JavaScript (tri.js) chargé
      this.minuterie = null;
      this.occupe = false;
      this.jetons = { n: 100, t: 0 };  // ≤ 100 requêtes / minute (le contrat en permet 120)
      this.ignorer = new Map();        // tbId → échéance : nos propres poses d'étiquette
    }

    // --- persistance ------------------------------------------------------------------------
    async charger() {
      const r = await this.env.stockage.get("gesent");
      this.etat = Object.assign(ETAT_VIDE(), r && r.gesent || {});
      this._monterMoteur();
      return this.etat;
    }
    async sauver() { await this.env.stockage.set({ gesent: this.etat }); }
    maintenant() { return this.env.maintenant ? this.env.maintenant() : new Date(); }
    noter(niveau, texte) {
      this.etat.journal.unshift({ le: isoLocal(this.maintenant()), niveau, texte });
      this.etat.journal.length = Math.min(this.etat.journal.length, 60);
    }
    estRelie() { return this.etat.statut === "actif" && !!this.etat.jeton; }
    boitesReliees() { return this.estRelie() ? this.etat.boites.filter(b => b.relie) : []; }
    _etiquete(cle, oui) {             // messages que CE poste a étiquetés « Traité » (garde anti-course)
      if (oui) this.etat.etiquetes[cle] = 1; else delete this.etat.etiquetes[cle];
      const cles = Object.keys(this.etat.etiquetes);
      if (cles.length > 20000) for (const k of cles.slice(0, cles.length - 20000)) delete this.etat.etiquetes[k];
    }
    _envoye(cle) {                    // arrivées déjà reçues par GesEnt (balayage sans doublon)
      this.etat.envoyes[cle] = 1;
      const cles = Object.keys(this.etat.envoyes);
      if (cles.length > 30000) for (const k of cles.slice(0, cles.length - 30000)) delete this.etat.envoyes[k];
    }
    boite(id) { return this.etat.boites.find(b => b.boite_id === id) || null; }

    // --- HTTP -------------------------------------------------------------------------------
    async _jeton() {                    // seau de jetons : lissage sous la limite du contrat
      for (;;) {
        const t = this.maintenant().getTime();
        this.jetons.n = Math.min(100, this.jetons.n + (t - this.jetons.t) * 100 / 60000);
        this.jetons.t = t;
        if (this.jetons.n >= 1) { this.jetons.n -= 1; return; }
        await (this.env.dormir || (ms => new Promise(r => setTimeout(r, ms))))(700);
      }
    }
    async requete(methode, chemin, { json, brut, entetes, sansJeton } = {}) {
      await this._jeton();
      const h = Object.assign({ "Accept": "application/json",
        "User-Agent": "IAMail-" + (this.env.type || "thunderbird") + "/" + (this.env.version || "0") }, entetes || {});
      if (!sansJeton) h["Authorization"] = "Bearer " + this.etat.jeton;
      let corps;
      if (json !== undefined) { h["Content-Type"] = "application/json"; corps = JSON.stringify(json); }
      else if (brut !== undefined) corps = brut;
      const url = this.etat.instance.replace(/\/+$/, "") + "/api/iaemail/v1" + chemin;
      let rep;
      try { rep = await this.env.fetch(url, { method: methode, headers: h, body: corps }); }
      catch (e) { return { statut: 0, reseau: true, erreur: { code: "reseau", message: String(e && e.message || e) } }; }
      const texte = await rep.text();
      let donnees = null;
      try { donnees = texte ? JSON.parse(texte) : null; } catch (e) { donnees = null; }
      const ra = rep.headers.get("Retry-After");
      this.etat.dernierContact = isoLocal(this.maintenant());
      return { statut: rep.status, corps: donnees, texte, etag: rep.headers.get("ETag"),
        retryAfter: ra ? (/^\d+$/.test(ra) ? +ra * 1000 : Math.max(0, Date.parse(ra) - this.maintenant().getTime())) : null,
        erreur: donnees && donnees.erreur || null };
    }

    // Politique commune (contrat § 6) pour une réponse non 2xx. Renvoie « reessayer » si la
    // même opération doit être retentée plus tard, « abandonner » sinon.
    async _subirErreur(r, quoi) {
      const code = r.erreur && r.erreur.code;
      if (r.reseau || r.statut >= 500 || r.statut === 429) {
        const delai = r.retryAfter != null ? r.retryAfter : DELAIS_MS[Math.min(this.etat.essais, DELAIS_MS.length - 1)];
        this.etat.essais++;
        this.etat.attenteJusqua = this.maintenant().getTime() + delai;
        this.noter("attente", `${quoi} : ${r.reseau ? "GesEnt injoignable" : "HTTP " + r.statut} — nouvel essai dans ${Math.round(delai / 1000)} s`);
        return "reessayer";
      }
      if (r.statut === 401) {
        this.etat.statut = "revoque";
        this.noter("erreur", `${quoi} : ${r.erreur && r.erreur.message || "jeton refusé"} Reliez de nouveau ce poste.`);
        return "arret";
      }
      if (r.statut === 403) {
        this.noter("erreur", `${quoi} : ${r.erreur && r.erreur.message || "accès refusé"}`);
        await this.rafraichir();
        return code === "module_inactif" ? "arret" : "abandonner";
      }
      if (r.statut === 409 && code === "moteur_incompatible") {
        this.noter("info", `${quoi} : moteur à mettre à jour (${(r.erreur.details || {}).version_serveur || "?"})`);
        await this.chargerMoteur(true);
        return "reessayer_maintenant";
      }
      if (r.statut === 409 && code === "protocole_incompatible") {
        this.etat.statut = "protocole";
        this.noter("erreur", "Protocole GesEnt plus récent : mettez à jour l'extension IAMail.");
        return "arret";
      }
      this.noter("erreur", `${quoi} : HTTP ${r.statut} ${code || ""} ${r.erreur && r.erreur.message || ""}`.trim());
      return "abandonner";
    }

    // --- appairage, portée, moteur -----------------------------------------------------------
    async appairer(instance, code, comptesLocaux) {
      this.etat.instance = String(instance).trim().replace(/\/+$/, "");
      if (!/^https?:\/\//.test(this.etat.instance)) this.etat.instance = "https://" + this.etat.instance;
      const tb = this.env.tb;
      const r = await this.requete("POST", "/appairage", { sansJeton: true, json: {
        code: String(code).trim().toUpperCase(),
        collecteur: { type: this.env.type || "thunderbird", version: this.env.version || "0",
          machine: (tb.machine && await tb.machine()) || undefined, utilisateur_os: (tb.utilisateurOs && await tb.utilisateurOs()) || undefined },
        comptes_locaux: comptesLocaux,
      } });
      if (r.statut !== 201 && r.statut !== 200) {
        const m = { code_inconnu: "Code inconnu.", code_expire: "Ce code a expiré : créez-en un autre dans GesEnt.",
          code_utilise: "Ce code a déjà servi.", requete_invalide: "Requête refusée par GesEnt." };
        throw new Error((r.erreur && (m[r.erreur.code] || r.erreur.message)) || (r.reseau ? "GesEnt injoignable : vérifiez l'adresse." : "HTTP " + r.statut));
      }
      const c = r.corps;
      Object.assign(this.etat, { jeton: c.jeton, collecteur_id: c.collecteur_id, statut: "actif",
        boites: c.boites || [], etiquette: c.etiquette || null, essais: 0, attenteJusqua: 0, rattrapage: {},
        balayage: this.etat.balayage || isoLocal(this.maintenant()) });
      this.noter("info", `Relié à ${this.etat.instance} (${this.etat.boites.length} boîte(s)).`);
      await this.sauver();
      await this.rafraichir();
      await this.chargerMoteur(true);
      return this.etat;
    }

    async delier() {
      const garde = { journal: this.etat.journal };
      this.etat = Object.assign(ETAT_VIDE(), garde);
      this.moteur = null;
      this.noter("info", "Poste délié de GesEnt (retour au fonctionnement autonome).");
      await this.sauver();
    }

    async rafraichir() {
      if (!this.etat.jeton) return false;
      const r = await this.requete("GET", "/moi");
      if (r.statut === 200 && r.corps) {
        const anciens = new Map(this.etat.boites.map(b => [b.boite_id, b]));
        this.etat.boites = (r.corps.boites || []).map(b => Object.assign({}, anciens.get(b.boite_id) || {}, b));
        if (r.corps.etiquette) this.etat.etiquette = r.corps.etiquette;
        if (this.etat.statut === "revoque") this.etat.statut = "actif";
        if (this.moteur && r.corps.moteur && r.corps.moteur.version !== this.etat.moteur.version) await this.chargerMoteur(false);
        await this.sauver();
        return true;
      }
      if (r.statut === 401) { await this._subirErreur(r, "GET /moi"); await this.sauver(); }
      return false;
    }

    _monterMoteur() {
      const m = this.etat.moteur;
      this.moteur = m && m.regles && m.taxonomie ? TRI.creerMoteur(m.regles, m.taxonomie, m.version) : null;
    }

    async chargerMoteur(forcer) {
      const actuel = this.etat.moteur;
      const r = await this.requete("GET", "/moteur", { entetes: !forcer && actuel && actuel.etag ? { "If-None-Match": actuel.etag } : {} });
      if (r.statut === 304) return true;
      if (r.statut !== 200 || !r.corps || !r.corps.fichiers) { if (r.statut) await this._subirErreur(r, "GET /moteur"); return false; }
      const nouveau = { version: r.corps.version, etag: r.etag, fichiers: r.corps.fichiers };
      for (const nom of FICHIERS_MOTEUR) {
        const f = await this.requete("GET", "/moteur/fichiers/" + encodeURIComponent(nom));
        if (f.statut !== 200) { this.noter("erreur", `moteur : ${nom} indisponible (HTTP ${f.statut})`); return false; }
        const emp = await sha256Hex(new TextEncoder().encode(f.texte));
        if (emp !== r.corps.fichiers[nom]) {
          this.noter("erreur", `moteur : empreinte de ${nom} différente du manifeste, fichier refusé`);
          return false;
        }
        nouveau[nom === "regles.json" ? "regles" : "taxonomie"] = JSON.parse(f.texte);
      }
      this.etat.moteur = nouveau;
      this._monterMoteur();
      this.noter("info", `Moteur ${nouveau.version} chargé depuis GesEnt (empreintes vérifiées).`);
      await this.sauver();
      return true;
    }

    // --- boîtes ↔ comptes Thunderbird ---------------------------------------------------------
    async boitePourCompte(accountId) {
      const comptes = await this.env.tb.comptes();
      const c = comptes.find(x => x.id === accountId);
      if (!c) return null;
      const adresses = new Set((c.identites || []).map(i => String(i.email || "").toLowerCase()));
      return this.etat.boites.find(b => adresses.has(String(b.adresse).toLowerCase())) || null;
    }
    async comptesDeBoite(b) {
      const comptes = await this.env.tb.comptes();
      return comptes.filter(c => (c.identites || []).some(i => String(i.email || "").toLowerCase() === String(b.adresse).toLowerCase())).map(c => c.id);
    }
    async mesAdresses(accountId) {
      const c = (await this.env.tb.comptes()).find(x => x.id === accountId);
      return c ? (c.identites || []).map(i => String(i.email || "").toLowerCase()).filter(Boolean) : [];
    }

    // --- préparation d'un message ---------------------------------------------------------------
    async preparer(entete, boite, brutDonne) {
      const brut = brutDonne || await this.env.tb.lireBrut(entete.id);
      if (!brut) return null;
      const lu = await this.moteur.lire(brut, await this.mesAdresses(entete.compte), []);
      const gz = await gzip(brut);
      const source = { format: "eml", origine: "brut", sha256: await sha256Hex(brut), taille: brut.length };
      const m = {
        boite_id: boite.boite_id, message_id: lu.message_id,
        recu_le: isoLocal(entete.date || this.maintenant()),
        source,
        tri_local: lu.tri_local,
        etat_local: { dossier: entete.dossier || "INBOX", lu: !!entete.lu,
          etiquette_traite: !!(this.etat.etiquette && (entete.tags || []).includes(this.etat.etiquette.cle)) },
      };
      if (gz.length > SEUIL_TELEVERSER) {
        source.a_televerser = true;
        m.entetes = lu.entetes;
      } else {
        source.encodage = "gzip+base64";
        // La source voyage à part dans le stockage : la file, réécrite à chaque arrivée,
        // reste légère même si GesEnt est injoignable pendant des heures.
        m.srcCle = "gesent_src_" + crypto.randomUUID();
        m.srcOctets = Math.ceil(gz.length / 3) * 4;
        await this.env.stockage.set({ [m.srcCle]: enBase64(gz) });
      }
      if (lu.message_id.startsWith("synth-")) {                         // Thunderbird ne connaît pas cette clé
        this.etat.synth[lu.message_id] = entete.id;
        const cles = Object.keys(this.etat.synth);
        if (cles.length > 5000) for (const k of cles.slice(0, cles.length - 5000)) delete this.etat.synth[k];
      }
      return m;
    }

    // Arrivée d'un message dans Thunderbird : préparé tout de suite (le mail peut être supprimé
    // dans les secondes qui suivent : il doit quand même être archivé, recette n° 7).
    async surArrivee(entete) {
      if (!this.estRelie()) return false;
      const b = await this.boitePourCompte(entete.compte);
      if (!b) return false;
      if (!this.moteur) {                       // moteur pas encore chargé (GesEnt injoignable au démarrage)
        this.etat.file.push({ type: "a_preparer", e: Object.assign({}, entete, { date: entete.date ? entete.date.toISOString() : null }) });
        await this.sauver();
        this.planifier(REGROUPEMENT_MS);
        return true;
      }
      const m = await this.preparer(entete, b);
      if (!m) return false;
      if (this.etat.envoyes[cleRef(m.boite_id, m.message_id)]
          || this.etat.file.some(x => x.type === "message" && x.m.boite_id === m.boite_id && x.m.message_id === m.message_id)) {
        await this._oublierSources([{ type: "message", m }]);
        return true;
      }
      this.etat.file.push({ type: "message", mode: "arrivee", tbId: entete.id, compte: entete.compte, m });
      await this.sauver();
      this.planifier(REGROUPEMENT_MS);
      return true;
    }

    // Arrivées reçues avant que le moteur soit là : préparées maintenant, à leur place dans la file.
    async _preparerEnAttente() {
      for (let i = 0; i < this.etat.file.length; i++) {
        const it = this.etat.file[i];
        if (it.type !== "a_preparer") continue;
        const e = Object.assign({}, it.e, { date: it.e.date ? new Date(it.e.date) : null });
        const b = await this.boitePourCompte(e.compte);
        const m = b ? await this.preparer(e, b) : null;
        if (m) this.etat.file[i] = { type: "message", mode: "arrivee", tbId: e.id, compte: e.compte, m };
        else { this.etat.file.splice(i, 1); i--; }
      }
    }

    planifier(ms) {
      if (this.minuterie || this.env.sansMinuterie) return;
      this.minuterie = setTimeout(() => { this.minuterie = null; this.pomper().catch(e => this.noter("erreur", String(e))); }, ms);
    }

    arreter() { if (this.minuterie) clearTimeout(this.minuterie); this.minuterie = null; }

    // --- gestes et état depuis le client ------------------------------------------------------------
    async _refDepuisEntete(entete) {
      const b = await this.boitePourCompte(entete.compte);
      if (!b) return null;
      const synth = Object.keys(this.etat.synth).find(k => this.etat.synth[k] === entete.id);
      return { boite: b, ref: { boite_id: b.boite_id, message_id: synth || sansChevrons(entete.messageIdTb) } };
    }
    async _geste(ref, geste, details) {
      const tb = this.env.tb;
      const g = { boite_id: ref.boite_id, message_id: ref.message_id, geste, le: isoLocal(this.maintenant()) };
      const u = tb.utilisateurOs && await tb.utilisateurOs(), mc = tb.machine && await tb.machine();
      if (u) g.utilisateur = String(u).slice(0, 100);
      if (mc) g.machine = String(mc).slice(0, 100);
      if (details) g.details = details;
      this.etat.file.push({ type: "geste", g });
    }

    // Étiquette « Traité » posée ou retirée À LA MAIN dans Thunderbird.
    // Boîte reliée : l'étiquette est celle de GesEnt ; sinon `cleLocale` (celle du cockpit local).
    async surEtiquette(entete, cleLocale) {
      if (!this.estRelie() || !this.etat.etiquette) return null;
      const t = this.ignorer.get(entete.id);
      if (t && t > this.maintenant().getTime()) return null;           // écho de notre propre pose
      const x = await this._refDepuisEntete(entete);
      if (!x) return null;
      const cle = cleRef(x.ref.boite_id, x.ref.message_id);
      const posee = (entete.tags || []).includes(x.boite.relie ? this.etat.etiquette.cle : (cleLocale || this.etat.etiquette.cle));
      const avant = !!this.etat.etiquetes[cle];
      if (posee === avant) return null;                                 // autre étiquette modifiée
      this._etiquete(cle, posee);
      if (x.boite.relie) this.etat.file.push({ type: "etat", liste: posee ? "traites" : "non_traites", ref: x.ref });
      else await this._geste(x.ref, posee ? "traite" : "non_traite");
      await this.sauver();
      this.planifier(2000);
      return posee ? "traite" : "non_traite";
    }

    // Supprimé ou déplacé hors de la boîte : rangé (relié) ou geste au journal (non relié).
    async surRangement(entete, { supprime, vers } = {}) {
      if (!this.estRelie()) return null;
      const x = await this._refDepuisEntete(entete);
      if (!x) return null;
      if (x.boite.relie) this.etat.file.push({ type: "etat", liste: "ranges", ref: x.ref });
      else await this._geste(x.ref, supprime ? "supprime" : "deplace", supprime ? undefined : { vers: vers || "" });
      await this.sauver();
      this.planifier(2000);
      return supprime ? "supprime" : "deplace";
    }

    // --- pompe : arrivées, sources, état, puis rattrapage ---------------------------------------
    async pomper() {
      if (this.occupe || !this.estRelie()) return;
      if (this.maintenant().getTime() < this.etat.attenteJusqua) {
        this.planifier(this.etat.attenteJusqua - this.maintenant().getTime() + 50);
        return;
      }
      this.occupe = true;
      try {
        if (!this.moteur && !(await this.chargerMoteur(true))) return;
        await this._preparerEnAttente();
        for (let tours = 0; tours < 50; tours++) {
          if (this.etat.statut !== "actif" || this.maintenant().getTime() < this.etat.attenteJusqua) break;
          let fait = await this._envoyerLot();
          if (fait === null) fait = await this._envoyerSource();
          if (fait === null) fait = await this._envoyerEtat();
          if (fait === null) fait = await this._rattraperUnPas();
          if (fait === null) break;                                       // rien à faire
          if (fait === false) break;                                      // en attente / arrêt
          if (fait === "rattrapage") { this.planifier(3000); break; }      // faible cadence
        }
      } finally {
        this.occupe = false;
        await this.sauver();
      }
      if (this.etat.statut === "actif" && this.maintenant().getTime() < this.etat.attenteJusqua) {
        this.planifier(this.etat.attenteJusqua - this.maintenant().getTime() + 50);
      }
    }

    async _donnees(m) {
      if (!m.srcCle) return null;
      const r = await this.env.stockage.get(m.srcCle);
      return (r && r[m.srcCle]) || null;
    }
    async _oublierSources(items) {
      const cles = items.filter(i => i.type === "message" && i.m.srcCle).map(i => i.m.srcCle);
      if (cles.length && this.env.stockage.remove) await this.env.stockage.remove(cles);
    }
    async _composerLot(items) {
      const messages = [];
      for (const it of items.filter(i => i.type === "message")) {
        const { srcCle, srcOctets, ...m } = it.m;
        if (srcCle) {
          const d = await this._donnees(it.m);
          if (!d) { it.perdu = true; continue; }                          // stockage effacé : on ne peut rien envoyer
          m.source = Object.assign({}, m.source, { donnees: d });
        }
        messages.push(m);
      }
      return {
        protocole: PROTOCOLE, lot_id: this.etat.lotEnCours.lot_id, mode: this.etat.lotEnCours.mode,
        emis_le: this.etat.lotEnCours.emis_le, moteur_version: this.etat.moteur.version,
        messages, gestes: items.filter(i => i.type === "geste").map(i => i.g),
      };
    }

    // Prend la tête de la file (messages d'un même mode + gestes), dans l'ordre d'arrivée.
    _choisirTete() {
      const pris = [];
      let octets = 0, n = 0, mode = null;
      for (const it of this.etat.file) {
        if (it.type !== "message" && it.type !== "geste") continue;
        if (it.type === "message") {
          if (mode && it.mode !== mode) break;
          const taille = (it.m.srcOctets || 0) + 2048;
          if (n >= MAX_MESSAGES || (n && octets + taille > MAX_LOT_OCTETS)) break;
          mode = it.mode; octets += taille; n++;
          pris.push(it);
        } else {
          if (pris.filter(x => x.type === "geste").length >= 1000) break;
          pris.push(it);
        }
      }
      return { pris, mode: mode || "arrivee" };
    }

    async _envoyerLot() {
      if (!this.etat.lotEnCours) {
        const { pris, mode } = this._choisirTete();
        if (!pris.length) return null;
        const ids = new Set(pris);
        this.etat.file = this.etat.file.filter(x => !ids.has(x));
        this.etat.lotEnCours = { lot_id: crypto.randomUUID(), mode, emis_le: isoLocal(this.maintenant()), items: pris };
        await this.sauver();                                              // avant l'envoi : même lot_id si coupure
      }
      const lot = this.etat.lotEnCours;
      const corps = await this._composerLot(lot.items);
      if (lot.items.some(i => i.perdu)) {
        this.noter("erreur", "source d'un message introuvable dans le stockage local : message retiré de la file");
        lot.items = lot.items.filter(i => !i.perdu);
        if (!lot.items.length) { this.etat.lotEnCours = null; return true; }
        return true;                                                       // recomposé au tour suivant (même lot_id)
      }
      const r = await this.requete("POST", "/lots", { json: corps });
      if (r.statut === 200 && r.corps) {
        this.etat.essais = 0;
        const rejetes = new Set((r.corps.rejets || []).filter(x => x.type === "message").map(x => x.index));
        const messages = lot.items.filter(i => i.type === "message");
        messages.forEach((it, i) => {
          if (rejetes.has(i)) return;
          if (it.mode === "arrivee") this._envoye(cleRef(it.m.boite_id, it.m.message_id));
          if (it.m.source.a_televerser) this.etat.file.push({ type: "source", ref: { boite_id: it.m.boite_id, message_id: it.m.message_id }, tbId: it.tbId, sha256: it.m.source.sha256, taille: it.m.source.taille, essais: 0 });
        });
        for (const rej of r.corps.rejets || []) {
          const it = rej.type === "message" ? messages[rej.index] : lot.items.filter(i => i.type === "geste")[rej.index];
          const ref = it ? (it.m || it.g) : {};
          if (rej.type === "message" && it && it.mode === "arrivee") this._envoye(cleRef(it.m.boite_id, it.m.message_id));   // pas réessayé tel quel
          this.etat.rejets.unshift({ le: isoLocal(this.maintenant()), type: rej.type, code: rej.code, message: rej.message, message_id: ref.message_id });
          this.noter("erreur", `rejet ${rej.type} ${ref.message_id || ""} : ${rej.code}`);
        }
        this.etat.rejets.length = Math.min(this.etat.rejets.length, 100);
        if (lot.mode === "rattrapage" && lot.curseur) {
          const st = this.etat.rattrapage[lot.boite_id] || (this.etat.rattrapage[lot.boite_id] = {});
          st.curseur = lot.curseur;
        }
        const nv = r.corps.messages || {};
        this.noter("info", `lot ${lot.mode} : ${nv.nouveaux || 0} nouveau(x), ${nv.deja_connus || 0} déjà connu(s)${r.corps.deja_recu ? " (déjà reçu)" : ""}`);
        await this._oublierSources(lot.items);
        this.etat.lotEnCours = null;
        return true;
      }
      if (r.statut === 413) {                                             // découpe : un message seul au besoin
        const msgs = lot.items.filter(i => i.type === "message");
        const gestes = lot.items.filter(i => i.type !== "message");
        if (msgs.length === 1 && !gestes.length) {
          this.noter("erreur", `message trop volumineux même seul (${msgs[0].m.message_id}) : retiré de la file`);
          await this._oublierSources(msgs);
          this.etat.lotEnCours = null;
          return true;
        }
        const garde = new Set(msgs.length > 1 ? msgs.slice(0, Math.ceil(msgs.length / 2))
          : msgs.length === 1 ? msgs : gestes.slice(0, Math.ceil(gestes.length / 2)));
        this.etat.file = lot.items.filter(i => !garde.has(i)).concat(this.etat.file);
        lot.items = lot.items.filter(i => garde.has(i));
        lot.lot_id = crypto.randomUUID();
        return true;
      }
      const action = await this._subirErreur(r, "POST /lots");
      if (action === "reessayer_maintenant") {
        for (const it of lot.items) if (it.type === "message") await this._retrier(it);
        lot.lot_id = crypto.randomUUID();                                  // contenu changé : nouveau lot
        return true;
      }
      if (action === "abandonner") {
        if (lot.mode === "rattrapage" && lot.curseur) (this.etat.rattrapage[lot.boite_id] || {}).curseur = lot.curseur;
        for (const it of lot.items) this.etat.rejets.unshift({ le: isoLocal(this.maintenant()), type: it.type, code: (r.erreur || {}).code || ("http_" + r.statut), message_id: (it.m || it.g || {}).message_id });
        await this._oublierSources(lot.items);
        this.etat.lotEnCours = null;
        return true;
      }
      return false;                                                       // reessayer plus tard / arrêt
    }

    // Moteur changé (409) : le témoin est recalculé à partir de la source gardée dans la file.
    async _retrier(it) {
      if (!this.moteur) return;
      let brut = null;
      const d = await this._donnees(it.m);
      if (d) brut = await gunzip(deBase64(d));
      else if (it.tbId != null) brut = await this.env.tb.lireBrut(it.tbId);
      if (!brut) return;
      const lu = await this.moteur.lire(brut, await this.mesAdresses(it.compte), []);
      it.m.tri_local = lu.tri_local;
    }

    async _envoyerSource() {
      const i = this.etat.file.findIndex(x => x.type === "source");
      if (i < 0) return null;
      const s = this.etat.file[i];
      const tb = this.env.tb;
      let brut = s.tbId != null ? await tb.lireBrut(s.tbId) : null;
      if (!brut || (await sha256Hex(brut)) !== s.sha256) {
        const autres = await tb.chercherParMessageId(s.ref.message_id, await this.comptesDeBoite(this.boite(s.ref.boite_id) || { adresse: "" }));
        brut = null;
        for (const e of autres) { const b = await tb.lireBrut(e.id); if (b && (await sha256Hex(b)) === s.sha256) { brut = b; break; } }
      }
      if (!brut) {
        this.etat.file.splice(i, 1);
        this.noter("erreur", `source introuvable dans Thunderbird pour ${s.ref.message_id} : GesEnt l'attendra`);
        return true;
      }
      if (brut.length > MAX_SOURCE) {
        this.etat.file.splice(i, 1);
        this.noter("erreur", `source de plus de 150 Mo (${s.ref.message_id}) : non envoyée`);
        return true;
      }
      const q = "?boite_id=" + encodeURIComponent(s.ref.boite_id) + "&message_id=" + encodeURIComponent(s.ref.message_id);
      const r = await this.requete("PUT", "/sources" + q, { brut, entetes: { "Content-Type": "message/rfc822", "Content-SHA256": s.sha256 } });
      if (r.statut === 201 || r.statut === 200) {
        this.etat.essais = 0;
        this.etat.file.splice(i, 1);
        this.noter("info", `gros message téléversé (${Math.round(brut.length / 1048576)} Mo) : ${s.ref.message_id}`);
        return true;
      }
      if (r.statut === 404 && s.essais < 5) {                              // annonce pas encore enregistrée
        s.essais++;
        this.etat.file.push(this.etat.file.splice(i, 1)[0]);
        this.etat.attenteJusqua = this.maintenant().getTime() + 60e3;
        return false;
      }
      if ([404, 409, 413, 422].includes(r.statut)) {
        this.etat.file.splice(i, 1);
        this.etat.rejets.unshift({ le: isoLocal(this.maintenant()), type: "source", code: (r.erreur || {}).code || ("http_" + r.statut), message_id: s.ref.message_id });
        this.noter("erreur", `PUT /sources ${s.ref.message_id} : ${(r.erreur || {}).code || r.statut} — non réessayé`);
        return true;
      }
      const action = await this._subirErreur(r, "PUT /sources");
      if (action === "abandonner") { this.etat.file.splice(i, 1); return true; }
      return false;
    }

    async _envoyerEtat() {
      const items = this.etat.file.filter(x => x.type === "etat").slice(0, SYNC_MAX);
      if (!items.length) return null;
      const corps = { traites: [], non_traites: [], ranges: [] };
      for (const it of items) corps[it.liste].push(it.ref);
      const r = await this.requete("POST", "/sync/depuis-client", { json: corps });
      if (r.statut === 200) {
        this.etat.essais = 0;
        const pris = new Set(items);
        this.etat.file = this.etat.file.filter(x => !pris.has(x));
        for (const ig of (r.corps && r.corps.ignores) || []) this.noter("info", `GesEnt a ignoré ${ig.ref && ig.ref.message_id} (${ig.raison})`);
        return true;
      }
      const action = await this._subirErreur(r, "POST /sync/depuis-client");
      if (action === "abandonner") { const pris = new Set(items); this.etat.file = this.etat.file.filter(x => !pris.has(x)); return true; }
      return false;
    }

    // --- balayage : les arrivées que Thunderbird n'a pas signalées -------------------------------
    // onNewMailReceived ne voit pas tout : mail relevé pendant que l'extension dormait, mail
    // déjà lu sur un téléphone (plus « nouveau » pour IMAP), poste délié puis relié… Le
    // balayage reprend la dernière heure avant le précédent passage (30 jours au plus) et met
    // en file ce qui n'a pas encore été envoyé. Le rattrapage, lui, ne va que vers le passé.
    async balayer() {
      if (!this.estRelie() || !this.moteur) return 0;
      const maintenant = this.maintenant().getTime();
      const precedent = this.etat.balayage ? Date.parse(this.etat.balayage) : maintenant;
      const depuis = new Date(Math.max(precedent - 3600e3, maintenant - 30 * 86400e3));
      let n = 0;
      for (const b of this.etat.boites.slice()) {
        const comptes = await this.comptesDeBoite(b);
        if (!comptes.length) continue;
        for (const e of await this.env.tb.messagesEntre(comptes, depuis, new Date(maintenant + 60e3))) {
          const mid = sansChevrons(e.messageIdTb);
          if (mid && this.etat.envoyes[cleRef(b.boite_id, mid)]) continue;
          if (this.etat.file.some(x => x.type === "message" && x.tbId === e.id)) continue;
          const avant = this.etat.file.length;
          await this.surArrivee(e);
          if (this.etat.file.length > avant) n++;
        }
      }
      this.etat.balayage = isoLocal(new Date(maintenant));
      if (n) this.noter("info", `balayage : ${n} mail(s) arrivé(s) sans signal de Thunderbird, mis en file`);
      await this.sauver();
      return n;
    }

    // --- rattrapage de l'existant (après les arrivées, du plus récent au plus ancien) ------------
    async _rattraperUnPas() {
      for (const b0 of this.etat.boites.slice()) {
        const st = this.etat.rattrapage[b0.boite_id] || (this.etat.rattrapage[b0.boite_id] = {});
        if (st.fini) continue;
        // Premier pas : plus_ancien_recu à jour (reprise d'un rattrapage interrompu, ou arrivées
        // déjà envoyées) pour ne pas renvoyer ce que GesEnt a déjà.
        if (!st.curseur && !(await this.rafraichir())) return false;
        const b = this.boite(b0.boite_id);
        if (!b) continue;
        const comptes = await this.comptesDeBoite(b);
        if (!comptes.length) continue;
        const serveur = b.plus_ancien_recu ? Date.parse(b.plus_ancien_recu) : null;
        let curseur = st.curseur ? Date.parse(st.curseur) : this.maintenant().getTime();
        if (serveur && serveur < curseur) curseur = serveur;               // reprise à plus_ancien_recu
        let fenetre = st.fenetreJours || 30;
        const plancher = Date.parse("1995-01-01T00:00:00Z");
        for (let essais = 0; essais < 12 && curseur > plancher; essais++) {
          const debut = new Date(curseur - fenetre * 86400e3);
          const entetes = (await this.env.tb.messagesEntre(comptes, debut, new Date(curseur)))
            .filter(e => e.date && e.date.getTime() <= curseur)
            .sort((x, y) => y.date - x.date);
          const pris = entetes.slice(0, MAX_MESSAGES);
          if (!pris.length) {
            curseur = debut.getTime();
            fenetre = Math.min(fenetre * 2, 730);
            st.curseur = isoLocal(new Date(curseur)); st.fenetreJours = fenetre;
            continue;
          }
          const items = [];
          for (const e of pris) {
            const m = await this.preparer(e, b);
            if (m) items.push({ type: "message", mode: "rattrapage", tbId: e.id, compte: e.compte, m });
          }
          // Fenêtre pleine : on reprend au plus ancien envoyé (les messages de cette même
          // seconde reviennent en « déjà connus », sans doublon) ; sinon la fenêtre est épuisée.
          const ancien = pris[pris.length - 1].date.getTime();
          const prochain = pris.length === MAX_MESSAGES ? (ancien < curseur ? ancien : ancien - 1000) : debut.getTime();
          st.fenetreJours = 30;
          this.etat.lotEnCours = { lot_id: crypto.randomUUID(), mode: "rattrapage", emis_le: isoLocal(this.maintenant()),
            items, boite_id: b.boite_id, curseur: isoLocal(new Date(prochain)) };
          await this.sauver();
          const ok = await this._envoyerLot();
          return ok === true ? "rattrapage" : ok;
        }
        if (curseur <= plancher || fenetre >= 730) {
          st.fini = true;
          this.noter("info", `rattrapage terminé pour ${b.adresse}`);
        }
        return true;
      }
      return null;
    }

    // --- synchro « Traité » : GesEnt fait foi sur les boîtes reliées --------------------------------
    async synchroTraite() {
      if (!this.boitesReliees().length || this.maintenant().getTime() < this.etat.attenteJusqua) return null;
      const tb = this.env.tb;
      let bilan = { marques: 0, demarques: 0 };
      for (let tour = 0; tour < 20; tour++) {
        const r = await this.requete("GET", "/sync/pour-client");
        if (r.statut !== 200 || !r.corps) { await this._subirErreur(r, "GET /sync/pour-client"); break; }
        const et = r.corps.etiquette || this.etat.etiquette;
        if (et) { this.etat.etiquette = et; await tb.assurerEtiquette(et); }
        const marques = [], demarques = [];
        for (const [liste, poser, sortie] of [["a_marquer", true, marques], ["a_demarquer", false, demarques]]) {
          for (const ref of r.corps[liste] || []) {
            const b = this.boite(ref.boite_id);
            if (!b) continue;
            const ids = this.etat.synth[ref.message_id] != null ? [{ id: this.etat.synth[ref.message_id] }]
              : await tb.chercherParMessageId(ref.message_id, await this.comptesDeBoite(b));
            let ok = false;
            for (const e of ids) {
              this.ignorer.set(e.id, this.maintenant().getTime() + 15000);
              if (await tb.poserEtiquette(e.id, et.cle, poser)) ok = true;
            }
            if (ok) {
              sortie.push({ boite_id: ref.boite_id, message_id: ref.message_id });
              const cle = cleRef(ref.boite_id, ref.message_id);
              this._etiquete(cle, poser);
            }
          }
        }
        if (marques.length || demarques.length) {
          const c = await this.requete("POST", "/sync/confirme", { json: { marques, demarques } });
          if (c.statut !== 204 && c.statut !== 200) { await this._subirErreur(c, "POST /sync/confirme"); break; }
          bilan.marques += marques.length; bilan.demarques += demarques.length;
        }
        const pleine = (r.corps.a_marquer || []).length >= SYNC_MAX || (r.corps.a_demarquer || []).length >= SYNC_MAX;
        if (!pleine || (!marques.length && !demarques.length)) break;
      }
      await this.sauver();
      return bilan;
    }
  }

  racine.IAMailGesEnt = { ClientGesEnt, isoLocal, sha256Hex, gzip, gunzip, enBase64, deBase64, SEUIL_TELEVERSER };
})(typeof globalThis !== "undefined" ? globalThis : this);

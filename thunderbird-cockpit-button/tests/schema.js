"use strict";
// Mini-validateur JSON Schema (sous-ensemble 2020-12 utilisé par les schémas du contrat
// GesEnt : type, properties, required, enum, const, $ref local, items, maxItems, minimum,
// minLength, maxLength, pattern, format date-time, oneOf, anyOf, allOf, not, if/then/else).
// Renvoie la liste des erreurs (vide = conforme).

function valider(schema, valeur, racine = schema, chemin = "$") {
  const err = [];
  const s = schema.$ref ? resoudre(racine, schema.$ref) : schema;
  if (schema.$ref && Object.keys(schema).length > 1) err.push(...valider(Object.assign({}, schema, { $ref: undefined }), valeur, racine, chemin));
  if (s === true || s === undefined) return err;
  const type = v => v === null ? "null" : Array.isArray(v) ? "array" : Number.isInteger(v) ? "integer" : typeof v;
  if (s.type) {
    const t = [].concat(s.type), vt = type(valeur);
    if (!t.some(x => x === vt || (x === "number" && vt === "integer"))) { err.push(`${chemin}: type ${vt} ≠ ${t}`); return err; }
  }
  if (s.enum && !s.enum.some(e => JSON.stringify(e) === JSON.stringify(valeur))) err.push(`${chemin}: ${JSON.stringify(valeur)} hors enum`);
  if ("const" in s && JSON.stringify(s.const) !== JSON.stringify(valeur)) err.push(`${chemin}: ≠ const ${JSON.stringify(s.const)}`);
  if (typeof valeur === "string") {
    if (s.minLength != null && [...valeur].length < s.minLength) err.push(`${chemin}: trop court`);
    if (s.maxLength != null && [...valeur].length > s.maxLength) err.push(`${chemin}: trop long`);
    if (s.pattern && !new RegExp(s.pattern, "u").test(valeur)) err.push(`${chemin}: motif ${s.pattern}`);
    if (s.format === "date-time" && !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(Z|[+-]\d\d:\d\d)$/.test(valeur)) err.push(`${chemin}: date-time invalide`);
  }
  if (typeof valeur === "number" && s.minimum != null && valeur < s.minimum) err.push(`${chemin}: < ${s.minimum}`);
  if (Array.isArray(valeur)) {
    if (s.maxItems != null && valeur.length > s.maxItems) err.push(`${chemin}: plus de ${s.maxItems} éléments`);
    if (s.items) valeur.forEach((v, i) => err.push(...valider(s.items, v, racine, `${chemin}[${i}]`)));
  }
  if (valeur && typeof valeur === "object" && !Array.isArray(valeur)) {
    for (const k of s.required || []) if (!(k in valeur)) err.push(`${chemin}: clé requise « ${k} » absente`);
    for (const [k, sub] of Object.entries(s.properties || {})) if (k in valeur) err.push(...valider(sub, valeur[k], racine, `${chemin}.${k}`));
  }
  if (s.allOf) for (const x of s.allOf) err.push(...valider(x, valeur, racine, chemin));
  if (s.anyOf && !s.anyOf.some(x => !valider(x, valeur, racine, chemin).length)) err.push(`${chemin}: aucun anyOf`);
  if (s.oneOf) {
    const n = s.oneOf.filter(x => !valider(x, valeur, racine, chemin).length).length;
    if (n !== 1) err.push(`${chemin}: oneOf satisfait ${n} fois`);
  }
  if (s.not && !valider(s.not, valeur, racine, chemin).length) err.push(`${chemin}: not violé`);
  if (s.if) {
    const ok = !valider(s.if, valeur, racine, chemin).length;
    if (ok && s.then) err.push(...valider(s.then, valeur, racine, chemin));
    if (!ok && s.else) err.push(...valider(s.else, valeur, racine, chemin));
  }
  return err;
}

function resoudre(racine, ref) {
  if (!ref.startsWith("#/")) throw new Error("$ref non local : " + ref);
  return ref.slice(2).split("/").reduce((o, k) => o[k], racine);
}

module.exports = { valider };

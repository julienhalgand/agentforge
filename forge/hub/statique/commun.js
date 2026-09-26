// Fonctions partagées par les pages du hub. Vanilla JS, aucune dépendance.

// replaceChildren ignore null/undefined (sinon ils s'affichent en texte « null »).
const _replaceChildren = Element.prototype.replaceChildren;
Element.prototype.replaceChildren = function (...enfants) {
  return _replaceChildren.apply(this, enfants.flat().filter(e => e !== null && e !== undefined));
};

async function api(chemin, options = {}) {
  const reponse = await fetch(chemin, {
    headers: { "Content-Type": "application/json" },
    ...options,
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
  });
  const donnees = await reponse.json().catch(() => ({ erreur: { cause: `réponse non JSON (${reponse.status})`, remede: "" } }));
  // Une vraie erreur est un objet { cause, remede } ; un état de tâche en échec porte aussi un champ « erreur »
  // (nom du fichier .erreur.txt) qui n'en est pas une.
  const erreurReelle = donnees && typeof donnees.erreur === "object" && donnees.erreur !== null && "cause" in donnees.erreur;
  if (!reponse.ok || erreurReelle) {
    const e = erreurReelle ? donnees.erreur : { cause: `HTTP ${reponse.status}`, remede: "" };
    const erreur = new Error(e.cause);
    erreur.forge = e;
    throw erreur;
  }
  return donnees;
}

function texteErreur(e) {
  const f = e.forge || { cause: e.message, remede: "", details: [] };
  let t = f.cause;
  for (const d of f.details || []) t += `\n  - ${d}`;
  if (f.remede) t += `\nRemède : ${f.remede}`;
  return t;
}

function el(balise, attributs = {}, ...enfants) {
  const n = document.createElement(balise);
  for (const [k, v] of Object.entries(attributs)) {
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) n.setAttribute(k, v);
  }
  for (const e of enfants.flat()) if (e !== null && e !== undefined) n.append(e.nodeType ? e : document.createTextNode(String(e)));
  return n;
}

function dateCourte(ms) {
  if (!ms) return "jamais";
  const d = new Date(ms);
  return d.toLocaleDateString("fr-FR", { day: "2-digit", month: "2-digit" }) + " " + d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
}

function textePlanification(p) {
  if (!p || p.mode === "jamais") return "à la demande";
  if (p.mode === "quotidien") return `quotidien ${p.heure || "08:00"}`;
  if (p.mode === "toutes_les_n_heures") return `toutes les ${p.heures || 1} h`;
  return p.mode;
}

function texteEtat(x) {
  const e = x || {};
  const libelles = { succes: "succès", partiel: "partiel", echec: "échec", en_cours: "en cours", jamais_execute: "jamais exécuté" };
  return libelles[e.etat] || e.etat || "?";
}

function nomContrat(schema) {
  if (typeof schema === "string") return schema;
  if (schema && schema.$ref) return schema.$ref;
  if (schema && schema.title) return schema.title;
  return "schéma en ligne";
}


// Le dock, comme en bas d'un iPhone : présent sur toutes les pages.
function dockHTML(actif) {
  const items = [["accueil", "/", "🏠", "Accueil"], ["creer", "/creer", "✨", "Créer"], ["assembleur", "/assembleur", "🧩", "Assembler"], ["modele", "/brique/modele-local", "🧠", "Modèle"]];
  return `<nav class="dock">${items.map(([id, url, ic, nom]) => `<a href="${url}" class="dock-item${id === actif ? " actif" : ""}" title="${nom}"><span class="dock-icone">${ic}</span><span class="dock-nom">${nom}</span></a>`).join("")}</nav>`;
}

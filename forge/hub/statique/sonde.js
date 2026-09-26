// Sonde injectée par le hub dans les pages des briques créées par une phrase : elle rapporte au hub
// les erreurs JavaScript (et les clics sur des boutons qui ne déclenchent rien), pour les montrer à
// l'utilisateur et les renvoyer au modèle lors d'une correction.
(function () {
  const brique = location.pathname.split("/")[2];
  if (!brique) return;
  const envoyer = (e) => { try { fetch(`/api/pages-erreurs/${brique}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(e) }); } catch (_) {} };
  window.addEventListener("error", (ev) => envoyer({ type: "erreur", message: ev.message, ligne: ev.lineno, colonne: ev.colno, source: (ev.filename || "").split("/").pop() }));
  window.addEventListener("unhandledrejection", (ev) => envoyer({ type: "promesse", message: String(ev.reason && ev.reason.message || ev.reason) }));
  const consoleError = console.error.bind(console);
  console.error = (...a) => { envoyer({ type: "console.error", message: a.map(String).join(" ") }); consoleError(...a); };
  // un clic sur un bouton sans aucun gestionnaire : rien ne peut se passer, on le signale
  document.addEventListener("click", (ev) => {
    const b = ev.target.closest("button, [role=button]");
    if (!b) return;
    const aGestionnaire = b.onclick || b.getAttribute("onclick") || b.closest("form") || (typeof getEventListeners === "function");
    if (!aGestionnaire) {
      const vus = window.__sondeClics = window.__sondeClics || {};
      const cle = (b.id || b.textContent || "").trim().slice(0, 40);
      if (!vus[cle]) { vus[cle] = true; envoyer({ type: "clic-sans-effet", message: `clic sur « ${cle} » : aucun gestionnaire onclick trouvé sur ce bouton (les écouteurs addEventListener ne sont pas détectables ici ; si rien ne se passe, c'est probable)` }); }
    }
  }, true);
})();

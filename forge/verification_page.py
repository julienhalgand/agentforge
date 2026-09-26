"""Essai réel d'une page créée : ouverte dans un navigateur sans fenêtre, tous ses boutons cliqués, erreurs relevées.

Utilise le Chromium déjà présent sur la machine (Edge sous Windows, Chrome, Chromium) en mode
« headless » avec `--dump-dom` : aucune bibliothèque à installer. Une page d'essai charge la page
dans un cadre, capte ses erreurs JavaScript, clique sur chaque bouton, puis écrit le bilan dans son
propre document, que le navigateur imprime. Sans navigateur trouvé, l'essai est sauté et dit
pourquoi (jamais en silence).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CANDIDATS_WINDOWS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
]
CANDIDATS_UNIX = ["chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "brave-browser", "microsoft-edge"]


def trouver_navigateur() -> str | None:
    """Chemin d'un Chromium utilisable, ou None."""
    env = os.environ.get("FORGE_NAVIGATEUR")
    if env and Path(env).exists():
        return env
    if sys.platform.startswith("win"):
        for c in CANDIDATS_WINDOWS:
            if Path(c).exists():
                return c
    else:
        for c in CANDIDATS_UNIX:
            trouve = shutil.which(c)
            if trouve:
                return trouve
        for base in (Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")), Path.home() / ".cache" / "ms-playwright", Path("/opt/pw-browsers")):
            if base and base.is_dir():
                for p in sorted(base.glob("chromium-*/chrome-linux/chrome")) + sorted(base.glob("chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium")):
                    return str(p)
    return None


PRELUDE = """<script>
(function(){ const W = window; W.__forge = { erreurs: [], compteurs: { minuteries: 0, audio: 0, animations: 0, journal: 0 } };
  const noter = (type, message) => { if (!W.__forge.erreurs.some(e => e.message === message)) W.__forge.erreurs.push({ type, message }); };
  W.addEventListener("error", ev => noter("erreur", ev.message + (ev.lineno ? " (ligne " + ev.lineno + ")" : "")));
  W.addEventListener("unhandledrejection", ev => noter("promesse", String(ev.reason && ev.reason.message || ev.reason)));
  const ce = console.error.bind(console); console.error = (...a) => { noter("console.error", a.map(String).join(" ")); ce(...a); };
  const cl = console.log.bind(console); console.log = (...a) => { W.__forge.compteurs.journal++; cl(...a); };
  const st = W.setTimeout.bind(W), si = W.setInterval.bind(W), raf = W.requestAnimationFrame && W.requestAnimationFrame.bind(W);
  W.setTimeout = (...a) => { W.__forge.compteurs.minuteries++; return st(...a); };
  W.setInterval = (...a) => { W.__forge.compteurs.minuteries++; return si(...a); };
  const ct = W.clearTimeout.bind(W), ci = W.clearInterval.bind(W);
  W.clearTimeout = (...a) => { W.__forge.compteurs.minuteries++; return ct(...a); };
  W.clearInterval = (...a) => { W.__forge.compteurs.minuteries++; return ci(...a); };
  if (raf) W.requestAnimationFrame = (...a) => { W.__forge.compteurs.animations++; return raf(...a); };
  const AC = W.AudioContext || W.webkitAudioContext;
  if (AC) { const P = new Proxy(AC, { construct(t, a) { W.__forge.compteurs.audio++; return new t(...a); } }); W.AudioContext = P; W.webkitAudioContext = P;
    for (const m of ["close", "suspend", "resume"]) { const o = AC.prototype[m]; if (o) AC.prototype[m] = function (...a) { W.__forge.compteurs.audio++; return o.apply(this, a); }; }
    for (const [C, ms] of [[W.OscillatorNode, ["start", "stop"]], [W.AudioBufferSourceNode, ["start", "stop"]]]) { if (C) for (const m of ms) { const o = C.prototype[m]; if (o) C.prototype[m] = function (...a) { W.__forge.compteurs.audio++; return o.apply(this, a); }; } } }
  const HA = W.HTMLMediaElement; if (HA) for (const m of ["play", "pause"]) { const o = HA.prototype[m]; if (o) HA.prototype[m] = function (...a) { W.__forge.compteurs.audio++; return o.apply(this, a); }; }
  const A = W.Audio; if (A) { W.Audio = new Proxy(A, { construct(t, a) { W.__forge.compteurs.audio++; return new t(...a); } }); }
})();
</script>"""

PAGE_ESSAI = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<iframe id="cadre" style="width:900px;height:700px" srcdoc={contenu}></iframe>
<pre id="bilan"></pre>
<script>
// Tout se fait de façon synchrone pendant l'événement « load » : le navigateur imprime la page juste après,
// sans attendre les minuteries. Un clic est synchrone, ses erreurs aussi.
const bilan = {{ erreurs: [], boutons: [], clics: 0, charge: false, avertissements: [] }};
function noter(type, message) {{ if (!bilan.erreurs.some(e => e.message === message)) bilan.erreurs.push({{ type, message }}); }}
function finir() {{ document.getElementById("bilan").textContent = "BILAN-" + "FORGE:" + JSON.stringify(bilan) + ":FIN-" + "FORGE"; }}
window.addEventListener("load", () => {{
  const cadre = document.getElementById("cadre");
  let w, d;
  try {{ w = cadre.contentWindow; d = cadre.contentDocument; }} catch (e) {{ noter("acces", "page inaccessible : " + e.message); return finir(); }}
  if (!d || !d.body) {{ noter("vide", "la page n'a pas de corps"); return finir(); }}
  bilan.charge = true;
  const boutons = Array.from(d.querySelectorAll("button, [role=button], input[type=button], input[type=submit], [onclick]")).filter((b, i, t) => t.indexOf(b) === i);
  bilan.boutons = boutons.map(b => (b.id || b.textContent || b.value || "").trim().slice(0, 40));
  if (!boutons.length) bilan.avertissements.push("la page n'a aucun bouton : rien à essayer");
  const etat = () => JSON.stringify([w.__forge ? w.__forge.compteurs : null, d.documentElement.innerHTML.length, d.documentElement.innerHTML.slice(0, 4000)]);
  for (const b of boutons) {{
    const nom = (b.id || b.textContent || b.value || "?").trim().slice(0, 40); const avant = etat();
    try {{ b.click(); bilan.clics++; }} catch (e) {{ noter("clic", "clic sur « " + nom + " » : " + e.message); }}
    if (etat() === avant) noter("clic-sans-effet", "le clic sur « " + nom + " » ne fait rien : ni changement de la page, ni minuterie, ni son, ni animation — le bouton n'a probablement pas de gestionnaire, ou celui-ci ne fait rien");
  }}
  if (w.__forge) w.__forge.erreurs.forEach(e => noter(e.type, e.message));
  finir();
}});
</script></body></html>"""


def essayer_page(fichier_html: Path | str, delai_s: float = 25.0) -> dict:
    """Rend {essaye: bool, erreurs: [{type, message}], boutons: [...], clics: int, raison?: str}."""
    navigateur = trouver_navigateur()
    if not navigateur:
        return {"essaye": False, "erreurs": [], "boutons": [], "clics": 0, "raison": "aucun navigateur trouvé (Edge, Chrome ou Chromium) : essai de la page sauté ; indiquez-en un dans FORGE_NAVIGATEUR"}
    fichier_html = Path(fichier_html).resolve()
    with tempfile.TemporaryDirectory(prefix="essai-page-") as dossier:
        essai = Path(dossier) / "essai.html"
        contenu = fichier_html.read_text(encoding="utf-8", errors="replace")
        # la sonde est placée avant tout script de la page : les erreurs au chargement sont vues aussi
        if re.search(r"<head[^>]*>", contenu, re.I):
            contenu = re.sub(r"(<head[^>]*>)", r"\1" + PRELUDE.replace("\\", "\\\\"), contenu, count=1, flags=re.I)
        else:
            contenu = PRELUDE + contenu
        import html as mod_html

        essai.write_text(PAGE_ESSAI.format(contenu='"' + mod_html.escape(contenu, quote=True) + '"'), encoding="utf-8")
        commande = [navigateur, "--headless=new", "--disable-gpu", "--no-sandbox", "--allow-file-access-from-files", "--autoplay-policy=no-user-gesture-required",
                    "--disable-dev-shm-usage", "--no-first-run", "--disable-extensions", f"--user-data-dir={dossier}/profil", "--dump-dom", essai.as_uri()]
        try:
            termine = subprocess.run(commande, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=delai_s)
        except subprocess.TimeoutExpired:
            return {"essaye": False, "erreurs": [], "boutons": [], "clics": 0, "raison": f"le navigateur n'a pas répondu en {delai_s:.0f} s"}
        except OSError as exc:
            return {"essaye": False, "erreurs": [], "boutons": [], "clics": 0, "raison": f"impossible de lancer {navigateur} : {exc}"}
    trouves = re.findall(r"BILAN-FORGE:(.*?):FIN-FORGE", termine.stdout, re.S)
    m = trouves[-1] if trouves else None
    if not m:
        return {"essaye": False, "erreurs": [], "boutons": [], "clics": 0, "raison": "le navigateur n'a pas rendu de bilan (" + (termine.stderr.strip().splitlines() or ["?"])[-1][:160] + ")"}
    try:
        import html as mod_html

        bilan = json.loads(mod_html.unescape(m))
    except json.JSONDecodeError:
        return {"essaye": False, "erreurs": [], "boutons": [], "clics": 0, "raison": "bilan illisible"}
    return {"essaye": True, **bilan}


def resume_essai(bilan: dict) -> list[str]:
    """Les échecs à renvoyer au modèle (vide = la page a passé l'essai)."""
    if not bilan.get("essaye"):
        return []
    return [f"{e['type']} : {e['message']}" for e in bilan.get("erreurs", [])]

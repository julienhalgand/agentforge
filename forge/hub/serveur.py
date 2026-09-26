"""Hub web local (http://localhost:8700) : bibliothèque standard seulement.

Routes :
  GET  /                          tuiles (briques, pipelines, état, dernier rapport)
  GET  /assembleur                assembler un pipeline sans code
  GET  /vue/<nom>                 vue composée (plusieurs cadres sur une page)
  GET  /api/etat                  briques, pipelines, contrats, planifications, exécutions
  GET  /api/executions/<cible>    état détaillé d'une exécution
  POST /api/executer/<cible>      met en file (verrou anti-doublon)
  POST /api/appeler/<brique>/<service>   appel direct (corps JSON), synchrone
  POST /api/verifier              vérifie un pipeline JSON sans rien lancer
  POST /api/pipelines             enregistre un pipeline JSON (~/.agentforge/pipelines/)
  POST /api/planification/<cible> remplace la planification
  POST /api/supprimer/<brique>    vers la corbeille
  POST /api/ouvrir-editeur/<brique>
  POST /api/vues                  enregistre une vue composée
  GET  /rapports/<brique>/        liste des rapports ; /rapports/<brique>/<fichier> les sert
  GET  /pages/<brique>/<chemin>   page interactive de la brique (forme B)
"""

from __future__ import annotations

import json
import mimetypes
import os
import shutil
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from .. import manifeste as mod_manifeste
from .. import pipeline as mod_pipeline
from .. import registre, schemas
from ..mcp.client import ClientMCP
from ..utils.erreurs import ErreurForge
from ..utils.fichiers import ecrire_json, lire_json
from . import planificateur as mod_planificateur

DOSSIER_STATIQUE = Path(__file__).resolve().parent / "statique"
DOSSIER_VUES = registre.DOSSIER_UTILISATEUR / "vues"
PLANIFICATEUR = mod_planificateur.Planificateur()


def _etat_global() -> dict:
    briques, problemes = registre.lister_briques()
    pipelines, problemes_p = registre.lister_pipelines()
    plans = {c["cible"]: c for c in PLANIFICATEUR.cibles()}
    return {
        "briques": [
            {
                **m.en_json(),
                "cible": f"brique:{m.nom}",
                "planification": plans.get(f"brique:{m.nom}", {}).get("planification", m.planification),
                "execution": PLANIFICATEUR.etat(f"brique:{m.nom}"),
                "rapports": _rapports(m)[:1],
                "page": bool(m.page),
                "livree": registre.RACINE_DEPOT in m.dossier.parents,
                "executable": _executable(m, plans.get(f"brique:{m.nom}", {}).get("planification", m.planification)),
            }
            for m in briques
        ],
        "pipelines": [
            {
                **m.en_json(),
                "cible": f"pipeline:{m.nom}",
                "planification": plans.get(f"pipeline:{m.nom}", {}).get("planification", m.planification),
                "execution": PLANIFICATEUR.etat(f"pipeline:{m.nom}"),
                "fichier": m.brut.get("_fichier"),
            }
            for m in pipelines
        ],
        "contrats": schemas.lister_contrats(),
        "vues": _vues(),
        "en_cours": PLANIFICATEUR.en_cours,
        "en_file": sorted(PLANIFICATEUR.en_file),
        "journal": list(PLANIFICATEUR.journal_courant[-30:]),
        "problemes": problemes + problemes_p,
        "dossier_utilisateur": str(registre.DOSSIER_UTILISATEUR),
    }


def _executable(m: mod_manifeste.Manifeste, plan: dict) -> bool:
    """Une brique s'exécute seule si son service planifié a tous ses champs obligatoires dans ses paramètres."""
    try:
        service = m.service(plan["service"]) if plan.get("service") else (m.services[0] if m.services else None)
    except ErreurForge:
        return False
    if service is None:
        return False
    entree = plan.get("entree", service.exemple or {})
    try:
        requis = schemas.resoudre(service.entree).get("required", [])
    except ErreurForge:
        return False
    return all(r in entree for r in requis)


def _rapports(m: mod_manifeste.Manifeste) -> list[dict]:
    dossier = m.dossier_rapports
    if not dossier.is_dir():
        return []
    fichiers = sorted((f for f in dossier.glob("rapport_*.html")), reverse=True)
    rapports = []
    for f in fichiers:
        md = f.with_suffix(".md")
        rapports.append({
            "nom": f.name,
            "url": f"/rapports/{m.nom}/{f.name}",
            "markdown": f"/rapports/{m.nom}/{md.name}" if md.exists() else None,
            "date": f.stem.replace("rapport_", ""),
        })
    return rapports


def _detail_brique(nom: str) -> dict:
    m = registre.trouver_brique(nom)
    cible = f"brique:{m.nom}"
    plan = PLANIFICATEUR.planification(cible, m.planification)
    return {
        **m.en_json(),
        "cible": cible,
        "planification": plan,
        "execution": PLANIFICATEUR.etat(cible),
        "rapports": _rapports(m),
        "page": f"/pages/{m.nom}/" if m.page else None,
        "livree": registre.RACINE_DEPOT in m.dossier.parents,
        "executable": _executable(m, plan),
        "en_cours": PLANIFICATEUR.en_cours == cible,
    }


def _detail_pipeline(nom: str) -> dict:
    m = registre.trouver_pipeline(nom)
    cible = f"pipeline:{m.nom}"
    execution = PLANIFICATEUR.etat(cible)
    # rapports produits par les étapes dont la sortie est un rapport@1
    produits = []
    for brut in m.etapes:
        try:
            brique = registre.trouver_brique(brut["brique"])
            service = brique.service(brut["service"])
        except (ErreurForge, KeyError):
            continue
        if service.sortie == "forge://document/rapport@1":
            resultat = (execution.get("resultats") or {}).get(brut.get("id"), {})
            if isinstance(resultat, dict) and resultat.get("html"):
                produits.append({"etape": brut.get("id"), "brique": brique.nom, "titre": resultat.get("titre"), "date": resultat.get("date"),
                                 "url": f"/rapports/{brique.nom}/{resultat['html']}", "resume": resultat.get("resume"), "alertes": resultat.get("alertes", [])})
    return {
        **m.en_json(),
        "cible": cible,
        "planification": PLANIFICATEUR.planification(cible, m.planification),
        "execution": execution,
        "fichier": m.brut.get("_fichier"),
        "rapports": produits,
        "en_cours": PLANIFICATEUR.en_cours == cible,
    }


def _vues() -> list[dict]:
    if not DOSSIER_VUES.is_dir():
        return []
    vues = []
    for f in sorted(DOSSIER_VUES.glob("*.json")):
        try:
            vues.append(lire_json(f))
        except Exception:
            continue
    return vues


class Requete(BaseHTTPRequestHandler):
    server_version = "agentforge-hub/0.1"

    def log_message(self, format, *args):  # journal sobre sur stderr
        sys.stderr.write(f"hub {self.command} {self.path} {args[1] if len(args) > 1 else ''}\n")

    # --- utilitaires ------------------------------------------------------
    def _json(self, donnees, code: int = 200) -> None:
        corps = json.dumps(donnees, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corps)))
        self.end_headers()
        self.wfile.write(corps)

    def _erreur(self, exc: ErreurForge, code: int = 400) -> None:
        self._json({"erreur": exc.en_json()}, code)

    def _fichier(self, chemin: Path, racine: Path | None = None) -> None:
        chemin = chemin.resolve()
        if racine and racine.resolve() not in chemin.parents and chemin != racine.resolve():
            self._json({"erreur": {"cause": "chemin hors du dossier autorisé", "remede": ""}}, 403)
            return
        if not chemin.is_file():
            self._json({"erreur": {"cause": f"fichier introuvable : {chemin.name}", "remede": "vérifiez le nom ou relancez la brique"}}, 404)
            return
        type_mime = mimetypes.guess_type(str(chemin))[0] or "application/octet-stream"
        if type_mime.startswith("text/") or type_mime in ("application/javascript", "image/svg+xml", "application/json"):
            type_mime += "; charset=utf-8"
        corps = chemin.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", type_mime)
        self.send_header("Content-Length", str(len(corps)))
        self.end_headers()
        self.wfile.write(corps)

    def _corps_json(self) -> dict:
        longueur = int(self.headers.get("Content-Length") or 0)
        brut = self.rfile.read(longueur) if longueur else b"{}"
        try:
            return json.loads(brut.decode("utf-8") or "{}")
        except json.JSONDecodeError as exc:
            raise ErreurForge(f"corps JSON invalide : {exc.msg}", "envoyez un objet JSON") from exc

    # --- GET ----------------------------------------------------------------
    def do_GET(self) -> None:
        chemin = unquote(urlparse(self.path).path)
        parties = [p for p in chemin.split("/") if p]
        try:
            if chemin == "/":
                return self._fichier(DOSSIER_STATIQUE / "index.html")
            if chemin == "/assembleur":
                return self._fichier(DOSSIER_STATIQUE / "assembleur.html")
            if parties[:1] == ["statique"] and len(parties) == 2:
                return self._fichier(DOSSIER_STATIQUE / parties[1], DOSSIER_STATIQUE)
            if parties[:1] == ["vue"] and len(parties) == 2:
                return self._fichier(DOSSIER_STATIQUE / "vue.html")
            if parties[:1] == ["brique"] and len(parties) == 2:
                return self._fichier(DOSSIER_STATIQUE / "brique.html")
            if parties[:1] == ["pipeline"] and len(parties) == 2:
                return self._fichier(DOSSIER_STATIQUE / "pipeline.html")
            if parties[:2] == ["api", "briques"] and len(parties) == 3:
                return self._json(_detail_brique(parties[2]))
            if parties[:2] == ["api", "pipelines"] and len(parties) == 3:
                return self._json(_detail_pipeline(parties[2]))
            if chemin == "/api/etat":
                return self._json(_etat_global())
            if parties[:2] == ["api", "executions"] and len(parties) == 3:
                return self._json(PLANIFICATEUR.etat(parties[2]))
            if parties[:2] == ["api", "vues"] and len(parties) == 3:
                for v in _vues():
                    if v.get("nom") == parties[2]:
                        return self._json(v)
                raise ErreurForge(f"vue « {parties[2]} » inconnue", "créez-la depuis l'accueil")
            if parties[:1] == ["rapports"] and len(parties) >= 2:
                m = registre.trouver_brique(parties[1])
                if len(parties) == 2:
                    return self._json(_rapports(m))
                return self._fichier(m.dossier_rapports / "/".join(parties[2:]), m.dossier_rapports)
            if parties[:1] == ["pages"] and len(parties) >= 2:
                m = registre.trouver_brique(parties[1])
                if not m.page:
                    raise ErreurForge(f"{m.nom} n'a pas de page interactive", "déclarez « page » dans forge.json")
                dossier_page = m.page.parent
                reste = "/".join(parties[2:]) or m.page.name
                return self._fichier(dossier_page / reste, dossier_page)
            self._json({"erreur": {"cause": f"route inconnue : {chemin}", "remede": "voir la liste des routes dans forge/hub/serveur.py"}}, 404)
        except ErreurForge as exc:
            self._erreur(exc, 404)

    # --- POST -------------------------------------------------------------
    def do_POST(self) -> None:
        chemin = unquote(urlparse(self.path).path)
        parties = [p for p in chemin.split("/") if p]
        try:
            if parties[:2] == ["api", "executer"] and len(parties) == 3:
                cible = parties[2]
                if ":" not in cible:
                    raise ErreurForge(f"cible invalide : {cible}", "forme attendue : pipeline:<nom> ou brique:<nom>")
                ajoute = PLANIFICATEUR.demander(cible)
                return self._json({"mise_en_file": ajoute, "message": "mise en file" if ajoute else "déjà en file ou en cours (verrou anti-doublon)"})
            if parties[:2] == ["api", "appeler"] and len(parties) == 4:
                m = registre.trouver_brique(parties[2])
                service = m.service(parties[3])
                entree = self._corps_json()
                with ClientMCP(m.commande_serveur(), m.dossier) as client:
                    return self._json({"resultat": client.appeler(service.nom, entree)})
            if chemin == "/api/verifier":
                brut = self._corps_json()
                m = _pipeline_depuis_json(brut)
                etapes = mod_pipeline.verifier(m)
                return self._json({"ok": True, "etapes": [{"id": e.identifiant, "brique": e.brique.nom, "service": e.service.nom} for e in etapes]})
            if chemin == "/api/pipelines":
                brut = self._corps_json()
                m = _pipeline_depuis_json(brut)
                mod_pipeline.verifier(m)
                dossier = registre.DOSSIER_UTILISATEUR / "pipelines"
                fichier = ecrire_json(dossier / f"{m.nom}.json", brut)
                return self._json({"ok": True, "fichier": str(fichier)})
            if parties[:2] == ["api", "planification"] and len(parties) == 3:
                PLANIFICATEUR.definir_planification(parties[2], self._corps_json())
                return self._json({"ok": True})
            if parties[:2] == ["api", "supprimer"] and len(parties) == 3:
                cible = registre.supprimer(parties[2])
                return self._json({"ok": True, "corbeille": str(cible)})
            if parties[:2] == ["api", "ouvrir-editeur"] and len(parties) == 3:
                m = registre.trouver_brique(parties[2])
                return self._json({"ok": True, "commande": _ouvrir_editeur(m.dossier)})
            if chemin == "/api/vues":
                vue = self._corps_json()
                if not vue.get("nom") or not isinstance(vue.get("cadres"), list):
                    raise ErreurForge("vue incomplète", "donnez « nom » et une liste « cadres » [{brique, type: page|rapport}]")
                fichier = ecrire_json(DOSSIER_VUES / f"{vue['nom']}.json", vue)
                return self._json({"ok": True, "fichier": str(fichier)})
            self._json({"erreur": {"cause": f"route inconnue : {chemin}", "remede": ""}}, 404)
        except ErreurForge as exc:
            self._erreur(exc)


def _pipeline_depuis_json(brut: dict) -> mod_manifeste.Manifeste:
    brut = {"forge": 1, "type": "pipeline", "version": "0.1.0", **brut}
    schemas.valider(brut, mod_manifeste.SCHEMA_MANIFESTE, contexte="pipeline")
    if brut.get("type") != "pipeline":
        raise ErreurForge("le JSON n'est pas un pipeline", 'utilisez "type": "pipeline"')
    m = mod_manifeste.Manifeste(dossier=registre.DOSSIER_UTILISATEUR / "pipelines", brut=brut)
    mod_manifeste.verifier_coherence(m)
    return m


def _ouvrir_editeur(dossier: Path) -> str:
    """Ouvre le dossier dans l'éditeur : $FORGE_EDITEUR, sinon `code`, sinon l'explorateur de fichiers."""
    editeur = os.environ.get("FORGE_EDITEUR") or ("code" if shutil.which("code") else None)
    if editeur:
        subprocess.Popen([editeur, str(dossier)])
        return f"{editeur} {dossier}"
    if sys.platform.startswith("win"):
        os.startfile(str(dossier))  # type: ignore[attr-defined]
        return f"explorateur {dossier}"
    ouvreur = "open" if sys.platform == "darwin" else "xdg-open"
    subprocess.Popen([ouvreur, str(dossier)])
    return f"{ouvreur} {dossier}"


def lancer(port: int = 8700, ouvrir: bool = True, bloquer: bool = True) -> ThreadingHTTPServer:
    registre.DOSSIER_UTILISATEUR.mkdir(parents=True, exist_ok=True)
    serveur = ThreadingHTTPServer(("127.0.0.1", port), Requete)
    PLANIFICATEUR.demarrer()
    url = f"http://localhost:{port}"
    print(f"hub agentforge : {url}  (Ctrl+C pour arrêter)", file=sys.stderr)
    if ouvrir:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    if not bloquer:
        threading.Thread(target=serveur.serve_forever, daemon=True).start()
        return serveur
    try:
        serveur.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        PLANIFICATEUR.arreter()
        serveur.server_close()
    return serveur

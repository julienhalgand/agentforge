"""modele-local avec son moteur intégré, contre des serveurs simulés : version GitHub + archive, fichier GGUF, moteur factice.

Le « moteur » de test est un script Python nommé llama-server qui imite l'API de llama.cpp (/health, /v1/models,
/v1/chat/completions). Tout le cycle est joué : installer le moteur, télécharger un modèle, démarrer, générer, arrêter.
"""

from __future__ import annotations

import io
import json
import os
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE))
os.environ["PRIX_AGENT_SOURCE"] = "test"

from forge import manifeste, pipeline, registre  # noqa: E402
from forge.mcp.client import ClientMCP  # noqa: E402
from forge.utils.erreurs import ErreurForge  # noqa: E402
from forge.utils.fichiers import ecrire_json  # noqa: E402

BRIQUE = RACINE / "briques" / "modele-local"
sys.path.insert(0, str(BRIQUE))
from modele_local import moteur as mod_moteur  # noqa: E402

FAUX_LLAMA_SERVER = '''#!/usr/bin/env python3
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
args = sys.argv[1:]
port = int(args[args.index("--port") + 1]); modele = args[args.index("-m") + 1]; ngl = args[args.index("-ngl") + 1]
class H(BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def _j(self, corps):
        d = json.dumps(corps).encode(); self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(d))); self.end_headers(); self.wfile.write(d)
    def do_GET(self):
        if self.path == "/health": return self._j({"status": "ok"})
        return self._j({"data": [{"id": modele}]})
    def do_POST(self):
        corps = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        contenu = corps["messages"][-1]["content"]
        if corps.get("response_format"):
            texte = json.dumps({"classe": "important"})
        else:
            texte = "ngl=" + ngl + " | " + contenu[:40]
        return self._j({"choices": [{"message": {"role": "assistant", "content": texte}, "finish_reason": "stop"}]})
HTTPServer(("127.0.0.1", port), H).serve_forever()
'''


class _FauxDepots(BaseHTTPRequestHandler):
    """GitHub (releases/latest + archive) et Hugging Face (fichier .gguf, avec Range)."""

    port = 0
    gguf = b"GGUF" + bytes(range(256)) * 400  # ≈ 100 Ko

    def log_message(self, *_):
        pass

    def _envoyer(self, code, corps, type_mime="application/json", entetes=None):
        self.send_response(code)
        self.send_header("Content-Type", type_mime)
        self.send_header("Content-Length", str(len(corps)))
        for k, v in (entetes or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(corps)

    def do_GET(self):
        base = f"http://127.0.0.1:{_FauxDepots.port}"
        if self.path == "/releases/latest":
            noms = ["llama-b9999-bin-win-cpu-x64.zip", "llama-b9999-bin-win-vulkan-x64.zip", "llama-b9999-bin-ubuntu-x64.zip", "llama-b9999-bin-ubuntu-vulkan-x64.zip", "llama-b9999-bin-macos-arm64.zip", "llama-b9999-bin-macos-x64.zip", "cudart-llama-bin-win-cuda-12.4-x64.zip"]
            # comme sur GitHub : la première version listée est une balise nocturne sans binaires
            nocturne = {"tag_name": "nightly", "assets": [{"name": "nightly-tag.txt", "browser_download_url": f"{base}/archive/nightly-tag.txt"}]}
            vraie = {"tag_name": "b9999", "assets": [{"name": n, "browser_download_url": f"{base}/archive/{n}"} for n in noms]}
            return self._envoyer(200, json.dumps([nocturne, vraie]).encode())
        if self.path.startswith("/archive/"):
            tampon = io.BytesIO()
            with zipfile.ZipFile(tampon, "w") as z:
                z.writestr("build/bin/llama-server", FAUX_LLAMA_SERVER)
                z.writestr("build/bin/llama-server.exe", FAUX_LLAMA_SERVER)
            return self._envoyer(200, tampon.getvalue(), "application/zip")
        if self.path.endswith(".gguf"):
            donnees = _FauxDepots.gguf
            plage = self.headers.get("Range")
            if plage:
                debut = int(plage.split("=")[1].rstrip("-"))
                return self._envoyer(206, donnees[debut:], "application/octet-stream", {"Content-Range": f"bytes {debut}-{len(donnees) - 1}/{len(donnees)}"})
            return self._envoyer(200, donnees, "application/octet-stream")
        self._envoyer(404, b"{}")


@pytest.fixture
def brique(tmp_path, monkeypatch):
    monkeypatch.setattr(registre, "DOSSIER_UTILISATEUR", tmp_path / "agentforge")
    serveur = HTTPServer(("127.0.0.1", 0), _FauxDepots)
    _FauxDepots.port = serveur.server_port
    threading.Thread(target=serveur.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{serveur.server_port}"
    # le moteur et les modèles vont dans un dossier temporaire, jamais dans le dépôt
    monkeypatch.setattr(mod_moteur, "DOSSIER_MOTEUR", tmp_path / "moteur")
    monkeypatch.setattr(mod_moteur, "DOSSIER_MODELES", tmp_path / "modeles")
    monkeypatch.setattr(mod_moteur, "FICHIER_MOTEUR", tmp_path / "moteur" / "moteur.json")
    monkeypatch.setattr(mod_moteur, "FICHIER_SERVEUR", tmp_path / "moteur" / "serveur.json")
    monkeypatch.setattr(mod_moteur, "FICHIER_JOURNAL", tmp_path / "moteur" / "llama-server.log")
    monkeypatch.setattr(mod_moteur, "API_VERSIONS", base + "/releases/latest")
    monkeypatch.setitem(mod_moteur.CATALOGUE, "faux-1b", {"titre": "faux", "taille_go": 0.0001, "url": base + "/modeles/faux-1b.gguf"})
    yield {"base": base, "tmp": tmp_path}
    mod_moteur.arreter_serveur()
    serveur.shutdown()


def test_choix_de_l_archive_selon_plateforme(monkeypatch):
    noms = ["llama-b1-bin-win-cpu-x64.zip", "llama-b1-bin-win-vulkan-x64.zip", "llama-b1-bin-ubuntu-x64.zip", "llama-b1-bin-macos-arm64.zip"]
    monkeypatch.setattr(mod_moteur, "plateforme", lambda: ("windows", "x64"))
    assert mod_moteur.choisir_asset(noms, gpu=True) == "llama-b1-bin-win-vulkan-x64.zip"
    assert mod_moteur.choisir_asset(noms, gpu=False) == "llama-b1-bin-win-cpu-x64.zip"
    monkeypatch.setattr(mod_moteur, "plateforme", lambda: ("linux", "x64"))
    assert mod_moteur.choisir_asset(noms, gpu=True) == "llama-b1-bin-ubuntu-x64.zip"  # pas de vulkan listé → repli CPU
    monkeypatch.setattr(mod_moteur, "plateforme", lambda: ("macos", "arm64"))
    assert mod_moteur.choisir_asset(noms, gpu=True) == "llama-b1-bin-macos-arm64.zip"
    with pytest.raises(ErreurForge):
        mod_moteur.choisir_asset(["rien.tar.gz"], gpu=False)


def test_cycle_complet_moteur_integre(brique):
    tmp = brique["tmp"]
    # 1. moteur
    progres = []
    info = mod_moteur.installer_moteur(gpu=False, progres=lambda p, e: progres.append((p, e)))
    assert info["version"] == "b9999" and Path(info["executable"]).exists() and progres[-1][0] == 100
    assert mod_moteur.moteur_installe()["executable"] == info["executable"]
    # 2. modèle, avec reprise : on simule un téléchargement interrompu à mi-chemin
    partiel = tmp / "modeles" / "faux-1b.gguf.part"
    partiel.parent.mkdir(exist_ok=True)
    partiel.write_bytes(_FauxDepots.gguf[:50_000])
    fichier = mod_moteur.telecharger_modele("faux-1b")
    assert fichier.read_bytes() == _FauxDepots.gguf  # reprise exacte grâce à Range
    assert [m["nom"] for m in mod_moteur.modeles_installes()] == ["faux-1b"]
    # 3. serveur
    info_s = mod_moteur.demarrer_serveur("faux-1b", gpu=False, port=8792, attente_s=20)
    assert mod_moteur.serveur_en_marche()["modele"] == "faux-1b"
    assert mod_moteur.demarrer_serveur("faux-1b", gpu=False, port=8792)["pid"] == info_s["pid"]  # déjà en marche : réutilisé
    assert mod_moteur.arreter_serveur() and mod_moteur.serveur_en_marche() is None


def test_brique_de_bout_en_bout(brique, monkeypatch):
    m = manifeste.charger(BRIQUE)
    config = BRIQUE / "modele.json"
    config.unlink(missing_ok=True)
    tmp = brique["tmp"]
    env = {"FORGE_TEST_TMP": str(tmp), "FORGE_LLAMA_RELEASES": brique["base"] + "/releases/latest"}
    # la brique tourne en sous-processus : on lui fait pointer ses dossiers vers tmp via un module d'amorce
    amorce = tmp / "sitecustomize.py"
    amorce.write_text(
        "import os, pathlib\n"
        "from modele_local import moteur as m\n"
        "t = pathlib.Path(os.environ['FORGE_TEST_TMP'])\n"
        "m.DOSSIER_MOTEUR = t / 'moteur'; m.DOSSIER_MODELES = t / 'modeles'\n"
        "m.FICHIER_MOTEUR = m.DOSSIER_MOTEUR / 'moteur.json'; m.FICHIER_SERVEUR = m.DOSSIER_MOTEUR / 'serveur.json'; m.FICHIER_JOURNAL = m.DOSSIER_MOTEUR / 'llama-server.log'\n"
        f"m.CATALOGUE['faux-1b'] = {{'titre': 'faux', 'taille_go': 0.0001, 'url': '{brique['base']}/modeles/faux-1b.gguf'}}\n"
        "m.PORT_PAR_DEFAUT = 8793\n",
        encoding="utf-8",
    )
    env["PYTHONPATH"] = os.pathsep.join([str(tmp), str(BRIQUE), str(RACINE)])
    try:
        with ClientMCP(m.commande_serveur(), m.dossier, environnement=env) as client:
            e = client.appeler("etat", {})
            assert e["backend"] == "integre" and not e["pret"] and "moteur" in e["explication"]
            # préparation automatique : moteur + modèle par défaut, sans rien demander
            t = client.appeler("preparer", {"gpu": False, "modele": "faux-1b"})
            assert t["etat"] == "termine" and t["etape"] == "prêt — faux-1b", t
            assert client.appeler("preparer", {"gpu": False, "modele": "faux-1b"})["etat"] == "termine"  # idempotent
            e = client.appeler("etat", {})
            assert e["pret"] and e["modele_actif"] == "faux-1b" and e["moteur"]["version"] == "b9999"
            g = client.appeler("generer", {"consigne": "résume", "faits": {"prix": 65000}})
            assert g["modele"] == "faux-1b" and g["backend"] == "integre" and g["texte"].startswith("ngl=0 | Faits (JSON)")
            j = client.appeler("generer_json", {"consigne": "classe", "schema": {"type": "object", "required": ["classe"], "properties": {"classe": {"type": "string", "enum": ["important", "ignorable"]}}}})
            assert j["valeur"] == {"classe": "important"}
            assert client.appeler("etat", {})["serveur"]["port"] == 8793
            assert client.appeler("arreter", {}) == {"arrete": True}
            echec = client.appeler("installer_modele", {"nom": "inconnu-9b"})
            assert echec["etat"] == "echec" and "catalogue" in (BRIQUE / "taches" / "inconnu-9b.erreur.txt").read_text(encoding="utf-8")
    finally:
        config.unlink(missing_ok=True)
        # le sous-processus a pu laisser un moteur factice en marche
        serveur_json = tmp / "moteur" / "serveur.json"
        if serveur_json.exists():
            import signal

            try:
                os.kill(json.loads(serveur_json.read_text())["pid"], signal.SIGTERM)
            except (OSError, ValueError):
                pass


def test_pipeline_commente_est_valide(tmp_path, monkeypatch):
    monkeypatch.setattr(registre, "DOSSIER_UTILISATEUR", tmp_path / "agentforge")
    p = registre.trouver_pipeline("rapport-btc-commente")
    etapes = pipeline.verifier(p)
    assert [e.brique.nom for e in etapes] == ["prix-agent", "rapport-marche", "modele-local"]


def test_repli_cpu_si_le_moteur_gpu_ne_demarre_pas(brique, monkeypatch):
    """Variante GPU installée mais qui meurt au démarrage → réinstallation CPU et second essai, automatiquement."""
    mod_moteur.installer_moteur(gpu=True)
    assert mod_moteur.moteur_installe()["gpu"] is True
    mod_moteur.telecharger_modele("faux-1b")
    vrai = mod_moteur._demarrer_serveur
    appels = []

    def faux(modele, gpu, port, contexte, attente_s):
        appels.append(gpu)
        if gpu:
            raise ErreurForge("le moteur s'est arrêté (code 1) au démarrage", "pas de pilote Vulkan")
        return vrai(modele, gpu, 8794, contexte, 20)

    monkeypatch.setattr(mod_moteur, "_demarrer_serveur", faux)
    info = mod_moteur.demarrer_serveur("faux-1b", gpu=True)
    assert appels == [True, False] and "repli_cpu" in info and mod_moteur.moteur_installe()["gpu"] is False
    mod_moteur.arreter_serveur()

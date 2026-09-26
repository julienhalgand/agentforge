"""La brique modele-local contre un serveur Ollama simulé : état, installation (tâche longue), génération, pipeline."""

from __future__ import annotations

import json
import os
import sys
import threading
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
from forge.utils.taches import lire_etat  # noqa: E402

BRIQUE = RACINE / "briques" / "modele-local"


class _FauxOllama(BaseHTTPRequestHandler):
    installes = ["deja:latest"]

    def log_message(self, *_):
        pass

    def _json(self, code, corps):
        d = json.dumps(corps).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(d))); self.end_headers(); self.wfile.write(d)

    def do_GET(self):
        self._json(200, {"models": [{"name": n} for n in _FauxOllama.installes]})

    def do_POST(self):
        corps = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/api/pull":
            if corps["model"] == "inexistant:1b":
                return self._json(200, {"error": "pull model manifest: file does not exist"})
            self.send_response(200); self.send_header("Content-Type", "application/x-ndjson"); self.end_headers()
            for fait in (0, 50, 100):
                self.wfile.write((json.dumps({"status": "pulling abc", "total": 100, "completed": fait}) + "\n").encode()); self.wfile.flush()
            self.wfile.write((json.dumps({"status": "success"}) + "\n").encode())
            _FauxOllama.installes.append(corps["model"] + ":latest")
            return
        if corps["model"].split(":")[0] not in [n.split(":")[0] for n in _FauxOllama.installes]:
            return self._json(404, {"error": "model not found"})
        faits = corps["messages"][-1]["content"]
        return self._json(200, {"message": {"role": "assistant", "content": f"Réponse ancrée sur : {faits[:60]}"}, "done_reason": "stop"})


@pytest.fixture
def brique_avec_faux_ollama(tmp_path, monkeypatch):
    monkeypatch.setattr(registre, "DOSSIER_UTILISATEUR", tmp_path / "agentforge")
    serveur = HTTPServer(("127.0.0.1", 0), _FauxOllama)
    threading.Thread(target=serveur.serve_forever, daemon=True).start()
    _FauxOllama.installes = ["deja:latest"]
    config = BRIQUE / "modele.json"
    sauvegarde = config.read_text(encoding="utf-8") if config.exists() else None
    ecrire_json(config, {"nom": "deja", "url": f"http://127.0.0.1:{serveur.server_port}"}, sauvegarder=False)
    yield manifeste.charger(BRIQUE)
    serveur.shutdown()
    if sauvegarde is None:
        config.unlink(missing_ok=True)
    else:
        config.write_text(sauvegarde, encoding="utf-8")


def test_etat_installation_et_generation(brique_avec_faux_ollama):
    m = brique_avec_faux_ollama
    with ClientMCP(m.commande_serveur(), m.dossier) as client:
        e = client.appeler("etat", {})
        assert e["joignable"] and e["pret"] and e["modele_actif"] == "deja" and "deja:latest" in e["modeles"]

        t = client.appeler("installer_modele", {"nom": "nouveau"})
        assert t["etat"] == "termine" and t["pourcentage"] == 100
        assert lire_etat(m.dossier_taches, "nouveau")["etat"] == "termine"
        assert "nouveau:latest" in client.appeler("etat", {})["modeles"]

        echec = client.appeler("installer_modele", {"nom": "inexistant:1b"})
        assert echec["etat"] == "echec" and (m.dossier_taches / echec["erreur"]).read_text(encoding="utf-8").startswith("Ollama")

        g = client.appeler("generer", {"consigne": "résume", "faits": {"prix": 65000}})
        assert g["modele"] == "deja" and g["texte"].startswith("Réponse ancrée sur : Faits (JSON)")
        assert client.appeler("choisir_modele", {"nom": "nouveau", "gpu": False}) == {"modele_actif": "nouveau", "gpu": False}
        assert client.appeler("etat", {})["modele_actif"] == "nouveau"

        with pytest.raises(ErreurForge) as exc:
            client.appeler("generer", {"consigne": "x", "modele": "absent"})
        assert "non disponible" in exc.value.cause and "page de la brique" in exc.value.remede


def test_pipeline_commente_est_valide(tmp_path, monkeypatch):
    monkeypatch.setattr(registre, "DOSSIER_UTILISATEUR", tmp_path / "agentforge")
    p = registre.trouver_pipeline("rapport-btc-commente")
    etapes = pipeline.verifier(p)
    assert [e.brique.nom for e in etapes] == ["prix-agent", "rapport-marche", "modele-local"]

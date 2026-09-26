"""Tâches longues (progrès sur disque, annulation coopérative) et modèle local (serveur Ollama simulé)."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE))
os.environ["PRIX_AGENT_SOURCE"] = "test"

from forge import llm, manifeste, registre  # noqa: E402
from forge.mcp.client import ClientMCP  # noqa: E402
from forge.utils.erreurs import ErreurForge  # noqa: E402
from forge.utils.taches import Tache, lire_etat, lister_taches, poser_annulation  # noqa: E402


@pytest.fixture(autouse=True)
def dossier_utilisateur(tmp_path, monkeypatch):
    monkeypatch.setattr(registre, "DOSSIER_UTILISATEUR", tmp_path / "agentforge")


# --- tâches longues ----------------------------------------------------------------

def test_tache_progres_sur_disque_et_annulation(tmp_path):
    t = Tache(tmp_path, "livre", intervalle_s=0.0)
    t.demarrer("lecture")
    assert lire_etat(tmp_path, "livre")["etat"] == "en_cours"
    t.progres(40, etape="page 120/300")
    e = lire_etat(tmp_path, "livre")
    assert e["pourcentage"] == 40 and e["etape"] == "page 120/300" and "eta_s" in e
    poser_annulation(tmp_path, "livre")
    assert t.annulee()
    t.annuler()
    e = lire_etat(tmp_path, "livre")
    assert e["etat"] == "annule" and not (tmp_path / "livre.annuler").exists()
    assert [x["tache"] for x in lister_taches(tmp_path)] == ["livre"]


def test_tache_echec_ecrit_erreur_txt(tmp_path):
    t = Tache(tmp_path, "ocr")
    t.demarrer()
    t.echouer("page 12 : image illisible")
    e = lire_etat(tmp_path, "ocr")
    assert e["etat"] == "echec" and (tmp_path / e["erreur"]).read_text(encoding="utf-8").startswith("page 12")


def test_tache_throttle_une_ecriture_par_seconde(tmp_path):
    t = Tache(tmp_path, "tts", intervalle_s=10.0)
    t.demarrer()
    t.progres(10)
    t.progres(20)  # ignoré : trop tôt
    assert lire_etat(tmp_path, "tts")["pourcentage"] == 0
    t.progres(30, force=True)
    assert lire_etat(tmp_path, "tts")["pourcentage"] == 30


def test_demo_tache_longue_de_bout_en_bout_avec_annulation():
    m = manifeste.charger(RACINE / "briques" / "demo-tache-longue")
    dossier = m.dossier_taches
    with ClientMCP(m.commande_serveur(), m.dossier) as client:
        r = client.appeler("travailler", {"nom": "test-court", "blocs": 3, "duree_bloc_s": 0})
        assert r["etat"] == "termine" and r["pourcentage"] == 100 and (dossier / r["resultat"]).exists()
        # annulation coopérative : on pose le drapeau pendant que la tâche tourne
        resultat = {}

        def appeler():
            resultat["r"] = client.appeler("travailler", {"nom": "test-annule", "blocs": 100, "duree_bloc_s": 0.05})

        fil = threading.Thread(target=appeler)
        fil.start()
        for _ in range(100):
            if lire_etat(dossier, "test-annule")["etat"] == "en_cours":
                break
            time.sleep(0.05)
        poser_annulation(dossier, "test-annule")
        fil.join(10)
        assert not fil.is_alive()
    r = resultat["r"]
    assert r["etat"] == "annule" and 0 < r["pourcentage"] < 100
    assert json.loads((dossier / "test-annule.resultat.json").read_text())["complet"] is False


# --- modèle local : serveur Ollama simulé ---------------------------------------------

class _FauxOllama(BaseHTTPRequestHandler):
    """Répond comme Ollama ; coupe la première réponse (done_reason=length) pour tester la continuation."""

    appels: list[dict] = []

    def log_message(self, *_):
        pass

    def _envoyer(self, code: int, corps: dict):
        donnees = json.dumps(corps).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(donnees)))
        self.end_headers()
        self.wfile.write(donnees)

    def do_GET(self):
        self._envoyer(200, {"models": [{"name": "faux:latest"}]})

    def do_POST(self):
        corps = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _FauxOllama.appels.append(corps)
        if corps["model"] != "faux":
            return self._envoyer(404, {"error": f"model '{corps['model']}' not found"})
        if "format" in corps:
            return self._envoyer(200, {"message": {"role": "assistant", "content": '```json\n{"important": ["a"], "ignore": ["b"]}\n```'}, "done_reason": "stop"})
        n = sum(1 for m in corps["messages"] if m["role"] == "assistant")
        if n == 0:
            return self._envoyer(200, {"message": {"role": "assistant", "content": "Début de la réponse, "}, "done_reason": "length"})
        return self._envoyer(200, {"message": {"role": "assistant", "content": "et sa fin."}, "done_reason": "stop"})


@pytest.fixture
def faux_ollama():
    serveur = HTTPServer(("127.0.0.1", 0), _FauxOllama)
    threading.Thread(target=serveur.serve_forever, daemon=True).start()
    _FauxOllama.appels.clear()
    yield f"http://127.0.0.1:{serveur.server_port}"
    serveur.shutdown()


def test_llm_continuation_automatique(faux_ollama):
    modele = llm.ModeleLocal({"backend": "ollama", "nom": "faux", "url": faux_ollama})
    assert modele.disponible() == (True, "prêt")
    assert modele.generer("dis quelque chose", systeme="recopie les chiffres") == "Début de la réponse, et sa fin."
    assert len(_FauxOllama.appels) == 2 and _FauxOllama.appels[1]["messages"][-1]["role"] == "user"


def test_llm_json_sous_schema_valide(faux_ollama):
    modele = llm.ModeleLocal({"backend": "ollama", "nom": "faux", "url": faux_ollama})
    schema = {"type": "object", "required": ["important", "ignore"], "properties": {"important": {"type": "array"}, "ignore": {"type": "array"}}}
    assert modele.generer_json("classe", schema) == {"important": ["a"], "ignore": ["b"]}
    assert _FauxOllama.appels[-1]["format"] == schema
    with pytest.raises(ErreurForge) as exc:  # le JSON rendu ne respecte pas ce schéma-là
        modele.generer_json("classe", {"type": "object", "required": ["autre"]})
    assert "autre" in "\n".join(exc.value.details)


def test_llm_erreurs_parlantes(faux_ollama):
    modele = llm.ModeleLocal({"backend": "ollama", "nom": "absent", "url": faux_ollama})
    ok, explication = modele.disponible()
    assert not ok and "ollama pull absent" in explication
    with pytest.raises(ErreurForge) as exc:
        modele.generer("x")
    assert "HTTP 404" in exc.value.cause and "ollama pull absent" in exc.value.remede
    with pytest.raises(ErreurForge) as exc:
        llm.ModeleLocal({"backend": "ollama", "nom": "x", "url": "https://api.exemple.com"})
    assert "local d'abord" in exc.value.remede
    injoignable = llm.ModeleLocal({"backend": "ollama", "nom": "x", "url": "http://127.0.0.1:9"})
    assert "injoignable" in injoignable.disponible()[1]

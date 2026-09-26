"""Tests d'agentforge : `python -m pytest`. Tout se prouve par une exécution."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE))
os.environ["PRIX_AGENT_SOURCE"] = "test"

from forge import manifeste, pipeline, registre, schemas  # noqa: E402
from forge.mcp.adaptateur import AdaptateurBrique  # noqa: E402
from forge.mcp.client import ClientMCP  # noqa: E402
from forge.mcp.serveur import ServeurMCP  # noqa: E402
from forge.utils.erreurs import ErreurForge, ReponsePartielle  # noqa: E402
from forge.utils.fichiers import ecrire_atomique, empreintes, mettre_a_la_corbeille, verifier_inchanges  # noqa: E402


@pytest.fixture(autouse=True)
def dossier_utilisateur(tmp_path, monkeypatch):
    """Chaque test a son ~/.agentforge, pour ne rien écrire chez l'utilisateur."""
    monkeypatch.setattr(registre, "DOSSIER_UTILISATEUR", tmp_path / "agentforge")
    return tmp_path / "agentforge"


# --- contrats et validation --------------------------------------------------

def test_les_contrats_se_chargent():
    refs = {c["reference"] for c in schemas.lister_contrats()}
    assert {"forge://serie-temporelle/ohlcv@1", "forge://marche/prix-comptant@1", "forge://document/rapport@1",
            "forge://tache/progres@1", "forge://planification/planification@1"} <= refs


def test_ohlcv_conforme_et_ecarts_parlants():
    bon = {"actif": "BTC", "intervalle": "1d", "bougies": [[1, 2.0, 3.0, 1.0, 2.5, 10.0]]}
    assert schemas.ecarts(bon, "forge://serie-temporelle/ohlcv@1") == []
    mauvais = {"actif": "BTC", "intervalle": "2d", "bougies": [[1, 2.0, 3.0]]}
    e = schemas.ecarts(mauvais, "forge://serie-temporelle/ohlcv@1")
    assert any("intervalle" in x and "hors de la liste" in x for x in e)
    assert any("bougies[0]" in x and "au moins 6" in x for x in e)


def test_contrat_inconnu_dit_le_remede():
    with pytest.raises(ErreurForge) as exc:
        schemas.charger_contrat("forge://inconnu/truc@9")
    assert "contrats/inconnu/truc.v9.json" in exc.value.remede


# --- manifestes ------------------------------------------------------------------

def test_manifestes_livres_valides():
    for nom in ("prix-agent", "rapport-marche"):
        m = manifeste.charger(RACINE / "briques" / nom)
        assert m.nom == nom and m.services


def test_manifeste_incoherent_liste_les_problemes(tmp_path):
    (tmp_path / "forge.json").write_text(json.dumps({
        "forge": 1, "type": "application", "nom": "cassee", "version": "0.1.0",
        "transport": {"type": "services-json"},
        "services": [{"nom": "x", "entree": {"type": "object"}, "sortie": "forge://nulle/part@1"}],
    }), encoding="utf-8")
    with pytest.raises(ErreurForge) as exc:
        manifeste.charger(tmp_path)
    details = "\n".join(exc.value.details)
    assert "services.json introuvable" in details and "Contrat inconnu" in details


def test_interpreteur_absolu_introuvable():
    m = manifeste.Manifeste(RACINE, {"forge": 1, "type": "application", "nom": "x", "version": "0.1.0", "interpreteur": "/nulle/part/python.exe"})
    with pytest.raises(ErreurForge) as exc:
        _ = m.interpreteur
    assert "interpreteur" in exc.value.remede


# --- MCP : serveur en mémoire ----------------------------------------------------

def _serveur_test() -> ServeurMCP:
    s = ServeurMCP("test", "0.0.1")

    @s.service("double", "double un nombre", entree={"type": "object", "required": ["n"], "properties": {"n": {"type": "number"}}},
               sortie={"type": "object", "required": ["resultat"], "properties": {"resultat": {"type": "number"}}})
    def double(e):
        return {"resultat": e["n"] * 2}

    @s.service("partiel", "répond partiellement", entree={"type": "object"}, sortie={"type": "object"})
    def partiel(_e):
        raise ReponsePartielle({"valeur": 1}, ["élément B introuvable"])

    @s.service("triche", "sortie non conforme", entree={"type": "object"}, sortie={"type": "object", "required": ["obligatoire"]})
    def triche(_e):
        return {"autre": 1}

    return s


def test_serveur_mcp_appel_validation_et_partiel():
    s = _serveur_test()
    assert s.traiter({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})["result"]["serverInfo"]["name"] == "test"
    outils = s.traiter({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    assert {o["name"] for o in outils} == {"double", "partiel", "triche"}
    ok = s.traiter({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "double", "arguments": {"n": 21}}})["result"]
    assert ok["structuredContent"] == {"resultat": 42} and not ok["isError"]
    mauvais = s.traiter({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "double", "arguments": {"n": "x"}}})["result"]
    assert mauvais["isError"] and "$.n" in mauvais["structuredContent"]["erreur"]["details"][0]
    partiel = s.traiter({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "partiel", "arguments": {}}})["result"]
    assert not partiel["isError"] and partiel["structuredContent"]["manques"] == ["élément B introuvable"]
    triche = s.traiter({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "triche", "arguments": {}}})["result"]
    assert triche["isError"] and "non conforme" in triche["structuredContent"]["erreur"]["cause"]
    assert s.traiter({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert s.traiter({"jsonrpc": "2.0", "id": 7, "method": "inconnue"})["error"]["code"] == -32601


# --- MCP : client + brique native en sous-processus -------------------------------

def test_client_mcp_rapport_marche_bout_en_bout():
    m = manifeste.charger(RACINE / "briques" / "rapport-marche")
    bougies = [[1_700_000_000_000 + i * 86_400_000, 100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i, 10.0] for i in range(40)]
    with ClientMCP(m.commande_serveur(), m.dossier) as client:
        assert client.infos_serveur["name"] == "rapport-marche"
        r = client.appeler("generer", {"donnees": {"actif": "TEST", "intervalle": "1d", "bougies": bougies}, "titre": "Test"})
    assert schemas.ecarts(r, "forge://document/rapport@1") == []
    html = (m.dossier_rapports / r["html"]).read_text(encoding="utf-8")
    assert "Test" in html and "<svg" in html and "139.50" in html  # dernière clôture recopiée à l'identique


def test_client_mcp_erreur_parlante_sur_entree_vide():
    m = manifeste.charger(RACINE / "briques" / "rapport-marche")
    with ClientMCP(m.commande_serveur(), m.dossier) as client, pytest.raises(ErreurForge) as exc:
        client.appeler("generer", {"donnees": {"actif": "TEST", "intervalle": "1d", "bougies": []}})
    assert "aucune bougie" in exc.value.cause and exc.value.remede


# --- adaptateur : brique existante « python -m agent.<service> » ------------------

def test_adaptateur_codes_de_sortie():
    m = manifeste.charger(RACINE / "briques" / "prix-agent")
    a = AdaptateurBrique(m)
    r = a.executer("historique", {"actif": "BTC", "intervalle": "1d", "limite": 5, "source": "test"})
    assert r["source"] == "test" and len(r["bougies"]) == 5 and schemas.ecarts(r, "forge://serie-temporelle/ohlcv@1") == []
    with pytest.raises(ErreurForge) as exc:  # code 1 : --actif manquant
        a.executer("historique", {"intervalle": "1d", "limite": 5})
    assert "code 1" in exc.value.cause and any("--actif" in d for d in exc.value.details)


def test_adaptateur_repli_fichier_au_dela_de_la_limite_windows(monkeypatch):
    m = manifeste.charger(RACINE / "briques" / "prix-agent")
    a = AdaptateurBrique(m)
    actifs = ["BTC"] + [f"X{i}" for i in range(4000)]  # ligne de commande > 30 000 caractères
    commandes = []
    vrai_run = subprocess.run
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: (commandes.append(cmd), vrai_run(cmd, **kw))[1])
    with pytest.raises(ReponsePartielle) as exc:  # BTC ok en test, les X… inconnus → partiel (code 2)
        a.executer("prix", {"actifs": actifs, "source": "test"})
    assert "--entree" in commandes[0] and "--actifs" not in commandes[0]  # passage par fichier, pas par la ligne de commande
    assert exc.value.resultat["prix"]["BTC"]["source"] == "test"
    assert len(exc.value.manques) == 4000  # chaque manque est listé, aucun n'est tronqué


def test_adaptateur_en_mcp_via_client():
    m = manifeste.charger(RACINE / "briques" / "prix-agent")
    with ClientMCP(m.commande_serveur(), m.dossier) as client:
        r = client.appeler("prix", {"actifs": ["BTC", "ETH"], "source": "test"})
    assert set(r["prix"]) == {"BTC", "ETH"} and schemas.ecarts(r, "forge://marche/prix-comptant@1") == []


# --- pipelines -----------------------------------------------------------------------

def test_pipeline_livre_valide_et_executable():
    p = registre.trouver_pipeline("rapport-btc-quotidien")
    etapes = pipeline.verifier(p)
    assert [e.identifiant for e in etapes] == ["prix", "rapport"]
    r = pipeline.executer(p)
    assert r.manques == [] and r.resultats["rapport"]["html"].startswith("rapport_")
    assert len(r.resultats["prix"]["bougies"]) == 90


def test_pipeline_branchement_incompatible_refuse_avant_execution(tmp_path):
    fichier = tmp_path / "faux.json"
    fichier.write_text(json.dumps({
        "forge": 1, "type": "pipeline", "nom": "faux", "version": "0.1.0",
        "etapes": [
            {"id": "p", "brique": "prix-agent", "service": "prix", "entree": {"actifs": ["BTC"]}},
            {"id": "r", "brique": "rapport-marche", "service": "generer", "entree": {"donnees": "$p"}},
            {"id": "z", "brique": "prix-agent", "service": "historique", "entree": {"actif": "BTC", "intervalle": "3d", "limite": "dix"}},
        ],
    }), encoding="utf-8")
    with pytest.raises(ErreurForge) as exc:
        pipeline.verifier(registre.charger_pipeline(fichier))
    details = "\n".join(exc.value.details)
    assert "prix-comptant@1 → forge://serie-temporelle/ohlcv@1" in details
    assert "intervalle" in details and "limite" in details
    assert "rien n'a été exécuté" in exc.value.remede


# --- fichiers : rien ne se perd ---------------------------------------------------------

def test_ecriture_atomique_sauvegarde_et_corbeille(tmp_path):
    f = tmp_path / "donnees.json"
    ecrire_atomique(f, "v1")
    ecrire_atomique(f, "v2")
    assert f.read_text() == "v2"
    sauvegardes = list((tmp_path / ".sauvegardes").iterdir())
    assert len(sauvegardes) == 1 and sauvegardes[0].read_text() == "v1"
    assert not [x for x in tmp_path.iterdir() if x.name.startswith(".donnees")]  # aucun temporaire oublié
    avant = empreintes([f])
    cible = mettre_a_la_corbeille(f)
    assert not f.exists() and cible.exists() and ".corbeille" in cible.parts
    assert verifier_inchanges(avant) == [str(f)]


# --- CLI ---------------------------------------------------------------------------------

def test_cli_lister_et_valider():
    env = {**os.environ, "PYTHONPATH": str(RACINE), "PRIX_AGENT_SOURCE": "test"}
    r = subprocess.run([sys.executable, "-m", "forge", "lister"], capture_output=True, text=True, cwd=RACINE, env=env)
    assert r.returncode == 0 and "prix-agent" in r.stdout and "rapport-btc-quotidien" in r.stdout
    r = subprocess.run([sys.executable, "-m", "forge", "valider", "rapport-btc-quotidien"], capture_output=True, text=True, cwd=RACINE, env=env)
    assert r.returncode == 0 and "pipeline valide" in r.stdout
    r = subprocess.run([sys.executable, "-m", "forge", "appeler", "inconnue", "x"], capture_output=True, text=True, cwd=RACINE, env=env)
    assert r.returncode == 1 and "introuvable" in r.stderr and "forge installer" in r.stderr

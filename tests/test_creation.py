"""Créer une brique et composer un pipeline depuis une phrase, avec un modèle simulé.

Le faux modèle se trompe d'abord (import interdit, puis code qui plante), et corrige : la boucle
d'essai réel doit refuser les deux premières versions et installer la troisième, testée.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE))
os.environ["PRIX_AGENT_SOURCE"] = "test"

from forge import creation, manifeste, pipeline, registre  # noqa: E402
from forge.mcp.client import ClientMCP  # noqa: E402
from forge.utils.erreurs import ErreurForge  # noqa: E402


@pytest.fixture(autouse=True)
def dossier_utilisateur(tmp_path, monkeypatch):
    d = tmp_path / "agentforge"
    d.mkdir()
    monkeypatch.setattr(registre, "DOSSIER_UTILISATEUR", d)
    return d


def _spec(code: str) -> dict:
    return {
        "nom": "statistiques-liste",
        "description": "Statistiques d'une liste de nombres.",
        "icone": "📊",
        "services": [
            {
                "nom": "calculer",
                "description": "Moyenne, médiane et écart-type d'une liste de nombres.",
                "entree": [{"nom": "nombres", "type": "array", "description": "les nombres", "obligatoire": True}],
                "sortie": {"contrat": "champs", "champs": [
                    {"nom": "moyenne", "type": "number", "description": "moyenne"},
                    {"nom": "mediane", "type": "number", "description": "médiane"},
                    {"nom": "ecart_type", "type": "number", "description": "écart-type"},
                ]},
                "exemple": {"nombres": [1, 2, 3, 4, 10]},
                "code": code,
            }
        ],
    }


CODE_INTERDIT = "import subprocess\n\ndef executer(entree):\n    return {}\n"
CODE_QUI_PLANTE = "import statistics\n\ndef executer(entree):\n    n = entree['nombres']\n    return {'moyenne': statistics.mean(n), 'mediane': statistics.median(n)}\n"  # ecart_type manquant → sortie non conforme
CODE_BON = (
    "import statistics\n\n"
    "def executer(entree):\n"
    "    n = entree['nombres']\n"
    "    return {'moyenne': statistics.mean(n), 'mediane': statistics.median(n), 'ecart_type': statistics.pstdev(n)}\n"
)


def test_creer_brique_corrige_jusqu_a_reussir(dossier_utilisateur):
    demandes = []
    reponses = iter([_spec(CODE_INTERDIT), _spec(CODE_QUI_PLANTE), _spec(CODE_BON)])

    def faux_modele(consigne, schema, systeme):
        demandes.append(consigne)
        return next(reponses)

    journal = []
    r = creation.creer_brique("calculer les statistiques d'une liste de nombres", faux_modele, journal=journal.append)
    assert r["nom"] == "statistiques-liste" and r["tours"] == 3
    # les erreurs sont bien renvoyées au modèle, en clair
    assert "import interdit : subprocess" in demandes[1]
    assert "ecart_type" in demandes[2] and "non conforme" in demandes[2]
    # la brique est installée, découvrable, et marche pour de vrai
    m = registre.trouver_brique("statistiques-liste")
    assert m.dossier == dossier_utilisateur / "briques" / "statistiques-liste" and m.brut["creee_depuis"].startswith("calculer")
    with ClientMCP(m.commande_serveur(), m.dossier) as client:
        assert client.appeler("calculer", {"nombres": [2, 4, 6]}) == {"moyenne": 4, "mediane": 4, "ecart_type": pytest.approx(1.632993)}
    assert not list(dossier_utilisateur.glob("brique-*"))  # aucun dossier temporaire oublié


def test_creer_brique_abandonne_proprement():
    def modele_tetu(consigne, schema, systeme):
        return _spec(CODE_INTERDIT)

    with pytest.raises(ErreurForge) as exc:
        creation.creer_brique("n'importe quoi de louche", modele_tetu, tours=2)
    assert "2 tours" in exc.value.cause and any("subprocess" in d for d in exc.value.details) and "7B" in exc.value.remede


def test_valider_code_refuse_les_appels_dangereux():
    with pytest.raises(ErreurForge) as exc:
        creation.valider_code("def executer(entree):\n    return eval(entree['x'])\n")
    assert "eval()" in "\n".join(exc.value.details)
    with pytest.raises(ErreurForge) as exc:
        creation.valider_code("def autre(entree):\n    return {}\n")
    assert "executer" in "\n".join(exc.value.details)


def test_composer_pipeline_corrige_un_branchement_invalide(dossier_utilisateur):
    invalide = {"nom": "btc-commente", "description": "x", "planification": {"mode": "quotidien", "heure": "08:00"},
                "etapes": [{"id": "prix", "brique": "prix-agent", "service": "prix", "entree": {"actifs": ["BTC"]}},
                           {"id": "rapport", "brique": "rapport-marche", "service": "generer", "entree": {"donnees": "$prix"}}]}
    valide = {**invalide, "etapes": [{"id": "prix", "brique": "prix-agent", "service": "historique", "entree": {"actif": "BTC", "intervalle": "1d", "limite": 90}},
                                     {"id": "rapport", "brique": "rapport-marche", "service": "generer", "entree": {"donnees": "$prix"}}]}
    demandes = []
    reponses = iter([invalide, valide])

    def faux_modele(consigne, schema, systeme):
        demandes.append(consigne)
        return next(reponses)

    p = creation.composer_pipeline("chaque matin, le prix du bitcoin et un rapport", faux_modele)
    assert p["type"] == "pipeline" and p["planification"]["mode"] == "quotidien" and len(p["etapes"]) == 2
    assert "prix-agent.historique" in demandes[0] and "contrats différents" in demandes[1]
    fichier = creation.enregistrer_pipeline(p)
    m = registre.trouver_pipeline("btc-commente")
    assert m.brut["_fichier"] == str(fichier) and [e.identifiant for e in pipeline.verifier(m)] == ["prix", "rapport"]


def test_composer_normalise_brique_point_service(dossier_utilisateur):
    brut = {"nom": "x-y", "description": "x", "planification": {"mode": "jamais"},
            "etapes": [{"id": "prix", "brique": "prix-agent.historique", "service": "", "entree": {"actif": "BTC", "intervalle": "1d", "limite": 30}}]}
    p = creation.composer_pipeline("bougies", lambda c, s, sy: brut)
    assert p["etapes"][0]["brique"] == "prix-agent" and p["etapes"][0]["service"] == "historique"


def test_decider_et_brique_avec_page(dossier_utilisateur):
    assert creation.decider("je veux un métronome", lambda c, s, sy: {"action": "creer", "raison": "aucune brique ne fait ça"})["action"] == "creer"
    spec = {"nom": "metronome", "description": "Un métronome.", "icone": "🎵", "services": [],
            "page": "<!doctype html><html lang='fr'><body><h1>Métronome</h1><button>Démarrer</button></body></html>"}
    r = creation.creer_brique("je veux un métronome", lambda c, s, sy: spec)
    m = registre.trouver_brique("metronome")
    assert m.page and m.page.read_text(encoding="utf-8").startswith("<!doctype") and m.services == []
    with pytest.raises(ErreurForge) as exc:
        creation.creer_brique("rien", lambda c, s, sy: {**spec, "nom": "vide", "page": "", "services": []}, tours=1)
    assert any("au moins un service ou une page" in d for d in exc.value.details)


def test_page_conservee_si_les_services_inventes_echouent(dossier_utilisateur):
    """Le petit modèle colle des services faux autour d'une page valide : la page est installée, les services retirés."""
    spec = {"nom": "metronome", "description": "Un métronome.", "icone": "🎵",
            "page": "<!doctype html><html lang='fr'><body><h1>Métronome</h1></body></html>",
            "services": [{"nom": "tic", "description": "tic", "entree": [], "sortie": {"contrat": "forge://texte/generation@1"}, "exemple": {},
                          "code": "def executer(entree):\n    return {'texte': 'tic'}\n"}]}
    journal = []
    r = creation.creer_brique("je veux un métronome", lambda c, s, sy: spec, journal=journal.append)
    m = registre.trouver_brique("metronome")
    assert r["tours"] == 1 and m.page and m.services == []
    assert any("service(s) retiré(s) : tic" in l for l in journal)


def test_genre_page_ne_laisse_aucune_place_aux_services(dossier_utilisateur):
    schemas_recus = []

    def modele(consigne, schema, systeme):
        schemas_recus.append(schema)
        return {"nom": "metronome", "description": "Un métronome.", "icone": "🎵", "page": "<!doctype html><html lang='fr'><body><h1>Métronome</h1></body></html>"}

    r = creation.creer_brique("je veux un métronome", modele, genre="page")
    assert "services" not in schemas_recus[0]["properties"] and "page" in schemas_recus[0]["required"]
    assert registre.trouver_brique("metronome").page and r["tours"] == 1


def test_genre_service_exige_un_service_et_pas_de_page():
    schema = creation._schema_services()
    assert "page" not in schema["properties"] and schema["properties"]["services"]["minItems"] == 1

"""Brique de démonstration du pattern tâche longue (contrat forge://tache/progres@1).

Le service bloque le temps du travail (c'est le cas d'une synthèse vocale ou
d'un OCR) ; le hub le lance dans un fil d'arrière-plan et la page suit
l'avancement en lisant `taches/<nom>.progres.json`, pas la réponse MCP.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from forge.mcp.serveur import ServeurMCP
from forge.utils.fichiers import ecrire_json
from forge.utils.taches import Tache

DOSSIER_TACHES = Path(__file__).resolve().parent.parent / "taches"

serveur = ServeurMCP("demo-tache-longue", "0.1.0")


@serveur.service(
    "travailler",
    "Simule un travail en N blocs avec progrès sur disque et annulation coopérative.",
    entree={
        "type": "object",
        "required": ["nom", "blocs"],
        "properties": {"nom": {"type": "string"}, "blocs": {"type": "integer", "minimum": 1}, "duree_bloc_s": {"type": "number", "minimum": 0}},
    },
    sortie="forge://tache/progres@1",
)
def travailler(entree: dict) -> dict:
    nom, blocs = entree["nom"], int(entree["blocs"])
    duree = float(entree.get("duree_bloc_s", 0.5))
    tache = Tache(DOSSIER_TACHES, nom)
    tache.demarrer(f"0/{blocs} blocs")
    traites = []
    for i in range(blocs):
        if tache.annulee():  # consulté à chaque bloc : jamais de kill
            ecrire_json(DOSSIER_TACHES / f"{nom}.resultat.json", {"blocs_traites": traites, "complet": False}, sauvegarder=False)
            return tache.annuler(f"arrêt propre après {i}/{blocs} blocs", pourcentage=100 * i / blocs)
        time.sleep(duree)
        traites.append(i + 1)
        tache.progres(100 * (i + 1) / blocs, etape=f"{i + 1}/{blocs} blocs")
    ecrire_json(DOSSIER_TACHES / f"{nom}.resultat.json", {"blocs_traites": traites, "complet": True}, sauvegarder=False)
    return tache.terminer(f"{nom}.resultat.json", etape=f"{blocs}/{blocs} blocs")


if __name__ == "__main__":
    serveur.servir()
    sys.exit(0)

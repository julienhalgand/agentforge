"""Serveur MCP de rapport-marche : un service `generer`, sans LLM.

Lancé par forge : `python -m rapport_marche.serveur` depuis le dossier de la
brique. Écrit `rapports/rapport_AAAA-MM-JJ.md`, `.html` et `.svg` de façon
atomique (fichier caché puis renommage), avec sauvegarde de la version
précédente du jour s'il y en a une.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

from forge.mcp.serveur import ServeurMCP
from forge.utils.erreurs import ErreurForge
from forge.utils.fichiers import ecrire_atomique

from . import analyse, rendu

DOSSIER_BRIQUE = Path(__file__).resolve().parent.parent
DOSSIER_RAPPORTS = DOSSIER_BRIQUE / "rapports"

serveur = ServeurMCP("rapport-marche", "0.1.0")


@serveur.service(
    "generer",
    "Génère le rapport Markdown + HTML (mode sombre, graphique SVG) à partir de bougies OHLCV.",
    entree={
        "type": "object",
        "required": ["donnees"],
        "properties": {
            "donnees": "forge://serie-temporelle/ohlcv@1",
            "titre": {"type": "string"},
            "seuil_attention_pct": {"type": "number"},
            "seuil_repli_pct": {"type": "number"},
        },
    },
    sortie="forge://document/rapport@1",
)
def generer(entree: dict) -> dict:
    donnees = entree["donnees"]
    bougies = donnees["bougies"]
    if not bougies:
        raise ErreurForge("aucune bougie reçue", "vérifiez la source de prix : la série est vide")
    titre = entree.get("titre") or f"Marché {donnees['actif']}"
    date = dt.date.today().isoformat()
    m = analyse.mesures(bougies, donnees["intervalle"])
    alertes = analyse.alertes(m, entree.get("seuil_attention_pct", 5), entree.get("seuil_repli_pct", 20))

    base = f"rapport_{date}"
    svg = rendu.graphique_svg(bougies)
    md = rendu.markdown(titre, date, donnees, m, alertes, f"{base}.svg")
    html = rendu.page_html(titre, date, md, svg)
    DOSSIER_RAPPORTS.mkdir(exist_ok=True)
    ecrire_atomique(DOSSIER_RAPPORTS / f"{base}.svg", svg)
    ecrire_atomique(DOSSIER_RAPPORTS / f"{base}.md", md)
    ecrire_atomique(DOSSIER_RAPPORTS / f"{base}.html", html)

    manques = list(m.pop("manques", [])) + [f"données : {x}" for x in donnees.get("manques", [])]
    resume = f"{donnees['actif']} : {rendu._fmt(m['dernier'])} {donnees.get('devise', 'USD')}, {rendu._fmt(m['variation_1'], ' %')} sur la dernière période, repli max {rendu._fmt(m['repli_max_pct'], ' %')}"
    return {
        "titre": titre,
        "date": date,
        "markdown": f"{base}.md",
        "html": f"{base}.html",
        "images": [f"{base}.svg"],
        "resume": resume,
        "alertes": alertes,
        "manques": manques,
    }


if __name__ == "__main__":
    serveur.servir()
    sys.exit(0)

"""modele-local : le modèle de langage local comme une brique MCP.

Le modèle actif est mémorisé dans `modele.json` à côté du manifeste (écrit
par la page ou par `choisir_modele`), le manifeste donne les valeurs par
défaut. Le téléchargement d'un modèle suit le pattern tâche longue.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from forge.llm import ModeleLocal
from forge.mcp.serveur import ServeurMCP
from forge.utils.erreurs import ErreurForge
from forge.utils.fichiers import ecrire_json, lire_json
from forge.utils.taches import Tache

DOSSIER = Path(__file__).resolve().parent.parent
FICHIER_CONFIG = DOSSIER / "modele.json"
DOSSIER_TACHES = DOSSIER / "taches"

SYSTEME_PAR_DEFAUT = (
    "Tu es un rédacteur ancré. Tu écris en français, clairement. Tu n'utilises que les faits fournis : "
    "chaque chiffre est recopié tel quel, tu n'en inventes aucun et tu ne complètes pas ce qui manque. "
    "Si les faits ne suffisent pas, dis-le."
)


def configuration() -> dict:
    with open(DOSSIER / "forge.json", encoding="utf-8") as f:
        defaut = json.load(f).get("modele", {})
    if FICHIER_CONFIG.exists():
        try:
            return {**defaut, **lire_json(FICHIER_CONFIG)}
        except Exception:
            pass
    return defaut


def modele(nom: str | None = None) -> ModeleLocal:
    c = configuration()
    if nom:
        c = {**c, "nom": nom}
    return ModeleLocal(c)


serveur = ServeurMCP("modele-local", "0.1.0")


@serveur.service("etat", "État du serveur de modèles et du modèle actif.", entree={"type": "object"}, sortie={"type": "object"})
def etat(_entree: dict) -> dict:
    c = configuration()
    m = modele()
    try:
        modeles = m.modeles()
        joignable = True
    except ErreurForge as exc:
        return {"backend": m.backend, "url": m.url, "joignable": False, "modeles": [], "modele_actif": c.get("nom", ""), "pret": False, "gpu": bool(c.get("gpu", True)),
                "explication": exc.cause + " — " + exc.remede}
    pret, explication = m.disponible()
    return {"backend": m.backend, "url": m.url, "joignable": joignable, "modeles": modeles, "modele_actif": c.get("nom", ""), "pret": pret, "gpu": bool(c.get("gpu", True)), "explication": explication}


@serveur.service("choisir_modele", "Rend un modèle actif.", entree={"type": "object", "required": ["nom"]}, sortie={"type": "object"})
def choisir_modele(entree: dict) -> dict:
    c = configuration()
    nouveau = {"nom": entree["nom"], "gpu": bool(entree.get("gpu", c.get("gpu", True)))}
    ecrire_json(FICHIER_CONFIG, nouveau)
    return {"modele_actif": nouveau["nom"], "gpu": nouveau["gpu"]}


@serveur.service("installer_modele", "Télécharge un modèle (tâche longue).", entree={"type": "object", "required": ["nom"]}, sortie="forge://tache/progres@1")
def installer_modele(entree: dict) -> dict:
    nom = entree["nom"]
    tache = Tache(DOSSIER_TACHES, nom.replace(":", "_").replace("/", "_"))
    tache.demarrer(f"téléchargement de {nom}")
    m = modele()
    try:
        m.installer(nom, progres=lambda pct, etape: tache.progres(pct, etape), annulee=tache.annulee)
    except ErreurForge as exc:
        return tache.echouer(exc.texte())
    if tache.annulee():
        return tache.annuler("téléchargement interrompu (les parties reçues sont conservées par Ollama)")
    actif = configuration().get("nom", "")
    installes = m.modeles()
    if not actif or (actif not in installes and f"{actif}:latest" not in installes):
        ecrire_json(FICHIER_CONFIG, {**configuration(), "nom": nom})  # aucun modèle actif utilisable → celui-ci
    return tache.terminer(etape=f"{nom} installé")


def _prompt(entree: dict) -> str:
    consigne = entree["consigne"]
    if entree.get("faits") is not None:
        return f"Faits (JSON) :\n{json.dumps(entree['faits'], ensure_ascii=False, indent=2)}\n\nConsigne : {consigne}"
    return consigne


@serveur.service("generer", "Rédige un texte ancré sur les faits fournis.", entree={"type": "object", "required": ["consigne"]}, sortie="forge://texte/generation@1")
def generer(entree: dict) -> dict:
    m = modele(entree.get("modele"))
    pret, explication = m.disponible()
    if not pret:
        raise ErreurForge(f"modèle non disponible : {explication}", "ouvrez la page de la brique modele-local pour installer ou choisir un modèle")
    debut = time.time()
    texte = m.generer(_prompt(entree), systeme=entree.get("systeme", SYSTEME_PAR_DEFAUT), temperature=float(entree.get("temperature", 0.0)), max_tokens=entree.get("max_tokens"))
    return {"texte": texte, "modele": m.nom, "backend": m.backend, "continuations": getattr(m, "dernieres_continuations", 0), "duree_s": round(time.time() - debut, 2)}


@serveur.service("generer_json", "Sortie JSON contrainte par un schéma.", entree={"type": "object", "required": ["consigne", "schema"]}, sortie={"type": "object", "required": ["valeur", "modele"]})
def generer_json(entree: dict) -> dict:
    m = modele(entree.get("modele"))
    pret, explication = m.disponible()
    if not pret:
        raise ErreurForge(f"modèle non disponible : {explication}", "ouvrez la page de la brique modele-local pour installer ou choisir un modèle")
    valeur = m.generer_json(_prompt(entree), entree["schema"], systeme=entree.get("systeme", SYSTEME_PAR_DEFAUT))
    return {"valeur": valeur, "modele": m.nom, "backend": m.backend}


if __name__ == "__main__":
    serveur.servir()
    sys.exit(0)

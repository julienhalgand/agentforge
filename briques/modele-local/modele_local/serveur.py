"""modele-local : le modèle de langage local comme une brique MCP, avec son moteur intégré.

Rien à installer à part : la brique télécharge llama.cpp et un modèle GGUF
depuis sa page, lance le moteur elle-même sur 127.0.0.1 et s'en sert.
Le modèle actif et le choix GPU sont mémorisés dans `modele.json`.
Backend « ollama » ou « openai-compatible » possible pour qui en a déjà un.
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

from . import moteur as mod_moteur

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


def enregistrer_configuration(**champs) -> dict:
    c = {**configuration(), **champs}
    ecrire_json(FICHIER_CONFIG, {k: c[k] for k in ("backend", "nom", "url", "gpu") if k in c})
    return c


def modele_pret(nom: str | None = None) -> ModeleLocal:
    """Un ModeleLocal prêt à générer : pour le backend intégré, démarre le moteur s'il le faut."""
    c = configuration()
    if nom:
        c = {**c, "nom": nom}
    if c.get("backend", "integre") == "integre":
        nom_modele = c.get("nom") or mod_moteur.MODELE_PAR_DEFAUT
        if not mod_moteur.moteur_installe() or not (mod_moteur.DOSSIER_MODELES / f"{nom_modele}.gguf").exists():
            preparer({"modele": nom_modele})  # première utilisation : tout se prépare tout seul (peut prendre plusieurs minutes)
            if not mod_moteur.moteur_installe() or not (mod_moteur.DOSSIER_MODELES / f"{nom_modele}.gguf").exists():
                raise ErreurForge("la préparation automatique a échoué", "ouvrez la page de la brique modele-local : la cause y est affichée", [Tache(DOSSIER_TACHES, "preparation").etat().get("etape", "")])
        info = mod_moteur.demarrer_serveur(nom_modele, bool(c.get("gpu", True)))
        if info.get("repli_cpu"):
            enregistrer_configuration(gpu=False)  # le GPU n'a pas marché : on reste en CPU désormais
        return ModeleLocal({"backend": "integre", "nom": nom_modele, "url": mod_moteur._url(info["port"]), "gpu": not info.get("repli_cpu") and c.get("gpu", True)})
    m = ModeleLocal(c)
    pret, explication = m.disponible()
    if not pret:
        raise ErreurForge(f"modèle non disponible : {explication}", "ouvrez la page de la brique modele-local")
    return m


serveur = ServeurMCP("modele-local", "0.1.0")


@serveur.service("etat", "Moteur, modèles, serveur : où en est-on ?", entree={"type": "object"}, sortie={"type": "object"})
def etat(_entree: dict) -> dict:
    c = configuration()
    backend = c.get("backend", "integre")
    base = {"backend": backend, "modele_actif": c.get("nom", ""), "gpu": bool(c.get("gpu", True)), "plateforme": "/".join(mod_moteur.plateforme()), "catalogue": [{"nom": k, **v} for k, v in mod_moteur.CATALOGUE.items()]}
    if backend == "integre":
        moteur = mod_moteur.moteur_installe()
        modeles = mod_moteur.modeles_installes()
        en_marche = mod_moteur.serveur_en_marche()
        pret = bool(moteur) and any(m["nom"] == c.get("nom") for m in modeles)
        if not moteur:
            explication = "le moteur n'est pas encore installé (un seul téléchargement, ≈ 20 à 80 Mo)"
        elif not modeles:
            explication = "aucun modèle téléchargé"
        elif not pret:
            explication = "choisissez un modèle parmi ceux téléchargés"
        else:
            explication = "prêt" + (" — moteur en marche" if en_marche else " — le moteur démarrera à la première demande")
        return {**base, "moteur": moteur, "modeles": [m["nom"] for m in modeles], "modeles_detail": modeles, "serveur": en_marche, "pret": pret, "explication": explication}
    m = ModeleLocal(c)
    try:
        modeles = m.modeles()
        pret, explication = m.disponible()
        return {**base, "url": m.url, "joignable": True, "modeles": modeles, "pret": pret, "explication": explication}
    except ErreurForge as exc:
        return {**base, "url": m.url, "joignable": False, "modeles": [], "pret": False, "explication": exc.cause + " — " + exc.remede}


@serveur.service("preparer", "Prépare tout seul : moteur puis modèle par défaut (tâche longue, idempotente). Lancé automatiquement par le hub.", entree={"type": "object"}, sortie="forge://tache/progres@1")
def preparer(entree: dict) -> dict:
    c = configuration()
    if c.get("backend", "integre") != "integre":
        t = Tache(DOSSIER_TACHES, "preparation")
        t.demarrer("backend externe : rien à préparer")
        return t.terminer(etape="backend externe : rien à préparer")
    tache = Tache(DOSSIER_TACHES, "preparation")
    if tache.etat().get("etat") == "en_cours" and tache.etat().get("mis_a_jour_ms", 0) > (time.time() - 120) * 1000:
        return tache.etat()  # déjà en cours ailleurs (verrou souple)
    tache.demarrer("préparation automatique")
    gpu = bool(entree.get("gpu", c.get("gpu", True)))
    modele = entree.get("modele") or c.get("nom") or mod_moteur.MODELE_PAR_DEFAUT
    try:
        mod_moteur.preparer(gpu, progres=tache.progres, annulee=tache.annulee, modele=modele)
    except ErreurForge as exc:
        return tache.echouer(exc.texte())
    if tache.annulee():
        return tache.annuler("préparation interrompue ; elle reprendra au prochain démarrage")
    enregistrer_configuration(backend="integre", nom=modele, gpu=gpu)
    return tache.terminer(etape=f"prêt — {modele}")


@serveur.service("installer_moteur", "Télécharge et installe le moteur llama.cpp (tâche longue).", entree={"type": "object"}, sortie="forge://tache/progres@1")
def installer_moteur(entree: dict) -> dict:
    gpu = bool(entree.get("gpu", configuration().get("gpu", True)))
    tache = Tache(DOSSIER_TACHES, "moteur")
    tache.demarrer("installation du moteur")
    try:
        info = mod_moteur.installer_moteur(gpu, progres=tache.progres, annulee=tache.annulee)
    except ErreurForge as exc:
        return tache.echouer(exc.texte())
    if tache.annulee():
        return tache.annuler("installation interrompue")
    enregistrer_configuration(backend="integre", gpu=gpu)
    return tache.terminer(info["executable"], etape=f"moteur {info['version']} installé")


@serveur.service("installer_modele", "Télécharge un modèle GGUF (tâche longue, reprise possible).", entree={"type": "object", "required": ["nom"]}, sortie="forge://tache/progres@1")
def installer_modele(entree: dict) -> dict:
    nom = entree["nom"]
    tache = Tache(DOSSIER_TACHES, nom.replace(":", "_").replace("/", "_"))
    tache.demarrer(f"téléchargement de {nom}")
    try:
        if configuration().get("backend", "integre") == "integre":
            fichier = mod_moteur.telecharger_modele(nom, progres=tache.progres, annulee=tache.annulee, url=entree.get("url"))
            if entree.get("activer", True):
                enregistrer_configuration(nom=nom)
                if mod_moteur.serveur_en_marche():
                    mod_moteur.arreter_serveur()  # la prochaine génération repartira avec ce modèle
            return tache.terminer(str(fichier), etape=f"{nom} téléchargé et actif")
        m = ModeleLocal(configuration())
        m.installer(nom, progres=tache.progres, annulee=tache.annulee)
        if tache.annulee():
            return tache.annuler("téléchargement interrompu")
        if not configuration().get("nom"):
            enregistrer_configuration(nom=nom)
        return tache.terminer(etape=f"{nom} installé")
    except ErreurForge as exc:
        return tache.echouer(exc.texte())


@serveur.service("choisir_modele", "Rend un modèle actif (et le choix CPU/GPU).", entree={"type": "object", "required": ["nom"]}, sortie={"type": "object"})
def choisir_modele(entree: dict) -> dict:
    c = enregistrer_configuration(nom=entree["nom"], gpu=bool(entree.get("gpu", configuration().get("gpu", True))))
    if c.get("backend", "integre") == "integre" and mod_moteur.serveur_en_marche():
        mod_moteur.arreter_serveur()  # le prochain appel relancera le moteur avec ce modèle
    return {"modele_actif": c["nom"], "gpu": c["gpu"]}


@serveur.service("demarrer", "Démarre le moteur intégré avec le modèle actif.", entree={"type": "object"}, sortie={"type": "object"})
def demarrer(_entree: dict) -> dict:
    c = configuration()
    if c.get("backend", "integre") != "integre":
        raise ErreurForge("le backend n'est pas le moteur intégré", "rien à démarrer ici")
    info = mod_moteur.demarrer_serveur(c.get("nom", ""), bool(c.get("gpu", True)))
    return {"port": info["port"], "modele": info["modele"], "gpu": info["gpu"], "pid": info["pid"]}


@serveur.service("arreter", "Arrête le moteur intégré (libère la mémoire).", entree={"type": "object"}, sortie={"type": "object"})
def arreter(_entree: dict) -> dict:
    return {"arrete": mod_moteur.arreter_serveur()}


@serveur.service("configurer_backend", "Utiliser un autre serveur de modèles déjà présent (ollama, openai-compatible) ou revenir au moteur intégré.", entree={"type": "object", "required": ["backend"]}, sortie={"type": "object"})
def configurer_backend(entree: dict) -> dict:
    champs = {"backend": entree["backend"]}
    if entree.get("url"):
        champs["url"] = entree["url"]
    c = enregistrer_configuration(**champs)
    return {"backend": c["backend"], "url": c.get("url")}


def _prompt(entree: dict) -> str:
    consigne = entree["consigne"]
    if entree.get("faits") is not None:
        return f"Faits (JSON) :\n{json.dumps(entree['faits'], ensure_ascii=False, indent=2)}\n\nConsigne : {consigne}"
    return consigne


@serveur.service("generer", "Rédige un texte ancré sur les faits fournis.", entree={"type": "object", "required": ["consigne"]}, sortie="forge://texte/generation@1")
def generer(entree: dict) -> dict:
    m = modele_pret(entree.get("modele"))
    debut = time.time()
    texte = m.generer(_prompt(entree), systeme=entree.get("systeme", SYSTEME_PAR_DEFAUT), temperature=float(entree.get("temperature", 0.0)), max_tokens=entree.get("max_tokens"))
    return {"texte": texte, "modele": m.nom, "backend": m.backend, "continuations": getattr(m, "dernieres_continuations", 0), "duree_s": round(time.time() - debut, 2)}


@serveur.service("generer_json", "Sortie JSON contrainte par un schéma.", entree={"type": "object", "required": ["consigne", "schema"]}, sortie={"type": "object", "required": ["valeur", "modele"]})
def generer_json(entree: dict) -> dict:
    m = modele_pret(entree.get("modele"))
    valeur = m.generer_json(_prompt(entree), entree["schema"], systeme=entree.get("systeme", SYSTEME_PAR_DEFAUT))
    return {"valeur": valeur, "modele": m.nom, "backend": m.backend}


if __name__ == "__main__":
    serveur.servir()
    sys.exit(0)

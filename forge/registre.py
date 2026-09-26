"""Registre local des briques et des pipelines.

Les briques sont découvertes dans, par ordre :
1. les dossiers listés dans la variable `FORGE_BRIQUES` (séparateur `;` sous
   Windows, `:` ailleurs) ;
2. `~/.agentforge/briques/` (installations `forge installer`) ;
3. `briques/` du dépôt agentforge (exemples livrés).
Les pipelines : `~/.agentforge/pipelines/` puis `pipelines/` du dépôt.

Installation depuis GitHub : `forge installer https://github.com/alice/prix-agent`
clone le dépôt (ou le met à jour) dans `~/.agentforge/briques/<nom>`, lit son
manifeste et le valide. Rien n'est jamais supprimé : `forge supprimer` déplace
vers `~/.agentforge/.corbeille/`.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from . import manifeste as mod_manifeste
from .utils.erreurs import ErreurForge
from .utils.fichiers import mettre_a_la_corbeille

RACINE_DEPOT = Path(__file__).resolve().parent.parent
DOSSIER_UTILISATEUR = Path(os.environ.get("FORGE_DOSSIER", Path.home() / ".agentforge"))


def dossiers_briques() -> list[Path]:
    dossiers: list[Path] = []
    for brut in os.environ.get("FORGE_BRIQUES", "").split(os.pathsep):
        if brut.strip():
            dossiers.append(Path(brut.strip()))
    dossiers.append(DOSSIER_UTILISATEUR / "briques")
    dossiers.append(RACINE_DEPOT / "briques")
    return dossiers


def dossiers_pipelines() -> list[Path]:
    return [DOSSIER_UTILISATEUR / "pipelines", RACINE_DEPOT / "pipelines"]


def lister_briques() -> tuple[list[mod_manifeste.Manifeste], list[str]]:
    """(manifestes valides, problèmes) — une brique cassée n'empêche pas les autres."""
    trouvees: dict[str, mod_manifeste.Manifeste] = {}
    problemes: list[str] = []
    for dossier in dossiers_briques():
        if not dossier.is_dir():
            continue
        for sous in sorted(dossier.iterdir()):
            if not (sous / mod_manifeste.NOM_MANIFESTE).exists() or sous.name.startswith("."):
                continue
            try:
                m = mod_manifeste.charger(sous)
            except ErreurForge as exc:
                problemes.append(f"{sous} : {exc.texte()}")
                continue
            if m.type == "pipeline":
                continue
            trouvees.setdefault(m.nom, m)  # la première trouvée gagne (ordre de priorité)
    return list(trouvees.values()), problemes


def trouver_brique(nom: str) -> mod_manifeste.Manifeste:
    briques, problemes = lister_briques()
    for m in briques:
        if m.nom == nom:
            return m
    details = [f"disponibles : {', '.join(b.nom for b in briques) or 'aucune'}"] + problemes
    raise ErreurForge(f"Brique « {nom} » introuvable", "installez-la avec `forge installer <url github>` ou vérifiez son forge.json", details)


def lister_pipelines() -> tuple[list[mod_manifeste.Manifeste], list[str]]:
    trouves: dict[str, mod_manifeste.Manifeste] = {}
    problemes: list[str] = []
    for dossier in dossiers_pipelines():
        if not dossier.is_dir():
            continue
        for fichier in sorted(dossier.glob("*.json")):
            if fichier.name.startswith("."):
                continue
            try:
                m = charger_pipeline(fichier)
            except ErreurForge as exc:
                problemes.append(f"{fichier} : {exc.texte()}")
                continue
            trouves.setdefault(m.nom, m)
    return list(trouves.values()), problemes


def charger_pipeline(fichier: Path | str) -> mod_manifeste.Manifeste:
    """Un pipeline est un manifeste de type « pipeline » dans un fichier JSON seul."""
    import json

    fichier = Path(fichier).resolve()
    if not fichier.exists():
        raise ErreurForge(f"Pipeline introuvable : {fichier}", "vérifiez le chemin ou le nom")
    try:
        with open(fichier, encoding="utf-8") as f:
            brut = json.load(f)
    except json.JSONDecodeError as exc:
        raise ErreurForge(f"{fichier} : JSON invalide ligne {exc.lineno} : {exc.msg}", "corrigez la syntaxe") from exc
    from . import schemas

    schemas.valider(brut, mod_manifeste.SCHEMA_MANIFESTE, contexte=f"pipeline {fichier}")
    if brut.get("type") != "pipeline":
        raise ErreurForge(f"{fichier} n'est pas un pipeline (type = {brut.get('type')})", "utilisez \"type\": \"pipeline\"")
    m = mod_manifeste.Manifeste(dossier=fichier.parent, brut=brut)
    m.brut["_fichier"] = str(fichier)
    mod_manifeste.verifier_coherence(m)
    return m


def trouver_pipeline(nom: str) -> mod_manifeste.Manifeste:
    chemin = Path(nom)
    if chemin.suffix == ".json" and chemin.exists():
        return charger_pipeline(chemin)
    pipelines, problemes = lister_pipelines()
    for m in pipelines:
        if m.nom == nom:
            return m
    raise ErreurForge(
        f"Pipeline « {nom} » introuvable",
        "donnez le nom d'un pipeline connu ou le chemin d'un fichier .json",
        [f"disponibles : {', '.join(p.nom for p in pipelines) or 'aucun'}"] + problemes,
    )


MOTIF_GITHUB = re.compile(r"^(?:https://github\.com/|git@github\.com:|github\.com/)?([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")


def installer(source: str, nom: str | None = None) -> mod_manifeste.Manifeste:
    """Clone (ou met à jour) une brique depuis GitHub ou un dossier local."""
    cible_parent = DOSSIER_UTILISATEUR / "briques"
    cible_parent.mkdir(parents=True, exist_ok=True)
    local = Path(source)
    if local.is_dir():
        m = mod_manifeste.charger(local)
        cible = cible_parent / (nom or m.nom)
        if cible.exists():
            raise ErreurForge(f"{cible} existe déjà", "supprimez-la d'abord avec `forge supprimer` (elle ira à la corbeille)")
        import shutil

        shutil.copytree(local, cible, ignore=shutil.ignore_patterns(".git", "__pycache__", ".corbeille", ".sauvegardes"))
        return mod_manifeste.charger(cible)

    m_url = MOTIF_GITHUB.match(source.strip())
    if not m_url:
        raise ErreurForge(f"Source non reconnue : {source}", "donnez une URL GitHub (https://github.com/utilisateur/depot) ou un dossier local")
    utilisateur, depot = m_url.groups()
    url = f"https://github.com/{utilisateur}/{depot}.git"
    cible = cible_parent / (nom or depot)
    if (cible / ".git").exists():
        commande = ["git", "-C", str(cible), "pull", "--ff-only"]
    else:
        commande = ["git", "clone", "--depth", "1", url, str(cible)]
    try:
        termine = subprocess.run(commande, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError as exc:
        raise ErreurForge("git est introuvable", "installez Git (https://git-scm.com) ou ajoutez-le au PATH") from exc
    if termine.returncode != 0:
        raise ErreurForge(f"Échec de `{' '.join(commande)}`", "vérifiez l'URL, vos droits et la connexion", termine.stderr.splitlines()[-10:])
    return mod_manifeste.charger(cible)


def supprimer(nom: str) -> Path:
    m = trouver_brique(nom)
    if RACINE_DEPOT in m.dossier.parents:
        raise ErreurForge(f"{nom} est une brique livrée avec agentforge ({m.dossier})", "supprimez-la avec git, pas avec forge")
    return mettre_a_la_corbeille(m.dossier, racine=DOSSIER_UTILISATEUR)

"""Fichiers : rien ne se perd.

- écriture atomique : fichier temporaire caché puis `os.replace` ;
- sauvegarde avant toute écriture sur des données existantes (`.sauvegardes/`) ;
- suppression = déplacement vers `.corbeille/`, jamais `rm` ;
- preuve après coup : empreinte SHA-256 avant/après pour montrer que ce qui
  ne devait pas changer n'a pas changé.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

NOM_CORBEILLE = ".corbeille"
NOM_SAUVEGARDES = ".sauvegardes"


def ecrire_atomique(chemin: Path | str, contenu: str | bytes, sauvegarder: bool = True) -> Path:
    """Écrit `contenu` dans `chemin` de façon atomique.

    Le contenu est d'abord écrit dans un fichier caché du même dossier
    (`.nom.tmp-<pid>`), puis renommé par-dessus la cible avec `os.replace`,
    atomique sur Windows comme sur Linux. Si la cible existe déjà et que
    `sauvegarder` est vrai, une copie horodatée est faite dans `.sauvegardes/`.
    """
    chemin = Path(chemin)
    chemin.parent.mkdir(parents=True, exist_ok=True)
    if sauvegarder and chemin.exists():
        sauvegarder_fichier(chemin)
    temporaire = chemin.parent / f".{chemin.name}.tmp-{os.getpid()}"
    mode = "wb" if isinstance(contenu, bytes) else "w"
    encodage = None if isinstance(contenu, bytes) else "utf-8"
    with open(temporaire, mode, encoding=encodage) as f:
        f.write(contenu)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporaire, chemin)
    return chemin


def ecrire_json(chemin: Path | str, donnees: Any, sauvegarder: bool = True) -> Path:
    texte = json.dumps(donnees, ensure_ascii=False, indent=2) + "\n"
    return ecrire_atomique(chemin, texte, sauvegarder=sauvegarder)


def lire_json(chemin: Path | str) -> Any:
    with open(chemin, encoding="utf-8") as f:
        return json.load(f)


def sauvegarder_fichier(chemin: Path | str) -> Path:
    """Copie `chemin` dans `.sauvegardes/<nom>.<horodatage>` à côté de lui."""
    chemin = Path(chemin)
    dossier = chemin.parent / NOM_SAUVEGARDES
    dossier.mkdir(exist_ok=True)
    horodatage = time.strftime("%Y-%m-%d_%H-%M-%S")
    cible = dossier / f"{chemin.name}.{horodatage}"
    compteur = 1
    while cible.exists():
        cible = dossier / f"{chemin.name}.{horodatage}.{compteur}"
        compteur += 1
    shutil.copy2(chemin, cible)
    return cible


def mettre_a_la_corbeille(chemin: Path | str, racine: Path | str | None = None) -> Path:
    """Déplace un fichier ou dossier vers `.corbeille/` (dans `racine` ou à côté)."""
    chemin = Path(chemin)
    if not chemin.exists():
        raise FileNotFoundError(chemin)
    base = Path(racine) if racine else chemin.parent
    corbeille = base / NOM_CORBEILLE
    corbeille.mkdir(parents=True, exist_ok=True)
    horodatage = time.strftime("%Y-%m-%d_%H-%M-%S")
    cible = corbeille / f"{chemin.name}.{horodatage}"
    compteur = 1
    while cible.exists():
        cible = corbeille / f"{chemin.name}.{horodatage}.{compteur}"
        compteur += 1
    shutil.move(str(chemin), str(cible))
    return cible


def empreinte(chemin: Path | str) -> str:
    """SHA-256 d'un fichier (ou de tous les fichiers d'un dossier, triés)."""
    chemin = Path(chemin)
    h = hashlib.sha256()
    if chemin.is_dir():
        for fichier in sorted(p for p in chemin.rglob("*") if p.is_file()):
            if NOM_CORBEILLE in fichier.parts or NOM_SAUVEGARDES in fichier.parts:
                continue
            h.update(str(fichier.relative_to(chemin)).encode("utf-8"))
            h.update(fichier.read_bytes())
    else:
        h.update(chemin.read_bytes())
    return h.hexdigest()


def empreintes(chemins: list[Path | str]) -> dict[str, str]:
    """Empreintes de plusieurs chemins, pour prouver qu'ils n'ont pas bougé."""
    return {str(c): empreinte(c) for c in chemins if Path(c).exists()}


def verifier_inchanges(avant: dict[str, str]) -> list[str]:
    """Retourne la liste des chemins dont l'empreinte a changé depuis `avant`."""
    changes = []
    for chemin, e in avant.items():
        if not Path(chemin).exists() or empreinte(chemin) != e:
            changes.append(chemin)
    return changes

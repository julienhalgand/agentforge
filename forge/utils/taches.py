"""Tâches longues : l'état vit sur le disque, jamais dans l'onglet.

Contrat forge://tache/progres@1. Pour une tâche `<nom>` dans un dossier :
- `<nom>.progres.json` : état (réécrit au plus une fois par seconde) ;
- `<nom>.annuler`     : drapeau posé par la page ; la tâche le consulte à
  chaque bloc et s'arrête proprement — jamais de kill ;
- `<nom>.erreur.txt`  : la cause en cas d'échec ;
- le résultat final est un fichier à côté (`<nom>.<type>.json`, `.mp3`…).

Côté brique :

    tache = Tache(dossier_taches, "livre-42")
    tache.demarrer("lecture du PDF")
    for i, page in enumerate(pages):
        if tache.annulee():
            return tache.annuler()
        traiter(page)
        tache.progres(100 * (i + 1) / len(pages), etape=f"page {i + 1}/{len(pages)}", eta_s=…)
    tache.terminer("livre-42.audio.json")

Côté page : interroger `<nom>.progres.json` (servi par le hub), poser le
drapeau via le hub. Un rafraîchissement ne perd rien.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .fichiers import ecrire_atomique, ecrire_json, lire_json

ETATS = ("en_attente", "en_cours", "termine", "echec", "annule")


class Tache:
    def __init__(self, dossier: Path | str, nom: str, intervalle_s: float = 1.0):
        self.dossier = Path(dossier)
        self.nom = nom
        self.intervalle_s = intervalle_s
        self._dernier_ecrit = 0.0
        self._debut = 0.0
        self.dossier.mkdir(parents=True, exist_ok=True)

    # --- chemins ----------------------------------------------------------
    @property
    def fichier_progres(self) -> Path:
        return self.dossier / f"{self.nom}.progres.json"

    @property
    def fichier_annuler(self) -> Path:
        return self.dossier / f"{self.nom}.annuler"

    @property
    def fichier_erreur(self) -> Path:
        return self.dossier / f"{self.nom}.erreur.txt"

    # --- cycle de vie -----------------------------------------------------
    def demarrer(self, etape: str = "démarrage") -> None:
        self._debut = time.time()
        if self.fichier_annuler.exists():
            self.fichier_annuler.unlink()  # un drapeau d'une exécution précédente ne compte pas
        if self.fichier_erreur.exists():
            self.fichier_erreur.unlink()
        self._ecrire("en_cours", 0.0, etape, force=True)

    def progres(self, pourcentage: float, etape: str = "", eta_s: float | None = None, force: bool = False) -> None:
        """Réécrit l'état au plus une fois par `intervalle_s` (sauf `force`)."""
        if eta_s is None and pourcentage > 0 and self._debut:
            ecoule = time.time() - self._debut
            eta_s = round(ecoule * (100 - pourcentage) / pourcentage, 1)
        self._ecrire("en_cours", pourcentage, etape, eta_s=eta_s, force=force)

    def annulee(self) -> bool:
        """À consulter à chaque bloc : la page a-t-elle posé le drapeau ?"""
        return self.fichier_annuler.exists()

    def annuler(self, etape: str = "annulée à la demande", pourcentage: float | None = None) -> dict:
        """`pourcentage` : l'avancement réel au moment de l'arrêt (le dernier écrit peut dater, à cause du lissage)."""
        if pourcentage is None:
            pourcentage = self._lire().get("pourcentage", 0.0)
        self._ecrire("annule", pourcentage, etape, force=True)
        if self.fichier_annuler.exists():
            self.fichier_annuler.unlink()
        return self._lire()

    def terminer(self, resultat: str | Path | None = None, etape: str = "terminé") -> dict:
        self._ecrire("termine", 100.0, etape, resultat=str(resultat) if resultat else None, force=True)
        return self._lire()

    def echouer(self, cause: str, etape: str = "échec") -> dict:
        ecrire_atomique(self.fichier_erreur, cause + "\n", sauvegarder=False)
        self._ecrire("echec", self._lire().get("pourcentage", 0.0), etape, erreur=self.fichier_erreur.name, force=True)
        return self._lire()

    # --- lecture ------------------------------------------------------------
    def etat(self) -> dict:
        return self._lire()

    # --- interne ------------------------------------------------------------
    def _lire(self) -> dict:
        if self.fichier_progres.exists():
            try:
                etat = lire_json(self.fichier_progres)
                if etat.get("erreur") and self.fichier_erreur.exists():
                    etat["erreur_texte"] = self.fichier_erreur.read_text(encoding="utf-8").strip()  # la cause, lisible sur place
                return etat
            except (OSError, json.JSONDecodeError):
                pass
        return {"tache": self.nom, "etat": "en_attente", "pourcentage": 0.0, "mis_a_jour_ms": 0}

    def _ecrire(self, etat: str, pourcentage: float, etape: str, eta_s=None, resultat=None, erreur=None, force=False) -> None:
        maintenant = time.time()
        if not force and maintenant - self._dernier_ecrit < self.intervalle_s:
            return
        self._dernier_ecrit = maintenant
        donnees = {
            "tache": self.nom,
            "etat": etat,
            "pourcentage": round(max(0.0, min(100.0, float(pourcentage))), 1),
            "etape": etape,
            "mis_a_jour_ms": int(maintenant * 1000),
        }
        if eta_s is not None:
            donnees["eta_s"] = eta_s
        if resultat:
            donnees["resultat"] = resultat
        if erreur:
            donnees["erreur"] = erreur
        ecrire_json(self.fichier_progres, donnees, sauvegarder=False)


def poser_annulation(dossier: Path | str, nom: str) -> Path:
    """Côté hub/page : demande l'arrêt coopératif de la tâche."""
    drapeau = Path(dossier) / f"{nom}.annuler"
    ecrire_atomique(drapeau, "", sauvegarder=False)
    return drapeau


def lire_etat(dossier: Path | str, nom: str) -> dict:
    return Tache(dossier, nom).etat()


def lister_taches(dossier: Path | str) -> list[dict]:
    dossier = Path(dossier)
    if not dossier.is_dir():
        return []
    etats = []
    for f in sorted(dossier.glob("*.progres.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            etats.append(lire_json(f))
        except (OSError, json.JSONDecodeError):
            continue
    return etats

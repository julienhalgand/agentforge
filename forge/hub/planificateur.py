"""Planificateur du hub : quotidien HH:MM, toutes les N heures, jamais.

- exécutions **séquentielles** : une file, un seul fil d'exécution ;
- **verrou anti-doublon** : une cible déjà en file ou en cours n'est pas
  ré-ajoutée ;
- **rattrapage au démarrage** : une occurrence manquée (machine éteinte) est
  exécutée au lancement du hub si `rattrapage` est vrai ;
- l'état vit sur le disque : `~/.agentforge/executions/<cible>.json`
  (dernier début/fin, état, manques, erreur, journal), écrit atomiquement.

Une cible est un pipeline (`pipeline:<nom>`) ou une brique planifiée
(`brique:<nom>`, exécution du service indiqué dans sa planification, ou du
premier service avec ses paramètres d'exemple). La planification d'une cible
peut être remplacée par l'utilisateur depuis le hub : le choix est conservé
dans `~/.agentforge/planifications.json` et prime sur le manifeste.
"""

from __future__ import annotations

import datetime as dt
import queue
import threading
import time
import traceback
from pathlib import Path
from typing import Any

from .. import pipeline as mod_pipeline
from .. import registre, schemas
from ..mcp.client import ClientMCP
from ..utils.erreurs import ErreurForge
from ..utils.fichiers import ecrire_json, lire_json

DOSSIER_EXECUTIONS = registre.DOSSIER_UTILISATEUR / "executions"
FICHIER_PLANIFICATIONS = registre.DOSSIER_UTILISATEUR / "planifications.json"


def _maintenant_ms() -> int:
    return int(time.time() * 1000)


class Planificateur:
    def __init__(self):
        self.file: "queue.Queue[str]" = queue.Queue()
        self.en_file: set[str] = set()
        self.en_cours: str | None = None
        self.verrou = threading.Lock()
        self.arret = threading.Event()
        self.journal_courant: list[str] = []

    # --- état sur disque ----------------------------------------------------
    def etat(self, cible: str) -> dict:
        fichier = DOSSIER_EXECUTIONS / f"{cible.replace(':', '_')}.json"
        if fichier.exists():
            try:
                return lire_json(fichier)
            except Exception:
                return {"etat": "illisible", "fichier": str(fichier)}
        return {"etat": "jamais_execute"}

    def _enregistrer(self, cible: str, donnees: dict) -> None:
        DOSSIER_EXECUTIONS.mkdir(parents=True, exist_ok=True)
        ecrire_json(DOSSIER_EXECUTIONS / f"{cible.replace(':', '_')}.json", donnees, sauvegarder=False)

    def planifications_utilisateur(self) -> dict[str, dict]:
        if FICHIER_PLANIFICATIONS.exists():
            try:
                return lire_json(FICHIER_PLANIFICATIONS)
            except Exception:
                return {}
        return {}

    def definir_planification(self, cible: str, planification: dict) -> None:
        schemas.valider(planification, "forge://planification/planification@1", contexte="planification")
        toutes = self.planifications_utilisateur()
        toutes[cible] = planification
        ecrire_json(FICHIER_PLANIFICATIONS, toutes)

    def planification(self, cible: str, defaut: dict) -> dict:
        return self.planifications_utilisateur().get(cible, defaut)

    # --- cibles -------------------------------------------------------------
    def cibles(self) -> list[dict]:
        """Toutes les cibles planifiables avec leur planification effective et leur état."""
        resultat = []
        briques, _ = registre.lister_briques()
        for m in briques:
            cible = f"brique:{m.nom}"
            resultat.append({"cible": cible, "type": "brique", "nom": m.nom, "planification": self.planification(cible, m.planification), "etat": self.etat(cible)})
        pipelines, _ = registre.lister_pipelines()
        for m in pipelines:
            cible = f"pipeline:{m.nom}"
            resultat.append({"cible": cible, "type": "pipeline", "nom": m.nom, "planification": self.planification(cible, m.planification), "etat": self.etat(cible)})
        return resultat

    # --- file d'exécution ---------------------------------------------------
    def demander(self, cible: str) -> bool:
        """Ajoute à la file ; False si déjà en file ou en cours (verrou anti-doublon)."""
        with self.verrou:
            if cible in self.en_file or cible == self.en_cours:
                return False
            self.en_file.add(cible)
        self.file.put(cible)
        return True

    def demarrer(self) -> None:
        threading.Thread(target=self._executant, name="forge-executant", daemon=True).start()
        threading.Thread(target=self._horloge, name="forge-horloge", daemon=True).start()

    def arreter(self) -> None:
        self.arret.set()

    def _executant(self) -> None:
        while not self.arret.is_set():
            try:
                cible = self.file.get(timeout=1)
            except queue.Empty:
                continue
            with self.verrou:
                self.en_file.discard(cible)
                self.en_cours = cible
            try:
                self.executer(cible)
            finally:
                with self.verrou:
                    self.en_cours = None

    def executer(self, cible: str) -> dict:
        self.journal_courant = []
        etat = {"cible": cible, "etat": "en_cours", "debut_ms": _maintenant_ms(), "fin_ms": None, "manques": [], "erreur": None, "journal": self.journal_courant}
        self._enregistrer(cible, etat)

        def journal(message: str) -> None:
            self.journal_courant.append(f"{dt.datetime.now().strftime('%H:%M:%S')} {message}")

        try:
            genre, nom = cible.split(":", 1)
            if genre == "pipeline":
                resultat = mod_pipeline.executer(registre.trouver_pipeline(nom), journal=journal)
                etat["manques"] = resultat.manques
                etat["resultats"] = resultat.resultats
                etat["durees_s"] = resultat.durees_s
            else:
                etat["resultats"] = self._executer_brique(nom, journal)
                etat["manques"] = list(etat["resultats"].get("manques", [])) if isinstance(etat["resultats"], dict) else []
            etat["etat"] = "partiel" if etat["manques"] else "succes"
        except ErreurForge as exc:
            etat["etat"] = "echec"
            etat["erreur"] = exc.en_json()
        except Exception as exc:  # jamais silencieux
            etat["etat"] = "echec"
            etat["erreur"] = {"cause": f"erreur interne : {exc}", "remede": "voir la trace", "details": traceback.format_exc().splitlines()[-8:]}
        etat["fin_ms"] = _maintenant_ms()
        self._enregistrer(cible, etat)
        return etat

    def _executer_brique(self, nom: str, journal) -> Any:
        m = registre.trouver_brique(nom)
        plan = self.planification(f"brique:{nom}", m.planification)
        service = m.service(plan["service"]) if plan.get("service") else (m.services[0] if m.services else None)
        if service is None:
            raise ErreurForge(f"{nom} n'expose aucun service", "ajoutez un service dans forge.json ou passez la planification à « jamais »")
        entree = plan.get("entree", service.exemple or {})
        journal(f"{nom}.{service.nom} {entree}")
        with ClientMCP(m.commande_serveur(), m.dossier) as client:
            return client.appeler(service.nom, entree)

    # --- horloge --------------------------------------------------------------
    def _horloge(self) -> None:
        self._rattrapage()
        derniere_minute = None
        while not self.arret.is_set():
            maintenant = dt.datetime.now()
            minute = maintenant.strftime("%H:%M")
            if minute != derniere_minute:
                derniere_minute = minute
                for c in self.cibles():
                    if self._est_du(c, maintenant):
                        self.demander(c["cible"])
            self.arret.wait(10)

    def _est_du(self, c: dict, maintenant: dt.datetime) -> bool:
        plan = c["planification"]
        etat = c["etat"]
        dernier = etat.get("debut_ms")
        if plan.get("mode") == "quotidien":
            if maintenant.strftime("%H:%M") != plan.get("heure", "08:00"):
                return False
            return not dernier or dt.datetime.fromtimestamp(dernier / 1000).date() < maintenant.date()
        if plan.get("mode") == "toutes_les_n_heures":
            heures = int(plan.get("heures", 1))
            return not dernier or (maintenant.timestamp() * 1000 - dernier) >= heures * 3_600_000 - 30_000
        return False

    def _rattrapage(self) -> None:
        """Au démarrage : exécute ce qui aurait dû tourner pendant que le hub était éteint."""
        maintenant = dt.datetime.now()
        for c in self.cibles():
            plan = c["planification"]
            if not plan.get("rattrapage", True):
                continue
            dernier = c["etat"].get("debut_ms")
            if plan.get("mode") == "quotidien":
                h, mn = (plan.get("heure", "08:00") + ":00").split(":")[:2]
                echeance = maintenant.replace(hour=int(h), minute=int(mn), second=0, microsecond=0)
                if maintenant >= echeance and (not dernier or dt.datetime.fromtimestamp(dernier / 1000) < echeance):
                    self.demander(c["cible"])
            elif plan.get("mode") == "toutes_les_n_heures":
                if not dernier or (maintenant.timestamp() * 1000 - dernier) >= int(plan.get("heures", 1)) * 3_600_000:
                    self.demander(c["cible"])


def instantane(p: Planificateur) -> dict:
    """État global pour l'interface : cibles, en cours, en file."""
    return {"cibles": p.cibles(), "en_cours": p.en_cours, "en_file": sorted(p.en_file), "journal": list(p.journal_courant[-50:])}

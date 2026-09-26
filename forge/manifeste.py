"""Manifeste `forge.json` : identité, transport, services, planification.

Exemple minimal d'une application :

    {
      "forge": 1,
      "type": "application",
      "nom": "prix-agent",
      "version": "0.1.0",
      "description": "Prix comptant et bougies OHLCV",
      "interpreteur": "python",
      "transport": { "type": "services-json", "fichier": "services.json" },
      "services": [
        { "nom": "historique", "entree": { … JSON Schema … }, "sortie": "forge://serie-temporelle/ohlcv@1" }
      ],
      "planification": { "mode": "jamais" }
    }

Champs :
- `type` : « application » (déterministe), « agent » (peut porter un modèle
  local, optionnel) ou « pipeline » ;
- `interpreteur` : « python » ou un chemin complet (conda hors PATH sous
  Windows : `C:\\Users\\…\\envs\\forge\\python.exe`) ;
- `transport.type` : « mcp-stdio » (la brique parle MCP elle-même, `commande`
  donne les arguments passés à l'interpréteur) ou « services-json »
  (brique existante « python -m agent.<service> », exposée par l'adaptateur) ;
- `services[]` : nom, description, `entree` et `sortie` (contrat forge:// ou
  JSON Schema en ligne), `exemple` (paramètres d'exemple pour l'assembleur) ;
- `planification` : contrat forge://planification/planification@1 ; absent ou
  mode « jamais » = à la demande ;
- `page` : page interactive servie par le hub (forme B), optionnel ;
- `rapports` : dossier des rapports (forme A), « rapports » par défaut ;
- `modele` : pour un agent avec LLM local (optionnel) : backend, nom, gpu, cpu_ok.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import schemas
from .utils.erreurs import ErreurForge

NOM_MANIFESTE = "forge.json"
TYPES = ("application", "agent", "pipeline")
TRANSPORTS = ("mcp-stdio", "services-json")
MOTIF_NOM = re.compile(r"^[a-z0-9][a-z0-9-]*$")

SCHEMA_SERVICE = {
    "type": "object",
    "required": ["nom", "entree", "sortie"],
    "properties": {
        "nom": {"type": "string", "pattern": "^[a-z0-9_-]+$"},
        "description": {"type": "string"},
        "entree": {"type": ["string", "object"]},
        "sortie": {"type": ["string", "object"]},
        "exemple": {"type": "object"},
        "duree_longue": {"type": "boolean"},
    },
}

SCHEMA_MANIFESTE = {
    "type": "object",
    "required": ["forge", "type", "nom", "version"],
    "properties": {
        "forge": {"const": 1},
        "type": {"enum": list(TYPES)},
        "nom": {"type": "string", "pattern": MOTIF_NOM.pattern},
        "version": {"type": "string", "pattern": r"^\d+\.\d+\.\d+"},
        "description": {"type": "string"},
        "icone": {"type": "string"},
        "interpreteur": {"type": "string"},
        "transport": {
            "type": "object",
            "required": ["type"],
            "properties": {
                "type": {"enum": list(TRANSPORTS)},
                "commande": {"type": "array", "items": {"type": "string"}},
                "fichier": {"type": "string"},
                "passage_entree": {"enum": ["arguments", "fichier", "stdin"]},
            },
        },
        "services": {"type": "array", "items": SCHEMA_SERVICE},
        "planification": "forge://planification/planification@1",
        "page": {"type": "string"},
        "rapports": {"type": "string"},
        "taches": {"type": "string"},
        "preparation": {"type": "object", "required": ["service"], "properties": {"service": {"type": "string"}, "entree": {"type": "object"}}},
        "modele": {
            "type": "object",
            "required": ["backend", "nom"],
            "properties": {
                "backend": {"enum": ["integre", "ollama", "openai-compatible"]},
                "nom": {"type": "string"},
                "url": {"type": "string"},
                "gpu": {"type": "boolean"},
                "cpu_ok": {"type": "boolean"},
            },
        },
        "depend_de": {"type": "array", "items": {"type": "string"}},
        "etapes": {"type": "array"},
    },
}


@dataclass
class Service:
    nom: str
    entree: Any
    sortie: Any
    description: str = ""
    exemple: dict = field(default_factory=dict)
    duree_longue: bool = False

    def en_json(self) -> dict:
        return {
            "nom": self.nom,
            "description": self.description,
            "entree": self.entree,
            "sortie": self.sortie,
            "exemple": self.exemple,
            "duree_longue": self.duree_longue,
        }


@dataclass
class Manifeste:
    dossier: Path
    brut: dict

    # --- identité ---------------------------------------------------------
    @property
    def nom(self) -> str:
        return self.brut["nom"]

    @property
    def type(self) -> str:
        return self.brut["type"]

    @property
    def version(self) -> str:
        return self.brut["version"]

    @property
    def description(self) -> str:
        return self.brut.get("description", "")

    @property
    def icone(self) -> str:
        return self.brut.get("icone", {"application": "🧩", "agent": "🤖", "pipeline": "🔗"}[self.type])

    # --- exécution --------------------------------------------------------
    @property
    def interpreteur(self) -> str:
        """Chemin de l'interpréteur Python de la brique.

        « python » (ou absent) = l'interpréteur qui exécute forge. Un chemin
        complet est rendu tel quel (environnement conda hors PATH).
        """
        valeur = self.brut.get("interpreteur", "python")
        if valeur in ("python", "python3", ""):
            return sys.executable
        chemin = Path(valeur)
        if chemin.is_absolute():
            if not chemin.exists():
                raise ErreurForge(
                    f"{self.nom} : interpréteur introuvable : {valeur}",
                    "corrigez le champ « interpreteur » de forge.json (chemin complet vers python.exe)",
                )
            return str(chemin)
        trouve = shutil.which(valeur)
        if not trouve:
            raise ErreurForge(
                f"{self.nom} : interpréteur « {valeur} » absent du PATH",
                "donnez le chemin complet dans le champ « interpreteur » de forge.json",
            )
        return trouve

    @property
    def transport(self) -> dict:
        return self.brut.get("transport", {"type": "mcp-stdio", "commande": ["-m", "brique"]})

    @property
    def services(self) -> list[Service]:
        return [
            Service(
                nom=s["nom"],
                entree=s["entree"],
                sortie=s["sortie"],
                description=s.get("description", ""),
                exemple=s.get("exemple", {}),
                duree_longue=s.get("duree_longue", False),
            )
            for s in self.brut.get("services", [])
        ]

    def service(self, nom: str) -> Service:
        for s in self.services:
            if s.nom == nom:
                return s
        raise ErreurForge(
            f"{self.nom} : service « {nom} » inconnu",
            "services disponibles : " + (", ".join(s.nom for s in self.services) or "aucun"),
        )

    @property
    def planification(self) -> dict:
        return self.brut.get("planification", {"mode": "jamais"})

    @property
    def page(self) -> Path | None:
        return (self.dossier / self.brut["page"]) if self.brut.get("page") else None

    @property
    def dossier_rapports(self) -> Path:
        return self.dossier / self.brut.get("rapports", "rapports")

    @property
    def preparation(self) -> dict | None:
        """Service à lancer automatiquement au démarrage du hub (ex. télécharger un moteur), idempotent."""
        return self.brut.get("preparation")

    @property
    def dossier_taches(self) -> Path:
        return self.dossier / self.brut.get("taches", "taches")

    @property
    def modele(self) -> dict | None:
        return self.brut.get("modele")

    @property
    def etapes(self) -> list[dict]:
        return self.brut.get("etapes", [])

    def commande_serveur(self) -> list[str]:
        """Commande qui lance la brique en serveur MCP stdio."""
        t = self.transport
        if t["type"] == "mcp-stdio":
            return [self.interpreteur, *t.get("commande", [])]
        # services-json : l'adaptateur générique (exécuté par le python de forge)
        return [sys.executable, "-m", "forge.mcp.adaptateur", "--dossier", str(self.dossier)]

    def en_json(self) -> dict:
        return {
            **self.brut,
            "dossier": str(self.dossier),
            "icone": self.icone,
            "services": [s.en_json() for s in self.services],
            "planification": self.planification,
        }


def charger(dossier: Path | str) -> Manifeste:
    """Lit et valide `<dossier>/forge.json`."""
    dossier = Path(dossier).resolve()
    fichier = dossier / NOM_MANIFESTE
    if not fichier.exists():
        raise ErreurForge(
            f"Aucun manifeste dans {dossier}",
            f"créez {fichier} (voir docs/PROTOCOLE.md, section « Manifeste »)",
        )
    try:
        with open(fichier, encoding="utf-8") as f:
            brut = json.load(f)
    except json.JSONDecodeError as exc:
        raise ErreurForge(f"{fichier} : JSON invalide ligne {exc.lineno}, colonne {exc.colno} : {exc.msg}", "corrigez la syntaxe") from exc
    schemas.valider(brut, SCHEMA_MANIFESTE, contexte=f"manifeste {fichier}")
    manifeste = Manifeste(dossier=dossier, brut=brut)
    verifier_coherence(manifeste)
    return manifeste


def verifier_coherence(m: Manifeste) -> None:
    """Vérifications au-delà du schéma : contrats existants, transport complet."""
    problemes: list[str] = []
    dossiers_contrats = [m.dossier / "contrats"]
    for s in m.services:
        for sens, schema in (("entree", s.entree), ("sortie", s.sortie)):
            try:
                schemas.resoudre(schema, dossiers_contrats)
            except ErreurForge as exc:
                problemes.append(f"service {s.nom}, {sens} : {exc.cause}")
    t = m.transport
    if m.type != "pipeline":
        if t["type"] == "mcp-stdio" and not t.get("commande"):
            problemes.append("transport mcp-stdio : le champ « commande » est obligatoire")
        if t["type"] == "services-json":
            fichier = m.dossier / t.get("fichier", "services.json")
            if not fichier.exists():
                problemes.append(f"transport services-json : {fichier} introuvable")
    else:
        if not m.etapes:
            problemes.append("pipeline sans étapes")
    if m.page and not m.page.exists():
        problemes.append(f"page déclarée introuvable : {m.page}")
    if problemes:
        raise ErreurForge(f"Manifeste de {m.nom} incohérent", "corrigez chaque point listé", problemes)

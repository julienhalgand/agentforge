"""Exécuteur de pipeline : des étapes typées, vérifiées avant de tourner.

    {
      "forge": 1, "type": "pipeline", "nom": "rapport-btc-quotidien",
      "etapes": [
        { "id": "prix",    "brique": "prix-agent",     "service": "historique",
          "entree": { "actif": "BTC", "intervalle": "1d", "limite": 90 } },
        { "id": "rapport", "brique": "rapport-marche", "service": "generer",
          "entree": { "donnees": "$prix", "titre": "Bitcoin — 90 jours" } }
      ]
    }

Une valeur d'entrée `"$prix"` est le résultat entier de l'étape `prix` ;
`"$prix.bougies"` en est un champ. Avant toute exécution, `verifier()`
contrôle que chaque brique et service existent, que les valeurs littérales
respectent le schéma d'entrée, et que chaque branchement `$…` relie une sortie
à une entrée compatible. Une étape qui répond partiellement n'arrête pas le
pipeline : ses manques sont listés dans le résultat final.
"""

from __future__ import annotations

import copy
import re
import time
from dataclasses import dataclass, field
from typing import Any

from . import manifeste as mod_manifeste
from . import registre, schemas
from .mcp.client import ClientMCP
from .utils.erreurs import ErreurForge

MOTIF_REFERENCE = re.compile(r"^\$([a-zA-Z0-9_-]+)((?:\.[a-zA-Z0-9_-]+)*)$")


@dataclass
class EtapeResolue:
    identifiant: str
    brique: mod_manifeste.Manifeste
    service: mod_manifeste.Service
    entree: dict


@dataclass
class ResultatPipeline:
    nom: str
    resultats: dict[str, Any] = field(default_factory=dict)
    manques: list[str] = field(default_factory=list)
    durees_s: dict[str, float] = field(default_factory=dict)
    debut_ms: int = 0
    fin_ms: int = 0

    def en_json(self) -> dict:
        return {
            "pipeline": self.nom,
            "debut_ms": self.debut_ms,
            "fin_ms": self.fin_ms,
            "durees_s": self.durees_s,
            "manques": self.manques,
            "resultats": self.resultats,
        }


def _reference(valeur: Any) -> tuple[str, list[str]] | None:
    if isinstance(valeur, str):
        m = MOTIF_REFERENCE.match(valeur)
        if m:
            return m.group(1), [c for c in m.group(2).split(".") if c]
    return None


def _sous_schema(schema: Any, chemin: list[str]) -> Any:
    courant = schemas.resoudre(schema)
    for cle in chemin:
        proprietes = courant.get("properties", {})
        if cle not in proprietes:
            raise ErreurForge(f"le champ « {cle} » n'existe pas dans le contrat {courant.get('$id', courant.get('title', ''))}")
        courant = schemas.resoudre(proprietes[cle])
    return courant


def _sous_valeur(valeur: Any, chemin: list[str], identifiant: str) -> Any:
    courant = valeur
    for cle in chemin:
        if not isinstance(courant, dict) or cle not in courant:
            raise ErreurForge(f"$${identifiant}.{'.'.join(chemin)} : champ « {cle} » absent du résultat de l'étape {identifiant}")
        courant = courant[cle]
    return courant


def resoudre_etapes(pipeline: mod_manifeste.Manifeste) -> list[EtapeResolue]:
    problemes: list[str] = []
    etapes: list[EtapeResolue] = []
    vus: set[str] = set()
    for i, brut in enumerate(pipeline.etapes):
        identifiant = brut.get("id") or f"etape{i + 1}"
        if identifiant in vus:
            problemes.append(f"étape {identifiant} : identifiant en double")
        vus.add(identifiant)
        try:
            brique = registre.trouver_brique(brut["brique"])
            service = brique.service(brut["service"])
        except KeyError as exc:
            problemes.append(f"étape {identifiant} : champ {exc} manquant (brique, service)")
            continue
        except ErreurForge as exc:
            problemes.append(f"étape {identifiant} : {exc.cause}")
            continue
        etapes.append(EtapeResolue(identifiant, brique, service, brut.get("entree", {})))
    if problemes:
        raise ErreurForge(f"Pipeline {pipeline.nom} : étapes invalides", "corrigez chaque étape listée", problemes)
    return etapes


def verifier(pipeline: mod_manifeste.Manifeste) -> list[EtapeResolue]:
    """Type-check complet avant exécution. Lève une ErreurForge listant tout."""
    etapes = resoudre_etapes(pipeline)
    problemes: list[str] = []
    sorties: dict[str, Any] = {}
    for e in etapes:
        schema_entree = schemas.resoudre(e.service.entree)
        proprietes = schema_entree.get("properties", {})
        litteraux: dict[str, Any] = {}
        for cle, valeur in e.entree.items():
            ref = _reference(valeur)
            if ref is None:
                litteraux[cle] = valeur
                continue
            source, chemin = ref
            if source not in sorties:
                problemes.append(f"étape {e.identifiant}, entrée « {cle} » : l'étape « {source} » n'existe pas avant celle-ci")
                continue
            if cle not in proprietes:
                problemes.append(f"étape {e.identifiant} : « {cle} » n'est pas un paramètre de {e.brique.nom}.{e.service.nom}")
                continue
            try:
                schema_source = _sous_schema(sorties[source], chemin)
            except ErreurForge as exc:
                problemes.append(f"étape {e.identifiant}, entrée « {cle} » : {exc.cause}")
                continue
            ok, explication = schemas.compatibles(
                sorties[source] if not chemin else schema_source,
                proprietes[cle],
            )
            if not ok:
                problemes.append(f"étape {e.identifiant}, entrée « {cle} » ← ${source}{'.' + '.'.join(chemin) if chemin else ''} : {explication}")
        # les littéraux sont validés seuls, sans les champs obligatoires branchés
        schema_litteraux = {**schema_entree, "required": [r for r in schema_entree.get("required", []) if r in litteraux or r not in e.entree]}
        for ecart in schemas.ecarts(litteraux, schema_litteraux):
            problemes.append(f"étape {e.identifiant} : {ecart}")
        sorties[e.identifiant] = e.service.sortie
    if problemes:
        raise ErreurForge(f"Pipeline {pipeline.nom} : branchements invalides", "corrigez chaque point listé ; rien n'a été exécuté", problemes)
    return etapes


def executer(pipeline: mod_manifeste.Manifeste, journal=None) -> ResultatPipeline:
    """Vérifie puis exécute. `journal(message)` reçoit l'avancement."""
    journal = journal or (lambda _m: None)
    etapes = verifier(pipeline)
    resultat = ResultatPipeline(nom=pipeline.nom, debut_ms=int(time.time() * 1000))
    clients: dict[str, ClientMCP] = {}
    try:
        for e in etapes:
            entree = copy.deepcopy(e.entree)
            for cle, valeur in list(entree.items()):
                ref = _reference(valeur)
                if ref:
                    source, chemin = ref
                    entree[cle] = _sous_valeur(resultat.resultats[source], chemin, source)
            if e.brique.nom not in clients:
                journal(f"lancement de {e.brique.nom} ({' '.join(e.brique.commande_serveur()[1:]) or 'mcp'})")
                clients[e.brique.nom] = ClientMCP(e.brique.commande_serveur(), e.brique.dossier).demarrer()
            journal(f"étape {e.identifiant} : {e.brique.nom}.{e.service.nom}")
            debut = time.time()
            valeur = clients[e.brique.nom].appeler(e.service.nom, entree)
            resultat.durees_s[e.identifiant] = round(time.time() - debut, 3)
            if isinstance(valeur, dict) and valeur.get("manques"):
                for m in valeur["manques"]:
                    resultat.manques.append(f"{e.identifiant} : {m}")
                journal(f"étape {e.identifiant} : réponse partielle ({len(valeur['manques'])} manque(s))")
            resultat.resultats[e.identifiant] = valeur
    finally:
        for c in clients.values():
            c.arreter()
    resultat.fin_ms = int(time.time() * 1000)
    return resultat

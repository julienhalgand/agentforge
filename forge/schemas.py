"""Contrats de données et validation.

Un contrat est un JSON Schema identifié par `forge://<domaine>/<nom>@<version>`,
stocké dans `contrats/<domaine>/<nom>.v<version>.json`. Le validateur couvre le
sous-ensemble de JSON Schema utilisé par les contrats (type, required,
properties, additionalProperties, items, minItems, maxItems, enum, minimum,
maximum, pattern, const, anyOf, oneOf, $ref vers un contrat forge://) sans
dépendance externe. Chaque écart est rapporté avec son chemin, élément par
élément, pour que l'erreur soit parlante.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .utils.erreurs import ErreurForge

RACINE = Path(__file__).resolve().parent.parent
DOSSIER_CONTRATS = RACINE / "contrats"
MOTIF_CONTRAT = re.compile(r"^forge://([a-z0-9-]+)/([a-z0-9-]+)@(\d+)$")


def est_reference_contrat(valeur: Any) -> bool:
    return isinstance(valeur, str) and MOTIF_CONTRAT.match(valeur) is not None


def chemin_contrat(reference: str, dossiers: list[Path] | None = None) -> Path:
    m = MOTIF_CONTRAT.match(reference)
    if not m:
        raise ErreurForge(
            f"Référence de contrat invalide : {reference!r}",
            "la forme attendue est forge://domaine/nom@version, ex. forge://serie-temporelle/ohlcv@1",
        )
    domaine, nom, version = m.groups()
    for dossier in (dossiers or []) + [DOSSIER_CONTRATS]:
        candidat = dossier / domaine / f"{nom}.v{version}.json"
        if candidat.exists():
            return candidat
    raise ErreurForge(
        f"Contrat inconnu : {reference}",
        f"ajoutez le fichier contrats/{domaine}/{nom}.v{version}.json ou corrigez la référence",
    )


_cache: dict[str, dict] = {}


def charger_contrat(reference: str, dossiers: list[Path] | None = None) -> dict:
    if reference not in _cache:
        with open(chemin_contrat(reference, dossiers), encoding="utf-8") as f:
            _cache[reference] = json.load(f)
    return _cache[reference]


def lister_contrats() -> list[dict]:
    """Tous les contrats disponibles : référence, titre, description, chemin."""
    resultat = []
    for fichier in sorted(DOSSIER_CONTRATS.glob("*/*.v*.json")):
        with open(fichier, encoding="utf-8") as f:
            schema = json.load(f)
        resultat.append(
            {
                "reference": schema.get("$id", ""),
                "titre": schema.get("title", fichier.stem),
                "description": schema.get("description", ""),
                "chemin": str(fichier.relative_to(RACINE)),
                "proprietes": schema.get("properties", {}),
            }
        )
    return resultat


def resoudre(schema: Any, dossiers: list[Path] | None = None) -> dict:
    """Un schéma peut être une référence `forge://…` ou un JSON Schema en ligne."""
    if est_reference_contrat(schema):
        return charger_contrat(schema, dossiers)
    if isinstance(schema, dict):
        if est_reference_contrat(schema.get("$ref")):
            return charger_contrat(schema["$ref"], dossiers)
        return schema
    raise ErreurForge(f"Schéma invalide : {schema!r}", "donnez une référence forge://… ou un objet JSON Schema")


def _type_ok(valeur: Any, attendu: str) -> bool:
    if attendu == "object":
        return isinstance(valeur, dict)
    if attendu == "array":
        return isinstance(valeur, list)
    if attendu == "string":
        return isinstance(valeur, str)
    if attendu == "integer":
        return isinstance(valeur, int) and not isinstance(valeur, bool)
    if attendu == "number":
        return isinstance(valeur, (int, float)) and not isinstance(valeur, bool)
    if attendu == "boolean":
        return isinstance(valeur, bool)
    if attendu == "null":
        return valeur is None
    return True


def _nom_type(valeur: Any) -> str:
    return {dict: "object", list: "array", str: "string", bool: "boolean", int: "integer", float: "number", type(None): "null"}.get(
        type(valeur), type(valeur).__name__
    )


def ecarts(valeur: Any, schema: Any, chemin: str = "$", dossiers: list[Path] | None = None) -> list[str]:
    """Liste des écarts entre `valeur` et `schema` (vide si conforme)."""
    schema = resoudre(schema, dossiers)
    e: list[str] = []

    if "const" in schema and valeur != schema["const"]:
        e.append(f"{chemin} : attendu la constante {schema['const']!r}, reçu {valeur!r}")
    if "enum" in schema and valeur not in schema["enum"]:
        e.append(f"{chemin} : valeur {valeur!r} hors de la liste {schema['enum']}")

    attendu = schema.get("type")
    if attendu:
        types = attendu if isinstance(attendu, list) else [attendu]
        if not any(_type_ok(valeur, t) for t in types):
            e.append(f"{chemin} : type attendu {'/'.join(types)}, reçu {_nom_type(valeur)}")
            return e  # inutile d'aller plus loin sur un mauvais type

    for cle in ("anyOf", "oneOf"):
        if cle in schema:
            essais = [ecarts(valeur, s, chemin, dossiers) for s in schema[cle]]
            conformes = sum(1 for t in essais if not t)
            if conformes == 0 or (cle == "oneOf" and conformes != 1):
                e.append(f"{chemin} : aucune des variantes {cle} ne correspond")

    if isinstance(valeur, dict):
        for requis in schema.get("required", []):
            if requis not in valeur:
                e.append(f"{chemin}.{requis} : champ obligatoire absent")
        proprietes = schema.get("properties", {})
        for cle, sous_schema in proprietes.items():
            if cle in valeur:
                e.extend(ecarts(valeur[cle], sous_schema, f"{chemin}.{cle}", dossiers))
        supplementaires = schema.get("additionalProperties", True)
        for cle in valeur:
            if cle in proprietes:
                continue
            if supplementaires is False:
                e.append(f"{chemin}.{cle} : champ inattendu")
            elif isinstance(supplementaires, dict):
                e.extend(ecarts(valeur[cle], supplementaires, f"{chemin}.{cle}", dossiers))

    if isinstance(valeur, list):
        if "minItems" in schema and len(valeur) < schema["minItems"]:
            e.append(f"{chemin} : au moins {schema['minItems']} éléments attendus, reçu {len(valeur)}")
        if "maxItems" in schema and len(valeur) > schema["maxItems"]:
            e.append(f"{chemin} : au plus {schema['maxItems']} éléments attendus, reçu {len(valeur)}")
        if "items" in schema:
            for i, element in enumerate(valeur):
                e.extend(ecarts(element, schema["items"], f"{chemin}[{i}]", dossiers))

    if isinstance(valeur, (int, float)) and not isinstance(valeur, bool):
        if "minimum" in schema and valeur < schema["minimum"]:
            e.append(f"{chemin} : {valeur} < minimum {schema['minimum']}")
        if "maximum" in schema and valeur > schema["maximum"]:
            e.append(f"{chemin} : {valeur} > maximum {schema['maximum']}")

    if isinstance(valeur, str) and "pattern" in schema and not re.search(schema["pattern"], valeur):
        e.append(f"{chemin} : {valeur!r} ne respecte pas le motif {schema['pattern']}")

    return e


def valider(valeur: Any, schema: Any, contexte: str = "donnée", dossiers: list[Path] | None = None) -> None:
    """Lève une ErreurForge listant chaque écart, ou ne fait rien."""
    liste = ecarts(valeur, schema, dossiers=dossiers)
    if liste:
        nom = schema if isinstance(schema, str) else schema.get("$id", schema.get("title", "schéma en ligne"))
        raise ErreurForge(
            f"{contexte} non conforme au contrat {nom}",
            "corrigez la brique émettrice ou le contrat ; chaque écart est listé ci-dessus",
            liste,
        )


def compatibles(sortie: Any, entree: Any, dossiers: list[Path] | None = None) -> tuple[bool, str]:
    """Une sortie peut-elle alimenter une entrée ?

    Deux références de contrat identiques sont compatibles. Sinon, on vérifie
    structurellement que tout champ obligatoire de l'entrée existe dans la
    sortie avec un type compatible. Retourne (ok, explication).
    """
    if est_reference_contrat(sortie) and est_reference_contrat(entree):
        if sortie == entree:
            return True, "même contrat"
        return False, f"contrats différents : {sortie} → {entree}"
    s = resoudre(sortie, dossiers)
    en = resoudre(entree, dossiers)
    if s.get("type") and en.get("type") and s["type"] != en["type"]:
        return False, f"types différents : {s['type']} → {en['type']}"
    manquants = []
    for requis in en.get("required", []):
        if requis not in s.get("properties", {}):
            manquants.append(requis)
    if manquants:
        return False, "champs obligatoires absents de la sortie : " + ", ".join(manquants)
    return True, "structure compatible"

"""Serveur MCP stdio : expose des services comme outils.

Une brique écrite pour agentforge crée un `ServeurMCP`, y enregistre ses
services (nom, description, schéma d'entrée, schéma de sortie, fonction) et
appelle `servir()`. Toute ErreurForge levée par un service est rendue en
résultat d'outil avec `isError` et un contenu structuré {cause, remede,
details} ; une ReponsePartielle est rendue comme succès avec ses manques.

    from forge.mcp.serveur import ServeurMCP
    serveur = ServeurMCP("ma-brique", "0.1.0")
    serveur.service("bonjour", "Dit bonjour", entree={...}, sortie={...})(fonction)
    serveur.servir()
"""

from __future__ import annotations

import json
import sys
import traceback
from dataclasses import dataclass
from typing import Any, Callable

from .. import schemas
from ..utils.erreurs import ErreurForge, ReponsePartielle
from . import protocole as p


@dataclass
class Outil:
    nom: str
    description: str
    entree: Any
    sortie: Any
    fonction: Callable[[dict], Any]

    def en_json(self) -> dict:
        d = {
            "name": self.nom,
            "description": self.description,
            "inputSchema": _schema_mcp(self.entree),
        }
        try:
            d["outputSchema"] = _schema_mcp(self.sortie)
        except ErreurForge:
            pass
        return d


def _schema_mcp(schema: Any) -> dict:
    """MCP attend un JSON Schema en ligne : les références forge:// sont résolues."""
    resolu = dict(schemas.resoudre(schema))
    resolu.pop("$schema", None)
    return resolu


class ServeurMCP:
    def __init__(self, nom: str, version: str, valider_sorties: bool = True):
        self.nom = nom
        self.version = version
        self.valider_sorties = valider_sorties
        self.outils: dict[str, Outil] = {}

    # --- enregistrement --------------------------------------------------
    def ajouter(self, nom: str, description: str, entree: Any, sortie: Any, fonction: Callable[[dict], Any]) -> None:
        self.outils[nom] = Outil(nom, description, entree, sortie, fonction)

    def service(self, nom: str, description: str, entree: Any, sortie: Any):
        """Décorateur : `@serveur.service("prix", "…", entree=…, sortie=…)`."""

        def enregistrer(fonction: Callable[[dict], Any]):
            self.ajouter(nom, description, entree, sortie, fonction)
            return fonction

        return enregistrer

    # --- traitement -------------------------------------------------------
    def traiter(self, message: dict) -> dict | None:
        """Traite un message JSON-RPC ; retourne la réponse (None pour une notification)."""
        identifiant = message.get("id")
        methode = message.get("method")
        parametres = message.get("params") or {}
        est_notification = "id" not in message
        try:
            if methode == "initialize":
                resultat = {
                    "protocolVersion": p.VERSION_PROTOCOLE,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": self.nom, "version": self.version},
                }
            elif methode == "notifications/initialized":
                return None
            elif methode == "ping":
                resultat = {}
            elif methode == "tools/list":
                resultat = {"tools": [o.en_json() for o in self.outils.values()]}
            elif methode == "tools/call":
                resultat = self._appeler(parametres.get("name"), parametres.get("arguments") or {})
            elif est_notification:
                return None
            else:
                raise p.ErreurRPC(p.METHODE_INCONNUE, f"méthode inconnue : {methode}")
        except p.ErreurRPC as exc:
            return None if est_notification else p.reponse_erreur(identifiant, exc)
        except Exception as exc:  # jamais silencieux : cause + trace
            erreur = p.ErreurRPC(p.ERREUR_INTERNE, f"erreur interne : {exc}", traceback.format_exc())
            return None if est_notification else p.reponse_erreur(identifiant, erreur)
        return None if est_notification else p.reponse(identifiant, resultat)

    def _appeler(self, nom: str | None, arguments: dict) -> dict:
        outil = self.outils.get(nom or "")
        if outil is None:
            raise p.ErreurRPC(p.PARAMETRES_INVALIDES, f"outil inconnu : {nom} (disponibles : {', '.join(self.outils) or 'aucun'})")
        ecarts = schemas.ecarts(arguments, outil.entree)
        if ecarts:
            return resultat_erreur(ErreurForge(f"entrée de « {nom} » invalide", "corrigez les paramètres", ecarts))
        try:
            valeur = outil.fonction(arguments)
            manques: list[str] = []
        except ReponsePartielle as partielle:
            valeur, manques = partielle.resultat, partielle.manques
        except ErreurForge as exc:
            return resultat_erreur(exc)
        if isinstance(valeur, dict) and manques and "manques" not in valeur:
            valeur = {**valeur, "manques": manques}
        if self.valider_sorties:
            ecarts = schemas.ecarts(valeur, outil.sortie)
            if ecarts:
                return resultat_erreur(
                    ErreurForge(f"sortie de « {nom} » non conforme à son contrat", "corrigez la brique ou son contrat", ecarts)
                )
        return resultat_ok(valeur)

    # --- boucle -----------------------------------------------------------
    def servir(self, entree=None, sortie=None) -> None:
        """Boucle de service sur stdin/stdout (binaires)."""
        entree = entree or sys.stdin.buffer
        sortie = sortie or sys.stdout.buffer
        while True:
            try:
                message = p.lire_message(entree)
            except p.ErreurRPC as exc:
                p.ecrire_message(sortie, p.reponse_erreur(None, exc))
                continue
            if message is None:
                return
            reponse = self.traiter(message)
            if reponse is not None:
                p.ecrire_message(sortie, reponse)


def resultat_ok(valeur: Any) -> dict:
    texte = json.dumps(valeur, ensure_ascii=False)
    d: dict[str, Any] = {"content": [{"type": "text", "text": texte}], "isError": False}
    if isinstance(valeur, dict):
        d["structuredContent"] = valeur
    return d


def resultat_erreur(erreur: ErreurForge) -> dict:
    return {
        "content": [{"type": "text", "text": erreur.texte()}],
        "structuredContent": {"erreur": erreur.en_json()},
        "isError": True,
    }

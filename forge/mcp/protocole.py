"""MCP sur stdio : JSON-RPC 2.0, un message par ligne.

Sous-ensemble du Model Context Protocol suffisant pour des briques :
`initialize`, `notifications/initialized`, `ping`, `tools/list`, `tools/call`.
Implémenté sur la bibliothèque standard pour ne rien imposer aux
environnements existants. Les charges volumineuses passent en JSON dans le
flux (pas en ligne de commande), donc la limite de ~32 000 caractères de
Windows ne s'applique pas.
"""

from __future__ import annotations

import json
from typing import Any, BinaryIO

VERSION_PROTOCOLE = "2025-06-18"


class ErreurRPC(Exception):
    """Erreur JSON-RPC : code, message, données optionnelles."""

    def __init__(self, code: int, message: str, donnees: Any = None):
        self.code = code
        self.message = message
        self.donnees = donnees
        super().__init__(message)

    def en_json(self) -> dict:
        d: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.donnees is not None:
            d["data"] = self.donnees
        return d


# Codes JSON-RPC standard
ANALYSE = -32700
REQUETE_INVALIDE = -32600
METHODE_INCONNUE = -32601
PARAMETRES_INVALIDES = -32602
ERREUR_INTERNE = -32603


def ecrire_message(flux: BinaryIO, message: dict) -> None:
    flux.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
    flux.flush()


def lire_message(flux: BinaryIO) -> dict | None:
    """Lit la prochaine ligne JSON ; None à la fin du flux. Ignore les lignes vides."""
    while True:
        ligne = flux.readline()
        if not ligne:
            return None
        ligne = ligne.strip()
        if not ligne:
            continue
        try:
            return json.loads(ligne.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ErreurRPC(ANALYSE, f"ligne JSON invalide : {exc.msg}") from exc


def requete(identifiant: int, methode: str, parametres: dict | None = None) -> dict:
    m: dict[str, Any] = {"jsonrpc": "2.0", "id": identifiant, "method": methode}
    if parametres is not None:
        m["params"] = parametres
    return m


def notification(methode: str, parametres: dict | None = None) -> dict:
    m: dict[str, Any] = {"jsonrpc": "2.0", "method": methode}
    if parametres is not None:
        m["params"] = parametres
    return m


def reponse(identifiant: Any, resultat: Any) -> dict:
    return {"jsonrpc": "2.0", "id": identifiant, "result": resultat}


def reponse_erreur(identifiant: Any, erreur: ErreurRPC) -> dict:
    return {"jsonrpc": "2.0", "id": identifiant, "error": erreur.en_json()}

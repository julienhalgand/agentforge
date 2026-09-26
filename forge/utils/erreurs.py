"""Erreurs parlantes : chaque échec dit la cause et le remède.

Une ErreurForge se rend en texte (« cause — remède ») et en JSON, pour que la
ligne de commande, le hub et les réponses MCP racontent la même chose.
"""

from __future__ import annotations


class ErreurForge(Exception):
    """Erreur avec cause, remède et détails élément par élément."""

    def __init__(self, cause: str, remede: str = "", details: list[str] | None = None):
        self.cause = cause
        self.remede = remede
        self.details = list(details or [])
        super().__init__(self.texte())

    def texte(self) -> str:
        lignes = [self.cause]
        for d in self.details:
            lignes.append(f"  - {d}")
        if self.remede:
            lignes.append(f"Remède : {self.remede}")
        return "\n".join(lignes)

    def en_json(self) -> dict:
        return {"cause": self.cause, "remede": self.remede, "details": self.details}


class ReponsePartielle(ErreurForge):
    """Une brique a répondu, mais avec des manques (code de sortie 2).

    Ce n'est pas un rejet : le résultat est rendu, les manques sont listés.
    """

    def __init__(self, resultat, manques: list[str]):
        self.resultat = resultat
        self.manques = list(manques)
        super().__init__("Réponse partielle", "voir les manques", self.manques)

"""Adaptateur générique : une brique existante exposée en MCP sans la réécrire.

Protocole des briques existantes (prix-agent et consœurs) :
- `python -m agent.<service> [--cle valeur …]` écrit **un JSON sur stdout** ;
- code de sortie 0 = succès, 1 = échec (cause sur stderr), 2 = réponse
  partielle (JSON sur stdout, manques dans le JSON ou sur stderr) ;
- un `services.json` décrit les services.

L'adaptateur lit le `forge.json` de la brique (transport « services-json »),
son `services.json`, et sert chaque service comme un outil MCP. Il exécute la
brique avec **son** interpréteur (champ `interpreteur`, chemin conda complet
sous Windows), depuis son dossier, en chargeant `.env.local` s'il existe.

Passage des paramètres (`transport.passage_entree`) :
- « arguments » (défaut) : `--cle valeur` par champ ; si la ligne dépasse
  30 000 caractères, repli automatique sur « fichier » ;
- « fichier » : `--entree <chemin d'un JSON temporaire>` ;
- « stdin » : le JSON d'entrée sur l'entrée standard.

`services.json` attendu (les deux formes sont acceptées) :
    { "services": { "prix": { "module": "agent.prix" }, … } }
    { "services": [ { "nom": "prix", "module": "agent.prix" }, … ] }
Si un service n'y figure pas, le module `agent.<nom>` est supposé.

Lancement (fait par forge, pas à la main) :
    python -m forge.mcp.adaptateur --dossier <dossier de la brique>
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .. import manifeste as mod_manifeste
from ..utils.erreurs import ErreurForge, ReponsePartielle
from .serveur import ServeurMCP

LIMITE_LIGNE_COMMANDE = 30_000  # Windows : ~32 767, avec marge


def charger_env_local(dossier: Path) -> dict[str, str]:
    """Clés d'API optionnelles : `.env.local` de la brique, jamais le manifeste."""
    env = dict(os.environ)
    fichier = dossier / ".env.local"
    if fichier.exists():
        for ligne in fichier.read_text(encoding="utf-8").splitlines():
            ligne = ligne.strip()
            if not ligne or ligne.startswith("#") or "=" not in ligne:
                continue
            cle, valeur = ligne.split("=", 1)
            env[cle.strip()] = valeur.strip().strip('"').strip("'")
    return env


def lire_services_json(m: mod_manifeste.Manifeste) -> dict[str, dict]:
    fichier = m.dossier / m.transport.get("fichier", "services.json")
    if not fichier.exists():
        return {}
    with open(fichier, encoding="utf-8") as f:
        brut = json.load(f)
    services = brut.get("services", brut)
    if isinstance(services, list):
        return {s["nom"]: s for s in services if "nom" in s}
    return services if isinstance(services, dict) else {}


def _valeur_argument(valeur: Any) -> str:
    return valeur if isinstance(valeur, str) else json.dumps(valeur, ensure_ascii=False)


class AdaptateurBrique:
    def __init__(self, m: mod_manifeste.Manifeste):
        self.manifeste = m
        self.details = lire_services_json(m)
        self.env = charger_env_local(m.dossier)
        self.passage = m.transport.get("passage_entree", "arguments")

    def module(self, nom_service: str) -> str:
        return self.details.get(nom_service, {}).get("module", f"agent.{nom_service}")

    def executer(self, nom_service: str, entree: dict) -> Any:
        base = [self.manifeste.interpreteur, "-m", self.module(nom_service)]
        fichier_temporaire: Path | None = None
        stdin_texte: str | None = None
        passage = self.passage
        commande = list(base)
        if passage == "arguments":
            for cle, valeur in entree.items():
                commande += [f"--{cle}", _valeur_argument(valeur)]
            if sum(len(c) + 1 for c in commande) > LIMITE_LIGNE_COMMANDE:
                passage = "fichier"  # trop long pour Windows : repli fichier
                commande = list(base)
        if passage == "fichier":
            fd, chemin = tempfile.mkstemp(prefix=f".{nom_service}-", suffix=".entree.json", dir=self.manifeste.dossier)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(entree, f, ensure_ascii=False)
            fichier_temporaire = Path(chemin)
            commande += ["--entree", str(fichier_temporaire)]
        elif passage == "stdin":
            stdin_texte = json.dumps(entree, ensure_ascii=False)

        try:
            termine = subprocess.run(
                commande,
                cwd=self.manifeste.dossier,
                env=self.env,
                input=stdin_texte,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError as exc:
            raise ErreurForge(f"{self.manifeste.nom} : interpréteur introuvable : {commande[0]}", "corrigez « interpreteur » dans forge.json") from exc
        finally:
            if fichier_temporaire and fichier_temporaire.exists():
                fichier_temporaire.unlink()

        erreurs = [l for l in termine.stderr.splitlines() if l.strip()]
        if termine.returncode == 1:
            raise ErreurForge(
                f"{self.manifeste.nom}.{nom_service} a échoué (code 1)",
                "lisez les détails ; reproduisez avec : " + " ".join(commande),
                erreurs or ["(aucun message sur stderr)"],
            )
        if termine.returncode not in (0, 2):
            raise ErreurForge(
                f"{self.manifeste.nom}.{nom_service} : code de sortie inattendu {termine.returncode}",
                "les codes attendus sont 0 (succès), 1 (échec), 2 (partiel)",
                erreurs,
            )
        try:
            resultat = json.loads(termine.stdout) if termine.stdout.strip() else None
        except json.JSONDecodeError as exc:
            raise ErreurForge(
                f"{self.manifeste.nom}.{nom_service} : la sortie n'est pas un JSON ({exc.msg})",
                "la brique doit écrire un seul JSON sur stdout (les messages vont sur stderr)",
                [termine.stdout[:500]],
            ) from exc
        if termine.returncode == 2:
            manques = resultat.get("manques", []) if isinstance(resultat, dict) else []
            raise ReponsePartielle(resultat, manques or erreurs or ["réponse partielle sans détail"])
        return resultat


def construire_serveur(dossier: Path | str) -> ServeurMCP:
    m = mod_manifeste.charger(dossier)
    adaptateur = AdaptateurBrique(m)
    serveur = ServeurMCP(m.nom, m.version)
    for s in m.services:
        def fonction(entree: dict, _nom=s.nom):
            return adaptateur.executer(_nom, entree)
        serveur.ajouter(s.nom, s.description or s.nom, s.entree, s.sortie, fonction)
    return serveur


def principal(argv: list[str] | None = None) -> int:
    analyseur = argparse.ArgumentParser(description="Expose une brique « services-json » en serveur MCP stdio.")
    analyseur.add_argument("--dossier", required=True, help="dossier de la brique (contient forge.json)")
    args = analyseur.parse_args(argv)
    try:
        serveur = construire_serveur(args.dossier)
    except ErreurForge as exc:
        print(exc.texte(), file=sys.stderr)
        return 1
    serveur.servir()
    return 0


if __name__ == "__main__":
    sys.exit(principal())

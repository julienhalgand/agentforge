"""Client MCP stdio : lance une brique et appelle ses services.

    with ClientMCP(commande, dossier) as client:
        outils = client.lister_outils()
        resultat = client.appeler("historique", {"actif": "BTC", "intervalle": "1d", "limite": 90})

Un résultat d'outil `isError` devient une ErreurForge (cause, remède, détails
tels que la brique les a donnés). Un résultat avec `manques` est rendu tel
quel : c'est à l'appelant de les afficher.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Any

from ..utils.erreurs import ErreurForge
from . import protocole as p


class ClientMCP:
    def __init__(self, commande: list[str], dossier: Path | str | None = None, environnement: dict | None = None, delai_s: float = 600.0):
        self.commande = commande
        self.dossier = str(dossier) if dossier else None
        # Les briques MCP natives importent `forge` : on le rend visible même sans installation pip.
        racine = str(Path(__file__).resolve().parent.parent.parent)
        chemin_python = os.pathsep.join(x for x in (racine, os.environ.get("PYTHONPATH", "")) if x)
        self.environnement = {**os.environ, "PYTHONPATH": chemin_python, "PYTHONIOENCODING": "utf-8", **(environnement or {})}
        self.delai_s = delai_s
        self.processus: subprocess.Popen | None = None
        self._compteur = 0
        self._verrou = threading.Lock()
        self._stderr: list[str] = []
        self.infos_serveur: dict = {}

    # --- cycle de vie -----------------------------------------------------
    def demarrer(self) -> "ClientMCP":
        try:
            self.processus = subprocess.Popen(
                self.commande,
                cwd=self.dossier,
                env=self.environnement,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise ErreurForge(
                f"Impossible de lancer la brique : {self.commande[0]} introuvable",
                "vérifiez le champ « interpreteur » ou « transport.commande » du manifeste",
            ) from exc
        threading.Thread(target=self._lire_stderr, daemon=True).start()
        reponse = self._requete(
            "initialize",
            {
                "protocolVersion": p.VERSION_PROTOCOLE,
                "capabilities": {},
                "clientInfo": {"name": "agentforge", "version": "0.1.0"},
            },
        )
        self.infos_serveur = reponse.get("serverInfo", {})
        p.ecrire_message(self.processus.stdin, p.notification("notifications/initialized"))
        return self

    def arreter(self) -> None:
        if not self.processus:
            return
        try:
            if self.processus.stdin:
                self.processus.stdin.close()
            self.processus.wait(timeout=5)
        except Exception:
            self.processus.terminate()
        self.processus = None

    def __enter__(self) -> "ClientMCP":
        return self.demarrer()

    def __exit__(self, *_) -> None:
        self.arreter()

    # --- appels -----------------------------------------------------------
    def lister_outils(self) -> list[dict]:
        return self._requete("tools/list").get("tools", [])

    def appeler(self, nom: str, arguments: dict | None = None) -> Any:
        resultat = self._requete("tools/call", {"name": nom, "arguments": arguments or {}})
        structure = resultat.get("structuredContent")
        if resultat.get("isError"):
            erreur = (structure or {}).get("erreur")
            if erreur:
                raise ErreurForge(erreur.get("cause", "échec"), erreur.get("remede", ""), erreur.get("details", []))
            texte = " ".join(c.get("text", "") for c in resultat.get("content", []) if c.get("type") == "text")
            raise ErreurForge(f"{nom} : {texte or 'échec sans message'}", "voir la sortie d'erreur de la brique", self.journal_erreurs())
        if structure is not None:
            return structure
        for c in resultat.get("content", []):
            if c.get("type") == "text":
                try:
                    return json.loads(c["text"])
                except json.JSONDecodeError:
                    return c["text"]
        return None

    def journal_erreurs(self) -> list[str]:
        return list(self._stderr[-20:])

    # --- interne ----------------------------------------------------------
    def _lire_stderr(self) -> None:
        assert self.processus and self.processus.stderr
        for ligne in self.processus.stderr:
            self._stderr.append(ligne.decode("utf-8", errors="replace").rstrip())

    def _requete(self, methode: str, parametres: dict | None = None) -> dict:
        if not self.processus or not self.processus.stdin or not self.processus.stdout:
            raise ErreurForge("Client MCP non démarré", "appelez demarrer() ou utilisez `with ClientMCP(...)`")
        with self._verrou:
            self._compteur += 1
            identifiant = self._compteur
            p.ecrire_message(self.processus.stdin, p.requete(identifiant, methode, parametres))
            reponse = self._attendre(identifiant)
        if "error" in reponse:
            e = reponse["error"]
            raise ErreurForge(
                f"{methode} : {e.get('message', 'erreur JSON-RPC')}",
                "voir les détails et la sortie d'erreur de la brique",
                ([str(e["data"])] if e.get("data") else []) + self.journal_erreurs(),
            )
        return reponse.get("result", {})

    def _attendre(self, identifiant: int) -> dict:
        assert self.processus and self.processus.stdout
        boite: dict[str, Any] = {}

        def lire():
            while True:
                m = p.lire_message(self.processus.stdout)  # type: ignore[arg-type]
                if m is None:
                    boite["fin"] = True
                    return
                if m.get("id") == identifiant:
                    boite["reponse"] = m
                    return

        fil = threading.Thread(target=lire, daemon=True)
        fil.start()
        fil.join(self.delai_s)
        if fil.is_alive():
            self.processus.kill()
            raise ErreurForge(
                f"La brique n'a pas répondu en {self.delai_s:.0f} s",
                "augmentez le délai ou utilisez le contrat tache/progres@1 pour les tâches longues",
                self.journal_erreurs(),
            )
        if "fin" in boite:
            code = self.processus.wait()
            raise ErreurForge(
                f"La brique s'est arrêtée (code {code}) sans répondre",
                "lisez la sortie d'erreur ci-dessous et lancez la brique à la main pour reproduire",
                self.journal_erreurs(),
            )
        return boite["reponse"]

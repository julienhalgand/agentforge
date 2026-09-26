"""Modèle de langage local, optionnel et ancré.

Backend « ollama » (http://localhost:11434, CPU ou GPU au choix d'Ollama) ou
tout serveur « openai-compatible » local (llama.cpp, vLLM, LM Studio). Aucun
service en ligne : une URL qui n'est pas locale est refusée.

Règles :
- **ancré** : le modèle trie ou rédige à partir des faits fournis dans le
  prompt ; les chiffres sont recopiés, jamais inventés — c'est à la brique de
  fournir les faits et de vérifier la sortie ;
- **continuation automatique** : si la réponse est coupée (limite de
  tokens), on demande la suite et on recolle, jusqu'à `max_continuations` ;
- **extraction sous grammaire** : `generer_json(schema)` contraint la
  sortie à un JSON Schema (Ollama : champ `format`) et la valide.

    modele = ModeleLocal({"backend": "ollama", "nom": "qwen2.5:7b"})
    texte = modele.generer("Résume ces faits : …", systeme="Tu recopies les chiffres tels quels.")
    tri = modele.generer_json("Classe ces courriels…", schema={"type": "object", …})
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any

from . import schemas
from .utils.erreurs import ErreurForge

URLS_PAR_DEFAUT = {"integre": "http://127.0.0.1:8791", "ollama": "http://localhost:11434", "openai-compatible": "http://localhost:8080"}
HOTES_LOCAUX = ("localhost", "127.0.0.1", "[::1]", "0.0.0.0")


class ModeleLocal:
    def __init__(self, configuration: dict, delai_s: float = 600.0, max_continuations: int = 6):
        self.backend = configuration.get("backend", "ollama")
        self.nom = configuration.get("nom")
        self.url = configuration.get("url") or URLS_PAR_DEFAUT.get(self.backend)
        self.gpu = configuration.get("gpu", True)
        self.delai_s = delai_s
        self.max_continuations = max_continuations
        self.derniere_raison_arret = "?"
        if self.backend not in URLS_PAR_DEFAUT:
            raise ErreurForge(f"backend inconnu : {self.backend}", "utilisez « integre » (llama.cpp embarqué), « ollama » ou « openai-compatible »")
        if self.backend == "integre":
            self.backend_http = "openai-compatible"  # le moteur intégré (llama-server) parle l'API openai-compatible
        else:
            self.backend_http = self.backend
        if not self.nom:
            raise ErreurForge("modèle sans nom", 'donnez « nom », ex. "qwen2.5:7b"')
        hote = re.sub(r"^https?://", "", self.url).split("/")[0].rsplit(":", 1)[0]
        if hote not in HOTES_LOCAUX:
            raise ErreurForge(
                f"URL de modèle non locale refusée : {self.url}",
                "agentforge est local d'abord : le modèle tourne sur cette machine (localhost)",
            )

    # --- API publique ---------------------------------------------------------
    def disponible(self) -> tuple[bool, str]:
        """(ok, explication) : serveur joignable et modèle présent ?"""
        try:
            if self.backend_http == "ollama":
                d = self._get("/api/tags")
                noms = [m.get("name", "") for m in d.get("models", [])]
                if self.nom in noms or f"{self.nom}:latest" in noms:
                    return True, "prêt"
                return False, f"modèle « {self.nom} » absent d'Ollama — lancez : ollama pull {self.nom}"
            self._get("/v1/models")
            return True, "prêt"
        except ErreurForge as exc:
            return False, exc.cause

    def modeles(self) -> list[str]:
        """Modèles installés sur le serveur local."""
        if self.backend_http == "ollama":
            return [m.get("name", "") for m in self._get("/api/tags").get("models", [])]
        return [m.get("id", "") for m in self._get("/v1/models").get("data", [])]

    def installer(self, nom: str, progres=None, annulee=None) -> None:
        """Télécharge un modèle (Ollama : /api/pull en flux). `progres(pourcentage, etape)` et `annulee()` optionnels."""
        if self.backend_http != "ollama":
            raise ErreurForge("le téléchargement de modèle n'existe que pour Ollama", "installez le modèle avec l'outil de votre serveur")
        corps = json.dumps({"model": nom, "stream": True}).encode("utf-8")
        requete = urllib.request.Request(self.url + "/api/pull", data=corps, method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(requete, timeout=self.delai_s) as reponse:
                for ligne in reponse:
                    if annulee and annulee():
                        return  # fermer la connexion suffit : Ollama arrête le téléchargement, les couches déjà reçues sont gardées
                    try:
                        d = json.loads(ligne.decode("utf-8"))
                    except json.JSONDecodeError:
                        continue
                    if "error" in d:
                        raise ErreurForge(f"Ollama : {d['error']}", f"vérifiez le nom du modèle sur https://ollama.com/library (reçu : {nom})")
                    total, fait = d.get("total"), d.get("completed")
                    if progres:
                        pct = (100.0 * fait / total) if total and fait is not None else (100.0 if d.get("status") == "success" else 0.0)
                        progres(pct, d.get("status", ""))
        except urllib.error.HTTPError as exc:
            raise ErreurForge(f"Ollama : HTTP {exc.code} sur /api/pull", "vérifiez le nom du modèle", [exc.read().decode("utf-8", errors="replace")[:300]]) from exc
        except urllib.error.URLError as exc:
            raise ErreurForge(f"Ollama injoignable sur {self.url} ({exc.reason})", "installez Ollama (https://ollama.com/download) et lancez-le") from exc

    def generer(self, prompt: str, systeme: str = "", temperature: float = 0.0, max_tokens: int | None = None) -> str:
        """Texte libre, avec continuation automatique si la réponse est coupée."""
        messages = ([{"role": "system", "content": systeme}] if systeme else []) + [{"role": "user", "content": prompt}]
        texte, coupe = self._chat(messages, temperature=temperature, max_tokens=max_tokens)
        continuations = 0
        while coupe and continuations < self.max_continuations:
            continuations += 1
            messages = messages + [
                {"role": "assistant", "content": texte},
                {"role": "user", "content": "Continue exactement là où tu t'es arrêté, sans répéter ce qui précède."},
            ]
            suite, coupe = self._chat(messages, temperature=temperature, max_tokens=max_tokens)
            texte += suite
        if coupe:
            raise ErreurForge(
                f"réponse toujours coupée après {continuations} continuations ({len(texte)} caractères)",
                "augmentez max_tokens ou découpez la tâche ; rien n'a été tronqué en silence, voici le début : " + texte[:200],
            )
        self.dernieres_continuations = continuations
        return texte

    def generer_json(self, prompt: str, schema: Any, systeme: str = "", temperature: float = 0.0, max_tokens: int | None = None) -> Any:
        """Sortie contrainte par un JSON Schema (ou une référence forge://…), validée avant d'être rendue."""
        schema_resolu = schemas.resoudre(schema)
        messages = ([{"role": "system", "content": systeme}] if systeme else []) + [{"role": "user", "content": prompt}]
        texte, coupe = self._chat(messages, temperature=temperature, max_tokens=max_tokens, format_json=schema_resolu)
        if coupe:
            raise ErreurForge("la réponse JSON a été coupée", "augmentez max_tokens ou réduisez la taille de la demande")
        try:
            valeur = json.loads(_extraire_json(texte))
        except json.JSONDecodeError as exc:
            details = [f"longueur de la réponse : {len(texte)} caractères", f"arrêt du moteur : {self.derniere_raison_arret}", "réponse brute : " + (repr(texte[:600]) if texte else "(vide)")]
            if not texte:
                details.append("réponse vide : le moteur n'a rien produit — grammaire JSON trop stricte pour ce modèle, contexte saturé, ou modèle qui refuse le format")
            raise ErreurForge(f"le modèle n'a pas rendu un JSON valide ({exc.msg})", "voir la réponse brute ci-dessous", details) from exc
        ecarts = schemas.ecarts(valeur, schema_resolu)
        if ecarts:
            raise ErreurForge("le JSON du modèle ne respecte pas le schéma", "la brique doit refuser cette sortie ou réessayer", ecarts)
        return valeur

    # --- transport ---------------------------------------------------------------
    def _chat(self, messages: list[dict], temperature: float, max_tokens: int | None, format_json: dict | None = None) -> tuple[str, bool]:
        """Retourne (texte, coupé ?)."""
        if self.backend_http == "ollama":
            corps: dict[str, Any] = {"model": self.nom, "messages": messages, "stream": False, "options": {"temperature": temperature}}
            if max_tokens:
                corps["options"]["num_predict"] = max_tokens
            if not self.gpu:
                corps["options"]["num_gpu"] = 0
            if format_json is not None:
                corps["format"] = format_json
            d = self._post("/api/chat", corps)
            if "error" in d:
                raise ErreurForge(f"Ollama : {d['error']}", f"vérifiez le modèle (ollama pull {self.nom}) et la mémoire disponible")
            self.derniere_raison_arret = str(d.get("done_reason"))
            return d.get("message", {}).get("content", "") or "", d.get("done_reason") == "length"
        corps = {"model": self.nom, "messages": messages, "temperature": temperature}
        if max_tokens:
            corps["max_tokens"] = max_tokens
        if format_json is not None:
            corps["response_format"] = {"type": "json_schema", "json_schema": {"name": "sortie", "schema": format_json}}
        d = self._post("/v1/chat/completions", corps)
        choix = (d.get("choices") or [{}])[0]
        self.derniere_raison_arret = f"{choix.get('finish_reason')} · tokens {d.get('usage', {}).get('completion_tokens', '?')}"
        return choix.get("message", {}).get("content", "") or "", choix.get("finish_reason") == "length"

    def _get(self, chemin: str) -> dict:
        return self._requete("GET", chemin)

    def _post(self, chemin: str, corps: dict) -> dict:
        return self._requete("POST", chemin, corps)

    def _requete(self, methode: str, chemin: str, corps: dict | None = None) -> dict:
        donnees = json.dumps(corps).encode("utf-8") if corps is not None else None
        requete = urllib.request.Request(self.url + chemin, data=donnees, method=methode, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(requete, timeout=self.delai_s) as reponse:  # http local : pas de TLS
                return json.loads(reponse.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise ErreurForge(f"{self.backend} : HTTP {exc.code} sur {chemin}", f"modèle « {self.nom} » absent ? ollama pull {self.nom}", [detail]) from exc
        except urllib.error.URLError as exc:
            raise ErreurForge(
                f"{self.backend} injoignable sur {self.url} ({exc.reason})",
                "lancez le serveur local (Ollama : `ollama serve`, ou l'application Ollama) puis réessayez",
            ) from exc
        except TimeoutError as exc:
            raise ErreurForge(f"{self.backend} : délai dépassé ({self.delai_s} s)", "modèle trop lourd pour la machine ? essayez un modèle plus petit ou gpu: true") from exc


def _extraire_json(texte: str) -> str:
    """Certains modèles entourent le JSON de ```json … ``` : on l'isole."""
    m = re.search(r"```(?:json)?\s*(.*?)```", texte, re.S)
    return m.group(1).strip() if m else texte.strip()

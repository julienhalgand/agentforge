"""Réseau : HTTPS avec repli sur certifi.

Sous Windows, un magasin de certificats corrompu fait échouer urllib avec
`ASN1: NOT_ENOUGH_DATA`. Le runtime applique une fois pour toutes le repli :
on tente le contexte système, et s'il échoue on utilise le paquet `certifi`
quand il est installé. Aucune vérification n'est jamais désactivée.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from typing import Any

from .erreurs import ErreurForge

_contexte_ssl: ssl.SSLContext | None = None


def contexte_ssl() -> ssl.SSLContext:
    """Contexte SSL utilisable : système d'abord, certifi en secours."""
    global _contexte_ssl
    if _contexte_ssl is not None:
        return _contexte_ssl
    try:
        ctx = ssl.create_default_context()
        # Un magasin corrompu ne se voit parfois qu'au chargement : on force.
        if ctx.cert_store_stats()["x509"] == 0:
            raise ssl.SSLError("magasin de certificats vide")
        _contexte_ssl = ctx
    except (ssl.SSLError, OSError, ValueError):
        try:
            import certifi  # dépendance optionnelle
        except ImportError as exc:
            raise ErreurForge(
                "Le magasin de certificats du système est inutilisable et certifi est absent",
                "installez certifi dans l'environnement (`pip install certifi`)",
            ) from exc
        _contexte_ssl = ssl.create_default_context(cafile=certifi.where())
    return _contexte_ssl


def obtenir_json(url: str, delai_s: float = 15.0, entetes: dict[str, str] | None = None) -> Any:
    """GET `url` et décode la réponse JSON. Erreurs parlantes."""
    requete = urllib.request.Request(url, headers={"User-Agent": "agentforge/0.1", **(entetes or {})})
    try:
        with urllib.request.urlopen(requete, timeout=delai_s, context=contexte_ssl()) as reponse:
            brut = reponse.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise ErreurForge(f"{url} : réponse HTTP {exc.code}", "vérifiez le paramètre demandé ou réessayez plus tard") from exc
    except urllib.error.URLError as exc:
        raise ErreurForge(f"{url} : injoignable ({exc.reason})", "vérifiez la connexion réseau ou le proxy") from exc
    except TimeoutError as exc:
        raise ErreurForge(f"{url} : délai dépassé ({delai_s} s)", "réessayez ou augmentez le délai") from exc
    try:
        return json.loads(brut)
    except json.JSONDecodeError as exc:
        raise ErreurForge(f"{url} : réponse non JSON", "la source a peut-être changé de format") from exc

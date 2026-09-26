"""Commun aux services de prix-agent : sources, réseau, sortie JSON, codes de sortie."""

from __future__ import annotations

import argparse
import json
import os
import random
import ssl
import sys
import time
import urllib.error
import urllib.request

SORTIE_SUCCES, SORTIE_ECHEC, SORTIE_PARTIEL = 0, 1, 2

# Identifiants CoinGecko des actifs courants (extensible dans .env.local : PRIX_AGENT_COINGECKO=SOL:solana,…)
COINGECKO_IDS = {
    "BTC": "bitcoin", "ETH": "ethereum", "BNB": "binancecoin", "SOL": "solana", "XRP": "ripple",
    "ADA": "cardano", "DOGE": "dogecoin", "DOT": "polkadot", "AVAX": "avalanche-2", "LINK": "chainlink",
    "LTC": "litecoin", "MATIC": "matic-network", "ATOM": "cosmos", "UNI": "uniswap", "TRX": "tron",
}
for paire in os.environ.get("PRIX_AGENT_COINGECKO", "").split(","):
    if ":" in paire:
        a, i = paire.split(":", 1)
        COINGECKO_IDS[a.strip().upper()] = i.strip()


class EchecSource(Exception):
    """Une source n'a pas pu répondre pour cet élément : message parlant, source nommée."""


_ctx = None


def contexte_ssl():
    """Système d'abord, certifi en secours (magasin Windows corrompu : ASN1 NOT_ENOUGH_DATA)."""
    global _ctx
    if _ctx is None:
        try:
            _ctx = ssl.create_default_context()
            if _ctx.cert_store_stats()["x509"] == 0:
                raise ssl.SSLError("magasin vide")
        except (ssl.SSLError, OSError, ValueError):
            import certifi  # repli : doit être installé dans l'environnement de la brique

            _ctx = ssl.create_default_context(cafile=certifi.where())
    return _ctx


def obtenir_json(url: str, delai_s: float = 15.0):
    requete = urllib.request.Request(url, headers={"User-Agent": "prix-agent/0.1"})
    try:
        with urllib.request.urlopen(requete, timeout=delai_s, context=contexte_ssl()) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise EchecSource(f"réponse HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise EchecSource(f"injoignable ({exc.reason})") from exc
    except (TimeoutError, ssl.SSLError, json.JSONDecodeError) as exc:
        raise EchecSource(str(exc) or exc.__class__.__name__) from exc


def journal(message: str) -> None:
    """Les messages vont sur stderr : stdout est réservé au JSON."""
    print(message, file=sys.stderr, flush=True)


def terminer(resultat, manques: list[str]) -> int:
    """Écrit le JSON sur stdout et rend le code : 0, ou 2 si des manques existent."""
    if manques:
        resultat["manques"] = manques
        for m in manques:
            journal(f"manque : {m}")
    print(json.dumps(resultat, ensure_ascii=False))
    return SORTIE_PARTIEL if manques else SORTIE_SUCCES


def echouer(message: str, details: list[str] | None = None) -> int:
    journal(message)
    for d in details or []:
        journal(f"  - {d}")
    return SORTIE_ECHEC


def analyseur(description: str) -> argparse.ArgumentParser:
    a = argparse.ArgumentParser(description=description)
    a.add_argument("--entree", help="fichier JSON de paramètres (remplace les autres options)")
    a.add_argument("--devise", default="USD")
    a.add_argument("--source", default=os.environ.get("PRIX_AGENT_SOURCE", "auto"), choices=["auto", "binance", "coingecko", "test"])
    return a


def parametres(a: argparse.ArgumentParser, argv=None) -> dict:
    """Paramètres depuis --entree fichier.json (charges volumineuses) ou depuis les options."""
    args = a.parse_args(argv)
    if args.entree:
        with open(args.entree, encoding="utf-8") as f:
            p = json.load(f)
        p.setdefault("devise", "USD")
        p.setdefault("source", os.environ.get("PRIX_AGENT_SOURCE", "auto"))
        return p
    return {k: v for k, v in vars(args).items() if k != "entree"}


def valeur_json(brut):
    """Une option reçue en ligne de commande peut être un JSON (liste, nombre) ou une chaîne."""
    if isinstance(brut, str):
        try:
            return json.loads(brut)
        except json.JSONDecodeError:
            return brut
    return brut


# --- source de test : déterministe, hors ligne ------------------------------

PRIX_TEST = {"BTC": 65000.0, "ETH": 3200.0, "SOL": 150.0}


def test_prix(actif: str) -> float:
    """Source hors ligne : quelques actifs connus, les autres sont des manques (comme une vraie source)."""
    if actif.upper() not in PRIX_TEST:
        raise EchecSource(f"actif inconnu de la source de test (connus : {', '.join(PRIX_TEST)})")
    return round(PRIX_TEST[actif.upper()] * (1 + (sum(ord(c) for c in actif.upper()) % 100) / 10000), 2)


def test_bougies(actif: str, intervalle: str, limite: int) -> list[list[float]]:
    pas_ms = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000, "1w": 604_800_000}[intervalle]
    alea = random.Random(f"{actif.upper()}-{intervalle}-{limite}")
    prix = test_prix(actif)
    fin = int(time.time() * 1000) // pas_ms * pas_ms
    bougies = []
    for i in range(limite):
        ouverture = prix
        cloture = round(prix * (1 + alea.uniform(-0.03, 0.03)), 2)
        haut = round(max(ouverture, cloture) * (1 + alea.uniform(0, 0.01)), 2)
        bas = round(min(ouverture, cloture) * (1 - alea.uniform(0, 0.01)), 2)
        volume = round(alea.uniform(1000, 5000), 3)
        bougies.append([fin - (limite - 1 - i) * pas_ms, ouverture, haut, bas, cloture, volume])
        prix = cloture
    return bougies

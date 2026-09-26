"""Service `prix` : prix comptant USD d'un ou plusieurs actifs.

    python -m agent.prix --actifs '["BTC","ETH"]' [--devise USD] [--source auto|binance|coingecko|test]

Sortie (contrat forge://marche/prix-comptant@1) :
    {"devise": "USD", "horodatage_ms": …, "prix": {"BTC": {"valeur": 65000.0, "source": "binance"}}, "manques": […]}

Binance d'abord, CoinGecko en secours. Un actif introuvable partout est listé
dans `manques` (« binance : paire XYZUSDT absente ; coingecko : actif inconnu —
ajoutez-le dans .env.local, PRIX_AGENT_COINGECKO=XYZ:identifiant ») et le code
de sortie est 2. Si aucun actif n'a de prix : code 1.
"""

from __future__ import annotations

import sys
import time

from . import _commun as c


def prix_binance(actif: str) -> float:
    donnees = c.obtenir_json(f"https://api.binance.com/api/v3/ticker/price?symbol={actif}USDT")
    if "price" not in donnees:
        raise c.EchecSource(f"paire {actif}USDT absente")
    return float(donnees["price"])


def prix_coingecko(actif: str) -> float:
    identifiant = c.COINGECKO_IDS.get(actif)
    if not identifiant:
        raise c.EchecSource(f"actif inconnu — ajoutez-le dans .env.local : PRIX_AGENT_COINGECKO={actif}:identifiant-coingecko")
    donnees = c.obtenir_json(f"https://api.coingecko.com/api/v3/simple/price?ids={identifiant}&vs_currencies=usd")
    try:
        return float(donnees[identifiant]["usd"])
    except (KeyError, TypeError) as exc:
        raise c.EchecSource(f"pas de prix USD pour {identifiant}") from exc


SOURCES = {"binance": prix_binance, "coingecko": prix_coingecko, "test": c.test_prix}


def principal(argv=None) -> int:
    a = c.analyseur("Prix comptant USD.")
    a.add_argument("--actifs", help='liste JSON, ex. ["BTC","ETH"]')
    p = c.parametres(a, argv)
    actifs = c.valeur_json(p.get("actifs"))
    if isinstance(actifs, str):
        actifs = [actifs]
    if not actifs:
        return c.echouer("paramètre --actifs manquant", ['exemple : --actifs \'["BTC","ETH"]\''])
    if p.get("devise", "USD") != "USD":
        return c.echouer(f"devise {p['devise']} non prise en charge", ["seule USD est disponible pour l'instant"])
    ordre = ["binance", "coingecko"] if p["source"] == "auto" else [p["source"]]

    resultat = {"devise": "USD", "horodatage_ms": int(time.time() * 1000), "prix": {}}
    manques: list[str] = []
    for actif in [str(x).upper() for x in actifs]:
        echecs = []
        for nom in ordre:
            try:
                resultat["prix"][actif] = {"valeur": SOURCES[nom](actif), "source": nom}
                break
            except c.EchecSource as exc:
                echecs.append(f"{nom} : {exc}")
        else:
            manques.append(f"{actif} — " + " ; ".join(echecs))
    if not resultat["prix"]:
        return c.echouer("aucun prix obtenu", manques)
    return c.terminer(resultat, manques)


if __name__ == "__main__":
    sys.exit(principal())

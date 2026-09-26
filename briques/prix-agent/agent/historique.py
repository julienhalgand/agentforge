"""Service `historique` : bougies OHLCV d'un actif.

    python -m agent.historique --actif BTC --intervalle 1d --limite 90 [--source auto|binance|coingecko|test]

Sortie (contrat forge://serie-temporelle/ohlcv@1) :
    {"actif": "BTC", "devise": "USD", "intervalle": "1d", "source": "binance",
     "bougies": [[horodatage_ms, ouverture, plus_haut, plus_bas, cloture, volume], …]}

Binance d'abord (bougies complètes). CoinGecko en secours ne fournit ni le
volume ni tous les intervalles : le volume est alors 0 et le manque est
annoncé (code 2), jamais tu en silence.
"""

from __future__ import annotations

import sys

from . import _commun as c

INTERVALLES_COINGECKO_JOURS = {"1d": 90, "4h": 30, "1h": 7, "30m": 1}


def bougies_binance(actif: str, intervalle: str, limite: int) -> list[list[float]]:
    donnees = c.obtenir_json(f"https://api.binance.com/api/v3/klines?symbol={actif}USDT&interval={intervalle}&limit={limite}")
    if not isinstance(donnees, list):
        raise c.EchecSource(f"paire {actif}USDT absente")
    return [[int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])] for k in donnees]


def bougies_coingecko(actif: str, intervalle: str, limite: int) -> tuple[list[list[float]], list[str]]:
    identifiant = c.COINGECKO_IDS.get(actif)
    if not identifiant:
        raise c.EchecSource(f"actif inconnu — ajoutez-le dans .env.local : PRIX_AGENT_COINGECKO={actif}:identifiant-coingecko")
    if intervalle not in INTERVALLES_COINGECKO_JOURS:
        raise c.EchecSource(f"intervalle {intervalle} indisponible (disponibles : {', '.join(INTERVALLES_COINGECKO_JOURS)})")
    jours = INTERVALLES_COINGECKO_JOURS[intervalle]
    donnees = c.obtenir_json(f"https://api.coingecko.com/api/v3/coins/{identifiant}/ohlc?vs_currency=usd&days={jours}")
    if not isinstance(donnees, list):
        raise c.EchecSource("réponse inattendue")
    bougies = [[int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), 0.0] for k in donnees][-limite:]
    manques = ["coingecko : volume indisponible (mis à 0)"]
    if len(bougies) < limite:
        manques.append(f"coingecko : {len(bougies)} bougies sur {limite} demandées (fenêtre maximale {jours} jours)")
    return bougies, manques


def principal(argv=None) -> int:
    a = c.analyseur("Bougies OHLCV.")
    a.add_argument("--actif")
    a.add_argument("--intervalle", default="1d")
    a.add_argument("--limite", type=int, default=90)
    p = c.parametres(a, argv)
    actif = str(p.get("actif") or "").upper()
    if not actif:
        return c.echouer("paramètre --actif manquant", ["exemple : --actif BTC --intervalle 1d --limite 90"])
    intervalle = str(p.get("intervalle", "1d"))
    limite = int(c.valeur_json(p.get("limite", 90)))
    if not 1 <= limite <= 1000:
        return c.echouer(f"limite {limite} hors de [1, 1000]")
    ordre = ["binance", "coingecko"] if p["source"] == "auto" else [p["source"]]

    echecs: list[str] = []
    for nom in ordre:
        try:
            if nom == "binance":
                bougies, manques = bougies_binance(actif, intervalle, limite), []
            elif nom == "coingecko":
                bougies, manques = bougies_coingecko(actif, intervalle, limite)
            else:
                bougies, manques = c.test_bougies(actif, intervalle, limite), []
            resultat = {"actif": actif, "devise": "USD", "intervalle": intervalle, "source": nom, "bougies": bougies}
            return c.terminer(resultat, manques)
        except c.EchecSource as exc:
            echecs.append(f"{nom} : {exc}")
    return c.echouer(f"aucune bougie pour {actif}", echecs)


if __name__ == "__main__":
    sys.exit(principal())

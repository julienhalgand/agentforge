"""Mesures calculées en code sur des bougies OHLCV. Aucun LLM, aucun chiffre inventé."""

from __future__ import annotations

import math
from statistics import mean, pstdev


def _variation_pct(actuel: float, ancien: float) -> float | None:
    if ancien == 0:
        return None
    return round((actuel / ancien - 1) * 100, 2)


def mesures(bougies: list[list[float]], intervalle: str) -> dict:
    """Retourne un dictionnaire de mesures ; les mesures impossibles (série trop courte) sont None et listées dans `manques`."""
    clotures = [b[4] for b in bougies]
    volumes = [b[5] for b in bougies]
    n = len(clotures)
    manques: list[str] = []
    dernier = clotures[-1]

    def variation(periodes: int, etiquette: str):
        if n > periodes:
            return _variation_pct(dernier, clotures[-1 - periodes])
        manques.append(f"variation {etiquette} : {periodes + 1} bougies nécessaires, {n} disponibles")
        return None

    def moyenne(periodes: int, etiquette: str):
        if n >= periodes:
            return round(mean(clotures[-periodes:]), 2)
        manques.append(f"moyenne {etiquette} : {periodes} bougies nécessaires, {n} disponibles")
        return None

    rendements = [math.log(clotures[i] / clotures[i - 1]) for i in range(1, n) if clotures[i - 1] > 0]
    volatilite = round(pstdev(rendements) * 100, 2) if len(rendements) >= 2 else None
    if volatilite is None:
        manques.append("volatilité : au moins 3 bougies nécessaires")

    sommet, repli_max = clotures[0], 0.0
    for c in clotures:
        sommet = max(sommet, c)
        if sommet > 0:
            repli_max = min(repli_max, (c / sommet - 1) * 100)

    return {
        "bougies": n,
        "intervalle": intervalle,
        "dernier": dernier,
        "premier": clotures[0],
        "plus_haut": max(b[2] for b in bougies),
        "plus_bas": min(b[3] for b in bougies),
        "variation_1": variation(1, "sur 1 période"),
        "variation_7": variation(7, "sur 7 périodes"),
        "variation_30": variation(30, "sur 30 périodes"),
        "variation_totale": _variation_pct(dernier, clotures[0]),
        "moyenne_7": moyenne(7, "7 périodes"),
        "moyenne_30": moyenne(30, "30 périodes"),
        "volatilite_pct": volatilite,
        "repli_max_pct": round(repli_max, 2),
        "volume_moyen": round(mean(volumes), 3) if volumes else None,
        "volume_total": round(sum(volumes), 3),
        "manques": manques,
    }


def alertes(m: dict, seuil_attention_pct: float, seuil_repli_pct: float) -> list[dict]:
    liste: list[dict] = []
    v1 = m.get("variation_1")
    if v1 is not None and abs(v1) >= seuil_attention_pct:
        sens = "hausse" if v1 > 0 else "baisse"
        liste.append({"niveau": "attention", "message": f"{sens} de {abs(v1):.2f} % sur la dernière période (seuil {seuil_attention_pct} %)"})
    if m["repli_max_pct"] <= -seuil_repli_pct:
        liste.append({"niveau": "critique", "message": f"repli maximal de {abs(m['repli_max_pct']):.2f} % depuis le sommet de la fenêtre (seuil {seuil_repli_pct} %)"})
    if m.get("moyenne_7") and m.get("moyenne_30"):
        if m["moyenne_7"] > m["moyenne_30"]:
            liste.append({"niveau": "info", "message": "moyenne 7 au-dessus de la moyenne 30 (tendance haussière de court terme)"})
        else:
            liste.append({"niveau": "info", "message": "moyenne 7 sous la moyenne 30 (tendance baissière de court terme)"})
    if m["volume_total"] == 0:
        liste.append({"niveau": "info", "message": "volume absent de la source : les mesures de volume ne sont pas significatives"})
    return liste

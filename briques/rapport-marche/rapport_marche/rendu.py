"""Rendu du rapport : Markdown, HTML en mode sombre, graphique SVG en pur Python (pas de matplotlib imposé)."""

from __future__ import annotations

import datetime as dt
import html


def _fmt(valeur, unite: str = "") -> str:
    if valeur is None:
        return "n/d"
    if isinstance(valeur, float):
        return f"{valeur:,.2f}{unite}".replace(",", " ")
    return f"{valeur}{unite}"


def _date(horodatage_ms: int) -> str:
    return dt.datetime.fromtimestamp(horodatage_ms / 1000, dt.timezone.utc).strftime("%Y-%m-%d")


def graphique_svg(bougies: list[list[float]], largeur: int = 900, hauteur: int = 320) -> str:
    """Courbe des clôtures + barres de volume, tout en SVG, lisible en mode sombre."""
    marge_g, marge_d, marge_h, marge_b = 70, 20, 20, 40
    zone_l = largeur - marge_g - marge_d
    zone_h = hauteur - marge_h - marge_b
    clotures = [b[4] for b in bougies]
    volumes = [b[5] for b in bougies]
    mini, maxi = min(clotures), max(clotures)
    if maxi == mini:
        maxi = mini + 1
    vmax = max(volumes) or 1
    n = len(bougies)

    def x(i: int) -> float:
        return marge_g + (i / max(n - 1, 1)) * zone_l

    def y(v: float) -> float:
        return marge_h + (1 - (v - mini) / (maxi - mini)) * zone_h

    points = " ".join(f"{x(i):.1f},{y(c):.1f}" for i, c in enumerate(clotures))
    barres = []
    largeur_barre = max(zone_l / n * 0.6, 1)
    for i, v in enumerate(volumes):
        h = (v / vmax) * zone_h * 0.25
        barres.append(f'<rect x="{x(i) - largeur_barre / 2:.1f}" y="{marge_h + zone_h - h:.1f}" width="{largeur_barre:.1f}" height="{h:.1f}" fill="#007aff" opacity="0.35"/>')
    graduations = []
    for k in range(5):
        v = mini + (maxi - mini) * k / 4
        graduations.append(f'<line x1="{marge_g}" y1="{y(v):.1f}" x2="{largeur - marge_d}" y2="{y(v):.1f}" stroke="#8e8e93" stroke-opacity="0.35" stroke-width="1"/>')
        graduations.append(f'<text x="{marge_g - 8}" y="{y(v) + 4:.1f}" text-anchor="end" font-size="11" fill="#9aa3b2">{_fmt(v)}</text>')
    dates = []
    for i, ancre in ((0, "start"), (n // 2, "middle"), (n - 1, "end")):
        dates.append(f'<text x="{x(i):.1f}" y="{hauteur - 12}" text-anchor="{ancre}" font-size="11" fill="#9aa3b2">{_date(int(bougies[i][0]))}</text>')
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {largeur} {hauteur}" width="100%" role="img" aria-label="Clôtures et volumes">'
        f'<rect width="{largeur}" height="{hauteur}" fill="transparent" rx="8"/>'
        + "".join(graduations)
        + "".join(barres)
        + f'<polyline points="{points}" fill="none" stroke="#ff9f0a" stroke-width="2.5" stroke-linejoin="round"/>'
        + "".join(dates)
        + "</svg>"
    )


def markdown(titre: str, date: str, donnees: dict, m: dict, alertes: list[dict], nom_svg: str) -> str:
    lignes = [f"# {titre}", "", f"_Rapport du {date} — {m['bougies']} bougies « {m['intervalle']} », source : {donnees.get('source', 'n/d')}_", ""]
    if alertes:
        lignes.append("## Alertes")
        for a in alertes:
            lignes.append(f"- **{a['niveau']}** : {a['message']}")
        lignes.append("")
    lignes += [
        "## Mesures",
        "",
        "| Mesure | Valeur |",
        "|---|---|",
        f"| Dernière clôture | {_fmt(m['dernier'])} {donnees.get('devise', 'USD')} |",
        f"| Variation 1 période | {_fmt(m['variation_1'], ' %')} |",
        f"| Variation 7 périodes | {_fmt(m['variation_7'], ' %')} |",
        f"| Variation 30 périodes | {_fmt(m['variation_30'], ' %')} |",
        f"| Variation sur la fenêtre | {_fmt(m['variation_totale'], ' %')} |",
        f"| Plus haut / plus bas | {_fmt(m['plus_haut'])} / {_fmt(m['plus_bas'])} |",
        f"| Moyenne 7 / 30 | {_fmt(m['moyenne_7'])} / {_fmt(m['moyenne_30'])} |",
        f"| Volatilité (écart-type des rendements) | {_fmt(m['volatilite_pct'], ' %')} |",
        f"| Repli maximal | {_fmt(m['repli_max_pct'], ' %')} |",
        f"| Volume moyen / total | {_fmt(m['volume_moyen'])} / {_fmt(m['volume_total'])} |",
        "",
        "## Graphique",
        "",
        f"![Clôtures et volumes]({nom_svg})",
        "",
    ]
    manques = list(m.get("manques", [])) + list(donnees.get("manques", []))
    if manques:
        lignes.append("## Manques")
        for x in manques:
            lignes.append(f"- {x}")
        lignes.append("")
    return "\n".join(lignes)


def page_html(titre: str, date: str, corps_markdown: str, svg: str) -> str:
    """HTML autonome, mode sombre, sans dépendance : le Markdown est rendu simplement (titres, listes, tableaux, gras)."""
    return f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(titre)} — {date}</title>
<style>
  :root {{ color-scheme: light dark; --fond: #f2f2f7; --carte: #ffffff; --texte: #1c1c1e; --sourd: #8e8e93; --sep: #e5e5ea; --accent: #007aff; --attention: #ff9f0a; --critique: #ff3b30; --info: #007aff; --graphe: #ffffff; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --fond: #000; --carte: #1c1c1e; --texte: #f2f2f7; --sourd: #8e8e93; --sep: #38383a; --accent: #0a84ff; --critique: #ff453a; --graphe: #1c1c1e; }} }}
  body {{ margin: 0; padding: 24px 16px; background: var(--fond); color: var(--texte); font: 15px/1.55 -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", system-ui, sans-serif; }}
  main {{ max-width: 900px; margin: 0 auto; }}
  h1 {{ font-size: 28px; font-weight: 700; letter-spacing: -.02em; margin: 0 0 4px; }} h2 {{ font-size: 13px; text-transform: uppercase; letter-spacing: .04em; color: var(--sourd); margin: 26px 4px 8px; }}
  em {{ color: var(--sourd); }}
  table {{ border-collapse: collapse; width: 100%; background: var(--carte); border-radius: 16px; overflow: hidden; box-shadow: 0 1px 2px rgba(0,0,0,.04), 0 8px 24px rgba(0,0,0,.06); }}
  th, td {{ text-align: left; padding: 11px 16px; border-bottom: 1px solid var(--sep); }} tr:last-child td {{ border-bottom: 0; }} th {{ color: var(--sourd); font-weight: 600; font-size: 13px; }}
  ul {{ background: var(--carte); border-radius: 16px; padding: 12px 16px 12px 34px; margin: 0; box-shadow: 0 1px 2px rgba(0,0,0,.04), 0 8px 24px rgba(0,0,0,.06); }}
  li strong.attention {{ color: var(--attention); }} li strong.critique {{ color: var(--critique); }} li strong.info {{ color: var(--info); }}
  figure {{ margin: 0; background: var(--carte); padding: 12px; border-radius: 16px; box-shadow: 0 1px 2px rgba(0,0,0,.04), 0 8px 24px rgba(0,0,0,.06); }}
  figure svg rect:first-child {{ fill: var(--graphe); }}
</style>
</head>
<body><main>
{_markdown_vers_html(corps_markdown, svg)}
</main></body></html>
"""


def _markdown_vers_html(md: str, svg: str) -> str:
    sortie: list[str] = []
    dans_liste = False
    dans_tableau = False
    for ligne in md.splitlines():
        if ligne.startswith("|"):
            cellules = [c.strip() for c in ligne.strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cellules):
                continue
            balise = "th" if not dans_tableau else "td"
            if not dans_tableau:
                sortie.append("<table>")
                dans_tableau = True
            sortie.append("<tr>" + "".join(f"<{balise}>{_inline(c)}</{balise}>" for c in cellules) + "</tr>")
            continue
        if dans_tableau:
            sortie.append("</table>")
            dans_tableau = False
        if ligne.startswith("- "):
            if not dans_liste:
                sortie.append("<ul>")
                dans_liste = True
            sortie.append(f"<li>{_inline(ligne[2:])}</li>")
            continue
        if dans_liste:
            sortie.append("</ul>")
            dans_liste = False
        if ligne.startswith("# "):
            sortie.append(f"<h1>{_inline(ligne[2:])}</h1>")
        elif ligne.startswith("## "):
            sortie.append(f"<h2>{_inline(ligne[3:])}</h2>")
        elif ligne.startswith("!["):
            sortie.append(f"<figure>{svg}</figure>")
        elif ligne.startswith("_") and ligne.endswith("_"):
            sortie.append(f"<p><em>{html.escape(ligne[1:-1])}</em></p>")
        elif ligne.strip():
            sortie.append(f"<p>{_inline(ligne)}</p>")
    if dans_liste:
        sortie.append("</ul>")
    if dans_tableau:
        sortie.append("</table>")
    return "\n".join(sortie)


def _inline(texte: str) -> str:
    texte = html.escape(texte)
    for niveau in ("attention", "critique", "info"):
        texte = texte.replace(f"**{niveau}**", f'<strong class="{niveau}">{niveau}</strong>')
    while "**" in texte:
        texte = texte.replace("**", "<strong>", 1).replace("**", "</strong>", 1)
    return texte

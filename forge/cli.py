"""Ligne de commande `forge`.

    forge lister                        briques, services, pipelines, contrats
    forge valider <dossier|pipeline>    vérifie un manifeste ou un pipeline sans rien lancer
    forge appeler <brique> <service> [--entree '{…}' | --fichier entree.json]
    forge executer <pipeline>           vérifie puis exécute un pipeline
    forge installer <url github|dossier> [--nom nom]
    forge supprimer <brique>            vers ~/.agentforge/.corbeille/
    forge hub [--port 8700]             lance le hub web local

Toute erreur est parlante : cause, détails élément par élément, remède.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import manifeste as mod_manifeste
from . import pipeline as mod_pipeline
from . import registre, schemas
from .mcp.client import ClientMCP
from .utils.erreurs import ErreurForge


def _imprimer_json(valeur) -> None:
    print(json.dumps(valeur, ensure_ascii=False, indent=2))


def commande_lister(_args) -> int:
    briques, problemes = registre.lister_briques()
    print("Briques")
    for m in briques:
        print(f"  {m.icone} {m.nom} {m.version} ({m.type}) — {m.description}")
        for s in m.services:
            print(f"      · {s.nom} : {s.entree if isinstance(s.entree, str) else 'entrée en ligne'} → {s.sortie if isinstance(s.sortie, str) else 'sortie en ligne'}")
    if not briques:
        print("  (aucune)")
    pipelines, problemes_p = registre.lister_pipelines()
    print("Pipelines")
    for m in pipelines:
        print(f"  🔗 {m.nom} — {m.description} ({len(m.etapes)} étape(s))")
    if not pipelines:
        print("  (aucun)")
    print("Contrats")
    for c in schemas.lister_contrats():
        print(f"  📄 {c['reference']} — {c['titre']}")
    for pb in problemes + problemes_p:
        print(f"⚠ {pb}", file=sys.stderr)
    return 0


def commande_valider(args) -> int:
    cible = Path(args.cible)
    if cible.is_dir():
        m = mod_manifeste.charger(cible)
        print(f"✔ manifeste valide : {m.nom} {m.version} ({m.type}), {len(m.services)} service(s)")
        return 0
    m = registre.trouver_pipeline(args.cible)
    etapes = mod_pipeline.verifier(m)
    print(f"✔ pipeline valide : {m.nom}, {len(etapes)} étape(s)")
    for e in etapes:
        print(f"    {e.identifiant} : {e.brique.nom}.{e.service.nom}")
    return 0


def commande_appeler(args) -> int:
    m = registre.trouver_brique(args.brique)
    service = m.service(args.service)
    if args.fichier:
        with open(args.fichier, encoding="utf-8") as f:
            entree = json.load(f)
    elif args.entree:
        entree = json.loads(args.entree)
    else:
        entree = service.exemple or {}
    with ClientMCP(m.commande_serveur(), m.dossier) as client:
        valeur = client.appeler(service.nom, entree)
    _imprimer_json(valeur)
    if isinstance(valeur, dict) and valeur.get("manques"):
        print("Réponse partielle, manques :", file=sys.stderr)
        for manque in valeur["manques"]:
            print(f"  - {manque}", file=sys.stderr)
        return 2
    return 0


def commande_executer(args) -> int:
    m = registre.trouver_pipeline(args.pipeline)
    resultat = mod_pipeline.executer(m, journal=lambda msg: print(f"… {msg}", file=sys.stderr))
    _imprimer_json(resultat.en_json() if args.tout else {k: _resume(v) for k, v in resultat.resultats.items()})
    if resultat.manques:
        print("Manques :", file=sys.stderr)
        for manque in resultat.manques:
            print(f"  - {manque}", file=sys.stderr)
        return 2
    return 0


def _resume(valeur):
    """Résumé lisible d'un résultat d'étape (les listes longues sont comptées, pas tronquées en silence)."""
    if isinstance(valeur, dict):
        return {k: (f"[{len(v)} éléments]" if isinstance(v, list) and len(v) > 10 else v) for k, v in valeur.items()}
    return valeur


def commande_installer(args) -> int:
    m = registre.installer(args.source, args.nom)
    print(f"✔ installée : {m.nom} {m.version} dans {m.dossier}")
    return 0


def commande_supprimer(args) -> int:
    cible = registre.supprimer(args.brique)
    print(f"✔ déplacée vers la corbeille : {cible}")
    return 0


def commande_hub(args) -> int:
    from .hub.serveur import lancer

    lancer(port=args.port, ouvrir=not args.sans_navigateur)
    return 0


def construire_analyseur() -> argparse.ArgumentParser:
    a = argparse.ArgumentParser(prog="forge", description="agentforge — relier des briques locales par un protocole unique.")
    sous = a.add_subparsers(dest="commande", required=True)

    sous.add_parser("lister", help="briques, pipelines et contrats disponibles").set_defaults(fonction=commande_lister)

    v = sous.add_parser("valider", help="vérifie un manifeste (dossier) ou un pipeline (nom ou .json)")
    v.add_argument("cible")
    v.set_defaults(fonction=commande_valider)

    ap = sous.add_parser("appeler", help="appelle un service d'une brique")
    ap.add_argument("brique")
    ap.add_argument("service")
    ap.add_argument("--entree", help="paramètres JSON en ligne")
    ap.add_argument("--fichier", help="fichier JSON de paramètres (pour les charges volumineuses)")
    ap.set_defaults(fonction=commande_appeler)

    ex = sous.add_parser("executer", help="vérifie puis exécute un pipeline")
    ex.add_argument("pipeline")
    ex.add_argument("--tout", action="store_true", help="imprime les résultats complets")
    ex.set_defaults(fonction=commande_executer)

    i = sous.add_parser("installer", help="installe une brique depuis GitHub ou un dossier")
    i.add_argument("source")
    i.add_argument("--nom")
    i.set_defaults(fonction=commande_installer)

    s = sous.add_parser("supprimer", help="déplace une brique installée vers la corbeille")
    s.add_argument("brique")
    s.set_defaults(fonction=commande_supprimer)

    h = sous.add_parser("hub", help="lance le hub web local")
    h.add_argument("--port", type=int, default=8700)
    h.add_argument("--sans-navigateur", action="store_true")
    h.set_defaults(fonction=commande_hub)
    return a


def principal(argv: list[str] | None = None) -> int:
    args = construire_analyseur().parse_args(argv)
    try:
        return args.fonction(args)
    except ErreurForge as exc:
        print(f"✘ {exc.texte()}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrompu", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(principal())

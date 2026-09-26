"""Créer une brique ou composer un pipeline à partir d'une phrase, sans code.

Le modèle local ne produit jamais de code libre : il remplit un gabarit sous
JSON contraint (nom, services, champs, et le corps d'UNE fonction Python par
service). Le gabarit génère tout le reste (manifeste, serveur MCP, erreurs).
Puis forge valide (manifeste, contrats, syntaxe, imports autorisés), exécute
la brique une fois pour de vrai sur son exemple, valide la sortie, et renvoie
chaque échec au modèle, jusqu'à `tours` fois. Une brique cassée n'est jamais
installée en silence.

`generer_json(consigne, schema, systeme)` est une fonction fournie par
l'appelant (en pratique : la brique modele-local), ce qui rend tout testable
avec un modèle simulé.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable

from . import manifeste as mod_manifeste
from . import pipeline as mod_pipeline
from . import registre, schemas
from .mcp.client import ClientMCP
from .utils.erreurs import ErreurForge
from .utils.fichiers import ecrire_json, mettre_a_la_corbeille

GenererJSON = Callable[[str, dict, str], Any]

IMPORTS_AUTORISES = {
    "json", "math", "statistics", "datetime", "time", "re", "collections", "itertools", "random", "string", "csv", "html",
    "urllib", "urllib.parse", "urllib.request", "pathlib", "textwrap", "unicodedata", "decimal", "fractions", "hashlib", "base64",
    "forge.utils.reseau", "forge.utils.erreurs",
}
TYPES_CHAMPS = {"string": {"type": "string"}, "number": {"type": "number"}, "integer": {"type": "integer"}, "boolean": {"type": "boolean"}, "array": {"type": "array"}, "object": {"type": "object"}}

SCHEMA_CHAMP = {
    "type": "object",
    "required": ["nom", "type", "description"],
    "properties": {
        "nom": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
        "type": {"type": "string", "enum": list(TYPES_CHAMPS)},
        "description": {"type": "string"},
        "obligatoire": {"type": "boolean"},
    },
}

SCHEMA_BRIQUE = {
    "type": "object",
    "required": ["nom", "description", "icone", "services"],
    "properties": {
        "nom": {"type": "string", "pattern": "^[a-z][a-z0-9-]{1,30}$", "description": "nom court en minuscules et tirets, ex. meteo-nantes"},
        "description": {"type": "string"},
        "icone": {"type": "string", "description": "un seul emoji"},
        "page": {"type": "string", "description": "optionnel : page HTML complète et autonome (HTML+CSS+JS dans un seul fichier, sans bibliothèque externe) pour une brique interactive : métronome, chronomètre, jeu, formulaire…"},
        "services": {
            "type": "array",
            "minItems": 0,
            "maxItems": 3,
            "items": {
                "type": "object",
                "required": ["nom", "description", "entree", "sortie", "exemple", "code"],
                "properties": {
                    "nom": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
                    "description": {"type": "string"},
                    "entree": {"type": "array", "items": SCHEMA_CHAMP, "description": "champs d'entrée"},
                    "sortie": {
                        "type": "object",
                        "required": ["contrat"],
                        "properties": {
                            "contrat": {"type": "string", "description": "une référence forge://… existante, ou \"champs\" pour une sortie décrite par « champs »"},
                            "champs": {"type": "array", "items": SCHEMA_CHAMP},
                        },
                    },
                    "exemple": {"type": "object", "description": "un jeu de paramètres d'entrée réel pour tester"},
                    "code": {"type": "string", "description": "Python : imports autorisés puis `def executer(entree: dict) -> dict:` qui rend le dictionnaire de sortie"},
                },
            },
        },
    },
}

SYSTEME_BRIQUE = (
    "Tu fabriques des briques pour agentforge, un outil local. Tu réponds uniquement par le JSON demandé. "
    "Règles du code : Python 3.10, bibliothèque standard seulement ; pour HTTPS utilise "
    "`from forge.utils.reseau import obtenir_json` (obtenir_json(url) rend le JSON décodé) ; "
    "pour signaler un problème lève `from forge.utils.erreurs import ErreurForge` : ErreurForge(cause, remede). "
    "La fonction s'appelle exactement `executer(entree)` ; `entree` est un dict avec les champs d'entrée ; elle rend un dict avec les champs de sortie. "
    "N'invente pas d'API : utilise des services publics sans clé (Open-Meteo, Wikipédia, Binance, CoinGecko, frankfurter.app…). "
    "Tous les noms, descriptions et messages sont en français. "
    "Si la demande est un outil interactif (métronome, minuteur, jeu, bloc-notes…), rends une « page » HTML complète et autonome, "
    "jolie et simple (fond clair, gros boutons), et une liste de services vide. Sinon, des services et pas de page."
)


# --- gabarit --------------------------------------------------------------------------

def _schema_champs(champs: list[dict]) -> dict:
    return {
        "type": "object",
        "required": [c["nom"] for c in champs if c.get("obligatoire", True)],
        "properties": {c["nom"]: {**TYPES_CHAMPS[c["type"]], "description": c.get("description", "")} for c in champs},
    }


def valider_code(code: str) -> None:
    """Syntaxe, imports autorisés, présence de `executer`. Lève une ErreurForge parlante."""
    try:
        arbre = ast.parse(code)
    except SyntaxError as exc:
        raise ErreurForge(f"code Python invalide : ligne {exc.lineno} : {exc.msg}", "corrige la syntaxe", [exc.text or ""])
    problemes = []
    a_executer = False
    for noeud in ast.walk(arbre):
        if isinstance(noeud, ast.Import):
            for alias in noeud.names:
                if alias.name not in IMPORTS_AUTORISES and alias.name.split(".")[0] not in IMPORTS_AUTORISES:
                    problemes.append(f"import interdit : {alias.name}")
        elif isinstance(noeud, ast.ImportFrom):
            module = noeud.module or ""
            if module not in IMPORTS_AUTORISES and module.split(".")[0] not in IMPORTS_AUTORISES:
                problemes.append(f"import interdit : from {module}")
        elif isinstance(noeud, ast.FunctionDef) and noeud.name == "executer":
            a_executer = True
        elif isinstance(noeud, ast.Call) and isinstance(noeud.func, ast.Name) and noeud.func.id in ("exec", "eval", "open", "__import__"):
            problemes.append(f"appel interdit : {noeud.func.id}()")
    if not a_executer:
        problemes.append("la fonction `def executer(entree):` est absente")
    if problemes:
        raise ErreurForge("code refusé", "utilise seulement la bibliothèque standard et forge.utils.reseau / forge.utils.erreurs", problemes)


GABARIT_SERVEUR = '''"""{description}

Brique créée sans code depuis la description : « {phrase} ».
"""

from __future__ import annotations

import sys

from forge.mcp.serveur import ServeurMCP

from . import services as _services

serveur = ServeurMCP({nom!r}, "0.1.0")
{enregistrements}

if __name__ == "__main__":
    serveur.servir()
    sys.exit(0)
'''


def _nom_module(nom: str) -> str:
    return nom.replace("-", "_")


def gabarit(spec: dict, phrase: str, dossier: Path) -> mod_manifeste.Manifeste:
    """Écrit la brique complète dans `dossier` à partir de la spécification du modèle. Lève si invalide."""
    problemes = []
    for s in spec["services"]:
        try:
            valider_code(s["code"])
        except ErreurForge as exc:
            problemes.append(f"service {s['nom']} : {exc.cause} — " + " ; ".join(exc.details))
        if s["sortie"]["contrat"] != "champs":
            try:
                schemas.charger_contrat(s["sortie"]["contrat"])
            except ErreurForge as exc:
                problemes.append(f"service {s['nom']} : {exc.cause}")
        elif not s["sortie"].get("champs"):
            problemes.append(f"service {s['nom']} : sortie « champs » sans aucun champ")
    page = (spec.get("page") or "").strip()
    if page and "<html" not in page.lower():
        problemes.append("la page doit être un document HTML complet (<!doctype html><html>…)")
    if not spec["services"] and not page:
        problemes.append("il faut au moins un service ou une page")
    if problemes:
        raise ErreurForge("spécification refusée", "corrige chaque point", problemes)

    module = _nom_module(spec["nom"])
    dossier.mkdir(parents=True, exist_ok=True)
    (dossier / module).mkdir(exist_ok=True)
    (dossier / module / "__init__.py").write_text(f'"""{spec["description"]}"""\n', encoding="utf-8")

    # services.py : une fonction par service, chacune dans son espace de noms (le code du modèle est isolé par fonction)
    blocs = ["# Code des services, généré depuis une description. Chaque service est une fonction executer_<nom>(entree).", "from __future__ import annotations", ""]
    enregistrements = []
    services_manifeste = []
    for s in spec["services"]:
        code = s["code"].replace("def executer(", f"def executer_{s['nom']}(", 1)
        blocs.append(f"# --- {s['nom']} : {s['description']}")
        blocs.append(code.rstrip() + "\n")
        entree = _schema_champs(s["entree"])
        sortie = s["sortie"]["contrat"] if s["sortie"]["contrat"] != "champs" else _schema_champs(s["sortie"]["champs"])
        enregistrements.append(
            f"serveur.ajouter({s['nom']!r}, {s['description']!r}, {json.dumps(entree, ensure_ascii=False)}, {json.dumps(sortie, ensure_ascii=False)}, _services.executer_{s['nom']})"
        )
        services_manifeste.append({"nom": s["nom"], "description": s["description"], "entree": entree, "sortie": sortie, "exemple": s.get("exemple", {})})
    (dossier / module / "services.py").write_text("\n".join(blocs), encoding="utf-8")
    (dossier / module / "serveur.py").write_text(
        GABARIT_SERVEUR.format(description=spec["description"], phrase=phrase.replace('"', "'"), nom=spec["nom"], enregistrements="\n".join(enregistrements)),
        encoding="utf-8",
    )
    manifeste_brut = {
        "forge": 1,
        "type": "application",
        "nom": spec["nom"],
        "version": "0.1.0",
        "description": spec["description"],
        "icone": spec.get("icone", "🧩"),
        "interpreteur": "python",
        "transport": {"type": "mcp-stdio", "commande": ["-m", f"{module}.serveur"]},
        "services": services_manifeste,
        "planification": {"mode": "jamais"},
        "creee_depuis": phrase,
    }
    if page:
        (dossier / "page").mkdir(exist_ok=True)
        (dossier / "page" / "index.html").write_text(page, encoding="utf-8")
        manifeste_brut["page"] = "page/index.html"
    ecrire_json(dossier / "forge.json", manifeste_brut, sauvegarder=False)
    return mod_manifeste.charger(dossier)


# --- création ----------------------------------------------------------------------------

def _catalogue_contrats() -> str:
    lignes = []
    for c in schemas.lister_contrats():
        champs = ", ".join(f"{k} ({v.get('type', '?')})" for k, v in c["proprietes"].items())
        lignes.append(f"- {c['reference']} : {c['titre']} — champs : {champs}")
    return "\n".join(lignes)


def _essai(m: mod_manifeste.Manifeste, journal=None) -> list[str]:
    """Appelle chaque service sur son exemple ; rend la liste des échecs (vide = tout va bien)."""
    journal = journal or (lambda _m: None)
    echecs = []
    if not m.services:
        return echecs
    with ClientMCP(m.commande_serveur(), m.dossier, delai_s=120) as client:
        for s in m.services:
            try:
                journal(f"essai de {s.nom} avec {json.dumps(s.exemple or {}, ensure_ascii=False)[:80]}")
                resultat = client.appeler(s.nom, s.exemple or {})
                journal(f"{s.nom} : réponse conforme au contrat")
                if isinstance(resultat, dict) and resultat.get("manques"):
                    echecs.append(f"service {s.nom} : réponse partielle — " + " ; ".join(resultat["manques"]))
            except ErreurForge as exc:
                echecs.append(f"service {s.nom} a échoué à l'essai : {exc.cause} — " + " ; ".join(exc.details[:6]))
    return echecs


def creer_brique(phrase: str, generer_json: GenererJSON, journal=None, tours: int = 3, installer: bool = True, annulee=None) -> dict:
    """Une phrase → une brique installée et testée. Rend {nom, dossier, tours, spec} ou lève une ErreurForge détaillée."""
    journal = journal or (lambda _m: None)
    annulee = annulee or (lambda: False)
    consigne = (
        f"Décris une brique qui fait ceci : « {phrase} ».\n\n"
        f"Contrats de sortie existants (préfère-les quand ils correspondent, sinon \"champs\") :\n{_catalogue_contrats()}\n\n"
        "Donne un exemple d'entrée réaliste pour chaque service : il servira à tester la brique pour de vrai."
    )
    dernier_probleme = ""
    spec = None
    for tour in range(1, tours + 1):
        if annulee():
            raise ErreurForge("création annulée", "relance quand tu veux")
        journal(f"tour {tour}/{tours} : le modèle {'corrige' if dernier_probleme else 'écrit'} la brique (nom, services, code) — cette étape est la plus longue")
        demande = consigne if not dernier_probleme else (
            consigne + f"\n\nTa proposition précédente était :\n{json.dumps(spec, ensure_ascii=False)}\n\n"
            f"Elle a échoué :\n{dernier_probleme}\n\nCorrige-la et renvoie la brique complète."
        )
        try:
            spec = generer_json(demande, SCHEMA_BRIQUE, SYSTEME_BRIQUE)
        except ErreurForge as exc:
            dernier_probleme = exc.texte()
            journal(f"réponse inutilisable : {exc.cause}")
            continue
        journal(f"proposition reçue : « {spec.get('nom', '?')} », {len(spec.get('services', []))} service(s)" + (", une page interactive" if spec.get("page") else ""))
        temporaire = Path(tempfile.mkdtemp(prefix="brique-", dir=str(registre.DOSSIER_UTILISATEUR)))
        try:
            journal("vérification du code (syntaxe, imports autorisés, contrats)")
            m = gabarit(spec, phrase, temporaire)
            journal(f"brique écrite ; essai réel de {len(m.services)} service(s)" if m.services else "brique écrite (page seule, pas de service à essayer)")
            echecs = _essai(m, journal)
            if echecs:
                dernier_probleme = "\n".join(echecs)
                for e in echecs[:3]:
                    journal("échec à l'essai : " + e[:220])
                continue
            if not installer:
                return {"nom": m.nom, "dossier": str(temporaire), "tours": tour, "spec": spec}
            cible = registre.DOSSIER_UTILISATEUR / "briques" / m.nom
            cible.parent.mkdir(parents=True, exist_ok=True)
            if cible.exists():
                mettre_a_la_corbeille(cible, racine=registre.DOSSIER_UTILISATEUR)
            shutil.move(str(temporaire), str(cible))
            journal(f"installée : {cible}")
            return {"nom": m.nom, "dossier": str(cible), "tours": tour, "spec": spec}
        except ErreurForge as exc:
            dernier_probleme = exc.texte()
            journal(f"refusée : {exc.cause}")
            for d in exc.details[:4]:
                journal("  → " + d[:220])
        finally:
            if temporaire.exists() and (not installer or not (registre.DOSSIER_UTILISATEUR / "briques" / (spec or {}).get("nom", "")).exists()):
                shutil.rmtree(temporaire, ignore_errors=True)
    raise ErreurForge(
        f"la brique n'a pas pu être créée en {tours} tours",
        "reformule plus simplement, ou choisis un modèle plus gros (7B) dans la brique modele-local",
        dernier_probleme.splitlines()[:12],
    )


# --- composition -----------------------------------------------------------------------------

SCHEMA_PIPELINE = {
    "type": "object",
    "required": ["nom", "description", "planification", "etapes"],
    "properties": {
        "nom": {"type": "string", "pattern": "^[a-z][a-z0-9-]{1,40}$"},
        "description": {"type": "string"},
        "planification": {
            "type": "object",
            "required": ["mode"],
            "properties": {"mode": {"type": "string", "enum": ["jamais", "quotidien", "toutes_les_n_heures"]}, "heure": {"type": "string"}, "heures": {"type": "integer"}},
        },
        "etapes": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["id", "brique", "service", "entree"],
                "properties": {"id": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"}, "brique": {"type": "string"}, "service": {"type": "string"}, "entree": {"type": "object"}},
            },
        },
    },
}

SYSTEME_PIPELINE = (
    "Tu composes des pipelines agentforge à partir de briques existantes. Réponds uniquement par le JSON demandé. "
    "Une étape appelle brique.service avec des paramètres ; pour passer le résultat d'une étape précédente, écris la chaîne \"$id\" "
    "(résultat entier) ou \"$id.champ\". Ne branche que des contrats identiques. « chaque matin » = planification quotidien à 08:00, "
    "« toutes les 6 heures » = toutes_les_n_heures avec heures 6, sinon jamais. Noms en minuscules et tirets, en français."
)


def _catalogue_briques() -> str:
    briques, _ = registre.lister_briques()
    lignes = []
    for m in briques:
        for s in m.services:
            entree = schemas.resoudre(s.entree)
            params = ", ".join(f"{k}{'*' if k in entree.get('required', []) else ''}: {nomc(v)}" for k, v in entree.get("properties", {}).items())
            lignes.append(f"- {m.nom}.{s.nom} — {s.description} — entrée {{{params}}} → sortie {nomc(s.sortie)}" + (f" — exemple {json.dumps(s.exemple, ensure_ascii=False)}" if s.exemple else ""))
    return "\n".join(lignes)


def nomc(schema: Any) -> str:
    if isinstance(schema, str):
        return schema
    if isinstance(schema, dict):
        return schema.get("$ref") or schema.get("type", "objet")
    return "?"


def composer_pipeline(phrase: str, generer_json: GenererJSON, journal=None, tours: int = 2) -> dict:
    """Une phrase → un pipeline JSON vérifié (non enregistré). Lève une ErreurForge détaillée sinon."""
    journal = journal or (lambda _m: None)
    consigne = f"Compose un pipeline qui fait ceci : « {phrase} ».\n\nBriques et services disponibles :\n{_catalogue_briques()}"
    dernier_probleme = ""
    brut = None
    for tour in range(1, tours + 1):
        journal(f"tour {tour}/{tours}")
        demande = consigne if not dernier_probleme else consigne + f"\n\nTa proposition précédente :\n{json.dumps(brut, ensure_ascii=False)}\n\nElle est invalide :\n{dernier_probleme}\n\nCorrige-la."
        try:
            brut = generer_json(demande, SCHEMA_PIPELINE, SYSTEME_PIPELINE)
            for etape in brut.get("etapes", []):
                if isinstance(etape.get("brique"), str) and "." in etape["brique"] and not etape.get("service"):
                    etape["brique"], etape["service"] = etape["brique"].split(".", 1)
                elif isinstance(etape.get("brique"), str) and "." in etape["brique"] and etape["brique"].endswith("." + str(etape.get("service"))):
                    etape["brique"] = etape["brique"].rsplit(".", 1)[0]
            pipeline = {"forge": 1, "type": "pipeline", "version": "0.1.0", "icone": "🔗", **brut, "creee_depuis": phrase}
            m = mod_manifeste.Manifeste(dossier=registre.DOSSIER_UTILISATEUR / "pipelines", brut=pipeline)
            schemas.valider(pipeline, mod_manifeste.SCHEMA_MANIFESTE, contexte="pipeline")
            mod_manifeste.verifier_coherence(m)
            mod_pipeline.verifier(m)
            return pipeline
        except ErreurForge as exc:
            dernier_probleme = exc.texte()
            journal(f"invalide : {exc.cause}")
    raise ErreurForge(f"pas de pipeline valide en {tours} tours", "reformule, ou vérifie que les briques nécessaires existent", dernier_probleme.splitlines()[:12])


def enregistrer_pipeline(pipeline: dict) -> Path:
    dossier = registre.DOSSIER_UTILISATEUR / "pipelines"
    return ecrire_json(dossier / f"{pipeline['nom']}.json", pipeline)


def nettoyer_nom(nom: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", nom.lower()).strip("-")[:40]


# --- un seul champ : composer ou créer ? -----------------------------------------------------

SCHEMA_DECISION = {
    "type": "object",
    "required": ["action", "raison"],
    "properties": {
        "action": {"type": "string", "enum": ["composer", "creer"]},
        "raison": {"type": "string"},
    },
}

SYSTEME_DECISION = (
    "Tu décides comment satisfaire une demande dans agentforge. « composer » = la demande se réalise en reliant "
    "des briques déjà installées (elles sont listées) ; « creer » = il manque une capacité, il faut fabriquer une nouvelle brique. "
    "Réponds uniquement par le JSON demandé, raison en une phrase en français."
)


def decider(phrase: str, generer_json: GenererJSON) -> dict:
    consigne = f"Demande : « {phrase} ».\n\nBriques installées :\n{_catalogue_briques()}"
    d = generer_json(consigne, SCHEMA_DECISION, SYSTEME_DECISION)
    if d.get("action") not in ("composer", "creer"):
        raise ErreurForge("décision inutilisable", "réessayez")
    return d

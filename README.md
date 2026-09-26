# agentforge

Relier des briques locales — applications et agents — par un **protocole unique**, pour qu'un
utilisateur, développeur ou non, les assemble en pipelines qui font ce qu'il veut.
**Local d'abord** : zéro cloud, zéro compte, zéro clé obligatoire. Bibliothèque standard seulement.

```
prix-agent.historique  ──ohlcv@1──▶  rapport-marche.generer  ──rapport@1──▶  rapports/rapport_2026-09-26.html
```

## Démarrer

```bash
git clone https://github.com/julienhalgand/agentforge
cd agentforge
python -m forge lister                          # briques, pipelines, contrats
python -m forge valider rapport-btc-quotidien   # vérifie les branchements, ne lance rien
python -m forge executer rapport-btc-quotidien  # exécute de bout en bout
python -m forge hub                             # http://localhost:8700
```

Sans accès à Binance/CoinGecko (ou pour un essai hors ligne) : `PRIX_AGENT_SOURCE=test`.
`pip install -e .` rend la commande `forge` disponible ; ce n'est pas obligatoire.

## Ce qu'il y a dedans

| Dossier | Rôle |
|---|---|
| `forge/` | le cœur : manifeste, contrats et validation, MCP (serveur, client, adaptateur pour briques existantes), pipelines, registre, CLI, hub |
| `contrats/` | les contrats de données partagés (`forge://serie-temporelle/ohlcv@1`, `marche/prix-comptant@1`, `document/rapport@1`, `tache/progres@1`, `planification/planification@1`) |
| `briques/prix-agent` | brique modèle au protocole existant « `python -m agent.<service>` → JSON, codes 0/1/2 », exposée en MCP **sans être réécrite** par l'adaptateur |
| `briques/rapport-marche` | agent **sans LLM** : mesures calculées en code, rapport `.md` + `.html` (mode sombre) + graphique SVG |
| `pipelines/` | `rapport-btc-quotidien.json` : les deux briques reliées, planifié tous les jours à 08:15 |
| `docs/PROTOCOLE.md` | le protocole complet |
| `tests/` | `python -m pytest` |

## Le hub

`forge hub` ouvre http://localhost:8700 : une tuile par brique et par pipeline (état, planification,
dernier rapport, page interactive), un planificateur (quotidien / toutes les N heures / jamais,
rattrapage au démarrage, verrou anti-doublon), les vues composées, et l'**assembleur** pour construire
un pipeline sans code : on choisit une brique, un service, et on branche chaque entrée sur une étape
précédente compatible — seules les sources dont le contrat correspond sont proposées.

## Écrire une brique

Un dépôt GitHub avec un `forge.json` à la racine (voir `docs/PROTOCOLE.md`, §3). Deux voies :

- **brique existante** (`python -m agent.<service>` → JSON) : `"transport": {"type": "services-json"}`,
  l'adaptateur fait le reste ;
- **brique native** : `from forge.mcp.serveur import ServeurMCP`, un décorateur par service
  (voir `briques/rapport-marche/rapport_marche/serveur.py`).

Puis `forge installer https://github.com/vous/votre-brique`.

## Les règles

1. **Local d'abord.** Les clés d'API optionnelles vivent dans `.env.local`, jamais dans un manifeste.
2. **Jamais tronquer en silence.** Une réponse partielle est rendue avec ses `manques` listés.
3. **Rien ne se perd.** Écritures atomiques, sauvegarde avant écriture, suppression = `.corbeille/`.
4. **Tout en français.** Fichiers, champs, services, messages, docs, commits.
5. **Erreurs parlantes.** Cause et remède, élément par élément.

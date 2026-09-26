# Le protocole agentforge

**Objet.** Relier des briques locales — applications déterministes et agents — par un protocole
unique, pour qu'un utilisateur, développeur ou non, les assemble en pipelines qui font ce qu'il
veut. Local d'abord : zéro cloud, zéro compte, zéro clé obligatoire.

## 1. Les briques

| Type | Ce que c'est | LLM |
|---|---|---|
| **application** | Fait une chose précise, de façon déterministe, avec entrée et sortie typées. | jamais |
| **agent** | Collecte, calcule, rédige un rapport ou une sortie typée. Peut porter un modèle **local** (CPU ou GPU), **optionnel** et **ancré** : il trie ou rédige à partir de faits fournis, il n'invente aucun chiffre. | optionnel |
| **pipeline** | Une suite d'étapes qui branchent des services entre eux. Vérifiée avant d'être exécutée. | — |

Treize briques sur quinze en production ne font tourner aucun modèle : l'agent sans LLM est **le cas
courant**, pas l'exception.

## 2. Le fil : MCP sur stdio

Chaque brique est un **serveur MCP** (JSON-RPC 2.0, un message par ligne, sur stdin/stdout). Chaque
service est un outil MCP avec `inputSchema` et `outputSchema`. Ce choix donne gratuitement
l'interopérabilité (Claude Desktop, Cursor, Ollama…) et n'impose aucune bibliothèque : le cœur
`forge/mcp/` tient sur la bibliothèque standard.

Deux façons d'être une brique :

- **`mcp-stdio`** — la brique parle MCP elle-même. En Python : `forge.mcp.serveur.ServeurMCP`.
  Exemple : `briques/rapport-marche`.
- **`services-json`** — brique existante au protocole « `python -m agent.<service>` → un JSON sur
  stdout, code 0 succès / 1 échec / 2 partiel ». L'**adaptateur générique** (`forge/mcp/adaptateur.py`)
  la lit et l'expose en MCP sans la modifier. Exemple : `briques/prix-agent`. C'est la voie de
  migration : tout continue de tourner, les briques migrent une à une.

Le runtime **valide chaque réponse** contre son contrat de sortie et transforme toute déviation en
erreur claire (chemin, attendu, reçu).

## 3. Le manifeste `forge.json`

```json
{
  "forge": 1,
  "type": "application",
  "nom": "prix-agent",
  "version": "0.1.0",
  "description": "Prix comptant USD et bougies OHLCV",
  "icone": "📈",
  "interpreteur": "C:\\Users\\moi\\miniconda3\\envs\\prix\\python.exe",
  "transport": { "type": "services-json", "fichier": "services.json", "passage_entree": "arguments" },
  "services": [
    {
      "nom": "historique",
      "description": "Bougies d'un actif",
      "entree": { "type": "object", "required": ["actif", "intervalle", "limite"], "properties": { … } },
      "sortie": "forge://serie-temporelle/ohlcv@1",
      "exemple": { "actif": "BTC", "intervalle": "1d", "limite": 90 }
    }
  ],
  "planification": { "mode": "quotidien", "heure": "08:15", "rattrapage": true },
  "page": "page/index.html",
  "rapports": "rapports"
}
```

| Champ | Rôle |
|---|---|
| `interpreteur` | `python` = celui de forge ; sinon **chemin complet** (conda hors PATH sous Windows). Le runtime ne modifie jamais les dépendances d'un environnement existant. |
| `transport` | `mcp-stdio` + `commande` (arguments passés à l'interpréteur), ou `services-json` + `fichier` + `passage_entree` (`arguments` par défaut, repli automatique sur `fichier` au-delà de 30 000 caractères — limite Windows ; `stdin` possible). |
| `services[]` | `entree` / `sortie` : une référence `forge://…` ou un JSON Schema en ligne. `exemple` alimente l'assembleur et l'exécution à la demande. `duree_longue: true` signale une tâche longue (voir §6). |
| `planification` | Contrat `forge://planification/planification@1`. Absent = à la demande. Pour une brique : `service` et `entree` précisent quoi lancer. |
| `page` | Page interactive servie par le hub (forme B). |
| `rapports` | Dossier des rapports (forme A), `rapports` par défaut. |
| `modele` | Agent avec LLM local : `{ "backend": "ollama", "nom": "qwen2.5:7b", "gpu": true, "cpu_ok": true }`. |

Les **clés d'API optionnelles** vivent dans `.env.local` à côté du manifeste, jamais dedans. Les
messages, noms de champs et de fichiers sont en français.

## 4. Les contrats de données

Le secret de la réutilisabilité : toute brique de prix produit le **même** OHLCV, donc n'importe quel
agent d'analyse consomme n'importe quelle source. Un contrat est un JSON Schema dans
`contrats/<domaine>/<nom>.v<version>.json`, référencé par `forge://<domaine>/<nom>@<version>`.

| Contrat | Contenu |
|---|---|
| `forge://serie-temporelle/ohlcv@1` | `actif`, `devise`, `intervalle`, `source`, `bougies[]` = `[horodatage_ms, ouverture, plus_haut, plus_bas, cloture, volume]` — compatible avec `prix-agent.historique`. |
| `forge://marche/prix-comptant@1` | `devise`, `horodatage_ms`, `prix{actif → {valeur, source}}`. |
| `forge://document/rapport@1` | `titre`, `date`, `markdown`, `html`, `images[]`, `resume`, `alertes[{niveau, message}]`. |
| `forge://tache/progres@1` | État d'une tâche longue sur le disque (§6). |
| `forge://planification/planification@1` | `mode`, `heure`, `heures`, `rattrapage`, `service`, `entree`. |

Tout contrat porte un champ `manques[]` : **une réponse partielle est rendue avec ses manques
listés, jamais rejetée, jamais tronquée en silence.**

Une version de contrat ne change jamais de sens : on ajoute des champs optionnels dans la même
version, on crée `@2` pour tout changement incompatible.

## 5. Les pipelines

```json
{
  "forge": 1, "type": "pipeline", "nom": "rapport-btc-quotidien", "version": "0.1.0",
  "planification": { "mode": "quotidien", "heure": "08:15" },
  "etapes": [
    { "id": "prix",    "brique": "prix-agent",     "service": "historique",
      "entree": { "actif": "BTC", "intervalle": "1d", "limite": 90 } },
    { "id": "rapport", "brique": "rapport-marche", "service": "generer",
      "entree": { "donnees": "$prix", "titre": "Bitcoin — 90 jours" } }
  ]
}
```

- `"$prix"` = le résultat entier de l'étape `prix` ; `"$prix.bougies"` = un champ.
- **Avant** toute exécution, `forge valider` contrôle : briques et services existants, littéraux
  conformes au schéma d'entrée, chaque branchement `$…` reliant une sortie à une entrée compatible.
  Une erreur liste tout, rien n'est lancé.
- Les étapes s'exécutent en séquence ; une étape partielle n'arrête pas le pipeline, ses manques
  remontent dans le résultat final.
- Un utilisateur non développeur construit le même JSON depuis l'**assembleur** du hub : il choisit
  une brique, un service, et branche chaque entrée sur une étape précédente compatible — le hub ne
  propose que les sources dont le contrat correspond.

## 6. Les tâches longues

Une brique qui travaille plusieurs minutes (synthèse vocale d'un livre, OCR de 300 pages,
téléchargement vidéo) suit ce contrat, `forge://tache/progres@1` :

- **l'état vit sur le disque, jamais dans l'onglet** : `<tache>.progres.json` (réécrit ≤ 1 fois/s :
  `pourcentage`, `etape`, `eta_s`), `<tache>.erreur.txt` en cas d'échec, résultat final
  `<tache>.<type>.json` ;
- **annulation coopérative** : la page pose un fichier drapeau `<tache>.annuler`, la tâche le
  consulte à chaque bloc et s'arrête proprement ; jamais de kill ;
- un rafraîchissement de page ne perd rien : la page interroge le disque.

Les plafonds se dimensionnent pour le cas réel (un livre de 300 pages) ; toute coupe est annoncée.

## 7. Les erreurs

Chaque échec dit **la cause et le remède**, élément par élément :

```
prix-agent.prix a échoué (code 1)
  - XYZ — binance : paire XYZUSDT absente ; coingecko : actif inconnu — ajoutez-le dans .env.local : PRIX_AGENT_COINGECKO=XYZ:identifiant-coingecko
Remède : lisez les détails ; reproduisez avec : python -m agent.prix --actifs ["XYZ"]
```

En Python : `forge.utils.erreurs.ErreurForge(cause, remede, details)`. Sur le fil MCP : résultat
`isError` avec `structuredContent.erreur = {cause, remede, details}`.

## 8. Rien ne se perd

- écriture **atomique** : fichier temporaire caché, puis `os.replace` (`forge.utils.fichiers.ecrire_atomique`) ;
- **sauvegarde** avant toute écriture sur des données existantes (`.sauvegardes/`) ;
- suppression = déplacement vers `.corbeille/`, jamais d'effacement ;
- **preuve après coup** : `empreintes()` avant, `verifier_inchanges()` après — ce qui ne devait pas
  changer n'a pas changé.

## 9. Le hub

`forge hub` sert http://localhost:8700 :

- une **tuile** par brique et par pipeline : icône, état de la dernière exécution, planification,
  dernier rapport, page interactive, ⋯ (planifier, ouvrir dans l'éditeur, supprimer vers la corbeille) ;
- **planification** : quotidien HH:MM / toutes les N heures / jamais ; exécutions séquentielles,
  rattrapage au démarrage, verrou anti-doublon ; le choix de l'utilisateur prime sur le manifeste et
  vit dans `~/.agentforge/planifications.json` ;
- **rapports** (forme A) et **pages** (forme B) servis depuis le dossier de chaque brique ;
- **vues composées** : plusieurs briques sur une page, chacune dans son cadre ;
- **assembleur** : construire, vérifier, enregistrer et lancer un pipeline sans code.

L'état des exécutions vit dans `~/.agentforge/executions/`. Le hub tourne sur la bibliothèque standard.

## 10. Installer et publier une brique

```
forge installer https://github.com/alice/meteo-agent
forge lister
forge appeler meteo-agent previsions --entree '{"ville": "Nantes"}'
forge supprimer meteo-agent          # → ~/.agentforge/.corbeille/
```

Une brique publiable = un dépôt GitHub avec `forge.json` à la racine. Les briques sont cherchées
dans `FORGE_BRIQUES` (dossiers séparés par `;` sous Windows), `~/.agentforge/briques/`, puis
`briques/` du dépôt agentforge.

## 11. Contraintes Windows prises en charge par le runtime

- **SSL** : magasin de certificats corrompu (`ASN1: NOT_ENOUGH_DATA`) → repli sur `certifi`
  (`forge.utils.reseau`), jamais de désactivation de la vérification ;
- **ligne de commande ≤ 32 000 caractères** : l'adaptateur passe par un fichier au-delà de 30 000 ;
  sur le fil MCP la charge voyage dans le flux, sans limite ;
- **pas de Docker, pas de `uv` imposé** : `interpreteur` pointe vers l'environnement conda existant ;
- **NumPy 1.26** et consorts : le runtime n'installe ni ne met à jour rien dans l'environnement
  d'une brique ;
- écritures atomiques et fichiers longs créés cachés puis renommés.

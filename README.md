# Watcher immobilier Lyon 6e

Watcher local, autonome et déterministe pour surveiller les résultats de location de Leboncoin, SeLoger et Seventee. Il extrait des données compactes, compare chaque site à son état SQLite, ouvre les pages de détail seulement pour les annonces concernées, puis écrit une unique réponse JSON exploitable par un autre programme.

Le périmètre est strictement :

```text
SCAN → NORMALIZE → COMPARE → OPTIONAL DETAIL FETCH → CLASSIFY → PERSIST → OUTPUT JSON
```

Le projet n'utilise ni LLM, ni API d'IA, ni agent, ni service externe de notification. Il ne contacte jamais un annonceur et ne remplit aucun formulaire. La V1 n'inclut pas de planificateur : elle s'exécute à la demande ou depuis un ordonnanceur placé autour du programme.

## Prérequis

- Python 3.11 ou plus récent ;
- un accès réseau pour les exécutions réelles ;
- Chromium fourni par Playwright en mode `launch`, ou un navigateur Chromium déjà démarré avec CDP en mode `cdp`.

SQLite est inclus dans Python. Les dépendances applicatives sont limitées à Playwright et, sous Windows, `tzdata`; pytest appartient à l'extra `test`.

## Installation

Depuis la racine du projet, sous Windows PowerShell :

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .[test]
python -m playwright install chromium
Copy-Item config.example.json config.json
```

Sous Linux ou macOS :

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e '.[test]'
python -m playwright install chromium
cp config.example.json config.json
```

La forme canonique d'installation du projet et des tests est `pip install -e .[test]`. Les quotes de l'exemple Unix empêchent seulement le shell d'interpréter les crochets. L'installation expose aussi la commande `lyon6-watcher`, mais les exemples ci-dessous utilisent systématiquement `python -m watcher` afin de rester indépendants du `PATH`.

## Configuration

Toutes les options sont centralisées dans `config.json`. Les chemins relatifs sont résolus par rapport au répertoire contenant ce fichier, pas nécessairement par rapport au répertoire courant.

Le fichier `config.example.json` contient les trois URL de recherche complètes. Elles sont chargées telles quelles : le programme ne les reconstruit pas. Les groupes principaux sont :

| Clé | Rôle |
| --- | --- |
| `timezone` | Fuseau des identifiants et horodatages de run, par défaut `Europe/Paris`. |
| `database_path`, `log_path`, `lock_path` | Emplacements de l'état SQLite, du journal et du verrou global. |
| `debug_directory`, `debug_artifacts_on_error` | Répertoire et activation des captures de diagnostic en cas d'erreur de parsing. |
| `browser` | Mode, URL CDP, affichage, délais et blocage optionnel des ressources lourdes. |
| `criteria` | Codes postaux, bornes de prix et de surface, inclusives. |
| `diff` | Seuil d'absence et garde contre une chute suspecte du nombre de résultats. |
| `scan` | Première page, nombre maximal de scrolls et unique retry autorisé. |
| `sites.<nom>` | Activation et URL constante de chaque site. |

Les critères livrés sont `69006`, 550–800 € et 30–60 m². Une valeur absente reste `null` ; elle n'est jamais devinée.

### Mode `launch`

```json
{
  "browser": {
    "mode": "launch",
    "headless": false,
    "navigation_timeout_ms": 15000,
    "selector_timeout_ms": 10000,
    "block_heavy_resources": true
  }
}
```

Playwright démarre et arrête son propre Chromium. `headless: false` permet de voir le navigateur. Le blocage optionnel concerne les images, polices et médias ; les scripts nécessaires au rendu ne sont pas bloqués.

### Mode `cdp`

```json
{
  "browser": {
    "mode": "cdp",
    "cdp_url": "http://127.0.0.1:9224"
  }
}
```

Le navigateur compatible Chromium doit déjà exposer son endpoint CDP à cette adresse. Le watcher emprunte son premier contexte, crée ses propres pages puis ne ferme que celles-ci ; il ne ferme ni les pages préexistantes ni le navigateur externe. Aucune dépendance à un logiciel d'orchestration particulier n'est introduite.

## Commandes

Par défaut, chaque commande lit `config.json`. Un autre fichier se passe avec `--config`, par exemple `python -m watcher status --config conf/dev.json`.

```bash
# Scanner tous les sites activés et persister le résultat
python -m watcher run

# Scanner un seul site
python -m watcher run --site leboncoin

# Effectuer navigation, extraction et diff sans aucune écriture SQLite
python -m watcher run --dry-run

# Afficher l'état courant et le dernier run, sans navigation réseau
python -m watcher status

# Afficher les 20 événements les plus récents, sans navigation réseau
python -m watcher history --limit 20

# Inspecter une page de résultats sans modifier l'état
python -m watcher diagnose --site seloger
```

`run` accepte aussi `--verbose`. `--site` accepte `leboncoin`, `seloger` ou `seventee`. `history --limit` exige un entier strictement positif. Le diagnostic reste compact : URL chargée, titre, nombre de liens candidats, identifiants valides, annonces, doublons, candidats rejetés et challenge éventuel ; il n'affiche pas le DOM complet.

En `--dry-run`, une base existante est ouverte en lecture seule. Si elle n'existe pas, le schéma est créé uniquement en mémoire : le fichier SQLite n'est pas créé et l'état n'est jamais modifié. Le journal opérationnel reste actif et les artifacts de debug peuvent toujours être écrits en cas d'erreur.

`status` renvoie `{"status":"OK","sites":{...},"last_run":...}` et `history` renvoie `{"status":"OK","limit":20,"events":[...]}`. `diagnose` renvoie les compteurs annoncés ci-dessus, plus `challenge` et `challenge_reason`.

Les payloads métier, y compris `PARTIAL_FAILURE`, `ERROR`, `CHALLENGE` en diagnostic et `ALREADY_RUNNING`, quittent avec le code `0` afin que leur JSON soit traité normalement. Une erreur de configuration ou d'usage renvoie le code `2`; une erreur d'entrée/sortie ou une erreur fatale de la CLI renvoie `1`. Leur forme stable est :

```json
{"error":{"code":"CONFIG_ERROR|USAGE_ERROR|IO_ERROR|FATAL_ERROR","message":"..."},"status":"ERROR"}
```

`python -m watcher --help` et l'aide des sous-commandes restent une aide texte standard, pas un payload JSON.

## Baseline et comparaison

La baseline est indépendante pour chaque site. Le premier scan fiable d'un site enregistre les annonces visibles avec le statut de site `BASELINE_CREATED`, sans produire d'événement `NEW` et sans ouvrir de page de détail. Si deux sites réussissent et que le troisième échoue, les deux premières baselines sont conservées ; le troisième site créera la sienne lors de son premier scan fiable ultérieur.

Chaque run normal suit deux passes :

1. toutes les pages de recherche sélectionnées sont chargées et extraites sous forme compacte ; les identités sont dédupliquées et comparées à SQLite ;
2. après la fin de tous les scans de recherche, seules les annonces préliminairement `NEW`, `UPDATED` ou `BECAME_ELIGIBLE` sont candidates à une page de détail.

Une annonce connue dont les données matérielles sont inchangées n'est donc jamais ouverte : un run `NO_CHANGE` effectue zéro navigation de détail. Si un détail échoue, le résumé déjà observé est conservé, l'erreur est signalée et `complete` vaut `false`.

Les champs matériels sont le prix, la surface, le nombre de pièces, la localisation, le code postal et l'URL canonique. Leur empreinte est déterministe ; le titre n'en fait pas partie afin d'éviter les faux positifs purement cosmétiques. Les changements retournent des valeurs `before` et `after`.

L'admissibilité est calculée sans heuristique :

- `ELIGIBLE` : code postal, prix et surface sont tous connus et dans les bornes ;
- `REJECTED` : au moins une valeur connue est hors critères ;
- `NEEDS_DETAIL` : aucune valeur connue ne rejette l'annonce, mais au moins une valeur indispensable manque.

Une transition de `REJECTED` à `ELIGIBLE` produit `BECAME_ELIGIBLE`. Ce cas, notamment un prix 850 € → 790 €, est couvert par les tests offline ; il ne peut pas être provoqué naturellement avec l'URL live fournie, dont le filtre fixe `priceMax=800`.

Une absence n'est jamais assimilée à une suppression. `missing_count` augmente uniquement après un scan fiable du site et `MISSING_FROM_SEARCH` est émis lorsque le seuil configuré est atteint, deux scans consécutifs par défaut. Une erreur, un challenge ou un résultat jugé suspect ne modifie pas ces compteurs. Un résultat vide sans marqueur d'état vide explicitement validé est suspect ; une chute sous le ratio de sécurité l'est aussi lorsque le volume précédent atteint le minimum configuré.

## Protocole JSON

`stdout` contient uniquement une ligne JSON compacte finale. Les journaux humains vont sur `stderr` et dans `data/watcher.log`. Il est donc sûr de rediriger le résultat :

```bash
python -m watcher run > result.json
```

Clés principales d'un run :

- `status`, `run_id`, `dry_run` et `complete` ;
- `sites`, avec un statut, un nombre et éventuellement une raison et des diagnostics par site ;
- `new`, `updated`, `became_eligible`, `missing_from_search` ;
- `actionable_count`, calculé à partir des événements dont `actionable` vaut `true`.

Est actionable : `NEW` avec une admissibilité `ELIGIBLE` ou `NEEDS_DETAIL`, ainsi que `BECAME_ELIGIBLE`. `UPDATED`, `MISSING_FROM_SEARCH` et `NEW + REJECTED` ne le sont pas. Un consommateur peut donc s'arrêter immédiatement lorsque `actionable_count == 0`.

### Statuts globaux

| Statut | Signification |
| --- | --- |
| `BASELINE_CREATED` | Au moins une baseline vient d'être créée et tous les sites sélectionnés sont fiables. |
| `NO_CHANGE` | Tous les sites sélectionnés sont fiables, aucune baseline nouvelle et aucun événement. |
| `CHANGES` | Au moins un événement fiable existe ; consulter aussi `complete`, qui peut être `false`. |
| `PARTIAL_FAILURE` | Au moins un site est fiable et au moins un autre traitement a échoué, sans événement. |
| `ERROR` | Aucun site sélectionné n'a produit de résultat fiable. |
| `ALREADY_RUNNING` | Le verrou est déjà détenu par une autre instance. |

### Statuts par site

| Statut | Signification |
| --- | --- |
| `OK` | Scan fiable comparé à une baseline existante. |
| `BASELINE_CREATED` | Premier scan fiable de ce site. |
| `CHALLENGE` | CAPTCHA, DataDome, refus d'accès ou challenge détecté ; aucun contournement n'est tenté. |
| `SUSPICIOUS_RESULT` | Résultat vide ou chute anormale ; état et compteurs d'absence inchangés. |
| `ERROR` | Navigation ou extraction impossible ; le site suivant est néanmoins traité. |

### Exemples compacts

Ces exemples montrent le protocole sur des données de test fictives, pas des observations live. Les durées et diagnostics par site sont omis uniquement pour garder les lignes lisibles.

Premier scan fiable :

```json
{"actionable_count":0,"became_eligible":[],"complete":true,"dry_run":false,"missing_from_search":[],"new":[],"run_id":"baseline-fixture","sites":{"seloger":{"count":1,"status":"BASELINE_CREATED"}},"status":"BASELINE_CREATED","updated":[]}
```

Scan identique :

```json
{"actionable_count":0,"became_eligible":[],"complete":true,"dry_run":false,"missing_from_search":[],"new":[],"run_id":"unchanged-fixture","sites":{"seloger":{"count":1,"status":"OK"}},"status":"NO_CHANGE","updated":[]}
```

Nouvelle annonce admissible :

```json
{"actionable_count":1,"became_eligible":[],"complete":true,"dry_run":false,"missing_from_search":[],"new":[{"actionable":true,"changes":{},"eligibility":"ELIGIBLE","id":"fixture-D","location":"Lyon 6e","postal_code":"69006","price_eur":750,"rooms":2,"site":"seloger","surface_m2":35.0,"title":"Appartement","type":"NEW","url":"https://example.test/seloger/fixture-D"}],"run_id":"changed-fixture","sites":{"seloger":{"count":2,"detail_attempted":1,"detail_succeeded":1,"status":"OK"}},"status":"CHANGES","updated":[]}
```

## SQLite

L'état persistant est `data/state.db` par défaut. La base est initialisée automatiquement, utilise le mode WAL sur disque et toutes les écritures d'un run fiable sont transactionnelles.

Le schéma effectif est :

```sql
CREATE TABLE listings (
    site TEXT NOT NULL,
    identity_key TEXT NOT NULL,
    listing_id TEXT,
    canonical_url TEXT,
    title TEXT,
    price_eur INTEGER,
    surface_m2 REAL,
    rooms INTEGER,
    location TEXT,
    postal_code TEXT,
    eligibility TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    last_changed TEXT,
    seen_count INTEGER NOT NULL DEFAULT 1,
    missing_count INTEGER NOT NULL DEFAULT 0,
    fingerprint TEXT,
    PRIMARY KEY (site, identity_key),
    CHECK (listing_id IS NOT NULL OR canonical_url IS NOT NULL),
    CHECK (seen_count >= 1),
    CHECK (missing_count >= 0)
);

CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT,
    leboncoin_count INTEGER,
    seloger_count INTEGER,
    seventee_count INTEGER,
    new_count INTEGER DEFAULT 0,
    updated_count INTEGER DEFAULT 0,
    duration_ms INTEGER
);

CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    site TEXT NOT NULL,
    listing_id TEXT,
    event_type TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE site_state (
    site TEXT PRIMARY KEY,
    baseline_created_at TEXT NOT NULL,
    last_successful_scan TEXT NOT NULL,
    last_result_count INTEGER NOT NULL,
    CHECK (last_result_count >= 0)
);

CREATE INDEX idx_events_created_at
    ON events(created_at DESC, id DESC);

CREATE INDEX idx_listings_site_missing
    ON listings(site, missing_count);
```

`identity_key` est la clé interne non nullable : `id:<listing_id>` est prioritaire, avec repli sur `url:<canonical_url>`. `listing_id` est volontairement nullable car un site peut exposer une URL canonique stable sans identifiant source extractible ; le watcher conserve alors l'annonce sans fabriquer d'ID. Symétriquement, `canonical_url` peut manquer lorsque l'identifiant source suffit, mais la contrainte exige toujours au moins l'un des deux. `events.listing_id` reste nullable pour la même raison. Si un ID source est observé plus tard pour une URL déjà connue, l'identité est mise à niveau sans faux événement `NEW`.

`site_state` porte la baseline et le dernier volume fiable de chaque site. `runs` résume les exécutions et leur durée. `events` conserve le type et les instantanés avant/après consultables avec `history`.

## Journaux, debug et verrou

- `data/watcher.log` reçoit les journaux humains ; `--verbose` augmente leur niveau de détail. Les mêmes messages opérationnels peuvent apparaître sur `stderr`, jamais dans le JSON de `stdout`.
- Lorsque `debug_artifacts_on_error` vaut `true`, une erreur de parser, de détail ou un résultat vide suspect peut créer `debug/YYYYMMDD_HHMMSS_<site>.png` et `.html`. Ces fichiers restent locaux, peuvent contenir la page reçue et ne doivent pas être publiés sans vérification. Aucun HTML complet n'est journalisé en fonctionnement normal.
- `data/watcher.lock` est un verrou système inter-processus, compatible Windows et Unix. Sa présence sur disque ne signifie pas à elle seule qu'une instance tourne : c'est la détention du verrou qui compte. Une seconde instance renvoie `ALREADY_RUNNING` proprement au lieu de modifier la base.

Le watcher ne journalise volontairement ni cookies, ni tokens, ni identifiants secrets. `config.json` ne doit pas servir à stocker des secrets inutiles.

## Tests

La suite par défaut est offline : elle utilise les fixtures HTML locales et des navigateurs simulés, sans dépendre des sites réels.

```bash
pytest
```

Elle couvre notamment normalisation, déduplication, admissibilité, empreintes, baseline par site, nouveau/updated/became-eligible, absence différée, résultat suspect, transactions, verrou, abstraction navigateur, deux passes et échec d'un site.

Les contrôles réseau sont isolés dans `tests/live/`, marqués `live` et ignorés tant que la variable n'est pas définie :

```bash
RUN_LIVE_TESTS=1 pytest tests/live
```

Sous PowerShell :

```powershell
$env:RUN_LIVE_TESTS = '1'
pytest tests/live
```

Un test live accepte soit un parsing non vide, soit un challenge explicitement détecté. Il ne garantit donc pas que le site sera accessible à chaque exécution.

## État de validation des parsers au 20 août 2026

Ce bilan décrit seulement les vérifications effectivement réalisées à cette date :

- **Leboncoin** : l'accès live a rencontré DataDome ; aucun DOM d'annonce n'a donc été validé. Le seul motif de résultat conservé est le pattern du cahier des charges `a[href*="/ad/locations/"]`, avec ID extrait de l'URL réelle. Le parser Leboncoin reste à valider sur un DOM live accessible.
- **SeLoger** : les sélecteurs `data-testid` de la page de résultats ont été validés. L'accès est toutefois instable et peut aboutir à un challenge. Les fallbacks génériques de page détail (`h1`, métadonnées et attributs Schema.org ciblés) n'ont pas été validés live.
- **Seventee** : quatre cartes rendues par JavaScript ont été observées via le conteneur `#offers`, ce qui valide le sélecteur de résultats. L'attente d'hydratation et l'extraction générique d'une page détail ont également été vérifiées sur une annonce le 20 août 2026 ; cela ne valide pas tous les champs ni les futurs layouts.

Ces observations ne constituent pas une promesse sur le contenu futur des sites. Les tests offline figent les comportements connus ; les tests live servent à signaler une évolution du DOM ou une protection d'accès.

La V1 ne consulte que la première page de résultats. `max_pages_per_site` est borné à `1` dans la configuration fournie, mais aucune navigation de pagination supplémentaire n'est activée. Le navigateur fournit un mécanisme de lazy-scroll borné par `max_lazy_scrolls` (trois au maximum dans l'exemple) ; aucun adaptateur ne l'active tant qu'une nécessité live n'a pas été établie.

## Sécurité et limites assumées

Le programme détecte les marqueurs de CAPTCHA, DataDome, refus d'accès et challenge, marque le site `CHALLENGE`, puis continue. Il n'implémente aucun contournement de CAPTCHA ou de 2FA, aucun spoofing d'empreinte, mode stealth, proxy tournant ou autre mécanisme destiné à contourner les protections d'un site.

Il n'envoie aucun message, e-mail ou SMS, ne clique jamais sur « contacter » et n'agit pas sur les annonceurs. Il n'intègre aucune IA et ne contient ni prompt, ni appel LLM, ni raisonnement en langage naturel. Sa responsabilité s'arrête à détecter, extraire, comparer, classifier, persister et retourner le JSON.

## Arborescence

Les répertoires `data/`, `debug/`, les caches Python et les métadonnées `*.egg-info` sont générés à l'exécution ou à l'installation.

```text
.
├── pyproject.toml
├── requirements.txt
├── config.example.json
├── README.md
├── watcher/
│   ├── __init__.py
│   ├── __main__.py
│   ├── cli.py
│   ├── browser.py
│   ├── config.py
│   ├── database.py
│   ├── diff.py
│   ├── eligibility.py
│   ├── locks.py
│   ├── models.py
│   ├── output.py
│   ├── runner.py
│   └── sites/
│       ├── __init__.py
│       ├── base.py
│       ├── leboncoin.py
│       ├── seloger.py
│       └── seventee.py
├── tests/
│   ├── fixtures/
│   │   ├── leboncoin_results.html
│   │   ├── seloger_results.html
│   │   └── seventee_results.html
│   ├── live/
│   │   └── test_live_sites.py
│   └── test_*.py
├── data/                       # créé à l'exécution
└── debug/                      # créé seulement si nécessaire
```

## Intégration

Le watcher n'a pas à connaître son consommateur. Une intégration minimale peut se limiter à :

```python
import json
import subprocess

result = subprocess.run(
    ["python", "-m", "watcher", "run"],
    capture_output=True,
    text=True,
    check=False,
)
data = json.loads(result.stdout)

if data.get("actionable_count", 0) > 0:
    process_changes(data)
```

Avant toute action aval, le consommateur devrait également examiner `complete` et les statuts par site : `CHANGES` peut coexister avec l'échec indépendant d'un autre site.

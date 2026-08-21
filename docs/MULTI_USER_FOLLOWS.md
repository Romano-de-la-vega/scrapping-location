# Suivis immobiliers multi-utilisateurs

Le watcher peut gérer plusieurs suivis indépendants. Chaque suivi possède :

- un nom et un identifiant ;
- un ou plusieurs liens de recherche ;
- ses propres critères ;
- son propre état SQLite et sa propre baseline par lien ;
- sa propre fréquence Hermes ;
- son propre destinataire Telegram ;
- deux booléens de comportement :
  - `notify_every_run` : envoyer ou non une notification à chaque exécution ;
  - `run_llm_on_new` : autoriser ou non le LLM lorsqu'une nouvelle annonce admissible apparaît.

## Créer et installer un suivi

Depuis le dossier du projet et avec le venv activé :

```powershell
property-follow add paul `
  --name "Paul - Lyon" `
  --telegram-chat 123456789 `
  --schedule "every 5m" `
  --url "https://www.leboncoin.fr/recherche?..." `
  --url "https://www.seloger.com/classified-search?..." `
  --postal-code 69003 `
  --price-min 600 `
  --price-max 900 `
  --surface-min 25 `
  --surface-max 55 `
  --notify-every-run true `
  --run-llm-on-new false `
  --install
```

`--url` est répétable, y compris plusieurs fois pour le même site.

Le site est détecté automatiquement à partir du domaine. Les adaptateurs actuellement supportés sont : Leboncoin, SeLoger et Seventee.

Chaque URL est stockée sous `data/follows/<id>/sources/<source>/` avec son propre `state.db`, son log et son lock.

## Les deux booléens

### `notify_every_run`

- `false` : pas de message lorsque le passage ne trouve rien de nouveau ;
- `true` : une notification Telegram d'audit est produite à chaque passage.

### `run_llm_on_new`

- `false` : aucun LLM n'est utilisé ; les nouvelles annonces sont envoyées directement par le script déterministe ;
- `true` : Hermes utilise un script gate. Sans nouveauté il renvoie `wakeAgent:false`; avec une nouveauté il renvoie `wakeAgent:true` et transmet uniquement les nouvelles annonces au LLM.

Les quatre combinaisons sont donc :

| notify_every_run | run_llm_on_new | Comportement |
|---|---|---|
| false | false | Silencieux sans nouveauté ; notification déterministe sur nouveauté ; zéro LLM |
| true | false | Notification à chaque passage ; zéro LLM |
| false | true | Silencieux sans nouveauté ; LLM uniquement sur nouveauté |
| true | true | Audit à chaque passage + LLM uniquement sur nouveauté |

Lorsque les deux options sont `true`, l'audit est un second cron Hermes `--no-agent` qui lit le résultat du dernier passage. Il n'effectue aucun nouveau scan et n'appelle aucun modèle.

## Commandes utiles

Lister les suivis :

```powershell
property-follow list
```

Tester un suivi immédiatement :

```powershell
property-follow run paul
```

Tester uniquement le gate LLM :

```powershell
property-follow gate paul
```

Lire la notification d'audit du dernier passage :

```powershell
property-follow audit paul
```

Installer un suivi déjà créé dans Hermes :

```powershell
property-follow install paul
```

Retirer ses cron Hermes :

```powershell
property-follow uninstall paul
```

Supprimer ensuite le suivi et son état local :

```powershell
property-follow delete paul
```

## Ajouter une nouvelle plateforme immobilière

Le gestionnaire multi-utilisateurs ne remplace pas les adaptateurs de scraping. Pour supporter un nouveau domaine comme Bien'ici, il faut d'abord ajouter un adaptateur dans `watcher/sites/`, puis enregistrer son domaine dans `SITE_DOMAINS` de `watcher/follows.py`. Une fois cet adaptateur disponible, tous les suivis peuvent utiliser des URLs de cette plateforme sans nouvelle logique Hermes.

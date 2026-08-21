# Suivis immobiliers multi-utilisateurs

Le watcher peut gérer plusieurs suivis indépendants. Chaque suivi possède :

- un nom et un identifiant ;
- un ou plusieurs liens de recherche ;
- ses propres critères ;
- son propre état SQLite et sa propre baseline par lien ;
- sa propre fréquence Hermes ;
- son propre destinataire Telegram ;
- un mode `notify_only` sans LLM.

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
  --install
```

`--url` est répétable, y compris plusieurs fois pour le même site.

Le site est détecté automatiquement à partir du domaine. Les adaptateurs actuellement supportés sont :

- Leboncoin ;
- SeLoger ;
- Seventee.

Chaque URL est stockée sous `data/follows/<id>/sources/<source>/` avec son propre `state.db`, son log et son lock.

## Pourquoi chaque lien est isolé

Deux suivis peuvent viser la même ville ou la même annonce sans se masquer mutuellement. La baseline et la déduplication sont propres à chaque source de chaque suivi.

## Livraison Telegram et coût LLM

L'installation crée un cron Hermes `--no-agent`. Le script exécute le watcher déterministe et n'émet une sortie que lorsqu'une annonce `NEW` ou `BECAME_ELIGIBLE` est actionable.

- aucune nouveauté : stdout vide, aucune notification ;
- nouveauté admissible : message Telegram avec site, titre, prix, surface, localisation et URL ;
- aucun appel LLM pour ces suivis secondaires.

Le suivi principal Lyon6 peut continuer à utiliser son workflow agent séparé pour les candidatures automatiques.

## Commandes utiles

Lister les suivis :

```powershell
property-follow list
```

Tester un suivi immédiatement :

```powershell
property-follow run paul
```

Installer un suivi déjà créé dans Hermes :

```powershell
property-follow install paul
```

Retirer uniquement son cron Hermes :

```powershell
property-follow uninstall paul
```

Supprimer ensuite le suivi et son état local :

```powershell
property-follow delete paul
```

## Ajouter une nouvelle plateforme immobilière

Le gestionnaire multi-utilisateurs ne remplace pas les adaptateurs de scraping. Pour supporter un nouveau domaine comme Bien'ici, il faut d'abord ajouter un adaptateur dans `watcher/sites/`, puis enregistrer son domaine dans `SITE_DOMAINS` de `watcher/follows.py`. Une fois cet adaptateur disponible, tous les suivis peuvent utiliser des URLs de cette plateforme sans nouvelle logique Hermes.

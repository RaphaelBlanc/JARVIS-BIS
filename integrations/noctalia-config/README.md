# Correctif `noctalia-config` — accès distant à l’assistant

Ce dossier est destiné au mainteneur du dépôt
[`abcdef123456-wq/noctalia-config`](https://github.com/abcdef123456-wq/noctalia-config).

Il contient les modifications à apporter au client Noctalia pour que
l’assistant personnel fonctionne aussi depuis un autre réseau que celui du
serveur (Wi‑Fi différent, 4G, Internet…).

## Pourquoi ?

Le client `nc-assistant` lit une URL dans `~/.config/nc/assistant.conf`.
Aujourd’hui elle ressemble souvent à :

```text
http://192.168.1.28:8000
```

C’est une adresse privée : elle n’est joignable que sur le réseau local du
serveur. Depuis un autre réseau, elle échoue. Le correctif ne peut pas créer
le tunnel à distance, mais il :

- explique clairement pourquoi une URL privée ne marche pas ;
- ajoute un diagnostic `nc-assistant verifier` ;
- distingue les vraies causes d’échec (clé, TLS, DNS, timeout, runner) ;
- ajoute une entrée « Tester l’assistant distant » dans `nc-reglages`.

L’exposition du serveur (Cloudflare Tunnel, Tailscale ou Render) est traitée
dans le dépôt serveur : `assistant-perso/docs/ACCES_DISTANT.md`.

## Deux façons de procéder

### Option 1 — appliquer le patch (recommandé)

Le patch est basé sur `origin/main` au commit `0bfb7e3`
(« add remote assistant conversation inside dynamic island »).

```bash
cd noctalia-config
git checkout -b feat/assistant-verification-distante
git am /chemin/vers/0001-assistant-verification-distante.patch
python3 -B -m unittest tests.test_assistant -v
git push origin feat/assistant-verification-distante
```

En cas de conflit :

```bash
git am --3way /chemin/vers/0001-assistant-verification-distante.patch
```

### Option 2 — suivre les fiches

Les fiches détaillent les mêmes changements fichier par fichier :

| Fiche | Fichier concerné |
|---|---|
| [`01-nc-assistant.md`](01-nc-assistant.md) | `home/.local/bin/nc-assistant` |
| [`02-nc-reglages.md`](02-nc-reglages.md) | `home/.local/bin/nc-reglages` |
| [`03-documentation-tests.md`](03-documentation-tests.md) | docs, aide et tests |

Vue d’ensemble et critères d’acceptation :
[`00-FICHE-COLLEGUE.md`](00-FICHE-COLLEGUE.md).

## Contraintes

- Ne pas modifier `~/.config/nc/assistant.conf` (fichier personnel, `0600`).
- Ne jamais afficher ou journaliser la clé `SECRET_KEY`.
- Ne pas casser la commande existante `requete-ile` (ajoutée récemment dans
  `nc-assistant`).
- Le patch ne contient aucun secret.
- L’exposition du port `8080` (runner) est interdite : seul le gateway `8000`
  doit être accessible par le client.

## Validation

```bash
cd noctalia-config
python3 -m py_compile home/.local/bin/nc-assistant home/.local/bin/nc-reglages home/.local/bin/nc-aide
python3 -B -m unittest tests.test_assistant -v
```

`tests.test_assistant_page` peut échouer dans un environnement sans
Quickshell ; ce n’est pas lié à ce correctif.

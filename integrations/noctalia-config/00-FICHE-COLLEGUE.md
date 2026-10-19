# Fiche 00 — vue d’ensemble pour le mainteneur de `noctalia-config`

## Contexte

Le client `nc-assistant` parle au gateway de `assistant-perso` sur le port
`8000` :

- `POST /api/ask`
- `GET /api/status`
- `POST /api/briefing`

Il lit sa configuration dans `~/.config/nc/assistant.conf` (`0600`).

## Problème constaté

Quand l’URL configurée est privée, par exemple :

```text
http://192.168.1.28:8000
```

le client ne fonctionne que sur le réseau local du serveur. Depuis un autre
réseau, il affiche :

```text
Serveur inaccessible (réseau, adresse ou certificat).
```

Ce message ne permet pas de distinguer une URL LAN d’une mauvaise clé, d’un
certificat TLS invalide ou d’un tunnel arrêté.

## Objectif du correctif

1. Ajouter `nc-assistant verifier` (diagnostic réseau, clé, TLS, runner).
2. Avertir dans `configurer` quand l’URL est locale/privée.
3. Donner des messages d’erreur distincts.
4. Ajouter « Tester l’assistant distant » dans `nc-reglages`.
5. Documenter et tester.

## Ce que le mainteneur doit modifier

| Ordre | Fiche | Fichiers |
|---|---|---|
| 1 | [`01-nc-assistant.md`](01-nc-assistant.md) | `home/.local/bin/nc-assistant` |
| 2 | [`02-nc-reglages.md`](02-nc-reglages.md) | `home/.local/bin/nc-reglages` |
| 3 | [`03-documentation-tests.md`](03-documentation-tests.md) | `docs/assistant-distant.md`, `README.md`, `home/.local/bin/nc-aide`, `tests/test_assistant.py` |

Le patch `0001-assistant-verification-distante.patch` contient déjà
l’ensemble ; les fiches servent à comprendre ou à refaire les changements.

## Contraintes à respecter

- Base attendue : `origin/main` au commit `0bfb7e3`.
- Ne pas casser `nc-assistant requete-ile` (utilisée par l’île Quickshell).
- Ne jamais exposer la clé `SECRET_KEY` dans une sortie, un log ou un test.
- Ne pas modifier `~/.config/nc/assistant.conf` depuis le code.
- Le client ne doit joindre que le gateway (`8000`), jamais le runner
  (`8080`).
- Rester en bibliothèque standard Python (pas de dépendance externe).

## Critères d’acceptation

- [ ] `nc-assistant verifier` renvoie `0` quand le gateway et le runner
      répondent.
- [ ] Avec une URL `192.168.*`, un avertissement explique le réseau local.
- [ ] Avec une mauvaise clé, le message dit « clé refusée » (HTTP 401/403).
- [ ] Avec une URL incorrecte, le message dit « route introuvable » (404).
- [ ] Avec un certificat invalide, le message parle de TLS.
- [ ] `verifier --json` produit un JSON exploitable.
- [ ] `nc-reglages` → Agents IA propose « Configurer » et « Tester ».
- [ ] `nc-assistant requete-ile` continue de fonctionner.
- [ ] Aucun secret n’apparaît dans les sorties ou les fichiers versionnés.

## Commandes de validation

```bash
cd noctalia-config
python3 -m py_compile home/.local/bin/nc-assistant home/.local/bin/nc-reglages home/.local/bin/nc-aide
python3 -B -m unittest tests.test_assistant -v
```

Puis, si un serveur est disponible :

```bash
nc-assistant configurer
nc-assistant verifier
nc-assistant demander "Bonjour"
```

## Ce qui reste côté serveur (hors dépôt `noctalia-config`)

Le mainteneur du rice n’a pas à exposer le serveur. C’est fait dans
`assistant-perso` :

- `scripts/expose-gateway.sh` (Cloudflare Tunnel / Tailscale) ;
- `docs/ACCES_DISTANT.md` ;
- limite de débit `/api/*` ;
- `GATEWAY_PUBLIC_URL` dans `/api/status`.

L’utilisateur devra ensuite configurer le client avec l’URL HTTPS du tunnel :

```bash
nc-assistant configurer
# URL     : https://assistant.tondomaine.fr
# Clé     : SECRET_KEY du serveur
# USER_ID : son identifiant
nc-assistant verifier
```

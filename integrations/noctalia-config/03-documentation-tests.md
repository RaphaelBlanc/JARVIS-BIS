# Fiche 03 — documentation, aide et tests

## 1. `docs/assistant-distant.md`

Ajouter une section « Depuis un autre réseau (Wi‑Fi différent, Internet) »
après la section « Connexion ».

Contenu minimum :

- une URL `192.168.*`, `10.*`, `172.16-31.*`, `localhost` ou `*.local` ne
  fonctionne que sur le réseau local du serveur ;
- il faut exposer le gateway (`8000`) avec un tunnel côté serveur :
  Cloudflare Tunnel, Tailscale ou Render ;
- une fois l’URL HTTPS obtenue :

```bash
nc-assistant configurer
nc-assistant verifier
nc-assistant demander "Bonjour"
```

- `verifier` distingue : URL privée, 401/403, 404, TLS, DNS, connexion
  refusée, timeout, runner hors ligne ;
- aucune clé n’est affichée ;
- `nc-reglages` → Agents IA contient « Configurer » et « Tester ».

Ajouter dans le tableau d’utilisation :

```markdown
| `nc-assistant verifier` | Teste URL, clé, TLS et runner ; `--json` pour un script |
```

## 2. `README.md`

Dans la section qui présente l’assistant distant, ajouter un paragraphe :

- depuis un autre réseau, une URL `192.168.x.x` ou `*.local` ne suffit pas ;
- utiliser l’URL HTTPS d’un tunnel exposé côté serveur ;
- `nc-assistant verifier` ou `nc-reglages` → Agents IA → « Tester
  l’assistant distant » diagnostique réseau, clé, TLS et runner.

## 3. `home/.local/bin/nc-aide`

Ajouter la mention de `verifier` dans les deux entrées `nc-assistant`
(français et anglais) :

```text
… nc-assistant verifier teste la connexion (réseau, clé, TLS, runner) …
… nc-assistant verifier tests the connection (network, key, TLS, runner) …
```

## 4. `tests/test_assistant.py`

Ajouter les cas listés dans les fiches 01 et 02 :

### `nc-assistant`

- détection des URL privées / publiques ;
- `verifier` en ligne, URL privée, sans clé dans la sortie ;
- `verifier --json` (champs `online`, `private`, `url`) ;
- 401 → message « clé refusée », pas de corps d’erreur ;
- 404 → message contenant `404` ;
- runner hors ligne → code `1` ;
- `etat` en échec → `private: true` + `hint`.

### `nc-reglages`

- deux entrées « Configurer » puis « Tester » ;
- `verifier_assistant()` retourne le bon message et appelle
  `nc-assistant verifier` ;
- le test existant `test_settings_action_available_and_terminal_restored_after_cancellation`
  doit continuer à passer (`items()[0]` reste la configuration).

## 5. Commandes de validation

```bash
cd noctalia-config
python3 -m py_compile \
  home/.local/bin/nc-assistant \
  home/.local/bin/nc-reglages \
  home/.local/bin/nc-aide

python3 -B -m unittest tests.test_assistant -v
```

Puis, si possible, avec un serveur accessible :

```bash
nc-assistant verifier
nc-assistant demander "Bonjour"
```

Note : `tests.test_assistant_page` peut échouer dans un environnement sans
Quickshell (test QML). Ce n’est pas lié à ce correctif ; lancer au minimum
`tests.test_assistant`.

## 6. Critères de relecture

- [ ] Aucun secret dans les documents, tests ou exemples.
- [ ] Aucun fichier `assistant.conf` ajouté au dépôt.
- [ ] La commande `requete-ile` de `nc-assistant` reste intacte.
- [ ] Le patch s’applique sur `origin/main` (`0bfb7e3`).
- [ ] Les messages sont bilingues français/anglais via `T(...)`.
- [ ] Le ton et le style respectent le reste du dépôt.

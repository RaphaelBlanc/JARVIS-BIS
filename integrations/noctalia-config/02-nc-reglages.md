# Fiche 02 — `home/.local/bin/nc-reglages`

## But

Ajouter une action « Tester l’assistant distant » dans les réglages, à côté
de « Configurer l’assistant distant », sans afficher la clé.

## 1. Fonction `verifier_assistant()`

Après `configurer_assistant()`, ajouter :

```python
def verifier_assistant():
    """Teste la connexion distante sans afficher la clé ; retourne (code, message court)."""
    try:
        r = subprocess.run([sys.executable, os.path.join(BIN, "nc-assistant"), "verifier"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return 1, T("Test impossible.", "Test impossible.")
    lignes = (r.stdout if r.returncode == 0 else (r.stderr or r.stdout)).strip().splitlines()
    if r.returncode == 0:
        return 0, (lignes[0] if lignes else T("Connexion OK.", "Connection OK."))
    return r.returncode, (lignes[-1] if lignes else T("Connexion impossible.", "Connection failed."))
```

## 2. Entrées dans la section « Agents IA (coucou) »

Dans `Ecran.items()`, là où le code insère actuellement l’entrée
« Configurer l’assistant distant », insérer **deux** entrées :

```python
lignes.insert(0, {"genre": "assistant", "action": "verifier",
                  "nom": T("Tester l'assistant distant", "Test remote assistant"),
                  "aide": T("Teste l'URL, la clé, le certificat TLS et l'état du runner ; la clé n'est jamais affichée.",
                            "Tests the URL, key, TLS certificate and runner status; the key is never displayed.")})
lignes.insert(0, {"genre": "assistant", "action": "configurer",
                  "nom": T("Configurer l'assistant distant", "Configure remote assistant"),
                  "aide": T("URL du serveur, clé secrète (saisie masquée) et utilisateur ; conservés séparément dans assistant.conf. Pour un autre réseau, utilise l'URL HTTPS d'un tunnel.",
                            "Server URL, secret key (hidden input) and user; kept separately in assistant.conf. For another network, use the HTTPS URL of a tunnel.")})
```

L’ordre final doit être :

1. Configurer l’assistant distant
2. Tester l’assistant distant

Un test existant vérifie que `items()[0]["genre"] == "assistant"` et attend
la configuration en premier.

## 3. Affichage

Dans `dessiner()`, pour une entrée `genre == "assistant"` :

```python
v = T("Tester… ↵", "Test… ↵") if it.get("action") == "verifier" else T("Ouvrir… ↵", "Open… ↵")
self.ligne(y, x0, iw, col, it["nom"], v, sel, False)
```

Dans la barre d’aide, adapter le texte :

- configuration : « Entrée : configurer la connexion » ;
- test : « Entrée : tester la connexion ».

## 4. Dispatch

Deux endroits appellent `self.assistant()` :

- clic souris dans `clic()` ;
- touche Entrée / Espace / Droite dans `boucle()`.

Les deux doivent passer l’entrée courante :

```python
self.assistant(it)     # clic
self.assistant(cur)    # clavier
```

## 5. Méthode `assistant()`

Remplacer la signature par :

```python
def assistant(self, it=None):
    if isinstance(it, dict) and it.get("action") == "verifier":
        self.statut(T("Test de la connexion…", "Testing connection…"))
        self.dessiner()
        code, message = verifier_assistant()
        self.statut(message, erreur=code != 0)
        return
    # comportement existant : configuration via nc-assistant configurer
```

Le test existant appelle `e.assistant()` sans argument : il doit donc
continuer à lancer la configuration.

## 6. Ligne de commande

Dans `cli(a)` :

```python
if a[0] == "assistant" and len(a) == 2 and a[1] in ("verifier", "verify", "test"):
    code, message = verifier_assistant()
    print(message, file=sys.stdout if code == 0 else sys.stderr)
    sys.exit(code)
if a[0] == "assistant" and len(a) == 1:
    sys.exit(configurer_assistant())
```

Mettre à jour les deux docstrings (`__doc__` et `USAGE_EN`) pour mentionner :

```text
nc-reglages assistant verifier   tester la connexion distante (réseau, clé, TLS, runner)
nc-settings assistant verify     test the remote connection (network, key, TLS, runner)
```

## 7. Tests à ajouter

Dans `tests/test_assistant.py`, classe `ReglagesAssistant` :

- `items()[0]["action"] == "configurer"` ;
- `items()[1]["action"] == "verifier"` ;
- `verifier_assistant()` appelle bien `nc-assistant verifier` et retourne le
  premier ligne en cas de succès, la dernière ligne d’erreur sinon ;
- aucune clé n’est passée en argument ni affichée.

Exemple de mock :

```python
with patch.object(m.subprocess, "run") as run:
    run.return_value = unittest.mock.Mock(returncode=0, stdout="Connection OK\n", stderr="")
    code, message = m.verifier_assistant()
self.assertEqual(code, 0)
self.assertIn("Connection OK", message)
self.assertEqual(run.call_args.args[0][-1], "verifier")
```

## 8. Validation

```bash
cd noctalia-config
python3 -m py_compile home/.local/bin/nc-reglages
python3 -B -m unittest tests.test_assistant -v
```

Vérifier aussi visuellement :

```bash
nc-reglages
# Agents IA (coucou) → Tester l'assistant distant
```

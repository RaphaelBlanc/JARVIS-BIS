# Fiche 01 — `home/.local/bin/nc-assistant`

## But

Ajouter un diagnostic fiable et expliquer les URL privées, sans changer le
contrat HTTP existant (`/api/ask`, `/api/status`, `/api/briefing`).

## 1. Imports

Ajouter :

```python
import ipaddress
import ssl
import time
```

`time` sert à mesurer la latence, `ipaddress` à reconnaître les adresses
privées, `ssl` à distinguer un certificat invalide.

## 2. Détection d’une URL privée

Ajouter après `valider()` :

```python
def hote_prive(url):
    """Vrai si l'hôte n'est joignable que depuis le réseau local du serveur."""
    h = (urllib.parse.urlsplit(url).hostname or "").lower().rstrip(".")
    if not h:
        return False
    if h == "localhost" or h.endswith(".local") or h.endswith(".lan") or h.endswith(".home.arpa"):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved)
```

Cas à couvrir : `192.168.*`, `10.*`, `172.16-31.*`, `127.*`, `::1`,
`fe80::*`, `*.local`, `*.lan`, `*.home.arpa`.

## 3. Avertissement dans `configurer()`

Après `sauvegarder(...)` et le message « Configuration enregistrée », ajouter :

- si `hote_prive(url)` : avertir que l’URL ne fonctionnera que sur le réseau
  local et qu’il faut l’URL HTTPS d’un tunnel depuis un autre réseau ;
- sinon si l’URL commence par `http://` : recommander HTTPS ;
- terminer par : `Teste la connexion avec : nc-assistant verifier`.

Ne jamais afficher la clé.

## 4. Messages d’erreur dans `api()`

Remplacer le bloc `except` actuel par une distinction explicite :

| Cas | Message attendu |
|---|---|
| 3xx | Redirection refusée, configurer l’URL finale en HTTPS |
| 401 / 403 | Clé refusée, vérifier `SECRET_KEY` |
| 404 | Route introuvable, vérifier l’URL et son préfixe |
| 429 | Trop de requêtes, réessayer dans une minute |
| 5xx | Erreur interne du serveur, consulter ses journaux |
| `socket.gaierror` | Nom du serveur introuvable (DNS) |
| `SSLCertVerificationError` / `SSLError` | Certificat TLS invalide |
| `ConnectionRefusedError` | Connexion refusée (service ou tunnel arrêté) |
| `TimeoutError` | Serveur pas répondu à temps |
| autre `OSError` | Serveur inaccessible |

Contraintes :

- ne jamais inclure le corps de la réponse d’erreur (données privées) ;
- ne jamais inclure la clé ;
- conserver le refus des redirections (`SansRedirection`) pour ne pas envoyer
  `X-Secret-Key` vers une autre adresse.

## 5. Commande `verifier`

Ajouter une fonction `verifier(c, json_out=False)` :

1. calcule `prive = hote_prive(c["URL"])` ;
2. mesure le temps autour de `api(c, "/api/status")` ;
3. en cas d’échec :
   - affiche `Connexion impossible : <message>` sur `stderr` ;
   - si `prive`, ajoute une indication sur le réseau local et le tunnel ;
   - si `http://`, recommande HTTPS ;
   - en `--json`, produit un objet avec `configured`, `online`, `url`,
     `private`, `error` ;
   - retourne `1` ;
4. en cas de succès :
   - `runner = r.get("online") is not False and r.get("runner_alive") is not False` ;
   - affiche `Connexion OK — gateway vX, runner en ligne, N ms` ;
   - si `prive`, avertit que l’URL n’est valable que sur le réseau local ;
   - si `gateway.public_url` existe et diffère de l’URL configurée, l’affiche ;
   - retourne `0` si le runner est en ligne, `1` sinon.

Schéma JSON :

```json
{
  "configured": true,
  "online": true,
  "runner_alive": true,
  "url": "https://assistant.example.fr",
  "private": false,
  "latency_ms": 42,
  "version": "1.0.0",
  "public_url": "https://assistant.example.fr",
  "error": null
}
```

## 6. Branchement CLI

Dans `main()`, ajouter :

```python
p = sous.add_parser("verifier", aliases=["check", "diagnostic"])
p.add_argument("--json", action="store_true")
```

Puis, après le chargement de la configuration :

```python
if commande in ("verifier", "check", "diagnostic"):
    return verifier(c, getattr(a, "json", False))
```

**Attention** : la branche `origin/main` actuelle contient déjà
`requete-ile`. Il faut conserver :

```python
sous.add_parser("requete-ile", help=argparse.SUPPRESS)
```

et la branche `elif commande == "requete-ile": requete_ile()`.

## 7. `etat` en cas d’échec

Dans le gestionnaire d’exception, pour `etat` / `status` :

```python
resultat = {"online": False, "error": message}
try:
    if hote_prive(configuration()["URL"]):
        resultat["private"] = True
        resultat["hint"] = T(
            "URL locale/privée : utilise l'URL HTTPS d'un tunnel depuis un autre réseau.",
            "Local/private URL: use a tunnel's HTTPS URL from another network.")
except (Erreur, OSError):
    pass
print(json.dumps(resultat, ensure_ascii=False))
```

Cela permet à l’île ou à un widget d’afficher un message utile.

## 8. Tests à ajouter

Dans `tests/test_assistant.py` :

- `hote_prive` vrai pour `localhost`, `192.168.*`, `10.*`, `172.16.*`,
  `*.local`, `::1` ;
- faux pour un domaine public et `100.64.*` (sauf si vous décidez de le
  traiter comme privé) ;
- `verifier` en ligne : code `0`, message « Connection OK », mention
  « local/private » avec une URL de test `127.0.0.1`, aucune clé dans la
  sortie ;
- `verifier --json` : champs `online`, `private`, `url` ;
- 401 : message « rejected the key » (ou l’équivalent français) et pas de
  corps d’erreur ;
- 404 : message contenant `404` ;
- runner hors ligne (`{"online": false}`) : code `1` ;
- `etat` en échec : `private: true` + `hint`, pas de fuite de clé.

## 9. Validation

```bash
cd noctalia-config
python3 -m py_compile home/.local/bin/nc-assistant
python3 -B -m unittest tests.test_assistant -v
```

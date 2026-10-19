# Accès distant à l'assistant (Noctalia, téléphone, autre PC)

## Pourquoi une URL `192.168.x.x` ne marche pas ailleurs

`http://192.168.1.28:8000` est une adresse **privée** : elle n'est valable que
sur le réseau local du serveur. Depuis un autre Wi-Fi, un partage de connexion
ou Internet, le PC ne sait pas où joindre cette adresse. Aucun réglage côté
Noctalia ne peut corriger cela : il faut donner au gateway une **adresse
publique ou un réseau privé partagé**.

Le client Noctalia parle toujours au **gateway** (port `8000`), qui expose
`/api/ask`, `/api/status` et `/api/briefing`. Il ne faut **jamais exposer
directement le home-runner** (port `8080`) : c'est l'API privée qui exécute
`agy`.

```
PC Noctalia (n'importe où) ──HTTPS──► URL publique ──► gateway :8000 ──► runner :8080
```

## Option A — Cloudflare Tunnel (recommandé, aucun port ouvert)

### Test rapide, sans compte

```bash
cd ~/assistant-perso
./scripts/expose-gateway.sh quick
```

La commande affiche une URL du type `https://xxxx.trycloudflare.com`. Garde-la
ouverte (Ctrl+C pour arrêter). Sur le PC Noctalia :

```bash
nc-assistant configurer
# URL     : https://xxxx.trycloudflare.com
# Clé     : la valeur SECRET_KEY de render-gateway/.env
# USER_ID : ton nom (ou "noctalia")
nc-assistant verifier
```

> L'URL `trycloudflare.com` change à chaque redémarrage. Elle convient pour
> valider, pas pour un usage quotidien.

### Installation durable avec un domaine

Prérequis : un domaine géré par Cloudflare, `cloudflared` installé et
`cloudflared tunnel login` effectué une première fois.

```bash
cd ~/assistant-perso
./scripts/expose-gateway.sh cloudflare --hostname assistant.tondomaine.fr
```

Le script :

1. crée (ou réutilise) un tunnel nommé `assistant` ;
2. écrit `~/.cloudflared/config.yml` vers `http://127.0.0.1:8000` ;
3. associe le DNS `assistant.tondomaine.fr` ;
4. installe et démarre le service utilisateur `assistant-tunnel`.

Vérifications :

```bash
./scripts/expose-gateway.sh status
systemctl --user status assistant-tunnel
curl -fsS https://assistant.tondomaine.fr/ping
```

Puis, sur le PC Noctalia :

```bash
nc-assistant configurer   # URL = https://assistant.tondomaine.fr
nc-assistant verifier
```

### Si le gateway tourne sur le Raspberry Pi

Exécute le script **sur le Pi**, dans le dossier du projet. Le tunnel part du
Pi vers `127.0.0.1:8000`. Inutile d'ouvrir un port sur la box.

## Option B — Tailscale (réseau privé, aucune exposition publique)

Tailscale crée un réseau privé entre tes appareils. C'est la solution la plus
discrète : le gateway n'est pas visible sur Internet.

```bash
# Sur le serveur ET sur le PC Noctalia
sudo tailscale up

# Sur le serveur uniquement
cd ~/assistant-perso
./scripts/expose-gateway.sh tailscale
```

Le script publie le port `8000` via `tailscale serve` et affiche une URL du
type `https://mon-pi.tailnet-xxxx.ts.net`. Configure le client Noctalia avec
cette URL. Les deux machines doivent être connectées au même tailnet.

## Option C — Gateway sur Render + tunnel vers le runner

C'est le mode « hybride » déjà décrit dans le README :

1. déploie `render-gateway` sur Render ;
2. expose le **home-runner** (`8080`) par un tunnel Cloudflare ;
3. renseigne `HOME_SERVER_URL` sur Render avec l'URL du tunnel ;
4. sur Noctalia, utilise l'URL HTTPS du service Render.

Dans ce mode, le tunnel ne sert qu'à la liaison Render → runner. Le client
Noctalia ne parle qu'à Render.

## Côté serveur : variables utiles

Dans `render-gateway/.env` :

```dotenv
# URL publique annoncée par /api/status (facultatif, purement informatif)
GATEWAY_PUBLIC_URL=https://assistant.tondomaine.fr

# Limite les appels /api/* par IP (0 = désactivé)
DESKTOP_RATE_LIMIT_PER_MINUTE=60

# À mettre à 1 uniquement derrière Cloudflare Tunnel / reverse proxy de confiance
TRUST_PROXY_HEADERS=1
```

`TRUST_PROXY_HEADERS=1` permet de compter la vraie IP du client à partir de
`CF-Connecting-IP` ou `X-Forwarded-For`. Ne l'active pas si le gateway est
joignable directement : un client pourrait falsifier l'en-tête.

Après modification :

```bash
systemctl --user restart assistant-gateway
```

## Côté Noctalia

Le client est `nc-assistant` (dépôt `noctalia-config`). Commandes utiles :

```bash
nc-assistant configurer        # URL, clé (saisie masquée), USER_ID
nc-assistant verifier          # test réseau + diagnostic
nc-assistant verifier --json   # sortie lisible par un script
nc-assistant etat              # état pour l'île / le widget
nc-assistant demander "Bonjour"
nc-assistant briefing
```

`nc-assistant verifier` distingue :

- **URL privée** (`192.168.*`, `10.*`, `172.16-31.*`, `localhost`, `*.local`) :
  elle ne fonctionnera que sur le réseau local ;
- **clé refusée** (HTTP 401/403) ;
- **route introuvable** (HTTP 404) : URL ou préfixe de chemin incorrect ;
- **certificat TLS invalide** : l'URL HTTPS ne correspond pas au certificat ;
- **nom introuvable / connexion refusée / délai dépassé** ;
- **gateway joignable mais runner hors ligne**.

La clé reste dans `~/.config/nc/assistant.conf` (`0600`) et n'est jamais
affichée ni transmise lors d'une redirection HTTP.

## Sécurité

- Utilise **HTTPS** dès que le gateway sort du réseau local.
- La clé `X-Secret-Key` est la seule authentification de l'API : garde-la
  secrète et fais-la tourner si elle a pu fuiter.
- Laisse `DESKTOP_RATE_LIMIT_PER_MINUTE` actif lorsque le gateway est exposé.
- N'expose jamais `8080` : le runner n'a pas la logique de rappels/briefing et
  donne un accès direct à `agy`.
- Un tunnel Cloudflare ne remplace pas l'authentification : la clé reste
  obligatoire.
- Si tu utilises uniquement un tunnel local, tu peux lier le gateway à
  `127.0.0.1` (`uvicorn main:app --host 127.0.0.1`) au lieu de `0.0.0.0` :
  le tunnel continue de fonctionner et le LAN n'y accède plus directement.

## Dépannage

| Symptôme | Cause probable | Correction |
|---|---|---|
| `Serveur inaccessible` sur un autre réseau | URL LAN privée | Utilise l'URL HTTPS du tunnel/Tailscale |
| `Erreur HTTP 401` | `SECRET_KEY` différente entre client et serveur | Recopie la clé de `render-gateway/.env` |
| `Erreur HTTP 404` | URL sans le bon préfixe de chemin | Utilise l'URL finale, sans slash superflu |
| `Redirection refusée` | URL `http://` redirigée vers `https://` | Configure directement l'URL `https://` |
| `online: false` mais la page `/ping` répond | Le runner est arrêté | `systemctl --user restart assistant-runner` |
| URL `trycloudflare.com` morte | Tunnel temporaire arrêté/redémarré | Relance le tunnel et reconfigure l'URL |

# Cloudflare Tunnel — quel port exposer ?

> Guide mis à jour. La procédure complète (Cloudflare, Tailscale, Render) est
> dans [`docs/ACCES_DISTANT.md`](../docs/ACCES_DISTANT.md).

Il y a **deux cas différents**, et confondre les deux est la cause la plus
fréquente d'un client Noctalia qui ne fonctionne que sur le réseau local.

## Cas 1 — Le gateway et le runner tournent sur la même machine (recommandé)

C'est le mode tout-en-un. Le PC Noctalia doit joindre le **gateway** :

```yaml
# ~/.cloudflared/config.yml
tunnel: <UUID>
credentials-file: /home/<user>/.cloudflared/<UUID>.json

ingress:
  - hostname: assistant.tondomaine.fr
    service: http://127.0.0.1:8000
  - service: http_status:404
```

Puis, sur le PC Noctalia :

```bash
nc-assistant configurer
# URL : https://assistant.tondomaine.fr
```

Le plus simple est d'utiliser le script fourni :

```bash
./scripts/expose-gateway.sh cloudflare --hostname assistant.tondomaine.fr
# ou, pour un test sans compte :
./scripts/expose-gateway.sh quick
```

**N'expose pas `8080`** : le runner est l'API privée qui exécute `agy`. Il ne
fournit ni `/api/ask`, ni `/api/status`, ni `/api/briefing`.

## Cas 2 — Le gateway est sur Render, le runner reste à la maison

Dans ce cas seulement, le tunnel relie Render au **runner** local :

```yaml
ingress:
  - hostname: runner.tondomaine.fr
    service: http://127.0.0.1:8080
```

Puis dans les variables d'environnement Render :

```dotenv
HOME_SERVER_URL=https://runner.tondomaine.fr
```

Le client Noctalia, lui, utilise l'URL HTTPS de Render — pas celle du tunnel.

## Rappel sécurité

- HTTPS obligatoire hors du réseau local.
- La clé `X-Secret-Key` reste requise dans les deux cas.
- `TRUST_PROXY_HEADERS=1` derrière Cloudflare, `0` si le gateway est joignable
  directement.
- `DESKTOP_RATE_LIMIT_PER_MINUTE` limite les appels à l'API publique.

## Commandes utiles

```bash
cloudflared tunnel list
cloudflared tunnel info assistant
sudo journalctl -u cloudflared -f
systemctl --user status assistant-tunnel
./scripts/expose-gateway.sh status
```

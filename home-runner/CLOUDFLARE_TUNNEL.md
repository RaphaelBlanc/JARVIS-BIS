# Guide : Cloudflare Tunnel (PC Maison)

Cloudflare Tunnel expose ton PC maison sur Internet **sans IP fixe, sans ouvrir de ports**.

---

## 1. Installer `cloudflared`

```bash
# Sur Debian/Ubuntu/Linux Mint
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -o cloudflared.deb
sudo dpkg -i cloudflared.deb
rm cloudflared.deb

# Vérifier
cloudflared --version
```

---

## 2. Authentification Cloudflare

```bash
cloudflared tunnel login
# Ouvre un navigateur → connecte-toi à ton compte Cloudflare
# Un fichier ~/.cloudflared/cert.pem est créé
```

> ⚠️ Si tu n'as pas de compte Cloudflare, crée-en un gratuit sur https://cloudflare.com

---

## 3. Créer le tunnel

```bash
cloudflared tunnel create assistant-home
# Retourne un UUID, ex: 550e8400-e29b-41d4-a716-446655440000
# Fichier de credentials créé dans ~/.cloudflared/
```

---

## 4. Configurer le tunnel

Crée le fichier `~/.cloudflared/config.yml` :

```yaml
tunnel: <TON_UUID_ICI>
credentials-file: /home/xenospacex/.cloudflared/<TON_UUID_ICI>.json

ingress:
  - service: http://localhost:8080
```

> Sans domaine personnalisé, tu peux utiliser un tunnel temporaire gratuit (voir étape 6).

---

## 5. (Optionnel) Lier à un domaine Cloudflare

Si tu as un domaine géré par Cloudflare :

```bash
cloudflared tunnel route dns assistant-home assistant.tondomaine.com
```

Puis dans `config.yml`, ajoute le hostname :

```yaml
ingress:
  - hostname: assistant.tondomaine.com
    service: http://localhost:8080
  - service: http_status:404
```

---

## 6. Tunnel temporaire (sans domaine, pour tester)

```bash
# Lance un tunnel rapide sans config → génère une URL aléatoire
cloudflared tunnel --url http://localhost:8080
# Ex: https://random-name.trycloudflare.com
```

> Utile pour tester. L'URL change à chaque redémarrage.

---

## 7. Lancer le tunnel en service systemd (permanent)

```bash
# Installe le service cloudflared
sudo cloudflared service install

# Démarre
sudo systemctl start cloudflared
sudo systemctl enable cloudflared

# Vérifie
sudo systemctl status cloudflared
```

---

## 8. Trouver ton URL publique

```bash
# Si tunnel temporaire
cloudflared tunnel --url http://localhost:8080
# → copie l'URL affichée

# Si tunnel nommé avec domaine
# → c'est ton hostname configuré
```

**Cette URL = `HOME_SERVER_URL` dans le `.env` de render-gateway.**

---

## Récapitulatif des commandes utiles

```bash
cloudflared tunnel list           # Liste tes tunnels
cloudflared tunnel info assistant-home  # Détails du tunnel
cloudflared tunnel delete assistant-home # Supprimer
sudo journalctl -u cloudflared -f  # Logs en temps réel
```

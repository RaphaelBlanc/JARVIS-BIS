# 🤖 Assistant Personnel

> Bot Telegram + Discord → Antigravity CLI sur ton PC maison.

## Architecture

```
Telegram / Discord
       ↓
  render-gateway   (Render.com)
       ↓ HTTPS
   home-runner     (PC maison, via Cloudflare Tunnel)
       ↓
   agy CLI (Antigravity)
```

## Structure

```
assistant-perso/
├── render-gateway/   # Déployé sur Render — bots Telegram + Discord
├── home-runner/      # Sur ton PC — API FastAPI + bridge AGY
├── .gitignore
└── README.md
```

## Démarrage rapide

### 1. home-runner (PC maison)

```bash
cd home-runner
bash setup.sh
# Édite .env avec ta SECRET_KEY
uvicorn main:app --host 0.0.0.0 --port 8080
```

Voir [home-runner/README.md](home-runner/README.md) pour le service systemd + Cloudflare Tunnel.

### 2. render-gateway (Render.com)

1. Push ce repo sur GitHub
2. Crée un service Web sur [render.com](https://render.com) à partir du dossier `render-gateway/`
3. Configure les variables d'env (voir `render-gateway/.env.example`)

Voir [render-gateway/README.md](render-gateway/README.md) pour les détails.

## Sécurité

- La `SECRET_KEY` est partagée entre `render-gateway` et `home-runner` — elle authentifie chaque requête
- Le `home-runner` ne répond qu'aux requêtes portant la bonne clé
- Les `.env` ne sont **jamais** committés (voir `.gitignore`)
- Configurer `TELEGRAM_ALLOWED_USER_IDS` et `DISCORD_ALLOWED_USER_IDS` pour restreindre l'accès

## Auteur

Cyprien Thuillier

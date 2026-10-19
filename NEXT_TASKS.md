# Backlog de passation — assistant-perso

## Contexte

- Dépôt local : `/home/xenospacex/Bureau/assistant-perso`
- Production : `/home/pi/assistant-perso` sur `pi@192.168.1.28`
- Branche : `main`, commit de référence : `90b7427`
- Services : `assistant-gateway` (port 8000) et `assistant-runner` (port 8080)
- Bots : Telegram (polling) et Discord (commandes serveur)
- Moteur : `agy` en worker persistant, sandbox activé (`AGY_SANDBOX=1`)
- Tests gateway : `cd render-gateway && .venv/bin/python -m unittest discover -s tests -q`
- Tests runner : `cd home-runner && .venv/bin/python -m unittest discover -s tests -q`
- Ne pas changer le token Telegram.
- Ne pas committer les fichiers d’exécution : `reminders.db`, `memory.json`, `briefing_state.json`, `saved_discord_channels.json`, `.env`.

## Règles de travail

1. Faire des commits petits et explicites.
2. Lancer les deux suites de tests avant chaque commit.
3. Après déploiement : redémarrer `assistant-runner` puis `assistant-gateway`, vérifier `/ping` et `/health`.
4. Ne pas casser les fonctionnalités déjà livrées : rappels récurrents, snooze, mémoire, STT auto, briefing configurable, sandbox.
5. Ne pas exposer de secrets dans les logs ou les fichiers.

---

## Lot 1 — Sécurité et sauvegardes (priorité haute)

### 1.1 Whitelists

- Renseigner `TELEGRAM_ALLOWED_USER_IDS` et `DISCORD_ALLOWED_USER_IDS` dans `render-gateway/.env`.
- Passer `ALLOW_ALL_USERS=0`.
- Vérifier que les tests `test_access.py` passent.
- Critère : un utilisateur non listé reçoit « Accès non autorisé ».

### 1.2 Sauvegardes automatiques

Sauvegarder :

- `render-gateway/reminders.db`
- `home-runner/memory.json`
- `render-gateway/saved_chats.json`
- `render-gateway/saved_discord_channels.json`
- `render-gateway/briefing_state.json`
- `integrations/google-workspace/token.json`

Créer un script de backup avec `sqlite3 .backup`, chiffrement (`age` ou GPG) et rétention 30 jours. Ajouter un cron et tester une restauration.

### 1.3 Rate limiting

- Ajouter une limite simple par utilisateur (ex. 1 requête toutes les 2 s, max 20/min).
- Appliquer côté Telegram et Discord avant l’appel au runner.
- Critère : les commandes restent réactives, le spam est bloqué sans erreur bruyante.

### 1.4 Audit des secrets

- Vérifier les permissions `600` sur les `.env`, `token.json`, `credentials.json`, `memory.json`.
- Vérifier que `.gitignore` couvre tous les fichiers sensibles.
- Ne pas changer le token Telegram.

---

## Lot 2 — Réduction des tokens (priorité haute)

### 2.1 Injection mémoire conditionnelle

Problème : le bloc mémoire est injecté à chaque `/run`, même quand rien n’a changé.

À faire dans `home-runner/memory.py` et `home-runner/main.py` :

- Calculer un hash/version de la mémoire (`profile` + `facts`).
- Stocker dans la session la dernière version injectée.
- N’injecter le bloc que si :
  - nouvelle session (`has_active_session=False`), ou
  - version mémoire différente de la dernière injectée.
- Après un `/run` réussi, mémoriser la version injectée.

Critère : deux requêtes successives sans modification de mémoire → le deuxième prompt ne contient pas le bloc `[Contexte utilisateur durable]`. Après ajout d’un souvenir → le bloc réapparaît.

### 2.2 Limiter la taille de la mémoire injectée

- Passer `MEMORY_PROMPT_MAX_CHARS` de `4000` à `1500` par défaut.
- Prioriser : profil d’abord, puis les 10 souvenirs les plus récents.
- Ajouter une option pour tronquer proprement avec `[…mémoire tronquée]`.

### 2.3 Compaction automatique

- Ajouter `AUTO_COMPACT_TURNS` (ex. 20) dans `home-runner/agy_bridge.py`.
- Après N tours dans une session, lancer `/compact` en one-shot ou réinitialiser proprement l’historique.
- Ne pas compacter les commandes stateless (`/briefing`, `/agenda`, etc.).
- Critère : une longue conversation ne fait pas grossir indéfiniment le contexte.

### 2.4 Cache du briefing

- Mettre en cache le texte du briefing généré dans `render-gateway/briefing_cache.json`.
- TTL configurable : `BRIEFING_CACHE_TTL_MINUTES=120`.
- `/briefing` réutilise le cache s’il est valide ; le scheduler peut forcer un rafraîchissement.
- Critère : deux `/briefing` rapprochés ne déclenchent qu’un seul appel au modèle.

### 2.5 Audit des commandes stateless

Vérifier que ces commandes utilisent `continue_session=False` :

- `/briefing`, `/agenda`, `/tasks`, `/meteo`, `/search`, `/summarize`, `/status`, `/usage`, `/skills`, `/models`, `/session`, `/help`.

Cela évite d’envoyer l’historique complet pour des commandes sans continuité.

### 2.6 Suivi des tokens

- Si `agy` expose `usage` dans les événements `result`, logger les tokens entrée/sortie par requête.
- Ajouter un total quotidien et un affichage dans `/usage`.
- Sinon, au minimum logger la taille des prompts et des réponses.

---

## Lot 3 — Multi-utilisateur et robustesse (moyen)

### 3.1 Sessions par utilisateur

Problème : `SESSION_CONFIG` est global dans `home-runner/agy_bridge.py`. Tous les utilisateurs partagent la même conversation.

- Remplacer par un dictionnaire de sessions indexé par `(platform, user_id)`.
- Adapter `/run`, `/session`, `/conversation/*`.
- Un worker `agy` par session ou une file d’attente avec un worker par utilisateur.
- Critère : deux utilisateurs ne se mélangent plus.

### 3.2 File de jobs

- Ajouter des jobs pour les tâches longues : ID, statut, progression, annulation.
- Endpoints : `POST /jobs`, `GET /jobs/{id}`, `DELETE /jobs/{id}`.
- Notification à la fin sur Telegram/Discord.

### 3.3 Persistance des réglages

- Sauvegarder modèle, effort, mode et permission dans un fichier/SQLite.
- Recharger au démarrage du runner.

### 3.4 Accès distant (livré)

- `scripts/expose-gateway.sh` expose le gateway `8000` via Cloudflare Tunnel (temporaire ou nommé) ou Tailscale Serve.
- Guide `docs/ACCES_DISTANT.md` : ne jamais exposer le runner `8080`.
- `GATEWAY_PUBLIC_URL` dans `/api/status`, `DESKTOP_RATE_LIMIT_PER_MINUTE` et `TRUST_PROXY_HEADERS`.
- Client Noctalia : `nc-assistant verifier` (réseau, clé, TLS, runner) et entrée « Tester l'assistant distant » dans `nc-reglages`.
- Reste à faire : notifications `desktop` réellement poussées vers l'île (le gateway journalise seulement les rappels desktop).

---

## Lot 4 — CI et qualité (moyen)

### 4.1 GitHub Actions

Créer `.github/workflows/ci.yml` :

- tests gateway ;
- tests runner ;
- `ruff` ;
- `pip-audit`.

### 4.2 Couverture

Ajouter des tests pour :

- API mémoire et rappels ;
- scheduler de rappels et de briefing ;
- `home-runner/main.py` ;
- `agy_bridge.py` (timeouts, fallback, sandbox) ;
- commandes Telegram/Discord (déjà partiellement fait).

### 4.3 Refactor

- `telegram_bot.py` et `discord_bot.py` font ~1 800 lignes chacun.
- Extraire un noyau commun « commandes/actions » et des adaptateurs de plateforme.
- Objectif : réduire la duplication et les divergences.

---

## Lot 5 — Produits (optionnel)

- Briefing du soir à 21h.
- Notes rapides et recherche personnelle.
- RSS / veille quotidienne.
- Réponse vocale TTS.
- Brouillons d’e-mails avec confirmation avant envoi.
- Intégration Home Assistant / MQTT.
- Alertes serveur (CPU, RAM, disque, température).

---

## Prompt à coller dans la nouvelle conversation

```text
Lis NEXT_TASKS.md dans /home/xenospacex/Bureau/assistant-perso.

Contexte : projet assistant-perso, branche main, production sur pi@192.168.1.28,
services assistant-gateway et assistant-runner. Ne change pas le token Telegram.
Ne commit pas les fichiers d'exécution (.env, reminders.db, memory.json,
briefing_state.json, saved_discord_channels.json).

Implémente uniquement le Lot 1 (sécurité et sauvegardes), puis :
- lance les tests gateway et runner ;
- commit et push ;
- déploie sur le serveur et vérifie /ping et /health ;
- donne un résumé court des changements et des risques.

Ne touche pas aux autres lots pour cette conversation.
```

Pour le Lot 2, remplacer « Lot 1 » par « Lot 2 » et garder les mêmes consignes.

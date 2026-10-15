# 🚀 Intégration Google Workspace (Calendrier, Gmail, Drive) pour Antigravity

Ce module permet de connecter votre compte Google à votre assistant personnel (Antigravity CLI) via le protocole MCP (**Model Context Protocol**).

---

## 🎯 Ce que l'assistant peut faire une fois connecté

- **📅 Google Calendar** :
  - Consulter votre emploi du temps du jour ou de la semaine.
  - Ajouter des rendez-vous, réunions et rappels avec date, heure et description.
  - Modifier ou supprimer des événements.
- **✉️ Gmail** :
  - Lire vos e-mails récents ou non lus.
  - Rechercher des e-mails par mots-clés, expéditeur ou date.
  - Préparer des brouillons et envoyer des e-mails en votre nom.
  - Trier et labelliser des messages.
- **📁 Google Drive** :
  - Rechercher des fichiers et dossiers.
  - Télécharger et lire le contenu de documents, feuilles de calcul ou PDF.
  - Uploader de nouveaux fichiers et créer des dossiers.

---

## 🛠️ Configuration en 3 étapes simples (Google Cloud Console)

Pour des raisons de sécurité, Google demande que vous créiez un identifiant d'application personnel (gratuit) :

### Étape 1 : Créer un projet Google Cloud
1. Rendez-vous sur la console Google Cloud : **https://console.cloud.google.com/**
2. En haut à gauche, cliquez sur le sélecteur de projet puis sur **"Nouveau projet"**.
3. Nommez-le par exemple `Assistant-Perso` et cliquez sur **Créer**.
4. Sélectionnez ce nouveau projet.

### Étape 2 : Activer les 3 APIs Google
1. Dans la barre de recherche en haut, tapez et activez successivement :
   - **Google Calendar API** (cliquez sur "Activer")
   - **Gmail API** (cliquez sur "Activer")
   - **Google Drive API** (cliquez sur "Activer")

### Étape 3 : Configurer l'écran de consentement & Créer les identifiants
1. Dans le menu de gauche, allez dans **APIs et services** > **Écran de consentement OAuth**.
2. Choisissez **Externe** et cliquez sur **Créer**.
3. Remplissez simplement :
   - **Nom de l'application** : `Assistant Perso`
   - **Adresse e-mail d'assistance utilisateur** : votre adresse Gmail
   - **Coordonnées du développeur** : votre adresse Gmail
   - Cliquez sur **Enregistrer et continuer** jusqu'à la fin.
4. Dans l'onglet **Utilisateurs tests**, ajoutez votre propre adresse Gmail (indispensable tant que l'application est en mode test).
5. Allez ensuite dans **Identifiants** > **Créer des identifiants** > **ID client OAuth**.
6. Type d'application : Choisissez **Application de bureau** (Desktop App).
7. Cliquez sur **Créer**, puis cliquez sur **Télécharger le fichier JSON** (bouton avec l'icône de flèche vers le bas).
8. Renommez le fichier téléchargé en `credentials.json` et placez-le dans ce dossier (`/home/xenospacex/Bureau/assistant-perso/integrations/google-workspace/`).

---

## ⚡ Lancement de l'authentification (1 commande)

Une fois `credentials.json` placé dans ce dossier, lancez simplement :

```bash
/home/xenospacex/Bureau/assistant-perso/home-runner/.venv/bin/python setup_oauth.py
```

Le script va :
1. Ouvrir votre navigateur sur la page officielle de connexion Google.
2. Vous demander de vous connecter à votre compte Google et d'autoriser l'accès.
3. Récupérer automatiquement le `refresh_token` de longue durée.
4. Enregistrer la configuration MCP directement dans `~/.gemini/config/mcp_config.json`.
5. Tester la connexion (profil Gmail, calendriers et fichiers Drive).

---

## 🔒 Sécurité et Confidentialité
- Vos identifiants Google (`credentials.json` et `token.json`) restent **strictement stockés sur votre PC local** (fichiers en permissions restreintes `0600`).
- Ils sont exclus du versioning Git via `.gitignore`.
- Le serveur MCP s'exécute localement sur votre machine sous votre contrôle.

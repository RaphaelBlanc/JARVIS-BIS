# Installer et tester le cloud distant

Le cloud est optionnel et fonctionne uniquement en mode `server`. La mise à jour du serveur reste une opération explicite. L’interface et les patchs pour Noctalia se trouvent dans `integrations/noctalia-config/cloud/`.

## Installation sur le serveur

Mettre à jour le checkout JARVIS sur la branche ou le commit cloud communiqué, puis vérifier que `gateway/.env` déclare `DEPLOY_MODE=server`. Installer Docker avec Compose, ou Podman avec `podman-compose`, selon le système du serveur.

Depuis le checkout JARVIS :

```bash
./deploy.sh cloud configure
./deploy.sh cloud up
./deploy.sh cloud status
```

`configure` crée un secret interne dans `deploy/cloud.env` et configure le gateway dans `.env` et `gateway/.env`, en conservant leurs autres valeurs. Les fichiers sont privés (`0600`) et le secret n’est pas affiché. L’opération ne lance aucun service. `up` construit et lance uniquement le stockage.

Redémarrer ensuite le gateway avec son mécanisme habituel (service utilisateur Noctalia, service système ou déploiement propre au serveur). Il n’est pas nécessaire de redémarrer le runner IA pour le cloud. Le gateway doit charger les valeurs `CLOUD_ENABLED`, `CLOUD_INTERNAL_KEY` et `CLOUD_INTERNAL_URL` configurées.

Le port du stockage `8091` est publié sur **127.0.0.1 uniquement**, pour un gateway installé sur l’hôte. Le point d’entrée distant reste le gateway existant, avec HTTPS ou votre réseau privé sécurisé. Ne pas exposer `8091` sur Internet. Si le gateway est lui aussi conteneurisé, placer les deux services sur un réseau privé commun et régler `CLOUD_INTERNAL_URL` sur l’adresse du service ; la publication loopback peut alors être retirée.

Le volume Compose `jarvis-cloud-data` contient fichiers, versions, morceaux et base SQLite. Le conteneur tourne sans root, avec son système de fichiers en lecture seule, un répertoire temporaire borné et aucune capacité Linux. Le poste Noctalia n’a pas besoin de Docker ou Podman pour accéder au drive.

Les limites se règlent dans `deploy/cloud.env` : `CLOUD_QUOTA_BYTES`, `CLOUD_MAX_FILE_BYTES`, `CLOUD_CHUNK_BYTES` et `CLOUD_UPLOAD_TTL`. Recréer le conteneur avec `cloud up` après modification. Le `CLOUD_INTERNAL_KEY` doit être identique côté stockage et gateway. Une rotation se fait en changeant ces deux configurations, puis en redémarrant les deux services.

## Depuis Noctalia

Installer le patch Noctalia livré, puis conserver la connexion JARVIS distante habituelle. Dans les réglages « Agents IA (coucou) », ouvrir « Cloud JARVIS distant », associer le PC et activer l’affichage dans la barre. Les commandes équivalentes sont :

```bash
nc-cloud associer
nc-reglages set CLOUD_JARVIS oui
nc-cloud fenetre
```

Le trousseau Secret Service doit être disponible et déverrouillé (`secret-tool`). L’ouverture dans le gestionnaire de fichiers requiert les bindings Python Gio et le backend WebDAV de GVfs. `nc-cloud verifier` contrôle les commandes et bindings disponibles ; si le montage échoue malgré ces composants, vérifier aussi la présence du backend WebDAV de GVfs dans les paquets de votre distribution.

`nc-cloud ouvrir` monte le drive à la demande avec Gio, puis ajoute le favori « Cloud JARVIS » sans toucher aux autres favoris. L’accès natif est en lecture seule. Les envois passent par le glisser-déposer dans la barre/fenêtre, `nc-cloud deposer /chemin/fichier`, ou « Scripts → Envoyer vers Cloud JARVIS » dans Nautilus.

L’icône est placée près du centre de la barre supérieure, une surface par écran. Son décalage est réglable avec `CLOUD_BARRE_DECALAGE`, par défaut `100` pixels logiques. Théo pourra ajuster l’emplacement à la composition officielle de la barre.

## Vérifications avec deux PC

Associer chaque PC séparément, puis vérifier :

1. Un fichier envoyé depuis le premier apparaît dans le drive du second.
2. Deux dépôts de même nom produisent un conflit ; « Conserver les deux » préserve les deux contenus.
3. Deux remplacements d’une même révision ne peuvent pas tous deux écraser silencieusement le fichier.
4. Réseau coupé avant dépôt : message hors ligne, aucun nouveau transfert et sources intactes.
5. Réseau coupé pendant transfert : aucune version partielle dans le drive ; « Reprendre / vérifier » réconcilie l’opération après reconnexion.
6. Fermeture de la fenêtre : l’envoi continue. Un arrêt de la session ou du processus peut interrompre l’envoi ; sa reprise reste explicite.
7. Un serveur en mode `local` refuse le cloud même avec `CLOUD_ENABLED=1`.

Le client ne supprime jamais les sources après dépôt. La v1 accepte jusqu’à 8 fichiers ordinaires par sélection ; dossiers récursifs et liens symboliques ne sont pas envoyés. Aucun envoi n’est relancé automatiquement au retour du réseau.

## Entretien et sauvegarde

Les anciennes versions sont conservées sans purge automatique et comptent dans le quota. Les reçus de publication sont conservés. Les morceaux terminés/expirés et objets orphelins peuvent être nettoyés **stockage arrêté**, par exemple avec Docker Compose :

```bash
docker compose --env-file deploy/cloud.env -f deploy/cloud.compose.yaml stop jarvis-cloud
docker compose --env-file deploy/cloud.env -f deploy/cloud.compose.yaml run --rm --no-deps jarvis-cloud \
  python -c 'from cloud.storage import Store, Settings; Store(Settings.environment()).maintenance()'
docker compose --env-file deploy/cloud.env -f deploy/cloud.compose.yaml up -d jarvis-cloud
```

Ne pas exécuter `maintenance()` pendant des transferts : le nettoyage des objets orphelins suppose l’absence de commits en cours. Le nettoyage préserve les versions publiées et les reçus d’idempotence.

Pour une sauvegarde cohérente, arrêter le stockage, puis exporter **tout le volume** (base SQLite, fichiers WAL éventuels, objets et états), avant de relancer le stockage. Utiliser le mécanisme de sauvegarde de volumes de votre moteur de conteneurs. Conserver séparément les configurations privées du gateway et du service. Restaurer vers un volume distinct à l’arrêt et tester des téléchargements avant de l’utiliser comme stockage principal. Le test de persistance automatisé recrée le moteur sur le même volume ; il ne remplace pas un essai de votre outil de sauvegarde.

`./deploy.sh cloud down` arrête et supprime le conteneur sans effacer le volume. Ne pas ajouter `--volumes` à la commande Compose pour une désactivation ordinaire. Désactiver aussi `CLOUD_ENABLED=0` dans le gateway et le redémarrer si le cloud n’est plus utilisé. Un changement de mode n’efface pas les données.

## Tests de développement

```bash
python3 -m venv /tmp/jarvis-cloud-tests
/tmp/jarvis-cloud-tests/bin/pip install -r cloud/requirements.txt httpx==0.27.2 pytest
/tmp/jarvis-cloud-tests/bin/python -m pytest -q tests/test_cloud.py tests/test_cloud_proxy.py
```

Les tests utilisent des dossiers temporaires et un serveur HTTP loopback. Aucun bot, moteur IA ou serveur de production n’est démarré. Le conteneur peut se construire séparément avec `podman build -f cloud/Dockerfile -t localhost/jarvis-cloud:test .`.

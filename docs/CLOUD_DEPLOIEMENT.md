# Installer et tester le cloud distant

Le cloud est optionnel et fonctionne uniquement en mode `server`. La mise à jour du serveur reste une opération explicite. Le client officiel Noctalia est suivi dans [l’issue #46](https://github.com/abcdef123456-wq/noctalia-config/issues/46) ; les patchs du prototype sont des références, pas des changements officiels du rice.

## Installation sur le serveur

Mettre à jour le checkout JARVIS sur la branche ou le commit cloud communiqué, puis vérifier que `gateway/.env` déclare `DEPLOY_MODE=server`. Installer Docker avec Compose, ou Podman avec `podman-compose`, selon le système du serveur.

Depuis le checkout JARVIS :

```bash
./deploy.sh cloud configure
./deploy.sh cloud up
./deploy.sh cloud status
```

`configure` crée un secret interne uniquement dans `deploy/cloud.env` et `gateway/.env`. Il configure le gateway sans changer les autres valeurs, les commentaires, les accents ou une adresse interne personnalisée. Les deux fichiers de clé sont privés (`0600`) et le secret n’est pas affiché. L’opération ne lance aucun service. `up` construit et lance uniquement le stockage. Le script utilise `python-dotenv`, déjà fourni dans le venv du gateway ; `deploy.sh` sélectionne cet interpréteur lorsqu’il est installé.

La clé ne doit pas figurer dans le `.env` racine, chargé également par le runner, ni dans `home-runner/.env`. Les synchronisations de configuration filtrent les paramètres cloud et préservent ceux du gateway. `deploy.sh start` retire aussi la clé de l’environnement transmis au processus runner.

Cette séparation évite de transmettre inutilement le secret au runner ; elle **ne l’isole pas du même compte Unix**. Un runner disposant d’un outil shell peut encore lire `gateway/.env` et `deploy/cloud.env` si ces fichiers appartiennent à son compte. Une isolation réelle exige des comptes distincts et des permissions adaptées pour le runner et le couple gateway/stockage. Le conteneur non privilégié ne fournit pas cette séparation à lui seul.

Si une ancienne installation a copié la clé dans le `.env` racine ou du runner, `cloud configure` retire ces copies et génère automatiquement une nouvelle clé. Pour imposer une rotation même sans copie détectée :

```bash
./deploy.sh cloud configure --rotate-key
./deploy.sh cloud up
```

Redémarrer le gateway après cette rotation pour qu’il utilise la nouvelle clé, et également le runner si l’ancienne clé était déjà chargée dans son processus. Les jetons des appareils et les données du volume ne changent pas. Les commentaires ou valeurs déjà altérés par l’ancien script ne peuvent pas être reconstruits automatiquement : vérifier les sauvegardes si nécessaire.

Redémarrer ensuite le gateway avec son mécanisme habituel (service utilisateur Noctalia, service système ou déploiement propre au serveur). Il n’est pas nécessaire de redémarrer le runner IA pour le cloud. Le gateway doit charger les valeurs `CLOUD_ENABLED`, `CLOUD_INTERNAL_KEY` et `CLOUD_INTERNAL_URL` configurées.

Le port du stockage `8091` est publié sur **127.0.0.1 uniquement**, pour un gateway installé sur l’hôte. Le point d’entrée distant reste le gateway existant, avec HTTPS ou votre réseau privé sécurisé. Ne pas exposer `8091` sur Internet. Si le gateway est lui aussi conteneurisé, placer les deux services sur un réseau privé commun et régler `CLOUD_INTERNAL_URL` sur l’adresse du service ; la publication loopback peut alors être retirée.

Le volume Compose `jarvis-cloud-data` contient fichiers, versions, morceaux et base SQLite. Le conteneur tourne sans root, avec son système de fichiers en lecture seule, un répertoire temporaire borné et aucune capacité Linux. Le poste Noctalia n’a pas besoin de Docker ou Podman pour accéder au drive.

Les limites se règlent dans `deploy/cloud.env` : `CLOUD_QUOTA_BYTES`, `CLOUD_MAX_FILE_BYTES`, `CLOUD_CHUNK_BYTES`, `CLOUD_UPLOAD_TTL`, `CLOUD_MAX_UPLOADS` (8 par appareil) et `CLOUD_MAX_VERSIONS` (10 anciennes versions par fichier, de 0 à 10). Recréer le conteneur avec `cloud up` après modification. Le `CLOUD_INTERNAL_KEY` doit être identique côté stockage et gateway. Une rotation se fait en changeant ces deux configurations, puis en redémarrant les deux services.

L’image Python est figée par digest ; `cloud/requirements.lock` fixe aussi les dépendances indirectes du stockage. `--require-hashes` vérifie les empreintes SHA-256 des fichiers téléchargés (toutes les variantes publiées des versions retenues sont listées). Seules les roues binaires sont admises ; `pip check` valide leur cohérence pendant la construction, qui ne résout pas de dépendance supplémentaire. Lors d’une mise à jour de dépendances, reconstruire un environnement Python 3.12 propre depuis `cloud/requirements.txt`, exporter toute sa fermeture avec `pip freeze`, ajouter les SHA-256 des artefacts depuis les métadonnées des versions PyPI, relire le lock et le digest puis rejouer les tests et le parcours conteneur. Le verrouillage décrit les versions validées, pas une promesse de maintenance de sécurité automatique. [Installations reproductibles, documentation pip](https://pip.pypa.io/en/stable/topics/repeatable-installs/).

## Configuration et démarrage hors Noctalia

Avec le déploiement autonome `deploy.sh`, le **`.env` racine fait foi pour tous les réglages communs**, dont `DEPLOY_MODE`. Les `CLOUD_*` du gateway font foi pour le cloud ; ceux de la racine ne sont pas recopiés. Les variables déjà définies dans l’environnement du service, notamment par `EnvironmentFile` de systemd, gardent priorité. Les réglages absents ou illisibles à la racine peuvent provenir du composant. Sans `.env` racine, une installation par composant conserve sa configuration propre.

`setup`, l’installation et `start` synchronisent les copies sans retirer de réglage propre au composant. Un manifeste privé `.env.jarvis-managed.json` suit seulement les clés effectivement écrites par la synchronisation. Une clé supprimée à la racine est retirée de la copie uniquement si elle est suivie et si sa valeur n’a pas été modifiée manuellement. Une liaison racine illisible suspend les suppressions de clés pour cette exécution. Les anciennes copies sans manifeste sont conservées par prudence. Avant modification d’un composant, `.env.jarvis-backup` conserve sa dernière configuration en 0600 ; une ancienne clé interne est retirée de la sauvegarde du runner. Conserver ces fichiers privés. `DEPLOY_MODE` est lu avec la même priorité par `cloud-control` : environnement, racine puis gateway. Un mode racine local refuse donc `cloud configure`, même si une ancienne copie gateway dit serveur.

Au démarrage, une liaison dotenv non cloud invalide ou contenant des octets non UTF-8 est ignorée avec un avertissement indiquant fichier et ligne, sans afficher sa valeur. Les autres paramètres restent chargés. Un lien symbolique ordinaire peut être lu ; la synchronisation ne remplace pas ce lien. Les réglages cloud ou de mode mal formés restent refusés, ainsi qu’une ancienne clé cloud à retirer d’un fichier lié. Les commandes qui éditent explicitement la configuration cloud restent strictes et refusent les liens symboliques. Les valeurs modifiées conservent leurs vraies lignes multiples plutôt que des échappements JSON, conformément à [EnvironmentFile de systemd](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html#EnvironmentFile=).

Avec Noctalia, utiliser son déployeur et ses services habituels ; seules les commandes `./deploy.sh cloud configure|up|status|down` concernent le conteneur. Ne pas lancer `deploy.sh start` ou `setup` pour piloter l’installation du rice.

## Depuis Noctalia

Conserver la connexion JARVIS distante habituelle. Théo écrit le client officiel depuis le contrat API ; sa disponibilité et ses commandes sont à vérifier dans le rice. Dans le prototype local déjà installé pour les essais, l’association et l’affichage utilisent :

```bash
nc-cloud associer
nc-reglages set CLOUD_JARVIS oui
nc-cloud fenetre
```

Le trousseau Secret Service doit être disponible et déverrouillé (`secret-tool`). L’ouverture dans le gestionnaire de fichiers requiert les bindings Python Gio et le backend WebDAV de GVfs. `nc-cloud verifier` contrôle les commandes et bindings disponibles ; si le montage échoue malgré ces composants, vérifier aussi la présence du backend WebDAV de GVfs dans les paquets de votre distribution.

`nc-cloud ouvrir` monte le drive à la demande avec Gio, puis ajoute le favori « Cloud JARVIS » sans toucher aux autres favoris. L’accès natif est en lecture seule. Les envois passent par le glisser-déposer dans la barre/fenêtre, `nc-cloud deposer /chemin/fichier`, ou « Scripts → Envoyer vers Cloud JARVIS » dans Nautilus.

Le prototype utilise une icône près du centre de la barre. L’intégration officielle prévue par Théo utilise l’île elle-même comme zone de dépôt, puis ajoutera l’ouverture dans Fichiers ; les commandes ci-dessus ne promettent pas que cette interface officielle est déjà disponible.

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

Chaque fichier conserve au plus dix anciennes versions, selon `CLOUD_MAX_VERSIONS`. Les plus anciennes sont retirées lors d’un remplacement. Si un historique existant dépasse la limite au démarrage, le service refuse de le réduire sans confirmation explicite. **Cette réduction est définitive : sauvegarder avant mise à jour.** Après sauvegarde, confirmer la limite actuellement configurée dans `deploy/cloud.env` puis relancer :

```bash
./deploy.sh cloud configure --confirm-history-prune
./deploy.sh cloud up
```

Cette commande enregistre `CLOUD_CONFIRMED_HISTORY_LIMIT` ; une autre limite exige une nouvelle confirmation. La valeur `0` autorise la suppression de toutes les anciennes versions, en préservant les versions courantes. Une confirmation permet la réduction au démarrage, y compris lors d’une migration ; sans elle, aucune version excédentaire n’est supprimée. La version courante n’expire jamais automatiquement. La suppression explicite sous `If-Match` retire le fichier et toutes ses versions ; elle est annoncée par `features.file_delete` et doit être proposée avec confirmation côté client.

Les reçus de publication et de suppression restent conservés. Les morceaux temporaires sont retirés après publication, annulation, révocation ou invalidation. Une tâche interne au stockage vérifie les expirations et reprend les nettoyages différés toutes les 30 secondes. Un objet impossible à effacer ne bloque pas les suivants : chacun a sa propre transaction et la sélection des lots tourne pour ne pas affamer les autres objets. Les ouvertures de téléchargement prennent un verrou fichier partagé avec le nettoyage, sans verrou d’écriture SQLite ; les descripteurs déjà ouverts restent valides après effacement. Une erreur inattendue est journalisée et le cycle suivant réessaie, au lieu de tuer silencieusement la tâche. Ce mécanisme est indépendant du client et de l’icône Noctalia.

Les objets retirés après suppression ou réduction d’historique passent par une file persistante. `used_bytes` est le quota logique des versions référencées ; `garbage_bytes` est l’espace des objets retirés restant à récupérer, encore inclus dans `reserved_bytes`. Si un effacement disque échoue, cet espace n’est pas annoncé comme récupéré et reste chargé jusqu’au succès. Un téléchargement déjà ouvert peut se terminer même si l’objet est ensuite retiré du répertoire.

Un ensemble borné de 64 verrous protège les transferts : deux identifiants peuvent partager un verrou et être sérialisés, sans compromettre l’intégrité. Le nettoyage ne supprime pas un morceau encore utilisé par un assemblage. En cas d’échec du nettoyage, sa réservation reste comptée pour empêcher de remplir le disque par des annulations répétées ; le service réessaie. Les reçus d’idempotence restent disponibles après suppression des morceaux, avec `chunks=[]` pour un état terminal.

Le schéma SQLite passe automatiquement de la version 1 ou 2 à la version 3. Les identités, jetons, versions courantes et reçus sont préservés ; la réduction d’un historique excédentaire exige la confirmation décrite ci-dessus. Les anciens reçus sans morceaux présents ne réservent plus inutilement leur taille complète. Sauvegarder le volume avant mise à jour : les anciens binaires refusent le schéma 3 et ne constituent pas un retour arrière compatible.

Les objets complets orphelins d’un arrêt brutal restent récupérables par la maintenance **stockage arrêté**, par exemple avec Docker Compose :

```bash
docker compose --env-file deploy/cloud.env -f deploy/cloud.compose.yaml stop jarvis-cloud
docker compose --env-file deploy/cloud.env -f deploy/cloud.compose.yaml run --rm --no-deps jarvis-cloud \
  python -c 'from cloud.storage import Store, Settings; Store(Settings.environment()).maintenance()'
docker compose --env-file deploy/cloud.env -f deploy/cloud.compose.yaml up -d jarvis-cloud
```

Ne pas exécuter `maintenance()` pendant des transferts : son nettoyage des objets complets orphelins suppose l’absence de commits en cours. Le nettoyage préserve les versions publiées et les reçus d’idempotence. Il n’est plus nécessaire d’arrêter le service pour récupérer les morceaux des transferts terminés ou expirés.

Pour une sauvegarde cohérente, arrêter le stockage, puis exporter **tout le volume** (base SQLite, fichiers WAL éventuels, objets et états), avant de relancer le stockage. Utiliser le mécanisme de sauvegarde de volumes de votre moteur de conteneurs. Conserver séparément les configurations privées du gateway et du service. Restaurer vers un volume distinct à l’arrêt et tester des téléchargements avant de l’utiliser comme stockage principal. Le test de persistance automatisé recrée le moteur sur le même volume ; il ne remplace pas un essai de votre outil de sauvegarde.

`./deploy.sh cloud down` arrête et supprime le conteneur sans effacer le volume. Ne pas ajouter `--volumes` à la commande Compose pour une désactivation ordinaire. Désactiver aussi `CLOUD_ENABLED=0` dans le gateway et le redémarrer si le cloud n’est plus utilisé. Un changement de mode n’efface pas les données.

## Tests de développement

```bash
python3 -m venv /tmp/jarvis-cloud-tests
/tmp/jarvis-cloud-tests/bin/pip install -r cloud/requirements.txt httpx==0.27.2 python-dotenv==1.0.1 pytest
/tmp/jarvis-cloud-tests/bin/python -m pytest -q tests/test_cloud.py tests/test_cloud_proxy.py tests/test_cloud_security.py tests/test_cloud_followup.py
```

Les tests utilisent des dossiers temporaires et un serveur HTTP loopback. Aucun bot, moteur IA ou serveur de production n’est démarré. Le conteneur peut se construire séparément avec `podman build -f cloud/Dockerfile -t localhost/jarvis-cloud:test .`.


Un appareil associé possède des droits de suppression définitive sur l’espace personnel. Révoquer immédiatement un appareil perdu ou un jeton compromis ; conserver des sauvegardes indépendantes. L’historique plafonné peut être évincé par des remplacements répétés et ne protège pas contre le vol d’un jeton.

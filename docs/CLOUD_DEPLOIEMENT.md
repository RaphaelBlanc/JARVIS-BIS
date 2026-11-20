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

`cloud up` recrée explicitement le conteneur pour appliquer l’image et les réglages, avec une brève interruption du stockage. Le volume est conservé. Pour Podman Compose 1.3, le contrôleur fournit les valeurs numériques par défaut dans l’environnement du processus Compose : les réglages de `deploy/cloud.env` et les variables explicites restent prioritaires. Cela évite que cette version transmette littéralement les expressions `${CLOUD_…:-…}` au conteneur. Utiliser les commandes `deploy.sh cloud` pour bénéficier de cette compatibilité.

Avec Podman sans root, après le premier `cloud up`, installer aussi le service utilisateur pour retrouver le conteneur après redémarrage du serveur :

```bash
mkdir -p ~/.config/systemd/user
cp systemd/assistant-cloud-user.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now assistant-cloud-user.service
sudo loginctl enable-linger "$USER"
```

Le service démarre le conteneur existant sans reconstruction et l’arrête proprement avec la session système. Adapter `CLOUD_CONTAINER` dans l’unité si le nom du projet Compose a changé. Vérifier avec `systemctl --user status assistant-cloud-user.service` et `./deploy.sh cloud status`. Avant une désactivation durable avec `cloud down`, désactiver d’abord cette unité avec `systemctl --user disable --now assistant-cloud-user.service`.

Le port du stockage `8091` est publié sur **127.0.0.1 uniquement**, pour un gateway installé sur l’hôte. Le point d’entrée distant reste le gateway existant, avec HTTPS ou votre réseau privé sécurisé. Ne pas exposer `8091` sur Internet. Si le gateway est lui aussi conteneurisé, placer les deux services sur un réseau privé commun et régler `CLOUD_INTERNAL_URL` sur l’adresse du service ; la publication loopback peut alors être retirée.

Le volume Compose `jarvis-cloud-data` contient fichiers, versions, morceaux et base SQLite. Le conteneur tourne sans root, avec son système de fichiers en lecture seule, un répertoire temporaire borné et aucune capacité Linux. Le poste Noctalia n’a pas besoin de Docker ou Podman pour accéder au drive.

Les limites se règlent dans `deploy/cloud.env` : `CLOUD_QUOTA_BYTES`, `CLOUD_MAX_FILE_BYTES`, `CLOUD_CHUNK_BYTES`, `CLOUD_UPLOAD_TTL`, `CLOUD_MAX_UPLOADS` (8 par appareil) et `CLOUD_MAX_VERSIONS` (10 anciennes versions par fichier, de 0 à 10). Recréer le conteneur avec `cloud up` après modification. Le `CLOUD_INTERNAL_KEY` doit être identique côté stockage et gateway. Une rotation se fait en changeant ces deux configurations, puis en redémarrant les deux services.

L’image Python est figée par digest ; `cloud/requirements.lock` fixe aussi les dépendances indirectes du stockage. `--require-hashes` vérifie les empreintes SHA-256 des fichiers téléchargés (toutes les variantes publiées des versions retenues sont listées). Seules les roues binaires sont admises ; `pip check` valide leur cohérence pendant la construction, qui ne résout pas de dépendance supplémentaire. Lors d’une mise à jour de dépendances, reconstruire un environnement Python 3.12 propre depuis `cloud/requirements.txt`, exporter toute sa fermeture avec `pip freeze`, ajouter les SHA-256 des artefacts depuis les métadonnées des versions PyPI, relire le lock et le digest puis rejouer les tests et le parcours conteneur. Le verrouillage décrit les versions validées, pas une promesse de maintenance de sécurité automatique. [Installations reproductibles, documentation pip](https://pip.pypa.io/en/stable/topics/repeatable-installs/).

## Configuration et démarrage hors Noctalia

Avec le déploiement autonome `deploy.sh`, le **`.env` racine fait foi pour tous les réglages communs**, dont `DEPLOY_MODE`. Les `CLOUD_*` du gateway font foi pour le cloud ; ceux de la racine ne sont pas recopiés. Les variables déjà définies dans l’environnement du service, notamment par `EnvironmentFile` de systemd, gardent priorité. Les réglages absents ou illisibles à la racine peuvent provenir du composant. Sans `.env` racine, une installation par composant conserve sa configuration propre.

`setup`, l’installation et `start` synchronisent les copies sans retirer de réglage propre au composant. Un manifeste privé `.env.jarvis-managed.json` suit seulement les clés effectivement écrites par la synchronisation. Une clé supprimée à la racine est retirée de la copie uniquement si elle est suivie et si sa valeur n’a pas été modifiée manuellement. Une liaison racine illisible suspend les suppressions de clés pour cette exécution. Les anciennes copies sans manifeste sont conservées par prudence. Avant modification d’un composant, `.env.jarvis-backup` conserve sa dernière configuration en 0600 ; une ancienne clé interne est retirée de la sauvegarde du runner. Conserver ces fichiers privés. `DEPLOY_MODE` est lu avec la même priorité par `cloud-control` : environnement, racine puis gateway. Un mode racine local refuse donc `cloud configure`, même si une ancienne copie gateway dit serveur.

Au démarrage, une liaison dotenv non cloud invalide ou contenant des octets non UTF-8 est ignorée avec un avertissement indiquant fichier et ligne, sans afficher sa valeur. Les autres paramètres restent chargés. Un lien symbolique ordinaire peut être lu ; la synchronisation ne remplace pas ce lien. Les réglages cloud ou de mode mal formés restent refusés, ainsi qu’une ancienne clé cloud à retirer d’un fichier lié. Les commandes qui éditent explicitement la configuration cloud restent strictes et refusent les liens symboliques. Les valeurs modifiées conservent leurs vraies lignes multiples plutôt que des échappements JSON, conformément à [EnvironmentFile de systemd](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html#EnvironmentFile=).

Un manifeste de synchronisation illisible, non UTF-8 ou de structure invalide est ignoré avec un avertissement, sans afficher son contenu et sans retirer de clé suivie. Cela permet au gateway et au runner de démarrer en conservant leurs réglages. Une synchronisation suivante reconstruit le suivi pour les valeurs qu’elle écrit ; les anciennes clés dont le suivi est perdu restent conservées. Un manifeste lié symboliquement reste refusé.

Avec Noctalia, utiliser son déployeur et ses services habituels ; seules les commandes `./deploy.sh cloud configure|up|status|down` concernent le conteneur. Ne pas lancer `deploy.sh start` ou `setup` pour piloter l’installation du rice.

## Depuis Noctalia

Conserver la connexion JARVIS distante habituelle. Le client terminal officiel est disponible dans Noctalia depuis `2cd7b0d` ; voir son [guide cloud](https://github.com/abcdef123456-wq/noctalia-config/blob/2cd7b0d04ff9ae0763670eb6e57d5bcba7486805/docs/cloud.md). Pour ce client : `nc-cloud verifier`, `nc-cloud associer`, `nc-cloud etat`, puis `nc-cloud lister`. L’interface officielle reste à intégrer. Dans le prototype local déjà installé pour les essais, l’association et l’affichage utilisent :

```bash
nc-cloud associer
nc-reglages set CLOUD_JARVIS oui
nc-cloud fenetre
```

Le trousseau Secret Service doit être disponible et déverrouillé (`secret-tool`). L’ouverture dans le gestionnaire de fichiers requiert les bindings Python Gio et le backend WebDAV de GVfs. `nc-cloud verifier` contrôle les commandes et bindings disponibles ; si le montage échoue malgré ces composants, vérifier aussi la présence du backend WebDAV de GVfs dans les paquets de votre distribution.

`nc-cloud ouvrir` monte le drive à la demande avec Gio, puis ajoute le favori « Cloud JARVIS » sans toucher aux autres favoris. Par défaut, l’accès natif est en lecture seule ; l’option d’écriture est décrite plus bas. Les envois passent par le glisser-déposer dans la barre/fenêtre, `nc-cloud deposer /chemin/fichier`, ou « Scripts → Envoyer vers Cloud JARVIS » dans Nautilus.

Le prototype local intègre désormais l’icône à la pilule et accepte le dépôt sur l’île elle-même. L’intégration officielle reste maintenue par Théo ; les commandes ci-dessus décrivent le prototype et ne promettent pas que cette interface officielle soit déjà disponible.

## Vérifications avec deux PC

### Gestion complète depuis Fichiers (option explicite)

Pour autoriser aussi création, déplacement et suppression dans le montage de Super + E :

```bash
./deploy.sh cloud configure --dav-write
./deploy.sh cloud up
```

Redémarrer le gateway, puis démonter et remonter le seul drive Cloud JARVIS pour que GVfs relise les capacités. La configuration est conservée dans les fichiers privés du gateway et du stockage. `--dav-read-only` revient au comportement initial, avec la même recréation et le même redémarrage. L’option ne modifie pas les jetons, les autres services ni les données du volume.

Les remplacements DAV conservent l’historique ; les suppressions retirent définitivement le fichier et ses versions. Les déplacements vers un nom occupé sont refusés. Une application qui ne transmet pas son ancienne révision ne permet pas de détecter une copie éditée depuis longtemps ; la fenêtre Cloud contrôle explicitement les révisions. Voir les détails et limites dans le contrat API.

Le prototype local fournit sélection et double-clic, ouverture d’une copie locale vérifiée par SHA-256, création de fichier vide et de dossier, renommage, déplacement avec choix du dossier et suppression confirmée. Il ne republie pas automatiquement les modifications d’une copie locale ouverte : la déposer à nouveau pour les publier. Les fichiers ouverts directement depuis le montage Fichiers peuvent être enregistrés via WebDAV si l’option est activée et si l’application gère les fichiers distants.

Le bouton cloud et la zone de dépôt sont intégrés à l’île ; la fenêtre garde l’écran choisi à son ouverture. Le client officiel reste maintenu séparément par Théo.

### Parcours entre appareils

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

La confirmation refuse une valeur `CLOUD_MAX_VERSIONS` non entière ou hors de 0 à 10 avec une erreur explicite, sans trace Python et sans modifier les fichiers de configuration.

Chaque fichier conserve au plus dix anciennes versions, selon `CLOUD_MAX_VERSIONS`. Les plus anciennes sont retirées lors d’un remplacement. Si un historique existant dépasse la limite au démarrage, le service refuse de le réduire sans confirmation explicite. **Cette réduction est définitive : sauvegarder avant mise à jour.** Après sauvegarde, confirmer la limite actuellement configurée dans `deploy/cloud.env` puis relancer :

```bash
./deploy.sh cloud configure --confirm-history-prune
./deploy.sh cloud up
```

Cette commande enregistre `CLOUD_CONFIRMED_HISTORY_LIMIT` ; une autre limite exige une nouvelle confirmation. La valeur `0` autorise la suppression de toutes les anciennes versions, en préservant les versions courantes. Une confirmation permet la réduction au démarrage, y compris lors d’une migration ; sans elle, aucune version excédentaire n’est supprimée. La version courante n’expire jamais automatiquement. La suppression explicite sous `If-Match` retire le fichier et toutes ses versions ; elle est annoncée par `features.file_delete` et doit être proposée avec confirmation côté client.

Les reçus de publication et de suppression restent conservés. Les morceaux temporaires sont retirés après publication, annulation, révocation ou invalidation. Une tâche interne au stockage vérifie les expirations et reprend les nettoyages différés toutes les 30 secondes. Un objet impossible à effacer ne bloque pas les suivants : chacun a sa propre transaction et la sélection des lots tourne pour ne pas affamer les autres objets. Les ouvertures de téléchargement prennent un verrou fichier partagé avec le nettoyage, sans verrou d’écriture SQLite ; les descripteurs déjà ouverts restent valides après effacement. Une erreur inattendue est journalisée et le cycle suivant réessaie, au lieu de tuer silencieusement la tâche. Ce mécanisme est indépendant du client et de l’icône Noctalia.

Les objets retirés après suppression ou réduction d’historique passent par une file persistante. `used_bytes` est le quota logique des versions référencées ; `garbage_bytes` est l’espace des objets retirés restant à récupérer, encore inclus dans `reserved_bytes`. Si un effacement disque échoue, cet espace n’est pas annoncé comme récupéré et reste chargé jusqu’au succès. Un téléchargement déjà ouvert peut se terminer même si l’objet est ensuite retiré du répertoire.

Si SQLite est occupé par un écrivain, le nettoyage ne patiente pas en gardant le verrou exclusif des objets : sa transaction utilise un délai d’attente nul et reporte l’effacement au cycle suivant. L’ordre des verrous reste objets puis SQLite, comme pour les ouvertures de lecture. Les objets reportés restent dans la file et comptés dans les réservations ; les téléchargements peuvent s’ouvrir pendant cette attente d’écriture.

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

Pour vérifier le client officiel sans le modifier, avec le vrai conteneur, l’adaptateur `nc_gateway` et un vrai trousseau jetable : installer les dépendances Python du gateway dans un environnement de test, ainsi que Podman, `dbus-run-session`, `gnome-keyring-daemon` et `secret-tool`. Fournir une copie de la révision Noctalia à tester :

```bash
podman build -f cloud/Dockerfile -t localhost/jarvis-cloud:review .
PYTHONPATH=.:gateway python scripts/check-cloud-noctalia.py \
  --noctalia-root /chemin/vers/noctalia-config \
  --image localhost/jarvis-cloud:review
```

Le script lance sa propre session D-Bus et son trousseau, isole HOME et XDG, crée un volume nommé et un conteneur temporaires puis les retire. Il conserve le magasin Podman de l’utilisateur pour trouver l’image construite. Les appels client et le gateway utilisent uniquement des configurations jetables et des ports loopback ; le lifespan du gateway reste désactivé pour ne lancer aucun bot. Les coupures sont injectées à la frontière HTTP publique : les morceaux acceptés, publications, fichiers, versions et suppressions sont traités par le vrai stockage.

Deux parcours supplémentaires sont disponibles, à lancer séparément :

```bash
PYTHONPATH=.:gateway python scripts/check-cloud-noctalia.py \
  --noctalia-root /chemin/vers/noctalia-config --https
PYTHONPATH=.:gateway python scripts/check-cloud-noctalia.py \
  --noctalia-root /chemin/vers/noctalia-config --gvfs
```

`--https` requiert OpenSSL. Il génère un certificat temporaire pour `localhost`, ajoute cette confiance uniquement aux processus clients de l’essai et rejoue le parcours complet en TLS, y compris WebDAV. Deux contrôles négatifs vérifient qu’un certificat inconnu et un mauvais nom d’hôte échouent avant toute requête HTTP applicative. Cela ne valide pas le certificat ou le reverse proxy du serveur personnel.

`--gvfs` requiert `/usr/bin/python3` avec les bindings Gio, le démon GVfs et son backend WebDAV. Il monte le drive réel via une opération Gio avec authentification, liste un nom Unicode, relit le contenu et vérifie son SHA-256, constate le refus d’écriture puis démonte. Le démon isolé utilise `--no-fuse` pour ne pas projeter ce montage sur le répertoire FUSE du bureau. Le script met aussi à jour l’environnement d’activation de sa seule session D-Bus jetable, sans `--systemd`, pour isoler les services activés à la demande. L’essai valide le backend natif utilisé par Fichiers, pas son interface graphique ni le favori officiel Noctalia.

Après mise à jour du serveur personnel et du client officiel, vérifier `nc-cloud verifier` puis `nc-cloud etat` avec l’adresse HTTPS ou Tailscale réelle. Associer chaque PC s’il ne l’est pas déjà, déposer un fichier de test depuis le premier, le télécharger depuis le second et comparer les SHA-256. Un ping Tailscale réussi ne valide que la connectivité ; si `/api/cloud/v1/capabilities` répond 404, l’API cloud n’est pas disponible à cette adresse et le parcours distant ne peut pas encore être validé. Les réglages, le panneau et le dépôt sur l’île sont fournis par l’intégration officielle Noctalia.

Pour le mode d’écriture, `python scripts/check-cloud-gvfs-write.py` lance un stockage et un gateway jetables en boucle locale, puis un vrai montage Gio/GVfs dans une session D-Bus isolée. Le parcours vérifie création, lecture, remplacement et refus d’une ancienne révision, copie depuis un fichier local, renommage, déplacement et suppression, sans utiliser le serveur ou le trousseau personnel. Prérequis : dépendances Python gateway/cloud, Python système avec Gio, backend GVfs WebDAV et D-Bus.


Un appareil associé possède des droits de suppression définitive sur l’espace personnel. Révoquer immédiatement un appareil perdu ou un jeton compromis ; conserver des sauvegardes indépendantes. L’historique plafonné peut être évincé par des remplacements répétés et ne protège pas contre le vol d’un jeton.

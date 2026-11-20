# Plan d’implémentation du cloud JARVIS

Créer un stockage persistant sur le serveur distant JARVIS BIS, commun aux PC autorisés, accessible depuis le gestionnaire de fichiers et par glisser-déposer dans la barre Noctalia. Le cloud est une option du déploiement `server` et reste désactivé en déploiement `local`.

Ce plan prépare l’implémentation dans JARVIS BIS, puis la transmission à Théo d’une issue accompagnée de patchs Noctalia applicables. Les changements fonctionnels et la publication constituent les étapes suivantes.

## Périmètre de la première version

- Un espace commun pour un propriétaire et ses différents PC, stocké uniquement sur le serveur.
- Une entrée « Cloud JARVIS » dans le gestionnaire de fichiers ouvert par `Super + E`.
- Une icône cloud dans la barre, avec dépôt de plusieurs fichiers, choix du dossier distant, création d’un dossier, progression, annulation et résultat par fichier.
- Des transferts binaires par morceaux, une reprise explicite après interruption et une protection contre les écrasements concurrents.
- Une désactivation effective côté serveur en mode local, même si un ancien client tente encore d’appeler les routes cloud.

La première version ne synchronise pas de dossier local et n’accepte pas de nouveaux dépôts hors ligne. L’accès aux disques des autres PC, l’édition collaborative, les liens publics, le partage entre utilisateurs et l’envoi récursif de dossiers viendront séparément. Les dossiers distants restent navigables et créables.

## Points d’intégration vérifiés

Inspection des dépôts locaux le 20 novembre 2026 :

| Dépôt | Révision inspectée | Points à utiliser |
| --- | --- | --- |
| `RaphaelBlanc/JARVIS-BIS` | `6041caf630606d3b1b2deb3f3447f839e1637206` | `core/config.py` définit `DEPLOY_MODE` ; `gateway/main.py` expose `/api/status` avec `deploy_mode` et authentifie les routes existantes avec `require_secret` |
| `abcdef123456-wq/noctalia-config` | `2a9f1c8e72963e08a8a79762b327459b079c7ea5` | `nc-jarvis`, `nc-assistant`, l’île Quickshell et le déployeur local/SSH fournissent les points d’entrée |

`home/.config/quickshell/nc-ile/Assistant.qml` sait déjà recevoir des URL de fichiers par `DropArea`. Cette zone sert aux pièces jointes du chat : le cloud aura sa propre zone et son propre traitement.

`home/.local/share/nc-jarvis/server/nc_gateway.py` conserve l’application upstream, remplace certaines routes et adapte son cycle de vie. Le déployeur référence une révision initiale épinglée et une liste de révisions acceptées. Il faudra tester le cloud avec cet adaptateur et faire valider le nouveau commit par Théo.

Les chemins nouveaux indiqués ci-dessous sont proposés. Avant de produire les patchs, actualiser les références Git des deux dépôts et utiliser une branche ou un worktree dédié ; ne pas modifier les fichiers installés du bureau.

## Architecture retenue

Créer un service `jarvis-cloud` dans un conteneur séparé, avec une API de stockage et un adaptateur WebDAV. Le gateway JARVIS reste le point d’entrée réseau public et relaie les flux sans les charger entièrement en mémoire. Le runner et les modèles IA ne participent pas aux transferts.

```text
Gestionnaire de fichiers ── WebDAV ──┐
                                    ├── HTTPS / réseau privé sécurisé
Barre et fenêtre Cloud ── nc-cloud ──┘              │
                                                   ▼
                                           Gateway JARVIS
                                                   │ réseau interne
                                                   ▼
                                      Conteneur jarvis-cloud
                                       ├── API de transferts
                                       ├── adaptateur WebDAV
                                       └── volume persistant
```

L’API et WebDAV utilisent le même moteur de stockage et les mêmes règles de publication. Un partage direct du répertoire de données contournerait ces règles : il ne sera pas exposé.

Utiliser une bibliothèque WebDAV existante avec un fournisseur adapté au stockage, plutôt que réimplémenter le protocole. WsgiDAV fournit cette extension ; son intégration et les comportements réels de GVfs seront validés dans le premier lot. [Fournisseurs WsgiDAV](https://wsgidav.readthedocs.io/en/stable/user_guide_configure.html)

Un volume contiendra une base SQLite de métadonnées, des objets de contenu immuables, les morceaux temporaires et les états des transferts. La publication d’un objet complet passe par une transaction de métadonnées. Le volume utilise un disque local du serveur ; une base SQLite sur un partage réseau ne fait pas partie de cette version.

Ce choix ajoute du code de stockage à maintenir, mais permet une politique identique pour les deux interfaces. Le lot initial devra confirmer que ce périmètre reste raisonnable avant de développer toute l’interface.

## Activation et états visibles

Ajouter `CLOUD_ENABLED`, désactivé par défaut. L’activation effective exige `DEPLOY_MODE=server` et `CLOUD_ENABLED=1`. Le paramétrage et le lancement du conteneur restent explicites ; une mise à jour du rice ne déploie aucun service sur le serveur.

Étendre `/api/status` avec un objet versionné `cloud` : `api_version`, `enabled`, `state`, `reason`, `server_id` et les URL relatives des points d’entrée. Les détails d’un espace et ses limites ne sont retournés qu’après authentification cloud. Un ancien serveur sans objet `cloud` est traité comme incompatible.

| Situation | Serveur | Comportement Noctalia |
| --- | --- | --- |
| Mode local | `enabled=false`, raison `local_deployment` ; routes cloud refusées | Pas d’icône, pas de montage, pas de tâche cloud |
| Serveur sans option cloud | `enabled=false`, raison `not_configured` | Fonction masquée ; explication dans les réglages |
| Cloud disponible | Capacité compatible et accès de l’appareil valide | Icône active et accès au drive |
| Serveur ou stockage injoignable | Erreur bornée ; aucun blocage du chat | Icône grisée si le cloud était configuré, message « Cloud hors ligne » |
| Identifiants invalides ou appareil révoqué | `401` ou `403` | Demande de reconnexion ; aucun nouvel envoi |
| Version cloud inconnue | Incompatibilité explicite | Fonction indisponible jusqu’à mise à jour |

Ne pas déduire le mode de déploiement de l’adresse IP : un serveur distant peut être joint par un tunnel local. Le serveur fournit le mode, le client mémorise l’identité de ce serveur.

Lors d’un changement de connexion JARVIS, isoler les états par `server_id` et `space_id`, retirer le montage de l’ancienne connexion et ne jamais rediriger un transfert vers le nouveau serveur. Un basculement du serveur en mode local coupe aussi les anciennes sessions cloud.

## Hors ligne et interruptions

Pour un nouveau dépôt hors ligne, informer immédiatement l’utilisateur : « Cloud hors ligne. Aucun fichier envoyé. Réessaie après reconnexion. » Les fichiers d’origine restent en place. Le client ne crée pas de file d’envoi différé.

Le contrôle de connexion à l’ouverture de la fenêtre ne suffit pas : chaque requête traite également les coupures, expirations et refus du serveur. Les sondes ont un délai court et sont déclenchées à l’ouverture, au retour du réseau et sur action « Réessayer ». Les reprises automatiques de sondes sont bornées et ne deviennent pas une boucle permanente en arrière-plan.

Pour un transfert déjà commencé :

1. Conserver un manifeste privé avec `server_id`, `space_id`, `upload_id`, clé d’idempotence, destination, révision attendue et identité du fichier source.
2. En cas de coupure, passer à « Interrompu » ou « Résultat à vérifier » si la publication a pu réussir. Ne pas annoncer un échec certain lorsque seule la réponse s’est perdue.
3. Au retour du réseau, interroger le statut de la même opération. Une opération déjà publiée retourne son résultat initial.
4. Si elle est incomplète, proposer « Reprendre » ; vérifier les morceaux reçus et que la source locale est toujours la même avant tout nouvel envoi.
5. Si la source a changé, a disparu ou si la session a expiré, expliquer le problème et demander un nouveau dépôt. Ne pas reprendre des morceaux de fichiers différents.
6. À la publication, revérifier la destination et la révision distante. Un changement provoque un conflit, même si le transfert avait commencé avant la coupure.

Un redémarrage du PC ne relance pas les envois tout seul. Le journal conserve des métadonnées de reprise, sans copier les fichiers dans un cache de synchronisation. Les temporaires distants expirent selon une durée configurable.

## Conflits entre PC et publication atomique

L’horloge des PC ne décide jamais de la version gagnante. Chaque fichier et dossier possède un identifiant stable et une révision opaque produite par le serveur.

- **Création** : publier seulement si le nom est encore libre dans le dossier cible. Une contrainte d’unicité vérifiée en transaction arbitre deux créations simultanées.
- **Remplacement** : exiger la révision présentée à l’utilisateur et la comparer dans la même transaction que la publication. Deux remplacements de la même révision ne peuvent pas tous deux réussir.
- **Conflit** : proposer « Conserver les deux », « Remplacer la version affichée » et « Annuler ». Un nouveau remplacement se base sur la révision actualisée ; il peut encore échouer si un autre PC écrit entre-temps.
- **Conserver les deux** : réserver un nouveau nom côté serveur, par exemple `rapport (portable-2).pdf`. La génération de ce nom est elle aussi transactionnelle.
- **Dossier déplacé ou supprimé** : vérifier l’identité et la révision du parent avant publication, puis demander de confirmer à nouveau la destination si elle a changé.
- **Coupure** : les morceaux ne deviennent jamais des fichiers visibles. L’ancienne version reste accessible jusqu’à validation de la nouvelle.

Les objets de contenu sont écrits et synchronisés sur disque avant de référencer la nouvelle version dans une transaction SQLite. Conserver la précédente version lors d’un remplacement, avec une politique de rétention documentée. Après un crash, une récupération identifie les objets non référencés et les transferts incomplets ; elle ne supprime pas les objets encore utilisés par un transfert actif ou une version conservée.

Réserver le quota des transferts en cours dans la base pour que plusieurs PC ne dépassent pas ensemble la limite. Vérifier aussi l’espace disque réel. L’identifiant d’opération est unique dans le périmètre de l’appareil et de l’espace ; sa réutilisation avec un contenu ou une destination différente est refusée.

Les préconditions HTTP `If-Match` et `If-None-Match` permettent d’exprimer ces protections. Une révision périmée produit `412` ; l’absence d’une précondition obligatoire produit `428`. [RFC 9110, préconditions](https://www.rfc-editor.org/rfc/rfc9110.html#section-13), [RFC 6585, code 428](https://www.rfc-editor.org/rfc/rfc6585.html#section-3)

### Même protection dans le gestionnaire de fichiers

WebDAV fournit navigation, téléchargement et création de nouveaux fichiers/dossiers. Une création sans précondition fournie par le client reste strictement une création : si la destination existe déjà, le serveur refuse l’écrasement.

Un remplacement exige une précondition portant sur la version concernée. Un client qui ne sait pas la fournir reçoit une erreur et doit utiliser « Envoyer vers Cloud JARVIS » pour obtenir le dialogue de conflit. Ne pas promettre l’édition transparente depuis toutes les applications.

Toute opération WebDAV supplémentaire, notamment `MOVE`, `COPY`, `DELETE` et les sauvegardes par fichier temporaire suivi d’un renommage, doit passer par les mêmes contrôles. Les méthodes non couvertes sont refusées. Un verrou WebDAV ne constitue pas à lui seul une protection contre une mise à jour perdue ; Nextcloud distingue aussi les verrous de transaction de la gestion des éditions concurrentes. [Verrouillage Nextcloud](https://docs.nextcloud.com/server/stable/admin_manual/configuration_files/files_locking_transactional.html)

Si l’essai GVfs ne permet pas d’offrir les écritures prévues avec ces garanties, livrer explicitement l’accès natif en lecture seule et les écritures par la fenêtre cloud ; ne pas activer un montage qui autorise les écrasements silencieux.

## Authentification et association des PC

Le système actuel authentifie plusieurs clients avec une clé partagée. Un `node_id` reçu dans une requête et une connexion WebSocket active ne prouvent pas, à eux seuls, l’identité durable d’un appareil.

Créer un enregistrement cloud persistant par appareil : identifiant, nom affiché, espace autorisé, empreinte du jeton et état de révocation. L’association est réalisée avec l’accès d’administration JARVIS ou un ticket d’association à usage unique et de courte durée. Elle ne dépend pas d’une conversation IA, d’un jeton d’origine de conversation ou du lancement de `nc-noeud`.

Le client reçoit un jeton limité au cloud, révocable sans changer la clé générale JARVIS. Utiliser un identifiant opaque par appareil pour WebDAV et le même périmètre de droits pour l’API. Le trousseau de session conserve le secret WebDAV ; les fichiers de connexion privés restent en `0600` et sont exclus des exports du rice. Aucun jeton dans les URL, arguments de commande, QML, patchs ou journaux.

Les routes de transfert acceptent le jeton cloud et n’exposent pas l’administration JARVIS. Le gateway supprime les en-têtes d’identité fournis par le client ; le service interne ne fait confiance qu’au gateway authentifié. Les chemins sont résolus dans l’espace autorisé, sans liens symboliques, traversée `..`, caractères de contrôle ou accès direct au volume.

## Contrat API proposé

Figer les schémas JSON et le format des erreurs dans `docs/CLOUD_API.md` avant de coder l’interface. Toutes les réponses concernées incluent la version du contrat et l’identité du serveur ou de l’espace. Les listes sont paginées.

| Route publique | Usage |
| --- | --- |
| `GET /api/status` | Mode JARVIS et capacité cloud ; conserve l’authentification existante |
| `POST /api/cloud/v1/devices` | Association avec authentification d’administration ; secret retourné une seule fois |
| `DELETE /api/cloud/v1/devices/{id}` | Révocation administrative d’un appareil |
| `GET /api/cloud/v1/status` | Accessibilité du stockage, appareil, espace, quota, limites et capacités |
| `GET /api/cloud/v1/entries?parent_id=…&cursor=…` | Arborescence et révisions |
| `POST /api/cloud/v1/directories` | Création atomique d’un dossier |
| `GET /api/cloud/v1/files/{id}/content` | Téléchargement avec révision et prise en charge des requêtes partielles |
| `POST /api/cloud/v1/uploads` | Réserver un transfert, son quota et sa politique de conflit |
| `PUT /api/cloud/v1/uploads/{id}/chunks/{index}` | Déposer un morceau binaire borné, indexé et vérifié par empreinte |
| `GET /api/cloud/v1/uploads/{id}` | Morceaux reçus, expiration et résultat éventuel |
| `POST /api/cloud/v1/uploads/{id}/commit` | Vérifier taille et empreinte complètes, puis publier sous précondition |
| `DELETE /api/cloud/v1/uploads/{id}` | Annuler un transfert non publié et libérer sa réservation |
| `/cloud/dav/` | Accès WebDAV au même espace, avec authentification propre au cloud |

Une empreinte SHA-256 est vérifiée côté serveur ; elle ne remplace ni l’authentification ni la révision. Rendre les envois de morceaux idempotents. Une annulation concurrente avec une publication renvoie l’état final réel ; elle ne supprime jamais un fichier déjà publié.

Prévoir des codes d’erreur stables : `cloud_disabled`, `cloud_unavailable`, `device_revoked`, `version_conflict`, `destination_changed`, `upload_expired`, `source_changed`, `quota_exceeded`, `storage_full`, `integrity_error` et `precondition_required`. Le client traduit ces codes et n’affiche pas une trace interne.

Les flux de données n’utilisent ni `/api/ask`, ni son encodage base64, ni son plafond de pièces jointes. Définir des limites spécifiques sur taille par fichier, taille des morceaux, volume temporaire et nombre de transferts concurrents. Vérifier que le proxy HTTPS relaie sans mise en mémoire intégrale et sans couper les transferts selon les délais du chat.

## Modifications JARVIS BIS

| Fichier ou ensemble proposé | Travail |
| --- | --- |
| `core/config.py`, `.env.example` | Option cloud, mode effectif, adresse interne, quota, tailles et expiration |
| `gateway/main.py`, nouveau `gateway/cloud.py` | Capacité dans `/api/status`, routes, association et proxy des flux API/WebDAV |
| Nouveau `cloud/` | Application du service, migrations SQLite, moteur commun de stockage, transferts, authentification, fournisseur WebDAV et récupération après crash |
| `cloud/Dockerfile`, `cloud/requirements.txt`, `deploy/cloud.compose.yaml` | Construction épinglée, utilisateur non privilégié, volume persistant et service privé |
| Nouveau `scripts/cloud-control.py`, `setup_wizard.py`, `deploy.sh` | Configuration explicite en mode serveur, diagnostic, lancement/arrêt et procédure de migration ; aucun montage du socket Docker dans le gateway |
| `docs/CLOUD_API.md`, `docs/CLOUD_DEPLOIEMENT.md`, `README.md` | Contrat, installation, limites, association/révocation, sauvegarde et restauration |
| Nouveaux tests cloud et tests gateway | Atomicité, reprise, authentification, WebDAV et compatibilité du gateway existant |

Le conteneur n’expose pas de nouveau port public. Les données, versions et métadonnées survivent à sa recréation. L’arrêt ou l’indisponibilité du cloud ne bloque pas le démarrage du gateway, ses bots ou le chat.

Sauvegarder ensemble la base et les objets référencés à l’aide d’un arrêt bref des écritures ou d’un mécanisme de snapshot cohérent. Tester la restauration dans un volume distinct. La rétention de versions ne remplace pas cette sauvegarde. Un retour en mode local désactive l’accès sans supprimer le volume ; une désinstallation conserve les données par défaut.

## Modifications Noctalia à transmettre à Théo

### Client et transferts

Ajouter `home/.local/bin/nc-cloud` et ses modules dans `home/.local/share/nc-cloud/`. Réutiliser la lecture de connexion de `nc-assistant`, tout en séparant authentification cloud, délais, streaming et états des transferts. Prévoir des commandes `etat`, `associer`, `ouvrir`, `lister`, `dossier`, `envoyer`, `reprise`, `annuler` et `transferts`, avec sortie JSON pour l’interface.

Le processus de transfert continue lorsque la fenêtre ferme et s’arrête proprement si la session utilisateur se termine. Ne pas ajouter de service actif lorsque le cloud est désactivé. Les manifestes et journaux sont privés, bornés et rangés dans les emplacements XDG, séparés par serveur et espace. Les événements de progression alimentent l’interface sans lancer une requête réseau à chaque image.

Réutiliser les validations de fichiers locaux utiles, en définissant une politique propre au drive : les règles des pièces jointes IA ne deviennent pas automatiquement les règles du stockage personnel. Accepter uniquement des fichiers ordinaires sélectionnés par l’utilisateur, ouvrir les sources sans suivre un lien substitué et vérifier qu’elles ne changent pas pendant l’envoi. Aucun shell construit à partir des noms de fichiers.

### Fenêtre et barre

Ajouter `CloudEtat.qml`, `Cloud.qml` et `CloudDepot.qml` dans `home/.config/quickshell/nc-ile/`, puis les points d’entrée et le chargement à la demande dans `shell.qml`. La fenêtre expose le fil d’Ariane distant, les dossiers, « Nouveau dossier », « Envoyer ici », les conflits et les transferts.

Créer une petite surface Quickshell dédiée à l’icône cloud dans la barre, capable de recevoir un glisser externe Wayland. Vérifier d’abord les possibilités du widget Noctalia natif ; si son API n’expose pas le dépôt externe, utiliser cette surface sans dupliquer l’icône. Adapter géométrie, masque d’entrée, affichage multiécran et position aux réglages du rice. La zone cachée ne doit jamais capturer les clics.

Le dépôt prépare une liste de fichiers et ouvre le choix de destination. Forcer une action de copie afin que le gestionnaire de fichiers ne supprime pas les sources. Le transfert commence après « Envoyer ici », pas à l’entrée du curseur dans la zone. Traiter les URI avec espaces et accents, refuser les URL distantes et expliquer les dossiers déposés non pris en charge dans cette version.

En mode local, la surface n’est pas instanciée. Pour une connexion distante déjà configurée mais hors ligne, elle reste grisée et explique le refus d’envoi. Les notifications indiquent le résultat réel ; « Déposé dans la fenêtre » ne signifie pas « Enregistré sur le serveur ».

### Gestionnaire de fichiers et installation

Ajouter un accès WebDAV nommé « Cloud JARVIS » dans les emplacements du gestionnaire configuré par `APPLI_FICHIERS`. Commencer par Nautilus/GVfs, défaut prévu par le raccourci `Super + E`, puis fournir une adaptation ou une ouverture documentée pour les autres gestionnaires pris en charge. Une entrée latérale est l’intégration native visée ; un onglet peut ensuite être ouvert par l’utilisateur.

Le montage se fait à la demande, sans copie miroir et sans cache d’écriture différée. L’identité du serveur et le secret ne figurent pas ensemble dans une URL persistée. Une déconnexion ne bascule jamais vers un dossier local vide qui semblerait être le drive. Préserver les autres favoris lors de l’ajout ou du retrait de l’entrée.

Adapter `nc-reglages`, l’installation et la génération de configuration pour l’activation de l’icône et les dépendances clientes. Ajouter une action « Envoyer vers Cloud JARVIS » au gestionnaire principal si son mécanisme d’extension le permet ; elle ouvre la même fenêtre que la barre. Enregistrer les traductions français/anglais selon les conventions du rice.

### Adaptateur serveur et révisions validées

Tester `home/.local/share/nc-jarvis/server/nc_gateway.py` contre les nouvelles routes : conservation des dépendances d’authentification, streaming, lifespan et objet `cloud` dans `/api/status`. Modifier l’adaptateur seulement si nécessaire.

Adapter `home/.local/share/nc-jarvis/deployer.py` et `verifier.py` pour la configuration distante optionnelle et les dépendances réellement nécessaires. Le poste client n’a pas besoin de Docker pour utiliser le drive. Le déploiement local ne propose ni conteneur ni montage cloud.

Après validation, proposer à Théo le SHA JARVIS exact à ajouter aux révisions acceptées et, si approprié, à utiliser comme nouvelle révision initiale. L’interface conserve un comportement propre avec les anciens serveurs.

## Lots de réalisation et critères de passage

| Lot | Livrable | Condition pour continuer |
| --- | --- | --- |
| 0 | Contrat, prototype WebDAV/GVfs et dépôt Wayland sur une surface de barre | Deux interfaces atteignent le même stockage de test ; un remplacement concurrent est refusé et les sources locales restent présentes |
| 1 | Service conteneurisé, volume, migrations, option serveur et authentification des appareils | Activation locale impossible, révocation effective, données conservées après recréation du conteneur |
| 2 | API de transferts, publication atomique, idempotence, quotas et révisions | Courses entre deux clients, interruption et perte de réponse de publication passent les tests |
| 3 | Client `nc-cloud` et intégration WebDAV protégée | Navigation, création, téléchargement et reprise explicite fonctionnent sur deux clients ; les écritures incompatibles échouent sans perte |
| 4 | Fenêtre, icône, dépôt, états hors ligne et intégration des favoris | Parcours complet sur une session Hyprland réelle, y compris multiécran et coupure réseau |
| 5 | Documentation, patchs Noctalia, push JARVIS et issue à Théo | Patchs applicables sur un SHA annoncé, commandes de validation reproductibles et liens vers les commits publiés |

Le lot 0 est un prototype isolé. Il doit fixer les méthodes WebDAV réellement supportées et la façon de placer l’icône avant de développer les écrans définitifs. En cas de limite du gestionnaire de fichiers, documenter le périmètre exact et adapter le lot 3 sans réduire les protections du serveur.

## Validation requise

Les tests automatisés utilisent des volumes temporaires et deux identités clientes ; ils ne touchent pas aux fichiers du bureau ni au serveur de production.

| Scénario | Résultat attendu |
| --- | --- |
| `DEPLOY_MODE=local`, y compris avec `CLOUD_ENABLED=1` | Capacité désactivée et routes refusées ; pas de conteneur lancé par l’installation locale |
| Ancien serveur ou serveur distant sans cloud | Chat utilisable, cloud absent ou indiqué incompatible |
| Dépôt alors que le réseau est coupé | Aucun nouvel envoi, aucune file différée et source intacte |
| Coupure pendant l’envoi d’un morceau | Aucun fichier partiel visible ; reprise des seuls morceaux manquants après action utilisateur |
| Publication réussie avec réponse perdue | Retrouver le résultat par la même opération, sans doublon |
| Deux PC créent le même nom | Une création réussit ; l’autre propose la résolution du conflit |
| Deux PC remplacent la même révision | Un seul remplacement réussit ; la seconde version n’écrase pas la première |
| Source modifiée, parent déplacé ou destination changée après coupure | Reprise refusée ou choix de destination renouvelé ; aucune publication dans un emplacement inattendu |
| WebDAV `PUT` sur un nom existant sans précondition | Refus d’écrasement, y compris par un client natif |
| WebDAV `MOVE`/`COPY`/`DELETE` et sauvegarde par renommage | Aucune méthode ne contourne les révisions ; méthode non prise en charge refusée |
| Dépassement concurrent de quota ou disque plein | Erreur explicite, réservation libérée et anciennes versions intactes |
| Annulation concurrente avec publication | État final exact, aucune suppression d’un fichier déjà publié |
| Jeton révoqué, espace interdit, traversée de chemin, lien substitué | Accès refusé et absence de fuite de secret |
| Redémarrage du service entre écriture de l’objet et commit SQLite | Ancienne ou nouvelle version complète ; récupération possible des objets orphelins |
| Passage au mode local ou changement de serveur dans Noctalia | Cloud désactivé/démonté ; aucune reprise envoyée à une autre identité |
| Recréation du conteneur et restauration de sauvegarde | Retrouver fichiers, révisions et associations attendues |

Compléter par les tests gateway déjà présents et ceux du déployeur/adaptateur Noctalia. Sur le bureau réel, contrôler le dépôt de fichiers multiples, espaces et accents, fermeture de fenêtre pendant l’envoi, arrêt de session, refus hors ligne, absence de capture des clics lorsque l’icône est cachée, et `Super + E`. L’absence d’une validation graphique doit être signalée dans l’issue.

## Livraison dans Git et issue pour Théo

Développer JARVIS sur une branche dédiée, en commits cohérents : configuration et service, protocole et protection des écritures, puis documentation et tests. Publier cette branche sur `RaphaelBlanc/JARVIS-BIS` une fois les vérifications terminées ; garder le SHA exact des commits livrés. Les patchs Noctalia n’entraînent aucune modification de la session installée.

Préparer la partie Noctalia dans un worktree basé sur un commit récent et identifié de `abcdef123456-wq/noctalia-config`. Exporter les vrais changements dans `JARVIS-BIS/integrations/noctalia-config/cloud/`, par exemple :

- `0001-cloud-client.patch` : client, états persistants, tests du protocole et intégration au gestionnaire de fichiers.
- `0002-cloud-interface.patch` : fenêtre, surface de dépôt, réglages, installation et traductions.
- `0003-cloud-deployment.patch` : adaptateur si nécessaire, déployeur, SHA JARVIS validé et documentation.
- `README.md` : SHA de base Noctalia, prérequis, ordre d’application et validations.
- `ISSUE_THEO.md` : corps exact de l’issue à publier.

Produire les patchs depuis des changements effectivement développés et relus. Les vérifier avec `git apply --check`, puis les appliquer dans un second worktree propre et y exécuter les tests concernés. S’ils proviennent de `git format-patch`, documenter l’application par `git am` et tester également ce parcours.

L’issue aura pour titre proposé « Cloud JARVIS distant — accès fichiers et dépôt dans la barre ». Elle contiendra le comportement attendu, la condition `server`, la politique hors ligne, les limites WebDAV, les préconditions de remplacement, le contrat API et sa version, les SHA JARVIS/Noctalia, les liens vers les patchs publiés, les validations réalisées et les changements restant à adapter par Théo. Présenter les patchs comme une base d’intégration à relire.

Avant publication, rechercher une issue équivalente pour éviter un doublon. Après le push des fichiers et avec le corps final préparé dans le fichier, utiliser :

```bash
gh issue create --repo abcdef123456-wq/noctalia-config \
  --title 'Cloud JARVIS distant — accès fichiers et dépôt dans la barre' \
  --body-file integrations/noctalia-config/cloud/ISSUE_THEO.md
```

L’issue est créée après publication des commits et patchs auxquels elle renvoie. Le résultat livré à l’utilisateur comprend les liens vers la branche ou les commits JARVIS, les patchs et l’issue GitHub.

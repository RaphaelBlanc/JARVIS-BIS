# API du cloud JARVIS

Version du contrat : `1`, étendue par capacités. Le gateway public sert `/api/cloud/v1` et `/cloud/dav/`. Le stockage est un service privé distinct du processus runner, réservé à `DEPLOY_MODE=server` avec `CLOUD_ENABLED=1`. Les fichiers de clé restent toutefois lisibles par les processus du même compte Unix ; cette disposition ne constitue pas une isolation de sécurité du runner. Aucun transfert ne passe par l’IA, `/api/ask` ou les pièces jointes du chat.

## Découverte et association

`GET /api/cloud/v1/capabilities`, avec l’en-tête JARVIS habituel `X-Secret-Key`, retourne `api_version`, `enabled`, `state`, `reason`, `server_id`, `api_path` et `dav_path`. Cette route ne contacte pas le runner. `/api/status` contient aussi cet objet dans `cloud`.

Lorsque le stockage est prêt, `features` annonce `file_delete=true`, `delete_if_match=true`, `delete_idempotency=true` et `max_old_versions` (10 par défaut, de 0 à 10 selon la configuration). `/status` authentifié avec le jeton cloud renvoie les mêmes capacités. Un client ne propose la suppression que si `features.file_delete` est explicitement vrai ; l’absence de ce champ désigne un ancien serveur.

Types de la réponse de découverte :

| Champ | Type JSON | Valeur ou sens |
| --- | --- | --- |
| `api_version` | entier | `1` |
| `enabled` | booléen | Activation effective en mode serveur |
| `state` | chaîne | `disabled`, `unavailable` ou `ready` |
| `reason` | chaîne ou `null` | `local_deployment`, `not_configured`, `cloud_unavailable` ou `null` |
| `server_id` | chaîne ou `null` | Identité persistante lorsque le stockage répond |
| `api_path` | chaîne | `/api/cloud/v1`, chemin relatif à l’origine du gateway |
| `dav_path` | chaîne | `/cloud/dav/` |
| `features` | objet, optionnel | Présent lorsque le stockage répond ; ne pas présumer une capacité absente |

Cette révision annonce également `features.entry_by_id=true` et `features.early_create_conflict=true`. Ces booléens permettent de détecter les deux ajouts ci-dessous sur un serveur v1 ; leur absence ne change pas les routes historiques de navigation et de transfert.

`enabled=false` signifie que le mode est local ou que l’option est désactivée. `enabled=true,state=unavailable` signifie que le stockage est configuré mais injoignable. Un mode local refuse toutes les routes de données, y compris avec un ancien jeton cloud.

L’administration utilise `X-Secret-Key` :

- `POST /api/cloud/v1/devices`, corps `{"name":"portable"}` : retourne `device_id` et `token` une seule fois, ainsi que `server_id`, `space_id` et `api_version`.
- `DELETE /api/cloud/v1/devices/{device_id}` : révoque l’appareil et annule ses réservations non publiées.

Le client range le jeton dans le trousseau Secret Service avec `secret-tool`. Les autres routes de l’API utilisent `Authorization: Bearer <jeton-cloud>`, jamais la clé JARVIS. Le stockage conserve uniquement l’empreinte SHA-256 d’un jeton aléatoire de 256 bits. L’association du cloud est indépendante des nœuds de contrôle du PC et des conversations.

Les réponses du service incluent `api_version`, `server_id` et `space_id`. La v1 expose un seul espace `personal`, commun aux appareils associés du même propriétaire.

## Navigation

| Requête | Corps ou paramètres | Résultat |
| --- | --- | --- |
| `GET /status` | Aucun | `state`, `root`, `device_id`, quotas, réservations, taille de morceau, taille maximale et `webdav=read_only` |
| `GET /entries` | `parent_id=root`, `cursor` optionnel | `parent`, `entries`, `next_cursor` ; pages de 100 entrées |
| `GET /entries/{id}` | Identifiant opaque ou `root` | Enveloppe `api_version`, `server_id`, `space_id` et `entry` ; `404 not_found` si absente |
| `POST /directories` | `parent_id`, `parent_revision`, `name` | `entry` du nouveau dossier ; `409` si le nom existe ou le parent a changé |
| `GET /files/{id}/content` | `Range` et `If-Range` optionnels | Flux binaire, `ETag`, `Accept-Ranges: bytes` ; une plage simple est prise en charge |
| `DELETE /files/{id}` | `If-Match: "revision"`, `Idempotency-Key` | Suppression transactionnelle du fichier courant et de ses anciennes versions ; voir ci-dessous |

Les chemins du tableau sont relatifs à `/api/cloud/v1`. Une entrée contient `id`, `parent`, `name`, `kind` (`directory` ou `file`), `revision`, `size`, `sha256` et `modified`. `root` est l’identifiant du dossier racine. Une révision est opaque ; les horloges des clients ne permettent pas d’arbitrer un conflit.

`id`, `name`, `kind` et `revision` sont des chaînes ; `parent` est une chaîne ou `null` pour la racine ; `size` est un entier en octets et `modified` un nombre de secondes Unix. `sha256` est l’empreinte hexadécimale minuscule du contenu d’un fichier, ou `null` pour un dossier. Un dossier a `size=0`. La lecture par identifiant utilise le jeton cloud et renvoie une seule entrée, sans parcourir les pages du parent, sans champ privé `blob` ni chemin disque. Les identifiants exposés sont `root` ou 32 caractères hexadécimaux minuscules. Un fichier supprimé retourne 404 ; un appareil révoqué reste refusé avec 403.

Les noms d’entrée sont normalisés en **Unicode NFC** avant comparaison et validation. Ils doivent être non vides, faire **au plus 240 octets UTF-8 après NFC**, différer de `.` et `..`, et ne contenir ni `/`, ni `\`, ni caractère d’une catégorie Unicode **C*** (`Cc`, `Cf`, `Cs`, `Co`, `Cn` : contrôles, formats, substituts, usage privé et points non attribués). La limite ne correspond pas au nombre de caractères JSON avant normalisation. Un nom invalide produit `400 invalid_name` ; deux graphies équivalentes après NFC désignent le même nom.

Champs de `GET /api/cloud/v1/status` après authentification cloud :

| Champ | Type JSON | Sens |
| --- | --- | --- |
| `api_version`, `server_id`, `space_id` | entier, chaîne, chaîne | Enveloppe commune (`1`, identité persistante, `personal`) |
| `state`, `device_id` | chaîne, chaîne | `ready` et appareil authentifié |
| `quota_bytes`, `used_bytes`, `reserved_bytes` | entiers | Quota partagé, octets référencés et réservations incluant les nettoyages différés |
| `max_file_bytes`, `chunk_bytes` | entiers | Taille maximale d’un fichier et taille d’un morceau en octets |
| `root` | objet entrée | Métadonnées du dossier racine, dont sa révision actuelle |
| `webdav` | chaîne | `read_only` |
| `features` | objet | Capacités de cette révision |
| `max_old_versions`, `garbage_bytes` | entiers | Limite d’historique et objets retirés restant à récupérer |

La révision du parent change à chaque mutation d’un enfant direct. Elle est vérifiée à la création d’un dossier ou d’une intention de transfert : une valeur périmée produit `409 destination_changed`. Au commit, le serveur vérifie seulement que le parent existe encore comme dossier et applique la précondition du nom/fichier selon `policy`. Une publication ou création de dossier sans rapport ne fait donc plus échouer un transfert déjà ouvert. La suppression du fichier ciblé continue d’invalider les anciennes intentions, sur tous les appareils. Plusieurs PC peuvent publier des fichiers différents dans le même dossier ; deux écritures concurrentes sur le même fichier restent arbitrées transactionnellement.

Les déplacements, renommages et suppressions de dossiers sont disponibles lorsque les capacités correspondantes décrites ci-dessous sont présentes. Chaque remplacement conserve au plus dix anciennes versions, configurables à la baisse côté serveur ; elles comptent dans le quota. La version courante n’expire pas automatiquement. Il n’existe pas encore d’écran de restauration des versions.

## Suppression de fichiers

### Gestion des fichiers et dossiers

Les capacités additives `entry_move`, `directory_delete` et `delete_preview` annoncent les opérations suivantes. Elles utilisent le jeton cloud habituel ; les anciennes routes restent disponibles.

| Requête | Préconditions / corps | Résultat |
| --- | --- | --- |
| `GET /entries/{id}/delete-preview` | Aucune mutation | `entry`, `tree_revision`, `files`, `directories`, `size` |
| `POST /entries/{id}/move` | `If-Match: "revision"`, `Idempotency-Key` ; `parent_id`, `parent_revision`, `name` | `entry`, `moved=true` |
| `DELETE /entries/{id}` | `If-Match: "revision"`, `Idempotency-Key` ; `recursive` (faux par défaut), `tree_revision` optionnel | `deleted=true`, `entry_id`, `count` |

Les chemins sont relatifs à `/api/cloud/v1`. Déplacement et renommage sont la même opération : l’identifiant, les octets et l’historique restent conservés. L’opération refuse la racine (`409 root_protected`), un dossier déplacé dans lui-même ou sa descendance (`409 invalid_destination`), et un nom déjà occupé (`409 name_conflict`), sans écraser la destination. Les révisions de la source et de la destination sont vérifiées dans la transaction. Les intentions de transfert visant l’ancien nom sont invalidées ; les transferts visant un enfant d’un dossier déplacé restent liés à son identifiant et peuvent terminer.

Une suppression non récursive de dossier occupé est refusée (`409 directory_not_empty`). Une suppression récursive exige le `tree_revision` du dernier aperçu. Cette empreinte inclut tous les identifiants, parents, noms et révisions du sous-arbre, pas seulement la révision de son sommet : une modification profonde produit `412 tree_changed` et exige un nouvel aperçu puis une nouvelle confirmation. Le compteur `directories` inclut le dossier sélectionné. La transaction retire le contenu et son historique, invalide les transferts dans les dossiers supprimés, et utilise la même file de récupération physique que la suppression de fichiers.

Les reçus de ces opérations sont persistants, liés à l’appareil et à la demande complète. Une reprise explicite avec la même clé et le même corps retrouve le résultat, y compris après suppression ou redémarrage. Un fichier vide se crée avec le protocole d’envoi existant : taille zéro, SHA-256 du contenu vide, aucun morceau, puis commit.

### Route historique de suppression de fichiers

Après confirmation explicite de l’utilisateur, envoyer `DELETE /api/cloud/v1/files/{id}` avec le jeton cloud, l’ETag fort exact `If-Match: "<revision affichée>"` et une nouvelle `Idempotency-Key` au même format que les transferts. L’absence de précondition produit `428 precondition_required` ; une ancienne révision, `*`, un ETag faible ou une liste d’ETags sont refusés avec `412 version_conflict`. Un dossier produit `409 not_a_file`.

Exemple de résultat : `{"api_version":1,"server_id":"…","space_id":"personal","deleted":true,"file_id":"…","revision":"…"}`. Le reçu est lié à l’appareil, à la clé, au fichier et à sa révision. Le rejouer retourne le même résultat même après redémarrage ou recréation d’un fichier du même nom ; réutiliser la clé pour une autre requête produit `409 idempotency_conflict`. Sans reçu correspondant, un fichier absent produit `404 not_found`.

La transaction retire la version courante et tout son historique, écrit un marqueur de suppression, change la révision du parent et invalide les transferts ouverts de tous les appareils visant ce parent et ce nom, y compris `create` et `keep_both`. Leur état devient `invalidated` ; envoyer un morceau ou finaliser produit `410 upload_invalidated`. Annuler ou rejouer ces intentions ne les réactive jamais. Un reçu déjà publié reste un résultat historique : son rejeu ne republie pas le fichier. Une création explicitement demandée après suppression utilise une nouvelle intention et un nouvel identifiant de fichier.

Si un remplacement gagne avant la suppression, l’ancien `If-Match` échoue. Si la suppression gagne, le remplacement en cours ne peut pas publier. Les morceaux restent protégés par leur verrou pendant l’assemblage, puis sont nettoyés. Les objets retirés sont placés dans une file de récupération persistante, distincte des objets en cours d’assemblage. Un téléchargement déjà ouvert peut terminer ; une nouvelle ouverture d’un fichier supprimé retourne 404.

`used_bytes` mesure les références courantes et l’historique conservé. `garbage_bytes` mesure les objets retirés restant à récupérer ; ils restent inclus dans `reserved_bytes` jusqu’au nettoyage effectif. Une suppression réussie ne promet donc pas une libération physique immédiate. Les échecs sont journalisés et réessayés toutes les 30 secondes. La réduction de l’historique utilise les mêmes règles et ne supprime jamais un objet encore référencé.

## Transfert par morceaux

1. `POST /uploads` avec un en-tête `Idempotency-Key` aléatoire de 16 à 128 caractères alphanumériques, `_` ou `-`, et le corps suivant :

```json
{
  "parent_id": "root",
  "parent_revision": "revision-du-dossier",
  "name": "rapport.pdf",
  "size": 12345,
  "sha256": "empreinte-sha256-complete-en-hexadecimal",
  "policy": "create",
  "expected_revision": null
}
```

2. La réponse contient `upload_id`, `state`, `chunk_bytes`, `expires`, `chunks` et `result`. Envoyer chaque morceau avec `PUT /uploads/{id}/chunks/{index}`, indexé depuis zéro, en binaire, avec `X-Content-SHA256`. Tous les morceaux sauf le dernier font exactement `chunk_bytes` octets. Un fichier vide ne comporte aucun morceau.
3. `POST /uploads/{id}/commit` assemble les morceaux, vérifie taille et empreinte complète, puis publie dans une transaction. Le résultat contient `state=committed` et l’entrée publiée dans `result`.
4. `GET /uploads/{id}` retrouve les morceaux reçus et le résultat. Une nouvelle requête de publication après succès retourne le même résultat. Elle ne crée jamais une seconde copie.
5. `DELETE /uploads/{id}` annule un transfert non publié ; si la publication a déjà gagné la course, retourne `state=committed` sans supprimer le fichier.

La clé d’idempotence est liée à l’appareil et à l’intention complète. La réutiliser avec une autre destination ou un autre contenu produit `409 idempotency_conflict`. La renvoyer avec la même intention retrouve le transfert initial, même après perte de sa réponse de création. Les reçus publiés sont conservés, y compris après nettoyage des morceaux.

`chunks` est toujours une **liste d’objets** `{"idx": 0, "sha256": "<64 caractères hexadécimaux minuscules>", "size": 4194304}`, et non une liste d’index. `idx` est un entier à partir de zéro, `size` un entier en octets ; la liste est triée par `idx`. `expires` est un nombre de secondes Unix. `upload_id` et `state` sont des chaînes, `chunk_bytes` un entier, `result` une entrée publiée ou `null`.

Les **cinq états** de transfert sont :

| `state` | Sens | `chunks` | `result` |
| --- | --- | --- | --- |
| `pending` | Transfert ouvert non expiré | Morceaux validés déjà reçus, éventuellement `[]` | `null` |
| `committed` | Publication réussie | `[]` | Entrée publiée au moment du succès, même si modifiée ou supprimée depuis |
| `cancelled` | Annulation ou révocation avant publication | `[]` | `null` |
| `expired` | Durée de réservation écoulée | `[]` | `null` |
| `invalidated` | Suppression ayant invalidé la destination de cette intention | `[]` | `null` |

Pour un appareil encore autorisé et propriétaire du transfert, **`GET /uploads/{id}` répond 200 dans ces cinq états**, y compris après annulation, expiration ou invalidation. Il ne faut pas confondre cette consultation avec un nouvel envoi de morceau ou un commit : ces mutations répondent `410 upload_expired`, `410 upload_invalidated` ou `409 upload_not_pending` selon l’état. Un transfert inconnu ou appartenant à un autre appareil produit 404 ; un appareil révoqué produit 403. Annuler ne réactive jamais un transfert terminal et ne supprime pas un fichier déjà publié. Les états terminaux exposent `chunks=[]` même si le nettoyage physique doit encore réessayer.

Chaque appareil ne voit que ses propres transferts. Les limites par défaut sont 5 Gio par fichier, morceaux de 4 Mio, 8 transferts non expirés **par appareil** et 20 Gio de quota partagé incluant les versions conservées. Le quota est réservé à la création et les réservations concurrentes sont additionnées. La limite disque réelle reste indépendante du quota logique. Les routes d’administration (`capabilities`, association, révocation) gardent la limitation de débit du gateway ; les morceaux ne consomment pas ce compteur.

## Conflits

- `policy=create` : si une entrée de même nom normalisé existe déjà dans le parent, `POST /uploads` refuse avec `412 version_conflict` et `current` avant de créer le transfert, réserver du quota ou recevoir les morceaux. La précondition du parent est vérifiée avant ce contrôle ; un parent périmé produit donc `409 destination_changed`. Les requêtes rejouant exactement une intention existante retrouvent d’abord leur reçu, même si le nom est désormais occupé. Le nom doit toujours être libre au commit : deux intentions concurrentes créées lorsque le nom était libre peuvent être acceptées, puis une seule publier. Le contrôle initial ne réserve pas le nom et ne remplace pas le contrôle transactionnel final.
- `policy=keep_both` : si le nom existe, le serveur réserve un nom numéroté libre dans la même transaction.
- `policy=replace` : fournir `expected_revision`, correspondant à la version montrée à l’utilisateur. Sans révision : `428 precondition_required`. Si elle a changé ou si le fichier a disparu : `412 version_conflict`.

Un conflit fournit `current`, l’entrée actuelle ou `null`. Pour changer de politique, le client annule l’ancien transfert et crée une nouvelle intention avec une nouvelle clé d’idempotence. La v1 renvoie les morceaux depuis la source locale ; elle ne réutilise pas encore les morceaux d’une autre intention.

Les morceaux restent invisibles dans l’arborescence. Les objets complets sont synchronisés sur disque avant la transaction qui les référence. Un remplacement conserve l’ancien objet et son ancienne révision. Une coupure peut laisser un objet orphelin, mais ne transforme pas un morceau en fichier publié.

## Hors ligne

Le client vérifie une connexion authentifiée avant de préparer un nouveau dépôt. Il ne met pas de fichiers en attente hors ligne et ne les copie pas dans un cache de synchronisation.

Après une coupure, le manifeste local permet une reprise explicite. Le client commence par retrouver le statut distant : si le commit avait réussi avec une réponse perdue, il affiche le succès initial. Sinon il vérifie l’identité du serveur, de l’appareil, de l’espace et du fichier source, puis renvoie les morceaux manquants. Une source modifiée exige un nouveau dépôt. Le commit revérifie la version distante.

Les morceaux sont effacés automatiquement après publication, annulation, révocation ou expiration. Les reçus de résultat restent conservés ; un état terminal expose `chunks=[]`. Si le nettoyage échoue, les octets réservés restent comptés jusqu’à son succès. Les opérations de nettoyage respectent les assemblages en cours et sont réessayées côté service, sans relevé du client.

## WebDAV

`/cloud/dav/` expose le même espace en **lecture seule**, avec Basic Auth : utilisateur `device_id`, mot de passe `token`. HTTPS avec certificat vérifié est requis hors boucle locale, LAN privé de confiance ou tunnel chiffré établi. Un nom `*.ts.net` seul ne prouve pas l’emploi du tunnel : vérifier le chemin Tailscale effectif ou utiliser HTTPS. Cette règle couvre association, API et WebDAV, sans repli vers HTTP ni redirection d’identifiants vers une autre origine. Les identifiants ne figurent jamais ensemble dans l’URL.

`OPTIONS`, `PROPFIND`, `GET` et `HEAD` sont permis. Toutes les mutations, dont `PUT`, `MKCOL`, `MOVE`, `COPY`, `DELETE`, `LOCK` et `PROPPATCH`, sont refusées. Le contournement du dialogue de conflit par un éditeur ou un gestionnaire de fichiers est ainsi impossible dans cette version. Les écritures s’effectuent par `nc-cloud`, la fenêtre ou le script Nautilus.

Pour toutes les méthodes WebDAV, le gateway et le service refusent un corps dépassant 8 Kio (`413`), même sans Basic Auth et sans `Content-Length`. La lecture du corps est limitée à 5 secondes (`408`). Le contrôle précède le pont WSGI et sa collecte en mémoire. Ces limites ne s’appliquent pas au contenu des réponses de téléchargement ni aux morceaux binaires de l’API de transfert.

### Écritures WebDAV optionnelles

Le mode précédent reste le défaut. Après activation explicite de `CLOUD_DAV_WRITE=1` côté gateway **et** stockage, `features.webdav_write=true` et `/status.webdav=read_write` annoncent l’extension. Les clients existants peuvent continuer d’utiliser l’API d’envoi et ses confirmations.

`PUT`, `MKCOL`, `MOVE` et `DELETE` sont alors acceptés avec la même authentification Basic. Le provider expose les autorisations d’écriture à GVfs. `OPTIONS` annonce uniquement les méthodes disponibles ; les verrous DAV, `COPY` et les propriétés arbitraires ne sont pas implémentés. Un déplacement vers un nom occupé est refusé, y compris avec `Overwrite: T` ; un remplacement de contenu passe par `PUT` et conserve les anciennes versions.

`PUT` est traité en ASGI, sans passer par l’adaptateur WSGI. Le gateway relaie le flux ; le stockage réserve le quota, écrit au plus un morceau en mémoire à la fois, calcule le SHA-256 complet, puis publie avec le même moteur transactionnel que l’API. Sans taille annoncée, la réservation initiale vaut la taille maximale d’un fichier et se réduit à la fin du flux. Une coupure annule le transfert et récupère ses morceaux sans modifier une version publiée. Aucun envoi DAV n’est repris automatiquement. La limite de 8 Kio continue de s’appliquer aux autres méthodes ; les flux PUT ont une limite de taille, 60 secondes d’inactivité et une durée totale maximale d’une heure.

Un `PUT` ou une mutation portant `If-Match` vérifie la révision. Pour interopérer avec GVfs, DAV accepte aussi l’identifiant hexadécimal exact sans guillemets ; les ETags faibles et listes ne sont pas acceptés. L’API JSON conserve son contrat strict avec guillemets. Sans `If-Match`, DAV prend la révision au début de la requête puis vérifie celle-ci au commit : deux écritures concurrentes ne gagnent pas toutes deux. Cela ne peut pas détecter une ancienne copie éditée si l’application n’en transmet pas la révision ; utiliser un éditeur transmettant l’ETag ou la fenêtre Cloud pour une précondition portant sur la version affichée.

Les suppressions DAV de dossiers s’appliquent au contenu présent lors de l’opération, avec contrôle du sous-arbre entre aperçu interne et transaction. La confirmation utilisateur relève du gestionnaire de fichiers. La fenêtre Cloud fournit, elle, un aperçu et une confirmation explicites avant suppression récursive.

## Erreurs et confidentialité

Les erreurs métier du stockage contiennent `error` et les identités de contrat. Codes principaux : `cloud_disabled`, `cloud_unavailable`, `authentication_required`, `device_revoked`, `version_conflict`, `destination_changed`, `upload_expired`, `upload_invalidated`, `upload_not_pending`, `quota_exceeded`, `storage_full`, `integrity_error`, `incomplete_upload`, `chunk_conflict`, `idempotency_conflict`, `not_a_file`, `invalid_name` et `precondition_required`. Autres codes à traiter explicitement :

| Code `error` | HTTP | Sens |
| --- | --- | --- |
| `name_conflict` | 409 | Nom déjà occupé à la création d’un dossier |
| `too_many_transfers` | 429 | Limite d’intentions non expirées atteinte pour cet appareil |
| `storage_error` | 503 | Erreur d’entrée/sortie du stockage ; aucun détail interne exposé |
| `file_too_large` | 413 | Taille déclarée dépassant la limite configurée |
| `not_found` | 404 | Entrée ou transfert introuvable dans le périmètre autorisé |

Les refus précoces du gateway ou du middleware peuvent n’avoir que `{"error":"…"}`, sans l’enveloppe complète. **Une clé JARVIS absente ou incorrecte**, sur une route protégée par `X-Secret-Key`, donne **`401 {"detail":"Clé API invalide"}` sans champ `error`**. Les erreurs de schéma utilisent le `422` FastAPI habituel avec `detail`. Le limiteur du gateway répond 429 avec `detail` et `Retry-After`, à distinguer de `too_many_transfers`. Un client doit donc examiner le statut HTTP puis `error` ou `detail`, sans présumer que toutes les erreurs ont la même forme.

Un en-tête `Range` mal formé, démesuré ou hors fichier retourne `416`, sans erreur interne.

Le gateway reconstruit ses en-têtes internes et refuse les routes non prévues. Les clés et jetons sont envoyés dans les en-têtes, jamais dans la query string. Les réponses ne sont pas mises en cache. Les URL internes et les traces d’erreur du serveur ne doivent pas être affichées par le client.


## Portée des jetons et conservation

Un jeton cloud autorise les opérations sur tout l’espace personnel, y compris la suppression définitive des fichiers et de toutes leurs versions. Un jeton volé peut donc effacer les données ; des remplacements répétés peuvent aussi évincer les anciennes versions de l’historique plafonné. Révoquer immédiatement un appareil perdu ou compromis via `DELETE /devices/{device_id}` avec la clé JARVIS, et conserver des sauvegardes indépendantes du service. L’historique de versions ne remplace pas ces sauvegardes.

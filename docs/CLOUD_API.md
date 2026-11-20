# API du cloud JARVIS

Version du contrat : `1`. Le gateway public sert `/api/cloud/v1` et `/cloud/dav/`. Le service de stockage est privé, séparé du runner et réservé à `DEPLOY_MODE=server` avec `CLOUD_ENABLED=1`. Aucun transfert ne passe par l’IA, `/api/ask` ou les pièces jointes du chat.

## Découverte et association

`GET /api/cloud/v1/capabilities`, avec l’en-tête JARVIS habituel `X-Secret-Key`, retourne `api_version`, `enabled`, `state`, `reason`, `server_id`, `api_path` et `dav_path`. Cette route ne contacte pas le runner. `/api/status` contient aussi cet objet dans `cloud`.

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
| `POST /directories` | `parent_id`, `parent_revision`, `name` | `entry` du nouveau dossier ; `409` si le nom existe ou le parent a changé |
| `GET /files/{id}/content` | `Range` et `If-Range` optionnels | Flux binaire, `ETag`, `Accept-Ranges: bytes` ; une plage simple est prise en charge |

Les chemins du tableau sont relatifs à `/api/cloud/v1`. Une entrée contient `id`, `parent`, `name`, `kind` (`directory` ou `file`), `revision`, `size`, `sha256` et `modified`. `root` est l’identifiant du dossier racine. Une révision est opaque ; les horloges des clients ne permettent pas d’arbitrer un conflit.

La v1 ne propose pas de suppression, déplacement ou renommage des fichiers publiés. Les nouvelles versions sont conservées côté serveur et comptent dans le quota. Il n’existe pas encore d’écran de restauration des versions.

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

Chaque appareil ne voit que ses propres transferts. Les limites par défaut sont 5 Gio par fichier, morceaux de 4 Mio, 8 transferts non expirés et 20 Gio de quota incluant les versions conservées. Le quota est réservé à la création et les réservations concurrentes sont additionnées. La limite disque réelle reste indépendante du quota logique.

## Conflits

- `policy=create` : le nom doit être libre au moment du commit.
- `policy=keep_both` : si le nom existe, le serveur réserve un nom numéroté libre dans la même transaction.
- `policy=replace` : fournir `expected_revision`, correspondant à la version montrée à l’utilisateur. Sans révision : `428 precondition_required`. Si elle a changé ou si le fichier a disparu : `412 version_conflict`.

Un conflit fournit `current`, l’entrée actuelle ou `null`. Pour changer de politique, le client annule l’ancien transfert et crée une nouvelle intention avec une nouvelle clé d’idempotence. La v1 renvoie les morceaux depuis la source locale ; elle ne réutilise pas encore les morceaux d’une autre intention.

Les morceaux restent invisibles dans l’arborescence. Les objets complets sont synchronisés sur disque avant la transaction qui les référence. Un remplacement conserve l’ancien objet et son ancienne révision. Une coupure peut laisser un objet orphelin, mais ne transforme pas un morceau en fichier publié.

## Hors ligne

Le client vérifie une connexion authentifiée avant de préparer un nouveau dépôt. Il ne met pas de fichiers en attente hors ligne et ne les copie pas dans un cache de synchronisation.

Après une coupure, le manifeste local permet une reprise explicite. Le client commence par retrouver le statut distant : si le commit avait réussi avec une réponse perdue, il affiche le succès initial. Sinon il vérifie l’identité du serveur, de l’appareil, de l’espace et du fichier source, puis renvoie les morceaux manquants. Une source modifiée exige un nouveau dépôt. Le commit revérifie la version distante.

## WebDAV

`/cloud/dav/` expose le même espace en **lecture seule**, avec Basic Auth : utilisateur `device_id`, mot de passe `token`. Sur Internet, utiliser HTTPS ; un réseau privé sécurisé est également possible. Les identifiants ne figurent jamais ensemble dans l’URL.

`OPTIONS`, `PROPFIND`, `GET` et `HEAD` sont permis. Toutes les mutations, dont `PUT`, `MKCOL`, `MOVE`, `COPY`, `DELETE`, `LOCK` et `PROPPATCH`, sont refusées. Le contournement du dialogue de conflit par un éditeur ou un gestionnaire de fichiers est ainsi impossible dans cette version. Les écritures s’effectuent par `nc-cloud`, la fenêtre ou le script Nautilus.

## Erreurs et confidentialité

Les erreurs du stockage contiennent `error` et les identités de contrat. Codes principaux : `cloud_disabled`, `cloud_unavailable`, `authentication_required`, `device_revoked`, `version_conflict`, `destination_changed`, `upload_expired`, `upload_not_pending`, `quota_exceeded`, `storage_full`, `integrity_error`, `incomplete_upload`, `chunk_conflict`, `idempotency_conflict` et `precondition_required`. Les erreurs de schéma utilisent le `422` FastAPI habituel.

Le gateway reconstruit ses en-têtes internes et refuse les routes non prévues. Les clés et jetons sont envoyés dans les en-têtes, jamais dans la query string. Les réponses ne sont pas mises en cache. Les URL internes et les traces d’erreur du serveur ne doivent pas être affichées par le client.

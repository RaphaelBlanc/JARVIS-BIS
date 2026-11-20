# Intégration du cloud JARVIS dans Noctalia

Cette série contient l’implémentation cliente proposée à Théo. Elle est distincte du backend et peut être relue puis adaptée à la barre officielle. Les patchs portent sur les sources du rice, sans configuration personnelle ni secret.

- Base Noctalia : `2a9f1c8e72963e08a8a79762b327459b079c7ea5` de `abcdef123456-wq/noctalia-config`.
- Backend cloud v1 : [`d605b224ee992f6c7fe89eec3b8b49a7f679164e`](https://github.com/RaphaelBlanc/JARVIS-BIS/commit/d605b224ee992f6c7fe89eec3b8b49a7f679164e), branche `feat/jarvis-cloud`.
- Livraison des patchs : branche `integration/jarvis-cloud-noctalia` de JARVIS BIS.
- Contrat : [CLOUD_API.md](../../../docs/CLOUD_API.md).
- Installation serveur : [CLOUD_DEPLOIEMENT.md](../../../docs/CLOUD_DEPLOIEMENT.md).

## Appliquer

Créer une branche de travail dans le dépôt Noctalia sur la base annoncée, puis appliquer dans cet ordre :

```bash
git switch -c feat/jarvis-cloud 2a9f1c8e72963e08a8a79762b327459b079c7ea5
git am /chemin/vers/cloud/0001-cloud-client.patch
git am /chemin/vers/cloud/0002-cloud-interface.patch
git am /chemin/vers/cloud/0003-cloud-deployment.patch
```

`changes.diff` donne le diff complet pour une lecture directe. Pour l’appliquer à la place de la série, utiliser `git apply --check` puis `git apply` sur ce diff. Ne pas appliquer les deux formats ensemble.

| Patch | Contenu |
| --- | --- |
| `0001-cloud-client.patch` | `nc-cloud`, moteur de transfert Python, trousseau, reprises, résolution des conflits, action Nautilus et 12 tests clients |
| `0002-cloud-interface.patch` | Fenêtre de destination, état partagé, surface de dépôt Wayland, IPC, réglages, documentation et test QML opt-in |
| `0003-cloud-deployment.patch` | SHA backend cloud accepté pour un checkout mis à jour manuellement ; désactivation du cloud en déploiement local |

La révision initiale du déployeur n’est pas changée par la série. Le serveur de test doit être mis à jour explicitement vers le commit cloud indiqué. Les routes cloud restent présentes lorsque le gateway charge l’adaptateur `nc_gateway` actuel ; aucun remplacement de cet adaptateur n’est nécessaire pour ce contrat.

## Valider

```bash
python3 -B -m unittest tests.test_cloud -v
NC_CLOUD_QML=1 python3 -B -m unittest tests.test_cloud_qml -v
python3 home/.local/bin/nc-cloud verifier
```

Le test QML nécessite une session Wayland et ouvre brièvement une fenêtre de test avec un HOME isolé et un faux client. Si le socket est ailleurs que dans `/run/user/UID`, renseigner `NC_CLOUD_WAYLAND_SOCKET` avec son chemin absolu. Aucune connexion au serveur personnel n’est effectuée par ces tests.

Les patchs ont été vérifiés séparément avec `git apply --check`, appliqués avec `git am` dans un worktree propre, puis testés. Le chemin réel conteneur Podman → gateway avec adaptateur Noctalia → client → WebDAV/Gio a également été exercé, avec un volume temporaire, un conflit, une coupure et une recréation du conteneur.

Les gestes de glisser-déposer depuis les différents gestionnaires de fichiers, le placement dans les différentes dispositions de barre et les essais avec deux PC physiques restent à valider en session réelle. L’utilisateur mettra lui-même à jour son serveur personnel pour ces essais.

## Périmètre fonctionnel

Le cloud doit être explicitement activé côté serveur (`DEPLOY_MODE=server`, `CLOUD_ENABLED=1`) et l’icône côté client (`CLOUD_JARVIS=oui`). En mode local, la capacité est désactivée et la surface de barre disparaît.

Les nouveaux dépôts hors ligne sont refusés. Une interruption conserve un manifeste privé pour une reprise explicite et idempotente. Les noms déjà présents ou versions modifiées ouvrent une résolution : conserver les deux, remplacer la révision affichée ou annuler.

Le gestionnaire de fichiers accède au drive en lecture seule dans cette v1. Les écritures passent par la fenêtre cloud et l’API transactionnelle. Les fichiers d’origine ne sont jamais déplacés ou supprimés. La sélection est limitée à 8 fichiers ordinaires ; dossiers récursifs, suppression/renommage distants et synchronisation hors ligne sont hors de cette version.

Les jetons sont dans le trousseau Secret Service. L’association ne dépend pas de `nc-noeud` ou d’une session IA. Les fichiers privés et manifestes restent hors du dépôt et de l’export du bureau.

## Points à adapter dans le rice officiel

La surface de dépôt est un `PanelWindow` Quickshell de 36 × 42 pixels logiques près du centre de la barre supérieure. `CLOUD_BARRE_DECALAGE` règle son décalage. Théo pourra en adapter le placement, l’apparence, les écrans concernés et l’intégration aux composants existants. Le glisser doit conserver `Qt.CopyAction` et ne commencer l’envoi qu’après le choix de destination.

Le backend GVfs WebDAV et les bindings Python Gio sont nécessaires pour « Ouvrir dans Fichiers ». `secret-tool` et un trousseau déverrouillé sont nécessaires pour l’association. L’installation générique du rice copie déjà les nouveaux fichiers de `home/` ; elle n’a pas été étendue pour installer automatiquement ces dépendances optionnelles.

Deux échecs indépendants du cloud ont été reproduits avant modifications lors des vérifications élargies : le test du briefing daté au 19 octobre 2026, expiré par la rétention de 14 jours ; et le test opt-in du runner CLI refusé par la protection systemd dans l’environnement de test. Les tests cloud et le parcours réel avec l’adaptateur sont distincts et passent.

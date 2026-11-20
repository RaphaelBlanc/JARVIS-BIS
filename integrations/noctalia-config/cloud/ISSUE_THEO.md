Nous avons préparé une première implémentation du cloud distant JARVIS, côté serveur et côté Noctalia. L’utilisateur mettra personnellement à jour son serveur pour les essais ; cette issue transmet les changements clients pour une intégration officielle dans le rice.

## Comportement proposé

- Fonction disponible uniquement si JARVIS annonce le mode `server` avec l’option cloud activée. En mode local : routes cloud désactivées, aucune icône ni montage.
- Une entrée « Cloud JARVIS » dans le gestionnaire de fichiers ouvert par `Super + E`, en lecture seule pour cette v1.
- Une icône cloud dans la barre supérieure. Le dépôt de fichiers ouvre une fenêtre pour parcourir les dossiers distants, en créer un et choisir « Envoyer ici ».
- Progression, résultat par fichier, annulation, reprise explicite et notifications. Les sources locales sont conservées ; le dépôt utilise une action de copie.
- Hors ligne : refus d’un nouveau dépôt, sans file d’envoi différé. Après une interruption : vérification du résultat distant avant toute reprise.
- Conflit entre PC : contrôle de révision au commit et choix « Conserver les deux », « Remplacer » ou « Annuler ». Un verrou de fichier ne remplace pas ce contrôle.

## Backend disponible

[Commit JARVIS BIS d605b22](https://github.com/RaphaelBlanc/JARVIS-BIS/commit/d605b224ee992f6c7fe89eec3b8b49a7f679164e), sur la [branche feat/jarvis-cloud](https://github.com/RaphaelBlanc/JARVIS-BIS/tree/feat/jarvis-cloud).

Le conteneur conserve les objets complets et les métadonnées SQLite dans un volume persistant. Le gateway relaie les données en streaming ; le runner et l’IA ne participent pas aux transferts. Il n’y a pas de nouveau port public. Les jetons cloud sont propres aux appareils et révocables.

- [Contrat API v1](https://github.com/RaphaelBlanc/JARVIS-BIS/blob/d605b224ee992f6c7fe89eec3b8b49a7f679164e/docs/CLOUD_API.md)
- [Déploiement et limites](https://github.com/RaphaelBlanc/JARVIS-BIS/blob/d605b224ee992f6c7fe89eec3b8b49a7f679164e/docs/CLOUD_DEPLOIEMENT.md)

La découverte indépendante du runner utilise `GET /api/cloud/v1/capabilities`, avec la clé JARVIS habituelle. L’association administrative donne ensuite un jeton limité au cloud. Les routes `/api/cloud/v1/*` et `/cloud/dav/` sont conservées par l’adaptateur `nc_gateway` actuel, testé avec le vrai service.

## Diffs prêts à relire

Base exacte : `abcdef123456-wq/noctalia-config@2a9f1c8e72963e08a8a79762b327459b079c7ea5`.

[Fiche d’intégration et instructions](https://github.com/RaphaelBlanc/JARVIS-BIS/blob/integration/jarvis-cloud-noctalia/integrations/noctalia-config/cloud/README.md) · [Diff complet](https://github.com/RaphaelBlanc/JARVIS-BIS/blob/integration/jarvis-cloud-noctalia/integrations/noctalia-config/cloud/changes.diff)

| Patch | Modifications |
| --- | --- |
| [0001 — Client](https://github.com/RaphaelBlanc/JARVIS-BIS/blob/integration/jarvis-cloud-noctalia/integrations/noctalia-config/cloud/0001-cloud-client.patch) | `nc-cloud`, module Python, trousseau, transferts, reprise, action Nautilus et tests |
| [0002 — Interface](https://github.com/RaphaelBlanc/JARVIS-BIS/blob/integration/jarvis-cloud-noctalia/integrations/noctalia-config/cloud/0002-cloud-interface.patch) | `Cloud.qml`, `CloudEtat.qml`, `CloudDepot.qml`, `shell.qml`, réglages, documentation et test QML |
| [0003 — Déployeur](https://github.com/RaphaelBlanc/JARVIS-BIS/blob/integration/jarvis-cloud-noctalia/integrations/noctalia-config/cloud/0003-cloud-deployment.patch) | SHA backend accepté pour une mise à jour manuelle et `CLOUD_ENABLED=0` imposé en mode local |

Les trois patchs sont issus de commits locaux, applicables par `git am` dans cet ordre. Le diff complet est fourni comme alternative pour lecture ou application avec `git apply`. La série est une base d’intégration à adapter et relire ; aucune modification n’a été poussée directement sur ton dépôt.

## À intégrer ou adapter officiellement

- Le placement de la surface Quickshell dans la barre : décalage configurable autour du centre, une surface par écran, aucun masque de clic caché.
- L’apparence et les interactions de la fenêtre cloud pour rester cohérentes avec le rice.
- L’exposition des réglages et la vérification des dépendances optionnelles (`secret-tool`, Gio Python, backend WebDAV de GVfs).
- La politique de validation du SHA backend. La révision initiale du déployeur reste inchangée dans le patch ; le serveur de test sera mis à jour explicitement vers `d605b22`.
- Les vérifications des vrais gestes de glisser-déposer, du multiécran et des variantes de gestionnaire de fichiers.

Le WebDAV reste volontairement en lecture seule dans cette livraison. Autoriser les remplacements natifs demanderait de valider les préconditions de version de chaque client, y compris les sauvegardes par renommage. Les envois de la fenêtre et du script Nautilus utilisent déjà le même moteur de publication transactionnelle.

## Validation réalisée

- 25 tests serveur/proxy HTTP passent : association, révocation, persistance, quotas, intégrité, idempotence, plages de téléchargement, publication concurrente et course annulation/commit.
- 12 tests clients passent : refus hors ligne, source changée, interruption/reprise, réponse de création ou de publication perdue, identité du serveur et résolution des conflits.
- Test QML Wayland passé avec une instance séparée et un faux client : chargement de la fenêtre/zone de dépôt et sérialisation des commandes du client.
- Image réellement construite avec Podman. Parcours réel testé avec le gateway et l’adaptateur Noctalia : envoi, conflit, accès WebDAV, montage Gio/GVfs et favori, refus hors ligne et données conservées après recréation du conteneur.
- Installation cliente locale effectuée en conservant les ajouts existants de l’utilisateur. Le service de l’île redémarre et charge sa configuration.

L’application de la série et les tests sont revérifiés dans un worktree propre. Les essais à deux PC physiques et le geste manuel de dépôt depuis les différents gestionnaires restent à faire avec le serveur personnel mis à jour.

Deux échecs de suites élargies ont été reproduits sur les bases avant ces changements : `test_briefing.TestBriefingState.test_platform_tracking` utilise une date désormais hors de sa rétention ; `test_jarvis_upstream.ContratUpstream` rencontre le refus de la protection systemd du runner dans l’environnement de test. Ils ne concernent pas le parcours cloud.

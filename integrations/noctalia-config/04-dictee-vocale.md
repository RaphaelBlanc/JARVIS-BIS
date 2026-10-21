# Fiche 04 — dictée vocale dans l’assistant JARVIS

## Résumé

| Élément | Valeur |
|---|---|
| Symptôme | Le bouton micro affiche « Dictée et sortie vocale non configurées sur ce client. » |
| Cause | `Assistant.qml` contient un stub : `voix()` ne fait qu’afficher ce message. |
| Correctif | Brancher le micro sur un enregistreur PipeWire + un endpoint serveur `/api/transcribe`. |
| Fichiers | `home/.config/quickshell/nc-ile/Assistant.qml`, nouveau `home/.local/bin/nc-dictee` |
| Patch | [`0002-dictee-vocale.patch`](0002-dictee-vocale.patch) |
| Script | [`files/nc-dictee`](files/nc-dictee) |
| Serveur requis | `POST /api/transcribe` du gateway `assistant-unifie` (déjà déployé) |
| Base | `origin/main` au commit `0bfb7e3` (même base que le patch 0001) |

## Comportement attendu

1. Clic sur le micro → l’enregistrement démarre, l’île affiche « Écoute vocale… ».
2. Second clic → l’enregistrement s’arrête, l’île affiche « Transcription… ».
3. Le texte transcrit est **inséré dans le champ de saisie**, sans envoi automatique.
4. Le bouton haut-parleur conserve son message séparé : la synthèse vocale (TTS)
   n’est pas incluse dans ce correctif.

## Cause précise

Dans `Assistant.qml`, la fonction appelée par le bouton micro était :

```qml
function voix() {
    ecouteVocale = false
    information = Reglages.t("Dictée et sortie vocale non configurées sur ce client.",
                             "Dictation and voice output are not configured on this client.")
}
```

Aucun enregistrement, aucune requête réseau, aucune insertion de texte.
Le bouton micro et l’orbe appelaient tous les deux cette fonction.

## Application

```bash
cd noctalia-config
git checkout -b feat/dictee-vocale
git apply --3way /chemin/vers/0002-dictee-vocale.patch
install -m 755 /chemin/vers/files/nc-dictee home/.local/bin/nc-dictee
```

Si le patch ne s’applique pas à cause d’un décalage de lignes, appliquer les
extraits ci-dessous à la main : les changements sont localisés et indépendants
du reste du fichier.

## Correctif client

### 1. Importer Quickshell.Io

En tête de `Assistant.qml` :

```qml
import QtQuick
import Quickshell.Io
```

### 2. Séparer dictée et sortie vocale

Remplacer `voix()` par les deux fonctions suivantes :

```qml
function sortieVocale() {
    ecouteVocale = false
    information = Reglages.t("Sortie vocale non configurée sur ce client.",
                             "Voice output is not configured on this client.")
}

function voix() {
    if (AssistantEtat.enCours || dictee.running) return
    if (ecouteVocale) {
        ecouteVocale = false
        information = Reglages.t("Transcription…", "Transcribing…")
        dictee.commande = "stop"
        dictee.running = true
    } else {
        information = Reglages.t("Écoute vocale…", "Listening…")
        dictee.commande = "start"
        dictee.running = true
        ecouteVocale = true
    }
}
```

Le bouton haut-parleur doit appeler `page.sortieVocale()` et non `page.voix()`.

### 3. Ajouter le processus de dictée

À la fin du composant racine de `Assistant.qml` :

```qml
Process {
    id: dictee
    property string commande: "start"
    command: [AssistantEtat.bin + "nc-dictee", dictee.commande]
    stdout: StdioCollector {
        onStreamFinished: {
            var r = null
            try { r = JSON.parse(text) } catch (_) {}
            if (dictee.commande === "start") {
                if (!r || r.ok !== true) {
                    page.ecouteVocale = false
                    page.information = (r && r.error) ? String(r.error).slice(0, 300)
                        : Reglages.t("Micro indisponible.", "Microphone unavailable.")
                }
            } else {
                page.ecouteVocale = false
                if (r && r.ok === true && typeof r.text === "string" && r.text.trim().length) {
                    var t = r.text.trim()
                    AssistantEtat.brouillon = AssistantEtat.brouillon.trim().length
                        ? AssistantEtat.brouillon.trim() + " " + t : t
                    page.information = ""
                    Qt.callLater(function() {
                        entree.forceActiveFocus()
                        entree.cursorPosition = entree.text.length
                    })
                } else {
                    page.information = (r && r.error) ? String(r.error).slice(0, 300)
                        : Reglages.t("Transcription vide.", "Empty transcription.")
                }
            }
        }
    }
    stderr: StdioCollector {}
}
```

Le patch `0002-dictee-vocale.patch` contient exactement ces modifications.

### 4. Installer `nc-dictee`

Copier [`files/nc-dictee`](files/nc-dictee) dans `home/.local/bin/nc-dictee`
et le rendre exécutable :

```bash
install -m 755 files/nc-dictee home/.local/bin/nc-dictee
```

Le script :

- lit `~/.config/nc/assistant.conf` (`0600`) comme `nc-assistant` ;
- enregistre avec `parec`, sinon `pw-record`, sinon `arecord` ;
- envoie le WAV brut à `POST /api/transcribe` avec `X-Secret-Key` ;
- affiche une seule ligne JSON : `{"ok":true,"text":"…","engine":"…"}` ;
- ne journalise jamais la clé ;
- supprime l’audio temporaire après transcription.

Aucune dépendance Python externe : bibliothèque standard uniquement.

## Prérequis serveur

Le gateway `assistant-unifie` expose :

```http
POST /api/transcribe
Content-Type: audio/wav
X-Secret-Key: <SECRET_KEY>

<corps binaire WAV/OGG/MP3/M4A>
```

Réponse :

```json
{"success": true, "text": "Texte transcrit", "engine": "groq-whisper:auto", "error": null}
```

Le serveur choisit Groq Whisper si `GROQ_API_KEY` est configuré, sinon OpenAI,
sinon `faster-whisper` local. Le client n’a donc aucune clé vocale à stocker.

Si le serveur renvoie 401/403, vérifier `SECRET_KEY` dans `assistant.conf`.
Si le serveur renvoie 404, le gateway n’a pas encore l’endpoint : mettre à jour
`assistant-unifie` côté serveur.

## Sécurité

- Ne jamais écrire la clé `SECRET_KEY` dans un log, un fichier versionné ou
  une sortie QML.
- Conserver `~/.config/nc/assistant.conf` en `0600`.
- Ne pas envoyer l’audio à un autre hôte que l’URL configurée : le script ne
  suit pas les redirections HTTP.
- L’endpoint est protégé par `X-Secret-Key` et limité à `STT_MAX_AUDIO_MB`
  (25 Mo par défaut).
- Le micro n’est actif qu’entre `start` et `stop` ; un `cancel` supprime le
  fichier temporaire.

## Tests d’acceptation

### Client

```bash
python3 -m py_compile home/.local/bin/nc-dictee
qs -c nc-ile ipc show >/dev/null   # Quickshell répond
```

Vérifier dans les journaux Quickshell qu’aucune erreur QML n’apparaît au
chargement.

### Parcours manuel

1. Ouvrir JARVIS dans l’île.
2. Cliquer sur le micro : le badge passe à « Écoute vocale… ».
3. Prononcer une phrase, puis recliquer : le badge passe à « Transcription… ».
4. Le texte apparaît dans le champ de saisie et le focus y est placé.
5. Envoyer avec Entrée : la réponse JARVIS arrive normalement.
6. Cliquer sur le haut-parleur : le message « Sortie vocale non configurée »
   s’affiche, sans lancer d’enregistrement.

### Serveur

```bash
espeak-ng -v fr -w /tmp/test.wav "Ceci est un test de transcription."
ffmpeg -loglevel error -y -i /tmp/test.wav -c:a libopus -b:a 32k /tmp/test.ogg
curl -sS -X POST http://127.0.0.1:8000/api/transcribe \
  -H "Content-Type: audio/ogg" \
  -H "X-Secret-Key: $SECRET_KEY" \
  --data-binary @/tmp/test.ogg
```

Attendu : `success: true` et un texte non vide.

### Cas d’erreur

- Serveur arrêté → message « Transcription impossible… » dans l’île, pas de
  crash QML.
- Micro coupé → message « Enregistrement vide ou micro coupé. »
- Mauvaise clé → message HTTP 401/403.
- Double clic rapide → le garde `dictee.running` empêche deux processus
  concurrents.

## Limites assumées

- Pas de synthèse vocale : le bouton haut-parleur reste informatif.
- Pas de dictée dans le cockpit plein écran : le micro reste dans la barre de
  saisie de l’île, comme avant.
- Le correctif dépend du serveur `assistant-unifie` pour la transcription.
- Le script utilise l’enregistreur système ; si `parec`, `pw-record` et
  `arecord` sont absents, il renvoie une erreur explicite.

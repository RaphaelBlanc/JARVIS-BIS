# Fiche 05 — pièces jointes dans l’assistant JARVIS (trombone & glisser-déposer)

## Résumé

| Élément | Valeur |
|---|---|
| Objectif | Joindre et envoyer des fichiers (documents, code, PDFs, images) à l'assistant JARVIS |
| Interaction | Bouton trombone (`attach_file`) + glisser-déposer (`DropArea`) + prévisualisation |
| Limites | 5 fichiers maximum par envoi, 20 Mo par fichier |
| Fichiers modifiés | `home/.local/bin/nc-assistant`, `home/.config/quickshell/nc-ile/AssistantEtat.qml`, `home/.config/quickshell/nc-ile/Assistant.qml` |
| Tests | `tests/test_assistant.py`, `tests/test_assistant_page.py` |
| Patch | [`0003-pieces-jointes.patch`](0003-pieces-jointes.patch) |
| Côté serveur | Endpoint `/api/ask` et `/run` de `JARVIS-BIS` déjà mis à jour et déployés (support base64) |

---

## Comportement attendu

1. **Glisser-déposer** : l'utilisateur dépose un ou plusieurs fichiers depuis un explorateur de fichiers directement sur la vue du chat de l'assistant (`DropArea`).
2. **Bouton trombone** : un clic sur l'icône `attach_file` ouvre l'explorateur natif via `nc-assistant selectionner-fichiers` (`zenity --file-selection --multiple`, avec repli `kdialog`).
3. **Bandeau de prévisualisation** : au-dessus de la barre de saisie, les fichiers sélectionnés s'affichent sous forme de pastilles avec une icône trombone, le nom du fichier et un bouton `close` permettant de retirer un fichier avant l'envoi.
4. **Bouton d'envoi activé** : le bouton d'envoi s'active dès qu'un texte est saisi OU qu'au moins un fichier est en attente.
5. **Affichage dans la conversation** : sous la bulle du message utilisateur dans l'historique, un badge `📎 <nom_du_fichier>` confirme les fichiers envoyés avec la requête.
6. **Résilience et transport** : les fichiers sont encodés en base64 par `nc-assistant` avant l'appel à `/api/ask`, assurant un fonctionnement 100% identique en local ou à distance (tunnel HTTP / Tailscale). En cas d'erreur de communication, les fichiers en attente sont restaurés pour permettre à l'utilisateur de réexpédier sans devoir resélectionner ses fichiers.

---

## Guide d'intégration pas à pas

### 1. `home/.local/bin/nc-assistant`

#### A. Import `base64`
En tête de fichier :
```python
import argparse
import base64
import contextlib
```

#### B. Sous-commande `selectionner-fichiers`
Ajouter la fonction qui invoque l'explorateur graphique natif et retourne du JSON standardisé :
```python
def selectionner_fichiers():
    """Ouvre l'explorateur de fichiers natif (zenity, kdialog...) et renvoie du JSON sur stdout."""
    res = {"ok": True, "files": []}
    fichiers = []
    if shutil.which("zenity"):
        try:
            r = subprocess.run(
                ["zenity", "--file-selection", "--multiple", "--separator=|",
                 f"--title={T('Choisir des fichiers à joindre', 'Select files to attach')}"],
                capture_output=True, text=True, timeout=120
            )
            if r.returncode == 0 and r.stdout.strip():
                fichiers = [f.strip() for f in r.stdout.strip().split("|") if f.strip()]
        except Exception:
            pass
    elif shutil.which("kdialog"):
        try:
            r = subprocess.run(
                ["kdialog", "--getopenfilename", "--multiple",
                 f"--title={T('Choisir des fichiers à joindre', 'Select files to attach')}"],
                capture_output=True, text=True, timeout=120
            )
            if r.returncode == 0 and r.stdout.strip():
                fichiers = [f.strip() for f in r.stdout.strip().split() if f.strip()]
        except Exception:
            pass
    valides = []
    for f in fichiers:
        p = Path(f).expanduser().resolve()
        if p.is_file():
            valides.append(str(p))
    res["files"] = valides[:5]
    print(json.dumps(res, ensure_ascii=False), flush=True)
    return 0
```

#### C. Prise en charge des fichiers dans `demander()`
Ajouter le paramètre `fichiers=None` à la signature :
```python
def demander(c, sid, question, chat=False, briefing=False, suivi=True, mesures=None, choix=None, fichiers=None):
```
Et dans la constitution du corps de la requête `/api/ask` :
```python
corps = dict(prompt=question, platform="desktop", user_id=c["USER_ID"], continue_session=True, timeout=delai)
if fichiers:
    corps["files"] = fichiers
```

#### D. Validation et encodage base64 dans `requete_ile()`
Dans `requete_ile()`, lire le tableau `fichiers` reçu sur stdin :
```python
    fichiers_bruts = r.get("fichiers") if isinstance(r.get("fichiers"), list) else []
    fichiers_payload = []
    for f in fichiers_bruts[:5]:
        if not isinstance(f, str) or not f.strip():
            continue
        ch = f.strip()
        if ch.startswith("file://"):
            ch = urllib.parse.unquote(urllib.parse.urlsplit(ch).path)
        p = Path(ch).expanduser().resolve()
        if not p.is_file():
            continue
        taille = p.stat().st_size
        if taille > 20 * 1024 * 1024:
            raise Erreur(T(f"Fichier trop volumineux ({p.name}) : maximum 20 Mo.",
                           f"File too large ({p.name}): maximum 20 MB."))
        try:
            data = p.read_bytes()
            fichiers_payload.append({
                "filename": p.name,
                "content_base64": base64.b64encode(data).decode("ascii"),
            })
        except OSError:
            raise Erreur(T(f"Impossible de lire le fichier {p.name}.",
                           f"Cannot read file {p.name}.")) from None
```
Puis transmettre `fichiers=fichiers_payload` à `demander(...)`.

#### E. Parser d'arguments CLI dans `main()`
- Ajouter `-f / --fichier` dans le sous-parseur `demander` :
  ```python
  p.add_argument("-f", "--fichier", "--file", dest="fichiers", action="append", default=[], help=T("Joindre un fichier", "Attach a file"))
  ```
- Ajouter le sous-parseur :
  ```python
  sous.add_parser("selectionner-fichiers", help=argparse.SUPPRESS)
  ```
- Dans le dispatch `main()` :
  ```python
  elif commande == "selectionner-fichiers":
      return selectionner_fichiers()
  ```

---

### 2. `home/.config/quickshell/nc-ile/AssistantEtat.qml`

#### A. État des fichiers et utilitaires
Ajouter dans le singleton :
```qml
    property var fichiersEnAttente: []
    property var fichiersEnCours: []
    function nomFichier(chemin) {
        var p = String(chemin || "").split("/").pop()
        return p || chemin
    }
    function ajouterFichier(chemin) {
        if (!chemin || typeof chemin !== "string") return
        var c = chemin.trim()
        if (c.indexOf("file://") === 0) {
            try { c = decodeURIComponent(c.replace(/^file:\/\//, "")) } catch (_) { c = c.replace(/^file:\/\//, "") }
        }
        if (!c) return
        if (fichiersEnAttente.some(f => f.chemin === c)) return
        if (fichiersEnAttente.length >= 5) {
            erreur = Reglages.t("5 fichiers maximum par envoi.", "5 files maximum per request.")
            return
        }
        fichiersEnAttente = fichiersEnAttente.concat([{chemin: c, nom: nomFichier(c)}])
    }
    function retirerFichier(index) {
        if (index >= 0 && index < fichiersEnAttente.length) {
            var f = fichiersEnAttente.slice()
            f.splice(index, 1)
            fichiersEnAttente = f
        }
    }
    function effacerFichiers() {
        fichiersEnAttente = []
    }
```

#### B. Historique et envoi
Adapter `ajouter()` pour accepter la liste des noms de fichiers :
```qml
    function ajouter(role, texte, choix, fichiers) {
        var m = messages.concat([{
            role: role,
            texte: texte,
            num: ++dernierId,
            choix: nettoyerChoix(choix),
            choixFait: "",
            fichiers: Array.isArray(fichiers) ? fichiers : []
        }]).slice(-24)
        var total = m.reduce((n, x) => n + x.texte.length, 0)
        while (total > 48000 && m.length > 1) total -= m.shift().texte.length
        messages = m
    }
```

Mettre à jour `envoyer()` pour consommer `fichiersEnAttente` et l'associer au message :
```qml
    function envoyer(texte) {
        var duBrouillon = typeof texte !== "string"
        var q = (duBrouillon ? brouillon : texte).trim()
        if (enCours || ecritureReglagesEnCours) return
        if (!q && (!duBrouillon || fichiersEnAttente.length === 0)) return
        if (q.length > 8000) {
            erreur = Reglages.t("8 000 caractères maximum.", "8,000 characters maximum.")
            return
        }
        question = q || (duBrouillon && fichiersEnAttente.length > 0 ? Reglages.t("Analyse ces fichiers", "Analyze these files") : "")
        if (!question) return
        erreur = ""
        reponse = ""
        sortieFinie = false
        processusFini = false
        code = -1
        enCours = true
        fichiersEnCours = duBrouillon ? fichiersEnAttente.slice() : []
        if (duBrouillon) fichiersEnAttente = []
        ajouter("vous", question, [], fichiersEnCours.map(f => f.nom))
        if (duBrouillon) brouillon = ""
        requete.stdinEnabled = true
        requete.running = true
        demarrage.restart()
    }
```

Et dans `terminer()`, restaurer les fichiers si une erreur réseau survient :
```qml
        if (code === 0 && r && r.ok === true && typeof r.text === "string" && r.text.length) {
            ...
            fichiersEnCours = []
        } else {
            ...
            if (!brouillon) brouillon = question
            if (fichiersEnAttente.length === 0 && fichiersEnCours.length > 0) {
                fichiersEnAttente = fichiersEnCours
            }
            fichiersEnCours = []
        }
```

#### C. Payload dans le processus `requete`
Dans `Process { id: requete }` :
```qml
write(JSON.stringify({
    session_id: a.session,
    question: a.question,
    historique: h,
    ia_du_bureau: a.suiteParIA,
    fichiers: a.fichiersEnCours.map(f => f.chemin)
}) + "\n")
```

---

### 3. `home/.config/quickshell/nc-ile/Assistant.qml`

#### A. Zone de glisser-déposer (`DropArea`)
Ajouter dans `Item { id: contenu ... }` :
```qml
        DropArea {
            id: zoneDepotFichiers
            anchors.fill: parent
            onDropped: drop => {
                var trouves = false
                if (drop.hasUrls && drop.urls.length > 0) {
                    for (let i = 0; i < drop.urls.length; i++) {
                        AssistantEtat.ajouterFichier(String(drop.urls[i]))
                        trouves = true
                    }
                } else if (drop.hasText && drop.text) {
                    var lignes = drop.text.split("\n")
                    for (let i = 0; i < lignes.length; i++) {
                        let l = lignes[i].trim()
                        if (l.indexOf("/") === 0 || l.indexOf("file://") === 0) {
                            AssistantEtat.ajouterFichier(l)
                            trouves = true
                        }
                    }
                }
                if (trouves) {
                    page.historiqueOuvert = true
                    drop.acceptProposedAction()
                }
            }
        }
```

#### B. Badges de pièces jointes dans la bulle message
Dans le `Repeater` de `AssistantEtat.messages`, sous `TextEdit { objectName: "nc-jarvis-message" ... }` :
```qml
                                Flow {
                                    id: fichiersMessage
                                    objectName: "nc-jarvis-message-files"
                                    visible: (modelData.fichiers || []).length > 0
                                    width: parent.width; spacing: 6; topPadding: 2
                                    Repeater {
                                        model: fichiersMessage.visible ? modelData.fichiers : []
                                        Rectangle {
                                            required property string modelData
                                            height: 22
                                            width: etiquetteFichier.implicitWidth + 16
                                            radius: 11
                                            color: Qt.alpha(page.cyan, 0.15)
                                            border.width: 1; border.color: Qt.alpha(page.cyan, 0.35)
                                            Row {
                                                id: etiquetteFichier
                                                anchors.centerIn: parent; spacing: 4
                                                Ico { taille: 12; nom: "attach_file"; color: page.cyan }
                                                Texte { text: modelData; font.pixelSize: 11; color: Couleurs.texte; font.weight: Font.DemiBold }
                                            }
                                        }
                                    }
                                }
```

#### C. Prévisualisation des pièces jointes en attente
Juste au-dessus de `Item { id: saisie ... }` :
```qml
        Flow {
            id: previsualisationFichiers
            objectName: "nc-jarvis-attachments-preview"
            visible: !AssistantEtat.reglagesOuverts && AssistantEtat.fichiersEnAttente.length > 0
            anchors.bottom: saisie.top; anchors.bottomMargin: 8
            width: parent.width; spacing: 6
            Repeater {
                model: AssistantEtat.fichiersEnAttente
                Rectangle {
                    required property var modelData
                    required property int index
                    height: 26
                    width: Math.min(previsualisationFichiers.width, pastilleContenu.implicitWidth + 28)
                    radius: 13
                    color: Couleurs.carte
                    border.width: 1; border.color: Couleurs.gris3
                    Row {
                        id: pastilleContenu
                        anchors.verticalCenter: parent.verticalCenter
                        anchors.left: parent.left; anchors.leftMargin: 8
                        spacing: 5
                        Ico {
                            taille: 14; nom: "attach_file"; color: page.cyan
                            anchors.verticalCenter: parent.verticalCenter
                        }
                        Texte {
                            text: modelData.nom
                            font.pixelSize: 11
                            color: Couleurs.texte
                            anchors.verticalCenter: parent.verticalCenter
                            elide: Text.ElideMiddle
                            width: Math.min(implicitWidth, previsualisationFichiers.width - 58)
                        }
                    }
                    MouseArea {
                        id: zoneSuppr
                        anchors.right: parent.right; anchors.rightMargin: 6
                        anchors.verticalCenter: parent.verticalCenter
                        width: 16; height: 16; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                        Ico {
                            anchors.centerIn: parent; taille: 12; nom: "close"
                            color: zoneSuppr.containsMouse ? Couleurs.rouge : Couleurs.texteSec
                        }
                        onClicked: AssistantEtat.retirerFichier(parent.index)
                    }
                }
            }
        }
```

Ajuster les ancrages de `accueil`, `modeles` et `statutDashboard` pour se caler sur `previsualisationFichiers.visible ? previsualisationFichiers.top : saisie.top`.

#### D. Bouton trombone dans `saisie`
Dans `Item { id: saisie ... }` :
```qml
            AssistantCommande {
                id: trombone
                objectName: "nc-jarvis-attach"
                visible: true
                anchors.right: (page.dansDashboard || !micro.visible) ? envoi.left : micro.left
                anchors.rightMargin: 8
                rond: true; width: 46; height: 46
                icone: "attach_file"
                choisi: AssistantEtat.fichiersEnAttente.length > 0
                aide: Reglages.t("Joindre des fichiers", "Attach files")
                enabled: !AssistantEtat.enCours && !AssistantEtat.ecritureReglagesEnCours
                onClic: page.ouvrirSelecteurFichiers()
            }
```
Et ancrer le champ de saisie sur `trombone.left` :
```qml
anchors.right: trombone.left
anchors.rightMargin: 8
```
Et activer `envoi` lorsque des fichiers sont en attente même sans texte :
```qml
choisi: !AssistantEtat.enCours && (AssistantEtat.brouillon.trim().length > 0 || AssistantEtat.fichiersEnAttente.length > 0)
enabled: !AssistantEtat.enCours && !AssistantEtat.ecritureReglagesEnCours && (AssistantEtat.brouillon.trim().length > 0 || AssistantEtat.fichiersEnAttente.length > 0)
```

#### E. Process `dialogueFichiers`
À la fin de `Assistant.qml` :
```qml
    function ouvrirSelecteurFichiers() {
        if (!dialogueFichiers.running && !AssistantEtat.enCours) dialogueFichiers.running = true
    }

    Process {
        id: dialogueFichiers
        command: [AssistantEtat.bin + "nc-assistant", "selectionner-fichiers"]
        stdout: StdioCollector {
            onStreamFinished: {
                try {
                    let d = JSON.parse(text)
                    if (d && d.ok === true && Array.isArray(d.files)) {
                        for (let i = 0; i < d.files.length; i++) {
                            AssistantEtat.ajouterFichier(d.files[i])
                        }
                        if (d.files.length > 0) page.historiqueOuvert = true
                    }
                } catch (_) {}
            }
        }
        stderr: StdioCollector {}
    }
```

---

## Validation

```bash
cd noctalia-config
python3 -B -m unittest tests.test_assistant_page -v
python3 -B -m unittest tests.test_assistant.Assistant.test_selectionner_fichiers_cli \
                       tests.test_assistant.Assistant.test_requete_ile_forwards_attachments_to_server \
                       tests.test_assistant.Assistant.test_requete_ile_rejects_oversized_file
```
EOF

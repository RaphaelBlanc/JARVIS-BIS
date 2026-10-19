# Commandes serveur whitelistées (`/server`)

Le bot Discord expose une commande `/server` pour les actions courantes sur la
machine qui héberge `assistant-perso`. Elle **n'exécute aucune commande
arbitraire** : seules les actions ci-dessous existent.

| Commande | Effet |
|---|---|
| `/server status` | État du gateway, du runner et des tunnels |
| `/server restart-runner` | Redémarre `assistant-runner` |
| `/server restart-gateway` | Programme le redémarrage de `assistant-gateway` dans 3 s |
| `/server tunnel-status` | État des tunnels Cloudflare et URL temporaire |
| `/server logs-gateway` | 40 dernières lignes du gateway |
| `/server logs-runner` | 40 dernières lignes du runner |

Les mêmes actions existent en préfixe : `!server status`, etc.

## Fonctionnement

Le bot appelle le script :

```text
scripts/server-control.sh <action>
```

Pour `restart-gateway`, le redémarrage est planifié via `systemd-run` afin de
ne pas tuer la commande qui est en train de répondre au bot.

## Installation de la version privilégiée (recommandé)

Si le service `assistant-gateway` tourne sous l'utilisateur `pi` (et non
`root`), installe le script en root et autorise uniquement ces actions :

```bash
sudo install -o root -g root -m 755 \
  ~/assistant-perso/scripts/server-control.sh \
  /usr/local/sbin/assistant-server-control
```

Crée ensuite une règle sudoers limitée :

```bash
sudo visudo -f /etc/sudoers.d/assistant-server-control
```

Contenu (adapte `pi` si nécessaire) :

```sudoers
pi ALL=(root) NOPASSWD: /usr/local/sbin/assistant-server-control restart-runner
pi ALL=(root) NOPASSWD: /usr/local/sbin/assistant-server-control restart-gateway
pi ALL=(root) NOPASSWD: /usr/local/sbin/assistant-server-control logs-gateway
pi ALL=(root) NOPASSWD: /usr/local/sbin/assistant-server-control logs-runner
```

Vérifie :

```bash
sudo -n /usr/local/sbin/assistant-server-control status
sudo -n /usr/local/sbin/assistant-server-control restart-runner
```

Le bot détecte automatiquement `/usr/local/sbin/assistant-server-control` et
l'utilise pour les actions privilégiées. `status` et `tunnel-status` restent
exécutés en tant qu'utilisateur, pour voir les services systemd utilisateur.

## Sans installation privilégiée

Le script source fonctionne aussi directement, mais il utilise `sudo -n` pour
chaque commande privilégiée. Il faut alors autoriser séparément `systemctl`,
`journalctl` et `systemd-run`, ce qui est moins propre. Si `sudo` demande un
mot de passe, le bot renvoie :

```text
✗ sudo sans mot de passe n'est pas configuré pour pi.
```

## Sécurité

- Aucune entrée utilisateur n'est passée à un shell : l'action est choisie
  dans une liste fermée.
- Les réponses sont éphémères dans Discord (`/server`) et tronquées à
  1 800 caractères.
- Les journaux peuvent contenir des données privées : ne donne pas accès au
  bot à des utilisateurs non fiables.
- Ne mets pas `ALL=(ALL) NOPASSWD: ALL` dans sudoers. Limite la règle aux
  actions ci-dessus.
- Le sandbox `agy` reste actif : ces commandes ne passent pas par `agy`, elles
  sont exécutées directement par le bot.

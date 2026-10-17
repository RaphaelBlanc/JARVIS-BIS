#!/usr/bin/env python3
"""Script d'authentification Google OAuth pour Antigravity & MCP.

Permet de connecter facilement un compte Google (Gmail, Google Calendar, Google Drive)
et de configurer automatiquement le serveur MCP dans Antigravity.
"""

import argparse
import http.server
import json
import os
import socketserver
import sys
import urllib.parse
import webbrowser
from pathlib import Path

try:
    import httpx
except ImportError:
    print("❌ Erreur : httpx n'est pas installé dans l'environnement courant.")
    print("👉 Exécutez : /home/xenospacex/Bureau/assistant-perso/home-runner/.venv/bin/python setup_oauth.py")
    sys.exit(1)

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/tasks",
]

REDIRECT_PORT = 8085
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/"
AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

GEMINI_CONFIG_PATH = Path.home() / ".gemini" / "config" / "mcp_config.json"
OUTPUT_TOKEN_FILE = Path(__file__).resolve().parent / "token.json"


class OAuthCallbackHandler(http.server.BaseHTTPRequestHandler):
    """Serveur HTTP temporaire pour intercepter le code d'autorisation OAuth."""

    auth_code = None
    error = None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if "code" in params:
            OAuthCallbackHandler.auth_code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            html = """
            <!DOCTYPE html>
            <html lang="fr">
            <head><meta charset="utf-8"><title>Connexion Réussie</title></head>
            <body style="font-family: system-ui, sans-serif; text-align: center; padding: 50px; background: #0d1117; color: #c9d1d9;">
                <h1 style="color: #3fb950;">✅ Connexion Google Réussie !</h1>
                <p style="font-size: 1.2rem;">Votre compte Google a été autorisé avec succès pour Antigravity.</p>
                <p>Vous pouvez fermer cet onglet et revenir dans votre terminal.</p>
            </body>
            </html>
            """
            self.wfile.write(html.encode("utf-8"))
        elif "error" in params:
            OAuthCallbackHandler.error = params["error"][0]
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"<h1>Erreur : {OAuthCallbackHandler.error}</h1>".encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # Silence les requêtes HTTP standard
        return


def load_credentials_file(file_path: Path) -> tuple[str, str]:
    """Charge le client_id et client_secret depuis un fichier credentials.json téléchargé."""
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Google stocke les infos soit dans "installed", soit dans "web"
    root = data.get("installed") or data.get("web") or data
    client_id = root.get("client_id")
    client_secret = root.get("client_secret")

    if not client_id or not client_secret:
        raise ValueError(f"Impossible de trouver client_id et client_secret dans {file_path}")
    return client_id, client_secret


def update_gemini_mcp_config(client_id: str, client_secret: str, refresh_token: str) -> None:
    """Enregistre le serveur MCP google-workspace dans ~/.gemini/config/mcp_config.json."""
    GEMINI_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

    config = {}
    if GEMINI_CONFIG_PATH.exists() and GEMINI_CONFIG_PATH.stat().st_size > 0:
        try:
            with open(GEMINI_CONFIG_PATH, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception:
            config = {}

    mcp_servers = config.setdefault("mcpServers", {})
    mcp_servers["google-workspace"] = {
        "command": "npx",
        "args": ["-y", "mcp-server-google-workspace"],
        "env": {
            "GOOGLE_CLIENT_ID": client_id,
            "GOOGLE_CLIENT_SECRET": client_secret,
            "GOOGLE_REFRESH_TOKEN": refresh_token,
        },
    }

    with open(GEMINI_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write("\n")

    os.chmod(GEMINI_CONFIG_PATH, 0o600)
    print(f"✅ Configuration MCP mise à jour dans : {GEMINI_CONFIG_PATH}")


def test_api_access(access_token: str) -> None:
    """Teste l'accès réel aux APIs Google avec le token généré."""
    headers = {"Authorization": f"Bearer {access_token}"}
    print("\n🔍 Test de connectivité avec les services Google...")

    with httpx.Client(timeout=10) as client:
        # Test 1 : Gmail
        try:
            res_gmail = client.get("https://gmail.googleapis.com/gmail/v1/users/me/profile", headers=headers)
            if res_gmail.status_code == 200:
                gmail_data = res_gmail.json()
                print(f"  ✉️  Gmail : Connecté avec succès ({gmail_data.get('emailAddress')}) - {gmail_data.get('messagesTotal')} messages")
            else:
                print(f"  ⚠️ Gmail : Erreur HTTP {res_gmail.status_code} ({res_gmail.text})")
        except Exception as e:
            print(f"  ❌ Gmail : Erreur ({e})")

        # Test 2 : Calendar
        try:
            res_cal = client.get("https://www.googleapis.com/calendar/v3/users/me/calendarList?maxResults=5", headers=headers)
            if res_cal.status_code == 200:
                cal_data = res_cal.json()
                cals = [item.get("summary") for item in cal_data.get("items", [])]
                print(f"  📅 Google Calendar : Connecté avec succès (Calendriers : {', '.join(cals[:3])})")
            else:
                print(f"  ⚠️ Calendar : Erreur HTTP {res_cal.status_code} ({res_cal.text})")
        except Exception as e:
            print(f"  ❌ Calendar : Erreur ({e})")

        # Test 3 : Google Drive
        try:
            res_drive = client.get("https://www.googleapis.com/drive/v3/files?pageSize=5&fields=files(id,name)", headers=headers)
            if res_drive.status_code == 200:
                drive_data = res_drive.json()
                files = [f.get("name") for f in drive_data.get("files", [])]
                print(f"  📁 Google Drive : Connecté avec succès ({len(files)} fichiers récents trouvés)")
            else:
                print(f"  ⚠️ Google Drive : Erreur HTTP {res_drive.status_code} ({res_drive.text})")
        except Exception as e:
            print(f"  ❌ Google Drive : Erreur ({e})")

        # Test 4 : Google Tasks
        try:
            res_tasks = client.get("https://tasks.googleapis.com/tasks/v1/users/@me/lists", headers=headers)
            if res_tasks.status_code == 200:
                tasks_data = res_tasks.json()
                lists = [item.get("title") for item in tasks_data.get("items", [])]
                print(f"  📋 Google Tasks : Connecté avec succès ({len(lists)} liste(s) trouvée(s) : {', '.join(lists)})")
            else:
                print(f"  ⚠️ Google Tasks : Erreur HTTP {res_tasks.status_code} ({res_tasks.text})")
        except Exception as e:
            print(f"  ❌ Google Tasks : Erreur ({e})")


def main() -> None:
    parser = argparse.ArgumentParser(description="Authentification Google Workspace pour Antigravity MCP")
    parser.add_argument("--credentials", "-c", type=Path, help="Chemin vers le fichier credentials.json téléchargé depuis Google Cloud")
    parser.add_argument("--client-id", type=str, help="Client ID Google OAuth")
    parser.add_argument("--client-secret", type=str, help="Client Secret Google OAuth")
    args = parser.parse_args()

    client_id = args.client_id
    client_secret = args.client_secret

    if args.credentials and args.credentials.exists():
        client_id, client_secret = load_credentials_file(args.credentials)
    elif not client_id or not client_secret:
        # Recherche automatique d'un credentials.json dans le répertoire
        local_creds = Path(__file__).resolve().parent / "credentials.json"
        if local_creds.exists():
            print(f"📄 Détection automatique de {local_creds}")
            client_id, client_secret = load_credentials_file(local_creds)
        else:
            print("❌ Veuillez fournir soit un fichier credentials.json (--credentials), soit --client-id et --client-secret.")
            print("\nExemple d'utilisation :")
            print("  python setup_oauth.py --credentials /chemin/credentials.json")
            print("  ou")
            print("  python setup_oauth.py --client-id XXX --client-secret YYY")
            sys.exit(1)

    print("\n" + "=" * 60)
    print("🚀 Démarrage du flux d'autorisation Google OAuth")
    print("=" * 60)
    print(f"🔑 Client ID : {client_id[:25]}...")
    print(f"🌐 Port de redirection local : {REDIRECT_PORT}")

    # Construction de l'URL d'autorisation
    auth_params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
    }
    auth_url = f"{AUTH_ENDPOINT}?{urllib.parse.urlencode(auth_params)}"

    # Lancement du serveur HTTP local d'interception
    class ReuseAddressServer(socketserver.TCPServer):
        allow_reuse_address = True

    server = ReuseAddressServer(("localhost", REDIRECT_PORT), OAuthCallbackHandler)

    print("\n👉 Ouverture de la page de consentement Google dans votre navigateur...")
    print(f"🔗 Si le navigateur ne s'ouvre pas automatiquement, cliquez ici :\n{auth_url}\n")

    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    print("⏳ En attente de l'autorisation dans le navigateur...")
    while OAuthCallbackHandler.auth_code is None and OAuthCallbackHandler.error is None:
        server.handle_request()

    server.server_close()

    if OAuthCallbackHandler.error:
        print(f"\n❌ Erreur reçue de Google : {OAuthCallbackHandler.error}")
        sys.exit(1)

    code = OAuthCallbackHandler.auth_code
    print("✅ Code d'autorisation reçu !")

    # Échange du code contre tokens
    print("🔄 Échange du code d'autorisation contre un Refresh Token...")
    token_payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
    }

    with httpx.Client(timeout=20) as client:
        resp = client.post(TOKEN_ENDPOINT, data=token_payload)
        if resp.status_code != 200:
            print(f"❌ Échec de l'obtention du token : HTTP {resp.status_code}\n{resp.text}")
            sys.exit(1)

        token_data = resp.json()

    refresh_token = token_data.get("refresh_token")
    access_token = token_data.get("access_token")

    if not refresh_token:
        print("⚠️ Aucun refresh_token retourné. Si vous aviez déjà autorisé l'appli, Google ne renvoie pas de nouveau refresh_token.")
        print("👉 Assurez-vous d'avoir 'prompt=consent' ou révoquez les accès de l'app sur https://myaccount.google.com/permissions")
        sys.exit(1)

    # Sauvegarde locale sécurisée
    full_token_info = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "scope": token_data.get("scope"),
        "token_type": token_data.get("token_type"),
    }
    with open(OUTPUT_TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(full_token_info, f, indent=2)
    os.chmod(OUTPUT_TOKEN_FILE, 0o600)
    print(f"💾 Token sauvegardé en local : {OUTPUT_TOKEN_FILE}")

    # Mise à jour de la config Antigravity MCP
    update_gemini_mcp_config(client_id, client_secret, refresh_token)

    # Test immédiat
    if access_token:
        test_api_access(access_token)

    print("\n" + "=" * 60)
    print("🎉 SUCCÈS ! Antigravity dispose maintenant des accès suivants :")
    print("  • 📅 Google Calendar (consultation, création, modification d'événements)")
    print("  • ✉️ Gmail (lecture, recherche, envoi, tri des e-mails)")
    print("  • 📁 Google Drive (recherche, téléchargement, création de fichiers/dossiers)")
    print("=" * 60)


if __name__ == "__main__":
    main()

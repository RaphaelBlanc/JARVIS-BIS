"""
core/config.py — Configuration globale et chemins d'accès pour l'assistant personnel unifié.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Charge les variables depuis .env s'il existe
ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")

# Répertoire de données central (JSON, persistance, budgets, etc.)
DATA_DIR = Path(os.getenv("ASSISTANT_DATA_DIR", str(ROOT_DIR / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Mode de déploiement :
# - "local" : Gateway, Bots, Runner et Hardware direct sur le PC
# - "server" : Tourne sur un serveur distant 24/7 (les commandes matérielles nécessitent pc-agent)
# - "pc-agent" : Tourne sur le PC local en client du serveur distant
DEPLOY_MODE = os.getenv("DEPLOY_MODE", "local").strip().lower()

# Clé secrète partagée
SECRET_KEY = os.getenv("SECRET_KEY", "")

# Tokens bots
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")

# URLs et ports
PORT = int(os.getenv("PORT", "8000"))
HOME_SERVER_URL = os.getenv("HOME_SERVER_URL", "http://127.0.0.1:8080")
GATEWAY_PUBLIC_URL = os.getenv("GATEWAY_PUBLIC_URL", "").rstrip("/")

# Coffre Obsidian par défaut
OBSIDIAN_VAULT_PATH = Path(
    os.getenv(
        "OBSIDIAN_VAULT_PATH",
        str(Path.home() / "Documents" / "Apps" / "Obsidian" / "vaults" / "showcase-vault"),
    )
)

# Moodle
MOODLE_URL = os.getenv("MOODLE_URL", "https://moodle.epita.fr").rstrip("/")
MOODLE_API_KEY = os.getenv("MOODLE_API_KEY", "").strip()

# Gemini API pour OCR direct
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

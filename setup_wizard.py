#!/usr/bin/env python3
"""
setup_wizard.py — Assistant d'installation interactif pour l'Assistant Personnel.

Configure l'assistant (en local sur votre PC ou sur votre serveur hébergé)
en sélectionnant les outils souhaités (façon MCP) et en saisissant les tokens nécessaires.
"""

import getpass
import os
from pathlib import Path
import secrets
import sys

ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR))

from core.tools.base import ToolRegistry
import core.tools.definitions  # noqa: F401

CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


def print_banner():
    banner = f"""{CYAN}{BOLD}
╔═══════════════════════════════════════════════════════════════════════╗
║                                                                       ║
║             🤖 ASSISTANT PERSONNEL — INSTALLATION                     ║
║            Configuration des Bots et des Modules MCP                  ║
║                                                                       ║
╚═══════════════════════════════════════════════════════════════════════╝{RESET}
"""
    print(banner)


def ask_bool(prompt: str, default: bool = True) -> bool:
    default_str = "O/n" if default else "o/N"
    res = input(f"{BOLD}{prompt}{RESET} ({default_str}) : ").strip().lower()
    if not res:
        return default
    return res in ("o", "oui", "y", "yes", "1")


def ask_string(prompt: str, default: str = "", secret: bool = False) -> str:
    prompt_full = f"{BOLD}{prompt}{RESET}"
    if default:
        prompt_full += f" {DIM}(défaut: {default}){RESET}"
    prompt_full += " : "

    if secret:
        val = getpass.getpass(prompt_full).strip()
    else:
        val = input(prompt_full).strip()

    return val if val else default


def main():
    print_banner()

    env_config: dict[str, str] = {}
    env_file = ROOT_DIR / ".env"

    if env_file.exists():
        print(f"{YELLOW}ℹ️  Fichier .env existant détecté.{RESET}")
        try:
            with open(env_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        env_config[k.strip()] = v.strip().strip("'\"")
        except Exception:
            pass

    # ── ÉTAPE 1 : Configuration Réseau & Sécurité ──────────────────────────────
    print(f"\n{CYAN}{BOLD}--- ÉTAPE 1 : Sécurité & Réseau ---{RESET}")

    current_secret = env_config.get("SECRET_KEY", "")
    if not current_secret:
        current_secret = secrets.token_hex(24)

    secret_key = ask_string("Clé secrète d'API partagée (SECRET_KEY)", default=current_secret)
    env_config["SECRET_KEY"] = secret_key

    port = ask_string("Port d'écoute du Gateway", default=env_config.get("PORT", "8000"))
    env_config["PORT"] = port

    home_server_url = ask_string(
        "URL interne du Runner IA (home-runner)",
        default=env_config.get("HOME_SERVER_URL", "http://127.0.0.1:8080"),
    )
    env_config["HOME_SERVER_URL"] = home_server_url

    # ── ÉTAPE 2 : Messagerie & Plateformes ─────────────────────────────────────
    print(f"\n{CYAN}{BOLD}--- ÉTAPE 2 : Plateformes de Messagerie ---{RESET}")

    # Telegram
    enable_tg = ask_bool("Activer le bot Telegram ?", default=bool(env_config.get("TELEGRAM_TOKEN")))
    if enable_tg:
        tg_token = ask_string("Token du Bot Telegram (@BotFather)", default=env_config.get("TELEGRAM_TOKEN", ""), secret=True)
        env_config["TELEGRAM_TOKEN"] = tg_token
        tg_users = ask_string(
            "IDs utilisateurs Telegram autorisés (séparés par virgules, ou vide pour tous)",
            default=env_config.get("TELEGRAM_ALLOWED_USER_IDS", ""),
        )
        if tg_users:
            env_config["TELEGRAM_ALLOWED_USER_IDS"] = tg_users
    else:
        env_config["TELEGRAM_TOKEN"] = ""

    # Discord
    enable_dc = ask_bool("Activer le bot Discord ?", default=bool(env_config.get("DISCORD_TOKEN")))
    if enable_dc:
        dc_token = ask_string("Token du Bot Discord Developer Portal", default=env_config.get("DISCORD_TOKEN", ""), secret=True)
        env_config["DISCORD_TOKEN"] = dc_token
        dc_guild = ask_string("ID du Serveur Discord (Guild ID, recommandé)", default=env_config.get("DISCORD_GUILD_ID", ""))
        if dc_guild:
            env_config["DISCORD_GUILD_ID"] = dc_guild
        dc_users = ask_string(
            "IDs utilisateurs Discord autorisés (séparés par virgules, ou vide pour tous)",
            default=env_config.get("DISCORD_ALLOWED_USER_IDS", ""),
        )
        if dc_users:
            env_config["DISCORD_ALLOWED_USER_IDS"] = dc_users
    else:
        env_config["DISCORD_TOKEN"] = ""

    # ── ÉTAPE 3 : Outils & Services (Style MCP) ───────────────────────────────
    print(f"\n{CYAN}{BOLD}--- ÉTAPE 3 : Choix des Outils & Modules (Style MCP) ---{RESET}")

    tools = ToolRegistry.list_tools()
    for tool in tools:
        current_active = env_config.get(
            f"ENABLE_TOOL_{tool.id.upper()}",
            "1" if tool.id in ("shopping", "focus", "ideas", "projects", "cron") else "0",
        ) in ("1", "true", "yes", "on")

        print(f"\n{BOLD}{tool.icon} {tool.name} (id: {tool.id}){RESET}")
        print(f"  {DIM}{tool.description}{RESET}")

        activate = ask_bool(f"Activer le module {tool.name} ?", default=current_active)
        env_config[f"ENABLE_TOOL_{tool.id.upper()}"] = "1" if activate else "0"

        if activate and tool.params:
            print(f"  {CYAN}Configuration pour {tool.name} :{RESET}")
            for p in tool.params:
                curr_val = env_config.get(p.name, str(p.default))
                val = ask_string(f"    • {p.label}", default=curr_val, secret=p.secret)
                env_config[p.name] = val

    # ── ÉTAPE 4 : Sauvegarde .env ──────────────────────────────────────────────
    print(f"\n{CYAN}{BOLD}--- ÉTAPE 4 : Enregistrement ---{RESET}")

    lines = [
        "# ===================================================================",
        "# Assistant Personnel — Configuration .env",
        "# ===================================================================",
        "",
    ]

    for k, v in sorted(env_config.items()):
        if any(c in v for c in (" ", "#", "\n", "'", '"')):
            lines.append(f'{k}="{v}"')
        else:
            lines.append(f"{k}={v}")

    lines.append("")

    with open(env_file, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    try:
        os.chmod(env_file, 0o600)
    except Exception:
        pass

    print(f"{GREEN}✅ Fichier .env enregistré avec succès (permissions 0600).{RESET}")
    print(f"\nPour lancer l'assistant : {BOLD}./deploy.sh start{RESET}\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{YELLOW}Installation annulée.{RESET}")
        sys.exit(1)

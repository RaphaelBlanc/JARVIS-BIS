#!/usr/bin/env python3
"""
setup_wizard.py — Assistant d'installation interactif pour l'Assistant Personnel unifié.

Permet de configurer l'assistant en mode LOCAL ou SERVEUR DISTANT ou PC-AGENT,
et de choisir interactivement les modules/outils à activer (comme des MCP)
en saisissant les tokens nécessaires (Discord, Telegram, Moodle, Obsidian, etc.).
"""

import getpass
import os
from pathlib import Path
import re
import secrets
import sys

# Assure que le dossier courant est dans le PYTHONPATH
ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR))

from core.tools.base import ToolRegistry
import core.tools.definitions  # noqa: F401


# Couleurs ANSI pour un affichage soigné dans le terminal
CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


def print_banner():
    banner = f"""{CYAN}{BOLD}
╔═══════════════════════════════════════════════════════════════════════╗
║                                                                       ║
║          🤖 ASSISTANT PERSONNEL UNIFIÉ — INSTALLATEUR                ║
║          Architecture Hybride : Local / Serveur & Outils MCP          ║
║                                                                       ║
╚═══════════════════════════════════════════════════════════════════════╝{RESET}
"""
    print(banner)


def ask_choice(prompt: str, choices: list[str], default: str) -> str:
    choices_str = "/".join([f"[{c}]" if c == default else c for c in choices])
    while True:
        res = input(f"{BOLD}{prompt}{RESET} ({choices_str}) : ").strip().lower()
        if not res:
            return default
        for c in choices:
            if res == c.lower() or res == c[0].lower():
                return c
        print(f"{YELLOW}Option invalide. Choisissez parmi {choices}.{RESET}")


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

    # Charge les variables existantes si le fichier .env existe déjà
    if env_file.exists():
        print(f"{YELLOW}ℹ️  Un fichier .env existant a été détecté.{RESET}")
        try:
            with open(env_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        env_config[k.strip()] = v.strip().strip("'\"")
        except Exception:
            pass

    # ── ÉTAPE 1 : Mode de déploiement ──────────────────────────────────────────
    print(f"\n{CYAN}{BOLD}--- ÉTAPE 1 : Choix du Mode de Déploiement ---{RESET}")
    print(f"  {BOLD}1. Local{RESET}      : Tourne directement sur ce PC (accès matériel Wayland complet, bots, API).")
    print(f"  {BOLD}2. Serveur{RESET}    : Tourne 24/7 sur un serveur distant (VPS, Mini-PC, Render).")
    print(f"  {BOLD}3. PC-Agent{RESET}   : Agent léger pour connecter ce PC local à un serveur distant existant.")

    mode_choice = ask_choice("Mode d'installation souhaité", ["local", "serveur", "pc-agent"], "local")
    if mode_choice == "serveur":
        deploy_mode = "server"
    elif mode_choice == "pc-agent":
        deploy_mode = "pc-agent"
    else:
        deploy_mode = "local"

    env_config["DEPLOY_MODE"] = deploy_mode

    # ── ÉTAPE 2 : Sécurité & Réseau ───────────────────────────────────────────
    print(f"\n{CYAN}{BOLD}--- ÉTAPE 2 : Sécurité & Réseau ---{RESET}")

    current_secret = env_config.get("SECRET_KEY", "")
    if not current_secret:
        current_secret = secrets.token_hex(24)

    secret_key = ask_string("Clé secrète d'API partagée (SECRET_KEY)", default=current_secret)
    env_config["SECRET_KEY"] = secret_key

    if deploy_mode == "pc-agent":
        server_url = ask_string(
            "URL du serveur distant (ex: https://assistant.mondomaine.fr ou http://100.x.y.z:8000)",
            default=env_config.get("HOME_SERVER_URL", "http://127.0.0.1:8000"),
        )
        env_config["HOME_SERVER_URL"] = server_url
        env_config["GATEWAY_PUBLIC_URL"] = server_url
        print(f"\n{GREEN}✅ Configuration du PC-Agent terminée !{RESET}")

    else:
        port = ask_string("Port d'écoute du Gateway", default=env_config.get("PORT", "8000"))
        env_config["PORT"] = port

        home_server_url = ask_string(
            "URL interne du Runner IA (home-runner)",
            default=env_config.get("HOME_SERVER_URL", "http://127.0.0.1:8080"),
        )
        env_config["HOME_SERVER_URL"] = home_server_url

        pub_url = ask_string(
            "URL publique HTTPS (Tunnel Cloudflare ou Tailscale, optionnel)",
            default=env_config.get("GATEWAY_PUBLIC_URL", ""),
        )
        if pub_url:
            env_config["GATEWAY_PUBLIC_URL"] = pub_url

        # ── ÉTAPE 3 : Messagerie & Plateformes ─────────────────────────────────
        print(f"\n{CYAN}{BOLD}--- ÉTAPE 3 : Plateformes de Messagerie ---{RESET}")

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

        # ── ÉTAPE 4 : Modules & Outils (comme des MCP) ─────────────────────────
        print(f"\n{CYAN}{BOLD}--- ÉTAPE 4 : Activation des Outils & Services (Style MCP) ---{RESET}")
        print("Pour chaque module, vous pouvez choisir de l'activer et renseigner ses accès.\n")

        tools = ToolRegistry.list_tools()
        for tool in tools:
            # Vérifie si activé par défaut
            current_active = env_config.get(f"ENABLE_TOOL_{tool.id.upper()}", "1" if tool.id in ("shopping", "focus", "ideas", "projects", "cron") else "0") in ("1", "true", "yes", "on")

            print(f"\n{BOLD}{tool.icon} {tool.name} (id: {tool.id}){RESET}")
            print(f"  {DIM}{tool.description}{RESET}")
            if tool.requires_local_hardware and deploy_mode == "server":
                print(f"  {YELLOW}⚠️  Nécessite le PC local : sur serveur, ces commandes passeront par le pc-agent.{RESET}")

            activate = ask_bool(f"Activer le module {tool.name} ?", default=current_active)
            env_config[f"ENABLE_TOOL_{tool.id.upper()}"] = "1" if activate else "0"

            if activate and tool.params:
                print(f"  {CYAN}Configuration pour {tool.name} :{RESET}")
                for p in tool.params:
                    curr_val = env_config.get(p.name, str(p.default))
                    val = ask_string(f"    • {p.label}", default=curr_val, secret=p.secret)
                    env_config[p.name] = val

    # ── ÉTAPE 5 : Sauvegarde dans .env ─────────────────────────────────────────
    print(f"\n{CYAN}{BOLD}--- ÉTAPE 5 : Écriture de la Configuration ---{RESET}")

    lines = [
        "# ===================================================================",
        "# Assistant Personnel Unifié — Fichier de configuration .env",
        "# Généré automatiquement par setup_wizard.py",
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

    # Permissions strictes (0600)
    try:
        os.chmod(env_file, 0o600)
    except Exception:
        pass

    print(f"{GREEN}✅ Fichier .env enregistré avec succès (permissions 0600).{RESET}")

    # Résumé
    print(f"\n{GREEN}{BOLD}═══════════════════════════════════════════════════════════════════════{RESET}")
    print(f"{GREEN}{BOLD}🎉 INSTALLATION ET CONFIGURATION TERMINÉES AVEC SUCCÈS !{RESET}")
    print(f"{GREEN}{BOLD}═══════════════════════════════════════════════════════════════════════{RESET}")
    print(f"• Mode sélectionné : {BOLD}{deploy_mode.upper()}{RESET}")
    if deploy_mode == "local":
        print("• Lancer l'assistant :  ./deploy.sh start")
    elif deploy_mode == "server":
        print("• Lancer le serveur :    ./deploy.sh start")
        print("• Lier votre PC local :  sur votre PC, lancez `python3 pc-agent/agent.py`")
    elif deploy_mode == "pc-agent":
        print("• Démarrer le PC-Agent : python3 pc-agent/agent.py")
    print(f"• API Noctalia disponible : docs/NOCTALIA_API.md")
    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{YELLOW}Installation annulée.{RESET}")
        sys.exit(1)

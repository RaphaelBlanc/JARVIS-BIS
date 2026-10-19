"""Security module for JARVIS: PIN code protection for sensitive operations.

Protects dangerous commands (/run, /kill, /get, system changes) with a 4-digit PIN.
Provides an interactive in-memory unlock window (default: 10 minutes) with SHA-256 salted hash.
"""

from datetime import datetime
import hashlib
import json
import logging
import hmac
import os
from pathlib import Path
import time
import uuid

from core.config import DATA_DIR
SECURITY_FILE = DATA_DIR / "security.json"

PBKDF2_ITERATIONS = 100_000

# In-memory session tracking: user_id -> unlock timestamp
UNLOCKED_SESSIONS: dict[int, float] = {}
# Pending pin entry: user_id -> {"current_digits": "", "callback_action": ..., "message_id": ...}
PENDING_PIN_ENTRIES: dict[int, dict] = {}

# In-memory cached config to eliminate disk read latency during PIN verification
_CACHED_SECURITY_CFG: dict | None = None


def _hash_pin_pbkdf2(pin: str, salt: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    """Computes PBKDF2-HMAC-SHA256 hash (fast: ~15ms, resistant against brute-force)."""
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        pin.encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    )
    return dk.hex()


def _hash_pin_legacy(pin: str, salt: str) -> str:
    """Legacy SHA-256 hash for backward compatibility and transparent migration."""
    return hashlib.sha256(f"{salt}:{pin}".encode("utf-8")).hexdigest()


def load_security_config(force_reload: bool = False) -> dict:
    """Loads security config from memory cache or disk.

    Ensures file permissions are restricted to owner only (0600).
    """
    global _CACHED_SECURITY_CFG
    if _CACHED_SECURITY_CFG is not None and not force_reload:
        return _CACHED_SECURITY_CFG

    if not SECURITY_FILE.exists():
        cfg = {
            "pin_configured": False,
            "pin_hash": None,
            "salt": None,
            "algo": "pbkdf2",
            "iterations": PBKDF2_ITERATIONS,
            "session_duration_minutes": 10,
            "failed_attempts": 0,
            "locked_until": 0.0,
            "protected_commands": ["run", "kill", "get", "lock", "guard"],
        }
        _CACHED_SECURITY_CFG = cfg
        return cfg

    try:
        # Enforce strict 0600 file permissions on security.json
        try:
            SECURITY_FILE.chmod(0o600)
        except Exception:
            pass

        with open(SECURITY_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
            _CACHED_SECURITY_CFG = cfg
            return cfg
    except Exception as e:
        logging.warning(f"Failed to read security config: {e}")
        return {"pin_configured": False}


def save_security_config(cfg: dict) -> bool:
    """Saves security config atomically and sets file permissions to 0600 (owner only)."""
    global _CACHED_SECURITY_CFG
    try:
        tmp_file = SECURITY_FILE.with_suffix(".tmp")
        # Create tmp file with 0600 permissions immediately
        fd = os.open(str(tmp_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with open(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        os.replace(tmp_file, SECURITY_FILE)
        try:
            SECURITY_FILE.chmod(0o600)
        except Exception:
            pass
        _CACHED_SECURITY_CFG = cfg
        return True
    except Exception as e:
        logging.error(f"Failed to save security config: {e}")
        return False


def set_user_pin(pin: str) -> tuple[bool, str]:
    """Sets a new 4-digit PIN using PBKDF2-HMAC-SHA256."""
    if not pin.isdigit() or len(pin) != 4:
        return False, "Le code PIN doit être composé exactement de 4 chiffres."
    cfg = load_security_config()
    salt = uuid.uuid4().hex
    cfg["salt"] = salt
    cfg["algo"] = "pbkdf2"
    cfg["iterations"] = PBKDF2_ITERATIONS
    cfg["pin_hash"] = _hash_pin_pbkdf2(pin, salt, PBKDF2_ITERATIONS)
    cfg["pin_configured"] = True
    cfg["failed_attempts"] = 0
    cfg["locked_until"] = 0.0
    save_security_config(cfg)
    return True, "Code PIN de sécurité configuré avec succès !"


def is_pin_configured() -> bool:
    cfg = load_security_config()
    return bool(cfg.get("pin_configured") and cfg.get("pin_hash"))


def is_user_authenticated(user_id: int) -> bool:
    """Checks if the user currently holds an active unlocked session."""
    if not is_pin_configured():
        return True  # If no PIN configured, allow access
    unlock_time = UNLOCKED_SESSIONS.get(user_id, 0.0)
    cfg = load_security_config()
    duration_secs = cfg.get("session_duration_minutes", 10) * 60
    return (time.time() - unlock_time) < duration_secs


def verify_pin(user_id: int, entered_pin: str) -> tuple[bool, str]:
    """Verifies user PIN and grants session access using constant-time comparison."""
    cfg = load_security_config()
    if not is_pin_configured():
        return True, "Aucun code PIN configuré."

    now = time.time()
    if now < cfg.get("locked_until", 0.0):
        remaining = int(cfg["locked_until"] - now)
        return False, f"Trop de tentatives erronées. Réessayez dans {remaining} secondes."

    expected_hash = cfg.get("pin_hash", "")
    salt = cfg.get("salt", "")
    algo = cfg.get("algo", "legacy_sha256")
    iterations = cfg.get("iterations", PBKDF2_ITERATIONS)

    # Compute hash according to algorithm
    if algo == "pbkdf2":
        computed_hash = _hash_pin_pbkdf2(entered_pin, salt, iterations)
    else:
        # Legacy SHA-256
        computed_hash = _hash_pin_legacy(entered_pin, salt)

    # Constant-time comparison to prevent timing attacks
    is_valid = hmac.compare_digest(computed_hash, expected_hash)

    if is_valid:
        UNLOCKED_SESSIONS[user_id] = now
        cfg["failed_attempts"] = 0

        # Automatic seamless migration from legacy SHA-256 to PBKDF2
        if algo != "pbkdf2":
            new_salt = uuid.uuid4().hex
            cfg["salt"] = new_salt
            cfg["algo"] = "pbkdf2"
            cfg["iterations"] = PBKDF2_ITERATIONS
            cfg["pin_hash"] = _hash_pin_pbkdf2(entered_pin, new_salt, PBKDF2_ITERATIONS)

        save_security_config(cfg)
        return True, "Authentification réussie ! Session déverrouillée pour 10 minutes."
    else:
        cfg["failed_attempts"] = cfg.get("failed_attempts", 0) + 1
        if cfg["failed_attempts"] >= 3:
            cfg["locked_until"] = now + 180  # Lock 3 minutes
            save_security_config(cfg)
            return False, "Code PIN incorrect (3e échec). Système verrouillé pour 3 minutes."
        save_security_config(cfg)
        return False, f"Code PIN incorrect ({cfg['failed_attempts']}/3)."


def lock_user_session(user_id: int) -> None:
    UNLOCKED_SESSIONS.pop(user_id, None)
    PENDING_PIN_ENTRIES.pop(user_id, None)


def build_pin_keyboard(masked_digits: str = "") -> list:
    """Generates a secure interactive numerical keypad."""
    from telegram import InlineKeyboardButton
    display = "● " * len(masked_digits) + "○ " * (4 - len(masked_digits))
    keyboard = [
        [InlineKeyboardButton(f"Saisie : [ {display.strip()} ]", callback_data="pin:display")],
        [
            InlineKeyboardButton("1", callback_data="pin:num:1"),
            InlineKeyboardButton("2", callback_data="pin:num:2"),
            InlineKeyboardButton("3", callback_data="pin:num:3"),
        ],
        [
            InlineKeyboardButton("4", callback_data="pin:num:4"),
            InlineKeyboardButton("5", callback_data="pin:num:5"),
            InlineKeyboardButton("6", callback_data="pin:num:6"),
        ],
        [
            InlineKeyboardButton("7", callback_data="pin:num:7"),
            InlineKeyboardButton("8", callback_data="pin:num:8"),
            InlineKeyboardButton("9", callback_data="pin:num:9"),
        ],
        [
            InlineKeyboardButton("⌫ Effacer", callback_data="pin:del"),
            InlineKeyboardButton("0", callback_data="pin:num:0"),
            InlineKeyboardButton("❌ Annuler", callback_data="pin:cancel"),
        ]
    ]
    return keyboard


if __name__ == "__main__":
    import getpass
    import sys

    print("=" * 60)
    print("🔒 JARVIS - Configuration Sécurisée du Code PIN Local")
    print("=" * 60)
    
    cfg = load_security_config()
    status = "Actif" if is_pin_configured() else "Non configuré"
    print(f"Statut actuel : {status}")
    if is_pin_configured():
        algo = cfg.get("algo", "sha256")
        print(f"Algorithme actuel : {algo.upper()}")
    print("-" * 60)

    try:
        p1 = getpass.getpass("Entrez votre code PIN (4 chiffres) : ").strip()
        if not p1.isdigit() or len(p1) != 4:
            print("❌ Erreur : Le PIN doit être composé exactement de 4 chiffres.")
            sys.exit(1)

        p2 = getpass.getpass("Confirmez votre code PIN (4 chiffres) : ").strip()
        if p1 != p2:
            print("❌ Erreur : Les deux codes ne correspondent pas.")
            sys.exit(1)

        ok, msg = set_user_pin(p1)
        if ok:
            print("✅ Succès :", msg)
            print("🛡️ Stocké avec PBKDF2-HMAC-SHA256 (100 000 itérations) et permissions strictes (0600).")
        else:
            print("❌ Erreur :", msg)
            sys.exit(1)
    except KeyboardInterrupt:
        print("\nAnnulé par l'utilisateur.")
        sys.exit(0)


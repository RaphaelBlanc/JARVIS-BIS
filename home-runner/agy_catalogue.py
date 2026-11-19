"""Catalogue du compte AGY, relu en fond et partagé avec le cache Noctalia.

Les lectures HTTP ne doivent attendre ni le réseau d'agy ni son authentification.
En cas d'échec, la dernière liste connue reste utilisable.
"""
import contextlib
import json
import logging
import math
import os
from pathlib import Path
import re
import select
import signal
import stat
import subprocess
import tempfile
import threading
import time

logger = logging.getLogger(__name__)
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,54}")
TTL = 6 * 3600
RETRY = 15 * 60
TIMEOUT = 20
MAX_OUTPUT = 64 * 1024
MAX_CACHE = 256 * 1024
MAX_MODELS = 50
ENV_KEYS = (
    "HOME", "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "USER", "LOGNAME",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS", "GEMINI_API_KEY", "GOOGLE_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT", "CLOUDSDK_CORE_PROJECT",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy",
)


def clean_name(value):
    return " ".join("".join(c for c in value if c.isalnum() or c in " .()+/_:-").split())[:80].strip()


def parse_models(output):
    models, seen = [], set()
    for line in output.split("\n"):
        line = line.rstrip("\r")
        if not line.strip():
            continue
        ident, separator, name = line.partition("\t")
        if not separator or "\t" in name or not MODEL_ID.fullmatch(ident):
            return None
        if ident not in seen:
            models.append(dict(id=ident, name=clean_name(name) or ident))
            seen.add(ident)
    return models[:MAX_MODELS] or None


def read_cache(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > MAX_CACHE:
            return None
        data = json.loads(os.read(fd, MAX_CACHE + 1))
        if not isinstance(data, dict) or type(data.get("t")) not in (int, float):
            return None
        timestamp = float(data["t"])
        if not math.isfinite(timestamp) or timestamp <= 0:
            return None
        models = data.get("modeles")
        if not isinstance(models, list) or not 0 < len(models) <= MAX_MODELS:
            return None
        for model in models:
            if not isinstance(model, dict) or not isinstance(model.get("id"), str) or not MODEL_ID.fullmatch(model["id"]):
                return None
            name = model.get("name")
            if not isinstance(name, str) or not name or name != clean_name(name):
                return None
        return [dict(id=m["id"], name=m["name"]) for m in models], timestamp
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        return None
    finally:
        os.close(fd)


def write_cache(path, models, timestamp):
    if path.is_symlink():
        raise ValueError("Cache AGY symbolique refusé.")
    name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            name = stream.name
            stream.write(json.dumps(dict(t=timestamp, modeles=models), ensure_ascii=False).encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)  # NamedTemporaryFile crée un fichier privé (0600).
    finally:
        if name:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(name)


def run_cli(path, argument):
    """Sortie bornée, stdin fermé, aucun shell ni jeton des bots dans l'environnement."""
    env = {key: os.environ[key] for key in ENV_KEYS if key in os.environ}
    env.update(TERM="dumb", NO_COLOR="1", FORCE_COLOR="0")
    proc = subprocess.Popen(
        [path, argument], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if argument == '--help' else subprocess.DEVNULL,
        start_new_session=True, env=env,
    )
    deadline = time.monotonic() + TIMEOUT
    data = bytearray()
    try:
        poll = select.poll()
        poll.register(proc.stdout, select.POLLIN | select.POLLHUP)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not poll.poll(max(1, int(remaining * 1000))):
                return None
            chunk = os.read(proc.stdout.fileno(), 8192)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > MAX_OUTPUT:
                return None
        proc.wait(timeout=max(0.01, deadline - time.monotonic()))
        return data.decode("utf-8") if proc.returncode == 0 else None
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        proc.stdout.close()


class CatalogueAGY:
    def __init__(self, path, fallback, find_cli):
        self.path = Path(path)
        self.find_cli = find_cli
        cached = read_cache(self.path)
        self.models, self.updated = cached or (list(fallback), 0.0)
        self.attempt = None
        self.lock = threading.Lock()
        self.thread = None
        self.supports_models = None

    def refresh(self):
        now = time.time()
        if self.updated and 0 <= now - self.updated < TTL:
            return
        if self.attempt is not None and time.monotonic() - self.attempt < RETRY:
            return
        if not self.lock.acquire(blocking=False):
            return
        # Revoir les délais sous verrou : deux requêtes peuvent arriver ensemble.
        if self.attempt is not None and time.monotonic() - self.attempt < RETRY:
            self.lock.release()
            return
        self.attempt = time.monotonic()
        self.thread = threading.Thread(target=self._refresh, name="jarvis-agy-modeles", daemon=True)
        try:
            self.thread.start()
        except RuntimeError:
            self.lock.release()
            raise

    def _refresh(self):
        try:
            path = self.find_cli()
            if not path:
                return
            if self.supports_models is None:
                help_text = run_cli(path, "--help")
                if help_text is None:
                    return  # un --help raté n'est pas retenu jusqu'au redémarrage
                self.supports_models = bool(re.search(r"^\s*models\s", help_text, re.M))
            if not self.supports_models:
                return  # un ancien CLI pourrait interpréter « models » comme un prompt
            output = run_cli(path, "models")
            if not output:
                return
            models = parse_models(output)
            if not models:
                logger.warning("Sortie de agy models au format inattendu : catalogue précédent conservé.")
                return
            self.models, self.updated = models, time.time()
            write_cache(self.path, models, self.updated)
        except (OSError, ValueError, subprocess.SubprocessError):
            logger.warning("Catalogue AGY non relu ou non enregistré ; dernière liste conservée.")
        finally:
            self.lock.release()

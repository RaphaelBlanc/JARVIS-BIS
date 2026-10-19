"""System and desktop management utilities for JARVIS under Linux/Wayland (Hyprland).

Includes:
- Native Wayland screenshot via grim
- Media playback control via playerctl and PipeWire/WirePlumber volume (wpctl)
- Background command runner with execution duration
- Session database storage inspector
- Global Git repository scanner across the user's home directory
- Ultra-lightweight hardware health watchdog (RAM, Disk, Battery via procfs/sysfs)
- Remote file downloader (httpx async streaming)
"""

import asyncio
from datetime import datetime, timedelta
import glob
import json
import logging
import os
from pathlib import Path
import re
import shutil
import socket
import time

CONVERSATIONS_DIR = Path.home() / ".gemini" / "antigravity-cli" / "conversations"


async def take_screenshot(output_path: Path) -> tuple[bool, str]:
    """Takes a remote screenshot using native Wayland tool (grim) with fallback to ffmpeg."""
    is_wayland = bool(os.getenv("WAYLAND_DISPLAY") or os.getenv("XDG_SESSION_TYPE") == "wayland")
    grim_path = shutil.which("grim")

    if is_wayland and grim_path:
        try:
            proc = await asyncio.create_subprocess_exec(
                grim_path,
                str(output_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.wait()
            if output_path.exists() and output_path.stat().st_size > 0:
                return True, "Capture effectuée via grim (Wayland)."
        except Exception:
            pass

    # Fallback to ffmpeg x11grab
    display = os.getenv("DISPLAY", ":0")
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-f", "x11grab",
            "-video_size", "1920x1080",
            "-i", display,
            "-vframes", "1",
            str(output_path),
            "-y",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.wait()
        if output_path.exists() and output_path.stat().st_size > 0:
            return True, "Capture effectuée via ffmpeg (X11)."
    except Exception as e:
        return False, f"Échec de la capture d'écran : {e}"

    return False, "Aucun outil de capture fonctionnel trouvé ou écran indisponible."


async def media_action(action: str, value: str = "") -> tuple[bool, str]:
    """Controls media playback via playerctl or volume via wpctl."""
    playerctl = shutil.which("playerctl")
    wpctl = shutil.which("wpctl")

    if action in ("play-pause", "next", "previous", "status"):
        if not playerctl:
            return False, "⚠️ <code>playerctl</code> n'est pas installé."
        try:
            if action == "status":
                proc = await asyncio.create_subprocess_exec(
                    playerctl, "metadata", "--format", "{{artist}} - {{title}} ({{status}})",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await proc.communicate()
                status_txt = stdout.decode().strip()
                return True, f"🎵 <b>Lecture actuelle :</b>\n{status_txt}" if status_txt else "ℹ️ Aucun lecteur multimédia actif."

            proc = await asyncio.create_subprocess_exec(
                playerctl, action,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.wait()
            emojis = {"play-pause": "⏯️ Lecture / Pause", "next": "⏭️ Suivant", "previous": "⏮️ Précédent"}
            return True, f"{emojis.get(action, '🎵 Média mis à jour')}"
        except Exception as e:
            return False, f"⚠️ Erreur média : {e}"

    elif action in ("volume_up", "volume_down", "volume_set", "volume_get"):
        if not wpctl:
            return False, "⚠️ <code>wpctl</code> n'est pas disponible."
        try:
            if action == "volume_get":
                proc = await asyncio.create_subprocess_exec(
                    wpctl, "get-volume", "@DEFAULT_AUDIO_SINK@",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await proc.communicate()
                out = stdout.decode().strip()
                if "Volume:" in out:
                    val = float(out.split("Volume:")[1].split()[0])
                    pct = int(round(val * 100))
                    return True, f"🔊 <b>Volume actuel :</b> {pct}%"
                return True, f"🔊 {out}"

            val_arg = "5%+" if action == "volume_up" else ("5%-" if action == "volume_down" else f"{value}%")
            proc = await asyncio.create_subprocess_exec(
                wpctl, "set-volume", "@DEFAULT_AUDIO_SINK@", val_arg,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.wait()
            return await media_action("volume_get")
        except Exception as e:
            return False, f"⚠️ Erreur réglage volume : {e}"

    return False, f"Action média inconnue : {action}"


async def run_shell_task(command: str, timeout: int = 180) -> tuple[int, str, str, float]:
    """Executes a shell command asynchronously and returns (code, stdout, stderr, elapsed_sec)."""
    start_time = time.time()
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        elapsed = time.time() - start_time
        return proc.returncode, stdout.decode().strip(), stderr.decode().strip(), elapsed
    except asyncio.TimeoutError:
        elapsed = time.time() - start_time
        try:
            proc.kill()
        except Exception:
            pass
        return -1, "", f"Temps d'exécution dépassé ({timeout}s).", elapsed
    except Exception as e:
        elapsed = time.time() - start_time
        return -1, "", str(e), elapsed


def get_session_stats(conv_id: str | None) -> dict:
    """Returns database size and info for the current Antigravity session."""
    if not conv_id:
        return {"exists": False, "size_mb": 0.0, "warning": False}

    db_file = CONVERSATIONS_DIR / f"{conv_id}.db"
    if not db_file.exists():
        return {"exists": False, "size_mb": 0.0, "warning": False}

    size_bytes = db_file.stat().st_size
    size_mb = size_bytes / (1024 * 1024)
    modified = datetime.fromtimestamp(db_file.stat().st_mtime).strftime("%d/%m/%Y %H:%M:%S")

    warning = size_mb > 2.0
    return {
        "exists": True,
        "size_mb": round(size_mb, 2),
        "modified": modified,
        "warning": warning,
        "path": str(db_file),
    }


async def scan_all_git_repos() -> list[dict]:
    """Scans all Git repositories in the user's home directory, avoiding heavy directories."""
    home = Path.home()
    exclude_dirs = {
        ".cache", ".local", ".cargo", ".rustup", ".gemini", "venv",
        ".npm", ".nvm", "node_modules", ".var", ".mozilla", ".config"
    }

    repos: list[Path] = []
    # Fast os.walk ignoring excluded subtrees
    for root, dirs, _ in os.walk(home):
        dirs[:] = [d for d in dirs if d not in exclude_dirs and not (d.startswith(".") and d != ".git")]
        if ".git" in dirs:
            repos.append(Path(root))
            dirs.remove(".git")

    results = []
    for r in repos:
        name = r.name
        rel = r.relative_to(home)
        try:
            branch_p = await asyncio.create_subprocess_exec(
                "git", "branch", "--show-current",
                cwd=str(r), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout_b, _ = await branch_p.communicate()
            branch = stdout_b.decode().strip() or "HEAD détachée"

            status_p = await asyncio.create_subprocess_exec(
                "git", "status", "--porcelain",
                cwd=str(r), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout_s, _ = await status_p.communicate()
            dirty = bool(stdout_s.decode().strip())

            unpushed_p = await asyncio.create_subprocess_exec(
                "git", "rev-list", "@{u}..HEAD", "--count",
                cwd=str(r), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout_u, stderr_u = await unpushed_p.communicate()
            out_u = stdout_u.decode().strip()
            err_u = stderr_u.decode().strip().lower()

            unpushed = int(out_u) if unpushed_p.returncode == 0 and out_u.isdigit() else 0
            has_no_upstream = (unpushed_p.returncode != 0 and "no upstream" in err_u)

            results.append({
                "name": name,
                "path": str(r),
                "rel": str(rel),
                "branch": branch,
                "dirty": dirty,
                "unpushed": unpushed,
                "no_upstream": has_no_upstream,
            })
        except Exception:
            pass

    return results


def format_git_summary(repos: list[dict]) -> str:
    """Formats Git repositories status into clear readable HTML for Telegram."""
    if not repos:
        return "ℹ️ Aucun dépôt Git trouvé sur la machine."

    dirty_repos = [r for r in repos if r.get("dirty")]
    unpushed_repos = [r for r in repos if r.get("unpushed", 0) > 0]
    clean_repos = [r for r in repos if not r.get("dirty") and r.get("unpushed", 0) == 0 and not r.get("no_upstream")]

    lines = [
        f"🐙 <b>Supervision Git du PC ({len(repos)} dépôts analysés)</b>",
        f"• À jour et propres : <b>{len(clean_repos)}</b>",
        f"• Fichiers non commités : <b>{len(dirty_repos)}</b>",
        f"• Commits en attente de push : <b>{len(unpushed_repos)}</b>\n",
    ]

    needs_action = [r for r in repos if r.get("dirty") or r.get("unpushed", 0) > 0 or r.get("no_upstream")]
    if needs_action:
        lines.append("⚠️ <b>Dépôts nécessitant une action :</b>")
        for r in needs_action:
            flags = []
            if r.get("dirty"):
                flags.append("📝 Fichiers modifiés")
            if r.get("unpushed", 0) > 0:
                flags.append(f"⬆️ {r['unpushed']} commit(s) à push")
            if r.get("no_upstream"):
                flags.append("⚠️ Pas d'upstream configuré")
            details = " | ".join(flags)
            lines.append(f"• <b>{r['name']}</b> (branche <code>{r['branch']}</code>)")
            lines.append(f"   📁 <code>~/{r['rel']}</code>")
            lines.append(f"   ↳ {details}\n")

    if clean_repos:
        lines.append(f"✅ <i>{len(clean_repos)} autres dépôts sont totalement propres et synchronisés.</i>")

    return "\n".join(lines).strip()


def check_hardware_health() -> tuple[list[str], dict]:
    """Inspects RAM, Disk and Battery via procfs/sysfs with 0 extra memory.

    Returns:
    - alerts: list of warning strings if any threshold is exceeded.
    - metrics: dictionary with current values.
    """
    alerts = []
    metrics = {}

    # 1. RAM check
    try:
        meminfo = {}
        with open("/proc/meminfo", "r") as f:
            for line in f:
                p = line.split(":")
                if len(p) == 2:
                    meminfo[p[0].strip()] = int(p[1].split()[0])
        tot_ram = meminfo.get("MemTotal", 1) / (1024 * 1024)
        avail_ram = meminfo.get("MemAvailable", 1) / (1024 * 1024)
        used_ram = tot_ram - avail_ram
        pct_ram = (used_ram / tot_ram) * 100
        metrics["ram_pct"] = round(pct_ram, 1)
        metrics["ram_used_gb"] = round(used_ram, 1)
        metrics["ram_tot_gb"] = round(tot_ram, 1)

        if pct_ram >= 92.0:
            alerts.append(f"🧠 <b>Saturation Mémoire RAM ({pct_ram:.1f}%)</b> : {used_ram:.1f}/{tot_ram:.1f} Go utilisés.")
    except Exception:
        pass

    # 2. Disk check
    try:
        st = os.statvfs("/")
        tot_disk = (st.f_blocks * st.f_frsize) / (1024**3)
        free_disk = (st.f_bavail * st.f_frsize) / (1024**3)
        used_disk = tot_disk - free_disk
        pct_disk = (used_disk / tot_disk) * 100
        metrics["disk_free_gb"] = round(free_disk, 1)
        metrics["disk_pct"] = round(pct_disk, 1)

        if free_disk < 8.0 or pct_disk >= 92.0:
            alerts.append(f"💽 <b>Espace Disque Faible</b> : Plus que {free_disk:.1f} Go libres ({pct_disk:.1f}% plein).")
    except Exception:
        pass

    # 3. Battery check
    try:
        bats = glob.glob("/sys/class/power_supply/BAT*")
        for b in bats:
            with open(os.path.join(b, "capacity"), "r") as f:
                cap = int(f.read().strip())
            st_val = "Unknown"
            if os.path.exists(os.path.join(b, "status")):
                with open(os.path.join(b, "status"), "r") as f:
                    st_val = f.read().strip()

            metrics["battery_cap"] = cap
            metrics["battery_status"] = st_val

            if st_val.lower() == "discharging":
                if cap <= 12:
                    alerts.append(f"🚨 <b>Batterie Critique ({cap}%)</b> : Le PC est sur batterie et va bientôt s'éteindre.")
                elif cap <= 20:
                    alerts.append(f"🔋 <b>Batterie Faible ({cap}%)</b> : Pensez à brancher votre PC.")
    except Exception:
        pass

    return alerts, metrics


async def download_remote_file(
    url: str,
    dest_dir: Path | None = None,
    custom_filename: str | None = None
) -> tuple[bool, str, Path | None]:
    """Downloads a file asynchronously using httpx streaming."""
    downloads_dir = dest_dir or (Path.home() / "Downloads")
    downloads_dir.mkdir(parents=True, exist_ok=True)

    try:
        import httpx
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            async with client.stream("GET", url) as response:
                if response.status_code >= 400:
                    return False, f"Erreur HTTP {response.status_code}", None

                filename = custom_filename
                if not filename:
                    cd = response.headers.get("content-disposition", "")
                    if "filename=" in cd:
                        filename = cd.split("filename=")[1].strip('"\' ')
                    else:
                        url_path = url.split("?")[0].rstrip("/")
                        filename = url_path.split("/")[-1] if "/" in url_path else f"dl_{int(time.time())}"

                if not filename:
                    filename = f"dl_{int(time.time())}"

                filename = re.sub(r"[^\w\-.]", "_", filename)
                target_path = downloads_dir / filename

                total_bytes = 0
                with open(target_path, "wb") as f:
                    async for chunk in response.aiter_bytes():
                        f.write(chunk)
                        total_bytes += len(chunk)

                size_mb = total_bytes / (1024 * 1024)
                return True, f"Téléchargement réussi ({size_mb:.2f} Mo)", target_path
    except Exception as e:
        return False, f"Erreur lors du téléchargement : {e}", None


def find_matching_files(search_term: str, root_dir: Path | None = None, limit: int = 5) -> list[Path]:
    """Finds matching files in user home directories."""
    base = root_dir or Path.home()
    exclude_dirs = {
        ".cache", ".local", ".cargo", ".rustup", ".gemini", "venv",
        ".npm", ".nvm", "node_modules", ".var", ".mozilla"
    }
    matches = []
    term_lower = search_term.lower()

    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in exclude_dirs and not (d.startswith(".") and d != ".git")]
        for f in files:
            if term_lower in f.lower():
                matches.append(Path(root) / f)
                if len(matches) >= limit:
                    return matches
    return matches


# ==============================================================================
# WAYLAND CLIPBOARD SYNC (wl-copy / wl-paste)
# ==============================================================================

async def set_clipboard(text: str) -> tuple[bool, str]:
    """Copies text to the system Wayland clipboard using wl-copy."""
    wl_copy = shutil.which("wl-copy")
    if not wl_copy:
        return False, "Outil wl-copy introuvable sur le système."

    env = os.environ.copy()
    env["WAYLAND_DISPLAY"] = os.getenv("WAYLAND_DISPLAY", "wayland-1")
    env["XDG_RUNTIME_DIR"] = os.getenv("XDG_RUNTIME_DIR", "/run/user/1000")

    try:
        proc = await asyncio.create_subprocess_exec(
            wl_copy,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(input=text.encode("utf-8")), timeout=5)
        if proc.returncode == 0:
            return True, "Texte copié dans le presse-papier du PC avec succès."
        return False, f"Erreur wl-copy (code {proc.returncode}): {stderr.decode().strip()}"
    except Exception as e:
        return False, str(e)


async def get_clipboard() -> tuple[bool, str]:
    """Retrieves current text from the system Wayland clipboard using wl-paste."""
    wl_paste = shutil.which("wl-paste")
    if not wl_paste:
        return False, "Outil wl-paste introuvable sur le système."

    env = os.environ.copy()
    env["WAYLAND_DISPLAY"] = os.getenv("WAYLAND_DISPLAY", "wayland-1")
    env["XDG_RUNTIME_DIR"] = os.getenv("XDG_RUNTIME_DIR", "/run/user/1000")

    try:
        proc = await asyncio.create_subprocess_exec(
            wl_paste, "-n",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode == 0:
            content = stdout.decode("utf-8", errors="replace").strip()
            return True, content if content else "(Presse-papier vide)"
        err_msg = stderr.decode("utf-8", errors="replace").strip()
        if "Nothing is copied" in err_msg:
            return True, "(Presse-papier vide)"
        return False, f"Erreur wl-paste: {err_msg}"
    except Exception as e:
        return False, str(e)


# ==============================================================================
# WEBCAM & SECURITY / GEOLOCATION
# ==============================================================================

async def capture_webcam(output_path: Path) -> tuple[bool, str]:
    """Captures a single snapshot from the default webcam device (/dev/video0) using ffmpeg."""
    dev = "/dev/video0"
    if not os.path.exists(dev):
        return False, "Aucun périphérique webcam détecté sur /dev/video0."

    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-f", "v4l2",
            "-i", dev,
            "-frames:v", "1",
            "-y",
            str(output_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        if output_path.exists() and output_path.stat().st_size > 0:
            return True, "Photo webcam capturée avec succès."
        return False, f"Échec de capture webcam : {stderr.decode()[-200:].strip()}"
    except Exception as e:
        return False, f"Erreur webcam : {e}"


async def get_geolocation_info() -> dict:
    """Retrieves public IP, ISP and approximate geolocation information."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get("https://ipinfo.io/json")
            if resp.status_code == 200:
                data = resp.json()
                loc = data.get("loc", "")
                lat, lon = (loc.split(",") + [None, None])[:2] if loc else (None, None)
                return {
                    "ok": True,
                    "ip": data.get("ip", "N/A"),
                    "city": data.get("city", "Inconnue"),
                    "region": data.get("region", "Inconnue"),
                    "country": data.get("country", "FR"),
                    "org": data.get("org", "N/A"),
                    "lat": float(lat) if lat else None,
                    "lon": float(lon) if lon else None,
                    "postal": data.get("postal", ""),
                }
    except Exception as e:
        logging.warning(f"Failed to fetch geolocation info: {e}")
    return {"ok": False, "error": "Impossible de contacter le service de géolocalisation."}


# ==============================================================================
# HYPRLAND WINDOWS & PROCESS CONTROL
# ==============================================================================

def get_active_hyprland_signature() -> str | None:
    """Discovers and validates the currently active Hyprland instance signature."""
    xdg = Path(os.getenv("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "hypr"
    if not xdg.is_dir():
        return None

    # 1. Check current env var if it can actually connect
    env_sig = os.getenv("HYPRLAND_INSTANCE_SIGNATURE")
    if env_sig:
        sock_path = xdg / env_sig / ".socket.sock"
        if sock_path.exists():
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                s.connect(str(sock_path))
                s.close()
                return env_sig
            except Exception:
                pass

    # 2. Search for valid alive sockets in XDG_RUNTIME_DIR/hypr (newest first)
    try:
        candidates = sorted(
            [d for d in xdg.iterdir() if d.is_dir() and (d / ".socket.sock").exists()],
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )
        for cand in candidates:
            sock_path = cand / ".socket.sock"
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                s.connect(str(sock_path))
                s.close()
                os.environ["HYPRLAND_INSTANCE_SIGNATURE"] = cand.name
                return cand.name
            except Exception:
                continue
    except Exception as e:
        logging.warning(f"Error discovering Hyprland signature: {e}")

    return None


def ensure_hyprland_env() -> str | None:
    """Ensures os.environ['HYPRLAND_INSTANCE_SIGNATURE'] points to a live Hyprland instance."""
    sig = get_active_hyprland_signature()
    if sig:
        os.environ["HYPRLAND_INSTANCE_SIGNATURE"] = sig
    return sig


async def list_hyprland_windows() -> list[dict]:
    """Lists currently open windows and their workspaces from Hyprland."""
    ensure_hyprland_env()
    try:
        proc = await asyncio.create_subprocess_exec(
            "hyprctl", "clients", "-j",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode == 0 and stdout:
            clients = json.loads(stdout.decode())
            results = []
            for c in clients:
                if not c.get("mapped"):
                    continue
                results.append({
                    "address": c.get("address", ""),
                    "class": c.get("class", "App"),
                    "title": c.get("title", ""),
                    "workspace": c.get("workspace", {}).get("name", "1"),
                    "pid": c.get("pid", 0),
                })
            return results
    except Exception as e:
        logging.warning(f"Failed to query hyprctl clients: {e}")
    return []


async def focus_hyprland_window(address: str) -> tuple[bool, str]:
    """Focuses a Hyprland window by address."""
    ensure_hyprland_env()
    try:
        proc = await asyncio.create_subprocess_exec(
            "hyprctl", "dispatch", "focuswindow", f"address:{address}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.wait()
        return (proc.returncode == 0), "Fenêtre mise au premier plan."
    except Exception as e:
        return False, str(e)


async def close_hyprland_window(address: str) -> tuple[bool, str]:
    """Closes a Hyprland window gracefully by address."""
    ensure_hyprland_env()
    try:
        proc = await asyncio.create_subprocess_exec(
            "hyprctl", "dispatch", "closewindow", f"address:{address}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.wait()
        return (proc.returncode == 0), "Demande de fermeture de fenêtre envoyée."
    except Exception as e:
        return False, str(e)


async def get_top_processes(limit: int = 8) -> list[dict]:
    """Returns top system processes sorted by memory consumption."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ps", "-eo", "pid,user,%cpu,%mem,comm", "--sort=-%mem",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        lines = stdout.decode().strip().split("\n")
        results = []
        for line in lines[1:limit + 1]:
            parts = line.split(None, 4)
            if len(parts) >= 5:
                results.append({
                    "pid": int(parts[0]),
                    "user": parts[1],
                    "cpu": float(parts[2]),
                    "mem": float(parts[3]),
                    "comm": parts[4],
                })
        return results
    except Exception as e:
        logging.warning(f"Failed to query top processes: {e}")
        return []


async def terminate_process(target: str | int, force: bool = False) -> tuple[bool, str]:
    """Kills a process by PID or name."""
    try:
        flag = "-9" if force else "-15"
        if str(target).isdigit():
            proc = await asyncio.create_subprocess_exec("kill", flag, str(target))
        else:
            proc = await asyncio.create_subprocess_exec("pkill", flag, "-f", str(target))
        await proc.wait()
        if proc.returncode == 0:
            return True, f"Processus {target} arrêté avec succès."
        return False, f"Impossible d'arrêter {target} (code {proc.returncode})."
    except Exception as e:
        return False, str(e)


# ==============================================================================
# SPEECH SYNTHESIS (spd-say)
# ==============================================================================

async def speak_text(text: str, lang: str = "fr") -> tuple[bool, str]:
    """Speaks text out loud through PC speakers using spd-say."""
    spd = shutil.which("spd-say")
    if not spd:
        return False, "Outil spd-say introuvable."
    try:
        proc = await asyncio.create_subprocess_exec(
            spd, "-l", lang, text,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.wait()
        if proc.returncode == 0:
            return True, f"Message prononcé à voix haute : « {text} »"
        return False, f"Erreur lors de la lecture vocale (code {proc.returncode})"
    except Exception as e:
        return False, str(e)


# ==============================================================================
# NATURAL LANGUAGE REMINDER PARSER
# ==============================================================================

def parse_reminder_input(text: str) -> tuple[float, str] | None:
    """Parses natural language reminder input in French.

    Supports:
    - dans 10s test
    - dans 15m sortir le plat
    - dans 2h réviser
    - 14h30 réunion
    - 20h diner
    - à 18h45 ranger le bureau
    - demain 8h30 TP Archi
    """
    clean = text.strip()
    clean = re.sub(r"^/remind(?:@[a-zA-Z0-9_]+)?\s*", "", clean, flags=re.IGNORECASE).strip()
    if not clean:
        return None

    now = datetime.now()

    # Pattern 1: Relative "dans X (s|m|h)"
    m_rel = re.match(
        r"^dans\s+(\d+)\s*(s(?:ec(?:ondes?)?)?|m(?:in(?:utes?)?)?|h(?:eures?)?)\s+(.+)$",
        clean,
        re.IGNORECASE,
    )
    if m_rel:
        qty = int(m_rel.group(1))
        unit = m_rel.group(2).lower()
        desc = m_rel.group(3).strip()

        if unit.startswith("s"):
            seconds = qty
        elif unit.startswith("m"):
            seconds = qty * 60
        else:
            seconds = qty * 3600

        return time.time() + seconds, desc

    # Pattern 2: Tomorrow absolute "demain [à] HH[:h]MM"
    m_demain = re.match(
        r"^demain\s+(?:[àa]\s+)?(\d{1,2})(?:h(\d{1,2})?|:(\d{1,2}))?\s*(?:min)?\s+(.+)$",
        clean,
        re.IGNORECASE,
    )
    if m_demain:
        hour = int(m_demain.group(1))
        minute = int(m_demain.group(2) or m_demain.group(3) or 0)
        desc = m_demain.group(4).strip()
        if 0 <= hour <= 23 and 0 <= minute <= 59 and desc:
            target_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=1)
            return target_dt.timestamp(), desc

    # Pattern 3: Today absolute "[à] HH[:h]MM"
    m_abs = re.match(
        r"^(?:[àa]\s+)?(\d{1,2})(?:h(\d{1,2})?|:(\d{1,2}))?\s*(?:min)?\s+(.+)$",
        clean,
        re.IGNORECASE,
    )
    if m_abs:
        hour = int(m_abs.group(1))
        minute = int(m_abs.group(2) or m_abs.group(3) or 0)
        desc = m_abs.group(4).strip()
        if 0 <= hour <= 23 and 0 <= minute <= 59 and desc:
            target_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target_dt.timestamp() <= time.time():
                target_dt += timedelta(days=1)
            return target_dt.timestamp(), desc

    return None


# ==============================================================================
# WAYLAND SCREEN RECORDING (wf-recorder)
# ==============================================================================

async def start_screen_record(output_path: Path) -> tuple[bool, asyncio.subprocess.Process | None, str]:
    """Starts Wayland screen recording using wf-recorder in yuv420p format for Telegram compatibility."""
    wf_path = shutil.which("wf-recorder")
    if not wf_path:
        return False, None, "L'outil <code>wf-recorder</code> n'est pas installé sur le système."

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        try:
            output_path.unlink()
        except Exception:
            pass

    try:
        proc = await asyncio.create_subprocess_exec(
            wf_path,
            "-c", "libopenh264",
            "-x", "yuv420p",
            "-f", str(output_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        # Verify process launched cleanly
        await asyncio.sleep(0.4)
        if proc.returncode is not None:
            return False, None, f"wf-recorder s'est arrêté immédiatement (code {proc.returncode})."
        return True, proc, "Enregistrement démarré."
    except Exception as e:
        return False, None, f"Erreur lors du lancement de l'enregistrement : {e}"


async def stop_screen_record(proc: asyncio.subprocess.Process, output_path: Path, timeout: float = 8.0) -> tuple[bool, str]:
    """Gracefully terminates wf-recorder with SIGINT to properly finalize the MP4 file."""
    import signal
    if proc.returncode is None:
        try:
            proc.send_signal(signal.SIGINT)
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except Exception:
                pass
        except Exception as e:
            logging.warning(f"Error signaling wf-recorder: {e}")

    # Allow disk flush
    await asyncio.sleep(0.6)

    if output_path.exists() and output_path.stat().st_size > 0:
        return True, "Enregistrement finalisé avec succès."
    return False, "Le fichier vidéo n'a pas pu être finalisé ou est vide."


# ==============================================================================
# SENTINEL / GUARD MODE HELPERS (Hyprland activity detection)
# ==============================================================================

async def get_cursor_position() -> tuple[int, int] | None:
    """Returns current Hyprland cursor position (x, y) or None."""
    ensure_hyprland_env()
    try:
        proc = await asyncio.create_subprocess_exec(
            "hyprctl", "cursorpos",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
        if proc.returncode == 0 and stdout:
            parts = stdout.decode().strip().split(",")
            if len(parts) >= 2:
                return int(parts[0].strip()), int(parts[1].strip())
    except Exception:
        pass
    return None


async def get_active_window_info() -> dict | None:
    """Returns information on the currently active window in Hyprland."""
    ensure_hyprland_env()
    try:
        proc = await asyncio.create_subprocess_exec(
            "hyprctl", "activewindow", "-j",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
        if proc.returncode == 0 and stdout:
            return json.loads(stdout.decode())
    except Exception:
        pass
    return None


async def open_hyprland_socket2() -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
    """Opens an asynchronous Unix socket connection to Hyprland's socket2 event stream."""
    try:
        sig = ensure_hyprland_env()
        if not sig:
            return None
        xdg = Path(os.getenv("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "hypr"
        sock_path = xdg / sig / ".socket2.sock"
        if sock_path.exists():
            return await asyncio.open_unix_connection(str(sock_path))
    except Exception as e:
        logging.warning(f"Failed to connect to Hyprland socket2: {e}")
    return None


async def lock_desktop_session() -> tuple[bool, str]:
    """Locks the Linux desktop session remotely using Noctalia and loginctl."""
    locked = False
    # 1. Try direct Noctalia lock command if installed
    noctalia_bin = shutil.which("noctalia")
    if noctalia_bin:
        try:
            proc = await asyncio.create_subprocess_exec(
                noctalia_bin, "msg", "session", "lock",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.wait(), timeout=3)
            if proc.returncode == 0:
                locked = True
        except Exception:
            pass

    # 2. Try native systemd loginctl lock-session
    try:
        proc = await asyncio.create_subprocess_exec(
            "loginctl", "lock-session",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.wait(), timeout=5)
        if proc.returncode == 0:
            locked = True
    except Exception:
        pass

    if locked:
        return True, "Session verrouillée avec succès."
    return False, "Échec du verrouillage de la session."


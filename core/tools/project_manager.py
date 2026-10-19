"""Project Factory & Multi-Project DevOps Manager for JARVIS.

Provides end-to-end project lifecycle automation:
- Discovery & tracking of active projects (ReconForge, JARVIS, Cyber, Portfolio + ~/Documents/Dev/*)
- Per-project Antigravity conversation sessions and dynamic working directory (cwd) routing (/dev)
- Interactive ROADMAP.md / TODO.md task parsing & 1-click toggling
- Automated test execution (pytest, unittest, cargo, npm, Makefile)
- Instant project scaffolding (/newproj <name> [python-cli|fastapi|cyber-tool|web])
- Ship & Personal Branding generator (/ship <project> -> LinkedIn Post + Portfolio LaTeX snippet)
"""

import asyncio
from datetime import datetime
import html
import json
import logging
import os
from pathlib import Path
import re
import time

from core.config import DATA_DIR
PROJECTS_FILE = DATA_DIR / "projects.json"
PROJECT_SESSIONS_FILE = DATA_DIR / "project_sessions.json"
FOCUS_FILE = DATA_DIR / "focus_sessions.json"

HOME = Path.home()
DEV_DIR = HOME / "Documents" / "Dev"
DEV_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_PROJECTS = {
    "reconforge": {
        "name": "ReconForge",
        "key": "reconforge",
        "path": str(HOME / "Documents" / "Dev" / "reconforge"),
        "category": "🛡️ Cyber & Open-Source",
        "description": "Framework modulaire de reconnaissance offensive & OSINT",
    },
    "jarvis": {
        "name": "JARVIS",
        "key": "jarvis",
        "path": str(HOME / "Documents" / "JARVIS"),
        "category": "🤖 IA & Automatisation",
        "description": "OS personnel & Co-pilote automatisé sur Telegram/Hyprland",
    },
    "cyber": {
        "name": "Cyber Lab & CTF",
        "key": "cyber",
        "path": str(HOME / "Documents" / "Cyber"),
        "category": "🚩 Pentest & Recherche",
        "description": "Challenges CTF, scripts d'exploitation et outils sécurité",
    },
    "portfolio": {
        "name": "Portfolio & Pro",
        "key": "portfolio",
        "path": str(HOME / "Documents" / "Pro" / "Ing_2027-2030"),
        "category": "💼 Carrière & Branding",
        "description": "Portfolio LaTeX, CV et publications LinkedIn",
    },
}


def load_projects_registry() -> dict[str, dict]:
    """Loads registered projects and auto-discovers any new directories in ~/Documents/Dev."""
    registry = dict(DEFAULT_PROJECTS)
    if PROJECTS_FILE.exists():
        try:
            saved = json.loads(PROJECTS_FILE.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                registry.update(saved)
        except Exception as e:
            logging.warning(f"Failed to load projects registry: {e}")

    # Auto-discover subprojects in ~/Documents/Dev
    if DEV_DIR.exists():
        for d in sorted(DEV_DIR.iterdir()):
            if d.is_dir() and not d.name.startswith("."):
                k = d.name.lower()
                if k not in registry:
                    registry[k] = {
                        "name": d.name,
                        "key": k,
                        "path": str(d),
                        "category": "💻 Dev & Projets",
                        "description": _extract_readme_summary(d),
                    }

    return registry


discover_projects = load_projects_registry


def save_projects_registry(registry: dict[str, dict]) -> bool:
    try:
        with open(PROJECTS_FILE, "w", encoding="utf-8") as f:
            json.dump(registry, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logging.error(f"Failed to save projects registry: {e}")
        return False


def _extract_readme_summary(proj_dir: Path) -> str:
    for name in ("README.md", "readme.md", "ROADMAP.md"):
        p = proj_dir / name
        if p.exists():
            try:
                for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                    s = line.strip()
                    if s and not s.startswith("#") and not s.startswith("[!") and not s.startswith("!["):
                        return re.sub(r"[*_`]+", "", s)[:100]
            except Exception:
                pass
    return "Projet de développement local"


def resolve_project(query: str) -> dict | None:
    """Resolves a project key or partial name to its project dictionary."""
    if not query:
        return None
    q = query.strip().lower()
    registry = load_projects_registry()

    if q in registry:
        return registry[q]

    for k, proj in registry.items():
        if q in k or q in proj.get("name", "").lower():
            return proj
    return None


# ==============================================================================
# PER-PROJECT ANTIGRAVITY SESSION MANAGEMENT
# ==============================================================================

def load_project_sessions() -> dict:
    if not PROJECT_SESSIONS_FILE.exists():
        return {"active_project": None, "sessions": {}}
    try:
        return json.loads(PROJECT_SESSIONS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"active_project": None, "sessions": {}}


def save_project_sessions(data: dict) -> None:
    try:
        PROJECT_SESSIONS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logging.warning(f"Failed to save project sessions: {e}")


def get_active_project_key() -> str | None:
    return load_project_sessions().get("active_project")


def set_active_project_key(proj_key: str | None) -> None:
    data = load_project_sessions()
    data["active_project"] = proj_key
    save_project_sessions(data)


def get_project_conversation_id(proj_key: str) -> str | None:
    data = load_project_sessions()
    return data.get("sessions", {}).get(proj_key)


def set_project_conversation_id(proj_key: str, conv_id: str) -> None:
    data = load_project_sessions()
    data.setdefault("sessions", {})[proj_key] = conv_id
    save_project_sessions(data)


# ==============================================================================
# GIT & ROADMAP INSPECTION
# ==============================================================================

async def inspect_project_git(proj_path: Path) -> dict:
    """Inspects Git status, branch, unpushed count, and latest commit for a project."""
    if not (proj_path / ".git").exists():
        return {"is_git": False, "branch": "-", "dirty_count": 0, "unpushed": 0, "last_commit": "Non versionné"}

    try:
        b_proc = await asyncio.create_subprocess_exec(
            "git", "branch", "--show-current",
            cwd=str(proj_path), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        b_out, _ = await b_proc.communicate()
        branch = b_out.decode().strip() or "HEAD"

        s_proc = await asyncio.create_subprocess_exec(
            "git", "status", "--porcelain",
            cwd=str(proj_path), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        s_out, _ = await s_proc.communicate()
        dirty_lines = [l for l in s_out.decode().splitlines() if l.strip()]

        u_proc = await asyncio.create_subprocess_exec(
            "git", "rev-list", "@{u}..HEAD", "--count",
            cwd=str(proj_path), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        u_out, _ = await u_proc.communicate()
        u_str = u_out.decode().strip()
        unpushed = int(u_str) if u_proc.returncode == 0 and u_str.isdigit() else 0

        l_proc = await asyncio.create_subprocess_exec(
            "git", "log", "-1", "--pretty=format:%h - %s (%cr)",
            cwd=str(proj_path), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        l_out, _ = await l_proc.communicate()
        last_commit = l_out.decode().strip() or "Aucun commit"

        return {
            "is_git": True,
            "branch": branch,
            "dirty_count": len(dirty_lines),
            "dirty_files": dirty_lines[:8],
            "unpushed": unpushed,
            "last_commit": last_commit,
        }
    except Exception as e:
        return {"is_git": True, "branch": "?", "dirty_count": 0, "unpushed": 0, "last_commit": str(e)}


def extract_project_tasks(proj_path: Path) -> dict:
    """Extracts Markdown checklist items (- [ ] and - [x]) from ROADMAP.md or TODO.md."""
    target_file = None
    for candidate in ("TODO.md", "ROADMAP.md", "README.md"):
        p = proj_path / candidate
        if p.exists():
            content = p.read_text(encoding="utf-8", errors="ignore")
            if "- [" in content:
                target_file = p
                break

    if not target_file:
        return {"file": None, "pending": [], "done_count": 0, "total": 0}

    pending = []
    done_count = 0
    lines = target_file.read_text(encoding="utf-8", errors="ignore").splitlines()
    for line_idx, line in enumerate(lines):
        m_todo = re.match(r"^\s*[-*]\s+\[\s\]\s+(.+)$", line)
        if m_todo:
            clean_txt = re.sub(r"[*_`]+", "", m_todo.group(1)).strip()
            pending.append({"line": line_idx, "text": clean_txt})
        elif re.match(r"^\s*[-*]\s+\[[xX✓]\]\s+", line):
            done_count += 1

    for item in pending:
        item["line_idx"] = item["line"]

    return {
        "file": target_file,
        "pending": pending,
        "done_count": done_count,
        "total": len(pending) + done_count,
    }


def add_project_todo(proj_key: str, task_text: str) -> dict:
    """Appends a new task to a project's TODO.md or ROADMAP.md."""
    proj = resolve_project(proj_key)
    if not proj:
        return {"ok": False, "message": "⚠️ Projet introuvable."}
    proj_path = Path(proj["path"])
    proj_path.mkdir(parents=True, exist_ok=True)

    todo_file = proj_path / "ROADMAP.md" if (proj_path / "ROADMAP.md").exists() else (proj_path / "TODO.md")
    if not todo_file.exists():
        todo_file.write_text(f"# Roadmap & Tâches — {proj['name']}\n\n- [ ] {task_text.strip()}\n", encoding="utf-8")
    else:
        with open(todo_file, "a", encoding="utf-8") as f:
            f.write(f"\n- [ ] {task_text.strip()}\n")
    return {
        "ok": True,
        "message": f"✅ <b>Tâche ajoutée dans {html.escape(proj['name'])} ({todo_file.name}) :</b>\n• ⬜ {html.escape(task_text.strip())}",
    }


def toggle_project_todo(proj_key: str, line_idx: int) -> dict:
    """Marks a checklist item as completed [x] in ROADMAP.md / TODO.md."""
    proj = resolve_project(proj_key)
    if not proj:
        return {"ok": False, "message": "⚠️ Projet introuvable."}
    info = extract_project_tasks(Path(proj["path"]))
    target_file = info.get("file")
    if not target_file or not target_file.exists():
        return {"ok": False, "message": "⚠️ Aucun fichier de tâches trouvé."}

    lines = target_file.read_text(encoding="utf-8", errors="ignore").splitlines()
    if 0 <= line_idx < len(lines):
        if "[ ]" in lines[line_idx]:
            lines[line_idx] = lines[line_idx].replace("[ ]", "[x]", 1)
            target_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return {"ok": True, "message": "✅ Tâche marquée comme terminée !"}
    return {"ok": False, "message": "⚠️ Ligne de tâche introuvable."}


# ==============================================================================
# COCKPIT & OVERVIEW FORMATTING
# ==============================================================================

async def format_all_projects_overview() -> str:
    """Builds the executive HTML overview string of all projects."""
    registry = load_projects_registry()
    active_key = get_active_project_key()

    lines = ["🚀 <b>Project Factory — Cockpit de vos Projets</b>\n"]

    for key, proj in registry.items():
        p_path = Path(proj["path"])
        if not p_path.exists():
            continue
        git_info = await inspect_project_git(p_path)
        tasks_info = extract_project_tasks(p_path)

        is_active = (key == active_key)
        badge = "🎯 " if is_active else "📂 "
        if git_info["is_git"]:
            status_parts = [f"<code>{html.escape(git_info['branch'])}</code>"]
            if git_info["dirty_count"] > 0:
                status_parts.append(f"📝 {git_info['dirty_count']} modifs")
            if git_info["unpushed"] > 0:
                status_parts.append(f"⬆️ {git_info['unpushed']} à push")
            if git_info["dirty_count"] == 0 and git_info["unpushed"] == 0:
                status_parts.append("✅ propre")
            git_badge = " • ".join(status_parts)
        else:
            git_badge = "📁 Dossier local"

        todo_str = ""
        if tasks_info["total"] > 0:
            todo_str = f" • 📋 {tasks_info['done_count']}/{tasks_info['total']} tâches"

        lines.append(
            f"{badge}<b>{html.escape(proj['name'])}</b> ({proj.get('category', 'Dev')})\n"
            f"   └ {git_badge}{todo_str}\n"
            f"   └ <i>{html.escape(proj.get('description', '')[:80])}</i>"
        )

    lines.extend([
        "\n💡 <b>Commandes rapides & Langage naturel :</b>",
        "• <code>/proj reconforge</code> : Ouvrir le cockpit d'un projet",
        "• <code>/dev reconforge &lt;mission&gt;</code> : Coder avec l'agent dans ce projet",
        "• <code>/newproj &lt;nom&gt;</code> : Créer un nouveau projet prêt à coder",
        "• <code>/ship &lt;projet&gt;</code> : Générer Post LinkedIn + Release + Portfolio",
    ])

    return "\n".join(lines)


async def get_project_cockpit(proj_key: str) -> dict:
    """Returns detailed status and interactive controls for a single project."""
    proj = resolve_project(proj_key)
    if not proj:
        return {
            "ok": False,
            "key": proj_key,
            "message": f"⚠️ Projet « <code>{html.escape(proj_key)}</code> » introuvable. Tapez <code>/proj</code> pour voir vos projets.",
        }

    set_active_project_key(proj["key"])
    p_path = Path(proj["path"])
    git_info = await inspect_project_git(p_path)
    tasks_info = extract_project_tasks(p_path)
    conv_id = get_project_conversation_id(proj["key"])

    focus_mins = 0
    if FOCUS_FILE.exists():
        try:
            sessions = json.loads(FOCUS_FILE.read_text(encoding="utf-8"))
            focus_mins = sum(
                s.get("duration_min", 0)
                for s in sessions
                if proj["name"].lower() in s.get("project", "").lower() or proj["key"] in s.get("topic", "").lower()
            )
        except Exception:
            pass

    lines = [
        f"🚀 <b>Cockpit Projet : {html.escape(proj['name'])}</b> 🟢 <i>(Projet Actif)</i>",
        f"• <b>Catégorie :</b> {proj.get('category', '💻 Dev')}",
        f"• <b>Chemin :</b> <code>{p_path}</code>",
        f"• <b>Session Agent dédiée :</b> <code>{conv_id[:8] if conv_id else 'Nouvelle session'}</code>",
        f"• <b>Temps Focus cumulé :</b> <code>{focus_mins} min</code>\n",
    ]

    if git_info["is_git"]:
        lines.append("🐙 <b>État Git :</b>")
        lines.append(f"• <b>Branche :</b> <code>{html.escape(git_info['branch'])}</code>")
        lines.append(f"• <b>Dernier commit :</b> <i>{html.escape(git_info['last_commit'])}</i>")
        lines.append(
            f"• <b>Modifications :</b> {git_info['dirty_count']} fichier(s) modifié(s) | "
            f"{git_info['unpushed']} commit(s) non pushé(s)\n"
        )

    if tasks_info["pending"]:
        lines.append(f"📋 <b>Prochaines Tâches ({len(tasks_info['pending'])} restantes sur {tasks_info['total']}) :</b>")
        for t in tasks_info["pending"][:5]:
            lines.append(f"• ⬜ {html.escape(t['text'][:85])}")
        lines.append("")
    elif tasks_info["total"] > 0:
        lines.append(f"📋 <b>Roadmap :</b> 🎉 Toutes les {tasks_info['total']} tâches sont cochées !\n")

    lines.append(
        f"💡 <i>Pour déléguer du code :</i> <code>/dev {proj['key']} ta consigne...</code>"
    )

    return {
        "ok": True,
        "key": proj["key"],
        "project": proj,
        "git": git_info,
        "tasks": tasks_info,
        "pending_tasks": tasks_info["pending"],
        "message": "\n".join(lines),
    }


# ==============================================================================
# DEVOPS ACTIONS: TESTS, DIFF, SCAFFOLDING & SHIPPING
# ==============================================================================

async def run_project_tests(proj_key: str) -> dict:
    """Auto-detects and runs the test suite for a project."""
    proj = resolve_project(proj_key)
    if not proj:
        return {"ok": False, "message": "⚠️ Projet introuvable."}
    p_path = Path(proj["path"])
    if not p_path.exists():
        return {"ok": False, "message": f"⚠️ Dossier {p_path} introuvable."}

    if (p_path / "venv" / "bin" / "pytest").exists():
        cmd = f"{p_path / 'venv' / 'bin' / 'pytest'} -q"
    elif (p_path / ".venv" / "bin" / "pytest").exists():
        cmd = f"{p_path / '.venv' / 'bin' / 'pytest'} -q"
    elif (p_path / "pyproject.toml").exists() or (p_path / "tests").exists():
        cmd = "pytest -q || python3 -m unittest discover -s tests"
    elif (p_path / "Cargo.toml").exists():
        cmd = "cargo test"
    elif (p_path / "package.json").exists():
        cmd = "npm test"
    elif (p_path / "Makefile").exists():
        cmd = "make test || make check"
    else:
        cmd = "python3 -m py_compile *.py"

    start = time.time()
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=str(p_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=90.0)
        elapsed = time.time() - start
        out = (stdout.decode("utf-8", errors="replace") + "\n" + stderr.decode("utf-8", errors="replace")).strip()
        ok = (proc.returncode == 0)
        badge = "✅ <b>TESTS RÉUSSIS</b>" if ok else f"❌ <b>ÉCHEC DES TESTS (Code {proc.returncode})</b>"
        return {
            "ok": ok,
            "message": (
                f"🧪 {badge} — <b>{html.escape(proj['name'])}</b> ({elapsed:.2f}s)\n"
                f"Commande : <code>{html.escape(cmd)}</code>\n\n"
                f"<pre>{html.escape(out[-2500:] if out else '(Sortie vide)')}</pre>"
            ),
        }
    except Exception as e:
        return {"ok": False, "message": f"⚠️ Erreur lors de l'exécution des tests : {e}"}


async def get_project_diff(proj_key: str) -> dict:
    """Returns git status + git diff summary for a project."""
    proj = resolve_project(proj_key)
    if not proj:
        return {"ok": False, "message": "⚠️ Projet introuvable."}
    p_path = Path(proj["path"])
    try:
        proc = await asyncio.create_subprocess_shell(
            "git status -s && echo '---' && git diff --stat && echo '---' && git diff | head -n 90",
            cwd=str(p_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        out = stdout.decode("utf-8", errors="replace").strip() or "(Aucune modification en cours)"
        return {
            "ok": True,
            "message": (
                f"🔍 <b>Git Diff — {html.escape(proj['name'])}</b>\n\n"
                f"<pre>{html.escape(out[:3400])}</pre>"
            ),
        }
    except Exception as e:
        return {"ok": False, "message": f"⚠️ Erreur Git Diff : {e}"}


async def scaffold_new_project(
    raw_name: str,
    template: str = "python-cli",
    description: str = "Outil développé par Cyprien",
) -> dict:
    """Scaffolds a brand new project in ~/Documents/Dev/<name> with Git, tests, and ROADMAP.md."""
    clean_name = re.sub(r"[^a-zA-Z0-9_\-]", "-", raw_name.strip()).strip("-")
    if not clean_name:
        return {"ok": False, "message": "Nom de projet invalide."}

    pkg_name = clean_name.lower().replace("-", "_")
    target_dir = DEV_DIR / clean_name
    if target_dir.exists():
        return {
            "ok": False,
            "message": f"⚠️ Le dossier <code>{target_dir}</code> existe déjà. Utilisez <code>/proj {clean_name.lower()}</code>.",
        }

    try:
        (target_dir / "src" / pkg_name).mkdir(parents=True, exist_ok=True)
        (target_dir / "tests").mkdir(parents=True, exist_ok=True)
        (target_dir / "docs").mkdir(parents=True, exist_ok=True)

        # 1. .gitignore
        (target_dir / ".gitignore").write_text(
            "__pycache__/\n*.py[cod]\n*$py.class\n.venv/\nvenv/\ndist/\nbuild/\n*.egg-info/\n.pytest_cache/\n.ruff_cache/\n.env\n",
            encoding="utf-8",
        )

        # 2. pyproject.toml
        (target_dir / "pyproject.toml").write_text(
            f'[build-system]\nrequires = ["setuptools>=68.0"]\nbuild-backend = "setuptools.build_meta"\n\n'
            f'[project]\nname = "{clean_name.lower()}"\nversion = "0.1.0"\ndescription = "{description}"\n'
            f'readme = "README.md"\nrequires-python = ">=3.11"\ndependencies = [\n    "httpx>=0.27.0",\n]\n\n'
            f'[project.scripts]\n{clean_name.lower()} = "{pkg_name}.cli:main"\n\n'
            f'[tool.pytest.ini_options]\npythonpath = ["src"]\n',
            encoding="utf-8",
        )

        # 3. Source files
        (target_dir / "src" / pkg_name / "__init__.py").write_text(
            f'"""Package {clean_name}."""\n\n__version__ = "0.1.0"\n',
            encoding="utf-8",
        )
        (target_dir / "src" / pkg_name / "cli.py").write_text(
            f'"""CLI entrypoint for {clean_name}."""\n\nimport argparse\n\n\n'
            f'def run_core(target: str) -> dict:\n    return {{"target": target, "status": "ok"}}\n\n\n'
            f'def main() -> None:\n    parser = argparse.ArgumentParser(description="{description}")\n'
            f'    parser.add_argument("target", nargs="?", default="localhost", help="Target to process")\n'
            f'    args = parser.parse_args()\n    res = run_core(args.target)\n    print(f"[+] {clean_name}: {{res}}")\n\n\n'
            f'if __name__ == "__main__":\n    main()\n',
            encoding="utf-8",
        )

        # 4. Initial unit test
        (target_dir / "tests" / "test_core.py").write_text(
            f'import unittest\nfrom {pkg_name}.cli import run_core\n\n\n'
            f'class TestCore(unittest.TestCase):\n    def test_run_core(self):\n'
            f'        res = run_core("example.com")\n        self.assertEqual(res["status"], "ok")\n\n\n'
            f'if __name__ == "__main__":\n    unittest.main()\n',
            encoding="utf-8",
        )

        # 5. README.md & ROADMAP.md
        (target_dir / "README.md").write_text(
            f"# 🚀 {clean_name}\n\n> {description}\n\n## Installation\n\n```bash\npip install -e .\n```\n\n"
            f"## Utilisation\n\n```bash\n{clean_name.lower()} example.com\n```\n",
            encoding="utf-8",
        )
        (target_dir / "ROADMAP.md").write_text(
            f"# Feuille de Route — {clean_name}\n\n"
            f"## Phase 1 : MVP Fondations\n"
            f"- [x] Initialisation de l'architecture et du dépôt Git\n"
            f"- [ ] Implémenter le moteur principal dans `src/{pkg_name}/cli.py`\n"
            f"- [ ] Ajouter la couverture de tests unitaires dans `tests/`\n"
            f"- [ ] Préparer la documentation et le post de lancement (`/ship {clean_name.lower()}`)\n",
            encoding="utf-8",
        )

        # 6. Initialize Git repository
        proc = await asyncio.create_subprocess_shell(
            'git init && git add . && git commit -m "feat: initial project scaffold via JARVIS"',
            cwd=str(target_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

        # 7. Register project
        registry = load_projects_registry()
        key = clean_name.lower()
        registry[key] = {
            "name": clean_name,
            "key": key,
            "path": str(target_dir),
            "category": f"🚀 {template}",
            "description": description,
        }
        save_projects_registry(registry)
        set_active_project_key(key)

        return {
            "ok": True,
            "key": key,
            "name": clean_name,
            "path": str(target_dir),
            "message": (
                f"🎉 <b>Nouveau Projet Créé et Initialisé : {html.escape(clean_name)}</b>\n\n"
                f"• <b>Dossier :</b> <code>{target_dir}</code>\n"
                f"• <b>Template :</b> <code>{html.escape(template)}</code>\n"
                f"• <b>Structure :</b> <code>pyproject.toml</code>, <code>src/{pkg_name}/</code>, <code>tests/</code>, <code>ROADMAP.md</code>\n"
                f"• <b>Git :</b> Dépôt initialisé avec premier commit ✅\n"
                f"• <b>Statut :</b> Défini comme projet actif 🎯\n\n"
                f"💡 <i>Lancez directement le développement avec :</i>\n"
                f"<code>/dev {key} implémente la fonctionnalité principale...</code>"
            ),
        }
    except Exception as e:
        return {"ok": False, "message": f"⚠️ Erreur lors de la création du projet : {e}"}


async def generate_ship_package(proj_key_or_topic: str, genai_client=None) -> dict:
    """Generates a LinkedIn launch post and Portfolio snippet from project commits and README."""
    proj = resolve_project(proj_key_or_topic)
    context_snippets = []
    title = proj_key_or_topic.strip() or "ReconForge"

    if proj:
        title = proj["name"]
        p_path = Path(proj["path"])
        if (p_path / "README.md").exists():
            context_snippets.append("README:\n" + (p_path / "README.md").read_text(encoding="utf-8", errors="ignore")[:1500])
        if (p_path / "ROADMAP.md").exists():
            context_snippets.append("ROADMAP:\n" + (p_path / "ROADMAP.md").read_text(encoding="utf-8", errors="ignore")[:1000])
        if (p_path / ".git").exists():
            proc = await asyncio.create_subprocess_exec(
                "git", "log", "-n", "8", "--oneline",
                cwd=str(p_path), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await proc.communicate()
            if out:
                context_snippets.append("DERNIERS COMMITS:\n" + out.decode("utf-8", errors="ignore"))
    else:
        context_snippets.append(f"Sujet / Projet : {proj_key_or_topic}")

    full_ctx = "\n\n".join(context_snippets)

    if genai_client:
        try:
            prompt = (
                "Tu es l'assistant Personal Branding & Tech de Cyprien (Ingénieur Cybersécurité & Développeur d'outils open-source comme ReconForge et JARVIS).\n"
                f"Voici les données réelles du projet « {title} » :\n{full_ctx}\n\n"
                "Rédige :\n"
                "1. Un post LinkedIn percutant, authentique et technique (Hook fort, Problème adressé, Architecture/Fonctionnalités clés avec puces, Ce que ça apporte, Call-to-Action + 5 hashtags pertinents).\n"
                "2. Un court encart Markdown/LaTeX prêt à ajouter dans son Portfolio d'ingénieur.\n"
                "Sois direct, évite le jargon marketing creux, mets en valeur la rigueur technique."
            )
            resp = await asyncio.to_thread(
                genai_client.models.generate_content,
                model="gemini-3.5-flash-lite",
                contents=prompt,
            )
            generated = (resp.text or "").strip()
            if generated:
                safe_gen = html.escape(generated)
                if len(safe_gen) > 3500:
                    safe_gen = safe_gen[:3500] + "\n... [tronqué]"
                return {
                    "ok": True,
                    "project": proj["key"] if proj else title,
                    "title": title,
                    "raw_text": generated,
                    "message": f"📣 <b>Kit de Lancement & Post LinkedIn — {html.escape(title)}</b>\n\n{safe_gen}",
                }
        except Exception as e:
            logging.warning(f"Ship generator fallback: {e}")

    fallback_post = (
        f"🚀 Fier de partager les dernières avancées sur {title} !\n\n"
        f"🎯 Objectif : Automatiser et fiabiliser les workflows techniques avec une architecture modulaire et rapide.\n\n"
        f"🛠️ Points clés :\n"
        f"• Architecture asynchrone haute performance\n"
        f"• Intégration native Linux / CLI\n"
        f"• Tests automatisés et documentation claire\n\n"
        f"#Cybersecurity #Python #OpenSource #Engineering #DevSecOps"
    )
    return {
        "ok": True,
        "project": proj["key"] if proj else title,
        "title": title,
        "raw_text": fallback_post,
        "message": f"📣 <b>Kit de Lancement — {html.escape(title)}</b>\n\n{html.escape(fallback_post)}",
    }

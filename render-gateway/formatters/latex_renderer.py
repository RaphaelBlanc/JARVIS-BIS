"""
latex_renderer.py — Rendu LaTeX, conversion Unicode et gestion des médias.

Permet de :
1. Détecter les équations LaTeX (blocs $$...$$, \\[...\\], \\begin{equation}...).
2. Rendre les formules mathématiques en images PNG nettes (thème sombre/clair).
3. Convertir les formules en ligne ($...$) en symboles mathématiques Unicode lisibles.
4. Détecter et préparer les extraits de code pour téléchargement et copie instantanée.
"""

import html
import io
import logging
import re
import urllib.parse
import uuid
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# ── Protection des blocs de code ──────────────────────────────────────────────
# Les formules LaTeX ne doivent jamais être interprétées à l'intérieur d'un bloc
# de code ou d'un code inline. On les remplace temporairement par des sentinelles
# avant le traitement mathématique, puis on les restaure à l'identique.

_FENCED_CODE_PATTERN = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_PATTERN = re.compile(r"`[^`\n]+`")


def _protect_code_spans(text: str) -> tuple[str, list[str], str]:
    """Remplace les blocs de code et le code inline par des sentinelles.

    Returns:
        (texte_protégé, contenus_originaux, jeton)
        Le jeton rend les sentinelles uniques à chaque appel, ce qui évite
        toute collision lorsque la protection est appliquée en cascade.
    """
    spans: list[str] = []
    token = uuid.uuid4().hex

    def _stash(match: re.Match[str]) -> str:
        spans.append(match.group(0))
        return f"\x00ASSISTANT_CODE_{token}_{len(spans) - 1}\x00"

    text = _FENCED_CODE_PATTERN.sub(_stash, text)
    text = _INLINE_CODE_PATTERN.sub(_stash, text)
    return text, spans, token


def _restore_code_spans(text: str, spans: list[str], token: str) -> str:
    """Restaure les blocs de code protégés par ``_protect_code_spans``."""
    for index, span in enumerate(spans):
        text = text.replace(f"\x00ASSISTANT_CODE_{token}_{index}\x00", span)
    return text


# ── Remplacement mathématique Unicode pour le texte en ligne ───────────────────

UNICODE_MATH_MAP = [
    # Lettres grecques minuscules
    (r"\\alpha\b", "α"),
    (r"\\beta\b", "β"),
    (r"\\gamma\b", "γ"),
    (r"\\delta\b", "δ"),
    (r"\\epsilon\b", "ε"),
    (r"\\zeta\b", "ζ"),
    (r"\\eta\b", "η"),
    (r"\\theta\b", "θ"),
    (r"\\iota\b", "ι"),
    (r"\\kappa\b", "κ"),
    (r"\\lambda\b", "λ"),
    (r"\\mu\b", "μ"),
    (r"\\nu\b", "ν"),
    (r"\\xi\b", "ξ"),
    (r"\\pi\b", "π"),
    (r"\\rho\b", "ρ"),
    (r"\\sigma\b", "σ"),
    (r"\\tau\b", "τ"),
    (r"\\upsilon\b", "υ"),
    (r"\\phi\b", "φ"),
    (r"\\chi\b", "χ"),
    (r"\\psi\b", "ψ"),
    (r"\\omega\b", "ω"),
    # Lettres grecques majuscules
    (r"\\Gamma\b", "Γ"),
    (r"\\Delta\b", "Δ"),
    (r"\\Theta\b", "Θ"),
    (r"\\Lambda\b", "Λ"),
    (r"\\Xi\b", "Ξ"),
    (r"\\Pi\b", "Π"),
    (r"\\Sigma\b", "Σ"),
    (r"\\Phi\b", "Φ"),
    (r"\\Psi\b", "Ψ"),
    (r"\\Omega\b", "Ω"),
    # Opérateurs & Symboles
    (r"\\times\b", "×"),
    (r"\\div\b", "÷"),
    (r"\\cdot\b", "·"),
    (r"\\pm\b", "±"),
    (r"\\mp\b", "∓"),
    (r"\\neq\b", "≠"),
    (r"\\approx\b", "≈"),
    (r"\\sim\b", "∼"),
    (r"\\le\b|\\leq\b", "≤"),
    (r"\\ge\b|\\geq\b", "≥"),
    (r"\\ll\b", "≪"),
    (r"\\gg\b", "≫"),
    (r"\\infty\b", "∞"),
    (r"\\partial\b", "∂"),
    (r"\\nabla\b", "∇"),
    (r"\\sum\b", "∑"),
    (r"\\prod\b", "∏"),
    (r"\\int\b", "∫"),
    (r"\\oint\b", "∮"),
    # Ensembles & Logique
    (r"\\in\b", "∈"),
    (r"\\notin\b", "∉"),
    (r"\\subset\b", "⊂"),
    (r"\\subseteq\b", "⊆"),
    (r"\\cup\b", "∪"),
    (r"\\cap\b", "∩"),
    (r"\\forall\b", "∀"),
    (r"\\exists\b", "∃"),
    (r"\\nexists\b", "∄"),
    (r"\\to\b|\\rightarrow\b", "→"),
    (r"\\leftarrow\b", "←"),
    (r"\\Rightarrow\b|\\implies\b", "⇒"),
    (r"\\Leftarrow\b", "⇐"),
    (r"\\iff\b|\\Leftrightarrow\b", "⇔"),
    (r"\\mathbb\{R\}", "ℝ"),
    (r"\\mathbb\{N\}", "ℕ"),
    (r"\\mathbb\{Z\}", "ℤ"),
    (r"\\mathbb\{Q\}", "ℚ"),
    (r"\\mathbb\{C\}", "ℂ"),
    # Fonctions standard
    (r"\\sqrt\{([^}]+)\}", r"√(\1)"),
    (r"\\sqrt\s*([a-zA-Z0-9])", r"√\1"),
    (r"\\frac\{([^}]+)\}\{([^}]+)\}", r"(\1)/(\2)"),
    (r"\\text\{([^}]+)\}", r"\1"),
    (r"\\mathrm\{([^}]+)\}", r"\1"),
    (r"\\mathbf\{([^}]+)\}", r"\1"),
]

# Exposants et indices
SUPERSCRIPTS = {
    "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴",
    "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹",
    "+": "⁺", "-": "⁻", "=": "⁼", "(": "⁽", ")": "⁾",
    "n": "ⁿ", "i": "ⁱ", "x": "ˣ", "y": "ʸ",
}

SUBSCRIPTS = {
    "0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄",
    "5": "₅", "6": "₆", "7": "₇", "8": "₈", "9": "₉",
    "+": "₊", "-": "₋", "=": "₌", "(": "₍", ")": "₎",
    "a": "ₐ", "e": "ₑ", "o": "ₒ", "x": "ₓ",
}


def latex_to_unicode(text: str) -> str:
    """Transforme les équations en ligne $...$ et symboles LaTeX en équivalent Unicode élégant.

    Les blocs de code (```...```) et le code inline (`...`) sont protégés :
    leur contenu n'est jamais modifié.
    """
    if not text:
        return ""

    protected_text, code_spans, code_token = _protect_code_spans(text)

    def convert_formula(formula: str) -> str:
        res = formula.strip()
        for pattern, replacement in UNICODE_MATH_MAP:
            res = re.sub(pattern, replacement, res)

        # Exposants courts : ^2 ou ^{12}
        def sub_sup(m):
            chars = m.group(1)
            return "".join(SUPERSCRIPTS.get(c, c) for c in chars)

        res = re.sub(r"\^\{([0-9+\-=()nixy]+)\}", sub_sup, res)
        res = re.sub(r"\^([0-9+\-=()nixy])", sub_sup, res)

        # Indices courts : _0 ou _{12}
        def sub_sub(m):
            chars = m.group(1)
            return "".join(SUBSCRIPTS.get(c, c) for c in chars)

        res = re.sub(r"_\{([0-9+\-=()aeox]+)\}", sub_sub, res)
        res = re.sub(r"_([0-9+\-=()aeox])", sub_sub, res)

        # Nettoyage des accolades résiduelles
        res = res.replace("{", "").replace("}", "")
        return res

    # Remplacement des $...$ (hors blocs $$...$$)
    def replace_inline(match):
        raw = match.group(1)
        return convert_formula(raw)

    converted = re.sub(r"(?<!\$)\$(?!\$)([^\$\n]+)\$(?!\$)", replace_inline, protected_text)
    return _restore_code_spans(converted, code_spans, code_token)


# ── Rendu d'équation en image PNG ─────────────────────────────────────────────

async def render_latex_to_png(
    formula: str,
    dark_mode: bool = True,
    timeout: float = 6.0,
) -> Optional[bytes]:
    """Rend une formule LaTeX en image PNG haute résolution (CodeCogs API).

    Args:
        formula: Code LaTeX de la formule (sans les délimiteurs $$).
        dark_mode: True pour fond sombre adapté à Discord/Telegram, False pour fond blanc.
        timeout: Délai d'attente max en secondes.

    Returns:
        Les octets PNG de l'image ou None si le rendu échoue.
    """
    clean_formula = formula.strip().strip("$").strip()
    if not clean_formula:
        return None

    # Configuration des couleurs : fond sombre Discord #2b2d31, texte blanc
    bg_color = "2b2d31" if dark_mode else "ffffff"
    text_color = "ffffff" if dark_mode else "000000"

    # Paramètres CodeCogs : 200 DPI pour netteté sur écrans Retina
    params = f"\\dpi{{220}}\\color{{{text_color}}}\\bg{{{bg_color}}} {clean_formula}"
    encoded = urllib.parse.quote(params)
    url = f"https://latex.codecogs.com/png.image?{encoded}"

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
            if resp.status_code == 200 and len(resp.content) > 100:
                return resp.content
    except Exception as exc:
        logger.debug("Échec rendu CodeCogs pour '%s': %s", clean_formula[:40], exc)

    return None


# ── Détection et extraction des équations de bloc ─────────────────────────────

BLOCK_LATEX_PATTERNS = [
    re.compile(r"\$\$(.*?)\$\$", re.DOTALL),
    re.compile(r"\\\[(.*?)\\\]", re.DOTALL),
    re.compile(r"\\begin\{equation\*?\}(.*?)\\end\{equation\*?\}", re.DOTALL),
    re.compile(r"\\begin\{align\*?\}(.*?)\\end\{align\*?\}", re.DOTALL),
]


async def extract_and_render_latex(
    text: str,
    dark_mode: bool = True,
    max_equations: int = 4,
) -> tuple[str, list[dict[str, Any]]]:
    """Extrait les équations blocs, génère leur image et adapte le texte.

    Returns:
        (texte_mis_a_jour, liste_des_images)
        où chaque image est un dict : {"bytes": bytes, "filename": str, "title": str, "formula": str}
    """
    if not text:
        return text, []

    rendered_images: list[dict[str, Any]] = []
    # Les blocs de code sont mis de côté : une formule qui s'y trouve est du
    # code source, pas une équation à rendre.
    protected_text, code_spans, code_token = _protect_code_spans(text)
    updated_text = protected_text

    # Recherche des blocs d'équations
    eq_index = 1
    for pattern in BLOCK_LATEX_PATTERNS:
        matches = list(pattern.finditer(updated_text))
        for match in matches:
            if len(rendered_images) >= max_equations:
                break
            raw_block = match.group(0)
            formula = match.group(1).strip()
            if not formula:
                continue

            png_bytes = await render_latex_to_png(formula, dark_mode=dark_mode)
            if png_bytes:
                filename = f"equation_{eq_index}.png"
                rendered_images.append({
                    "bytes": png_bytes,
                    "filename": filename,
                    "title": f"Équation {eq_index}",
                    "formula": formula,
                })
                # Remplacement dans le texte par une référence propre et lisible
                unicode_eq = latex_to_unicode(f"${formula}$")
                replacement = f"\n📐 **[Équation {eq_index}]** : `{unicode_eq}`\n"
                updated_text = updated_text.replace(raw_block, replacement, 1)
                eq_index += 1

    # Convertir également le LaTeX inline résiduel ($...$) en Unicode
    updated_text = latex_to_unicode(updated_text)

    # Restaure les blocs de code exactement tels qu'ils ont été fournis.
    return _restore_code_spans(updated_text, code_spans, code_token), rendered_images


# ── Détection et extraction des gros blocs de code ────────────────────────────

CODE_BLOCK_PATTERN = re.compile(r"```([a-zA-Z0-9_\-\+]*)\n(.*?)```", re.DOTALL)

EXT_MAP = {
    "python": "py", "py": "py",
    "javascript": "js", "js": "js", "typescript": "ts", "ts": "ts",
    "bash": "sh", "sh": "sh", "shell": "sh", "zsh": "sh",
    "html": "html", "css": "css", "json": "json",
    "sql": "sql", "c": "c", "cpp": "cpp", "rust": "rs", "go": "go",
    "java": "java", "yaml": "yml", "dockerfile": "Dockerfile",
}


def extract_downloadable_code(text: str, min_chars: int = 600) -> list[dict[str, Any]]:
    """Détecte les blocs de code volumineux pour les proposer en fichier téléchargeable."""
    snippets = []
    idx = 1
    for match in CODE_BLOCK_PATTERN.finditer(text):
        lang = (match.group(1) or "txt").lower()
        code = match.group(2)
        if len(code) >= min_chars:
            ext = EXT_MAP.get(lang, "txt")
            snippets.append({
                "lang": lang,
                "code": code,
                "filename": f"script_{idx}.{ext}" if ext != "Dockerfile" else "Dockerfile",
                "bytes": code.encode("utf-8"),
            })
            idx += 1
    return snippets


def smart_split_discord(text: str, max_chars: int = 1950) -> list[str]:
    """Découpe un texte pour Discord en préservant l'intégrité et la colorisation des blocs de code.

    Les lignes uniques démesurées (base64, code minifié, longue URL) sont
    découpées avant l'analyse ligne par ligne afin qu'aucun message ne dépasse
    la limite de Discord.
    """
    if max_chars < 50:
        max_chars = 50
    if len(text) <= max_chars:
        return [text]

    # Une ligne ne doit jamais occuper tout le budget : on garde de la place
    # pour les fences de code ajoutées lors du découpage.
    max_line_len = max(200, max_chars - 200)
    normalized_lines: list[str] = []
    for line in text.split("\n"):
        if len(line) <= max_line_len:
            normalized_lines.append(line)
        else:
            normalized_lines.extend(
                line[i:i + max_line_len] for i in range(0, len(line), max_line_len)
            )

    parts: list[str] = []
    lines = normalized_lines
    current_chunk: list[str] = []
    current_len = 0
    in_code_block = False
    current_lang = ""

    for line in lines:
        code_fence_match = re.match(r"^```([a-zA-Z0-9_\-\+]*)\s*$", line)
        line_len = len(line) + 1

        if current_len + line_len > max_chars:
            if in_code_block:
                current_chunk.append("```")
                parts.append("\n".join(current_chunk))
                current_chunk = [f"```{current_lang}", line]
                current_len = len(current_chunk[0]) + 1 + line_len
            else:
                if current_chunk:
                    parts.append("\n".join(current_chunk))
                current_chunk = [line]
                current_len = line_len
        else:
            current_chunk.append(line)
            current_len += line_len

        if code_fence_match:
            if not in_code_block:
                in_code_block = True
                current_lang = code_fence_match.group(1) or ""
            else:
                in_code_block = False
                current_lang = ""

    if current_chunk:
        parts.append("\n".join(current_chunk))

    # Filet de sécurité : aucun morceau ne doit dépasser la limite Discord,
    # même dans un cas limite où une fence de code très longue serait ajoutée.
    safe_parts: list[str] = []
    for part in parts:
        if len(part) <= max_chars:
            safe_parts.append(part)
        else:
            safe_parts.extend(
                part[i:i + max_chars] for i in range(0, len(part), max_chars)
            )

    return safe_parts


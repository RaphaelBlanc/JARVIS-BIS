"""
stt.py — Module de Speech-to-Text (reconnaissance vocale) hybride ultra-rapide.

Stratégie :
1. Si GROQ_API_KEY est défini : Groq Whisper-large-v3-turbo via API HTTP (~300ms, gratuit).
2. Sinon si OPENAI_API_KEY est défini : OpenAI Whisper-1 via API HTTP.
3. Sinon : Exécution locale via faster-whisper (modèle int8 sur CPU, autonome & hors-ligne).
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "").strip()
OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "").strip()
LOCAL_WHISPER_MODEL: str = os.getenv("WHISPER_MODEL", "base").strip()
# Langue de transcription par défaut : "fr" pour éviter les hallucinations (russe, cyrillique) sur les silences ou bruits de fond.
STT_LANGUAGE: str = os.getenv("WHISPER_LANGUAGE", "fr").strip().lower()
STT_MAX_AUDIO_MB: int = int(os.getenv("STT_MAX_AUDIO_MB", "25"))
MAX_AUDIO_BYTES: int = max(1, STT_MAX_AUDIO_MB) * 1024 * 1024

# Modèle local chargé en singleton paresseux (lazy loading)
_local_whisper_model = None


def _get_local_model():
    """Charge le modèle local faster-whisper en mémoire une seule fois."""
    global _local_whisper_model
    if _local_whisper_model is None:
        try:
            from faster_whisper import WhisperModel

            logger.info(
                "Initialisation du modèle local faster-whisper ('%s', int8 sur CPU)...",
                LOCAL_WHISPER_MODEL,
            )
            # Utilise int8 sur CPU pour vitesse maximale et consommation RAM minimale
            _local_whisper_model = WhisperModel(
                LOCAL_WHISPER_MODEL,
                device="cpu",
                compute_type="int8",
            )
            logger.info("Modèle faster-whisper chargé avec succès !")
        except Exception as exc:
            logger.error("Impossible d'initialiser faster-whisper local : %s", exc)
            return None
    return _local_whisper_model


async def _transcribe_groq(
    file_path: Path, language: Optional[str]
) -> Optional[str]:
    """Transcrit l'audio via l'API ultra-rapide Groq Whisper."""
    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        with open(file_path, "rb") as f:
            files = {"file": (file_path.name, f, "audio/wav")}
            data = {
                "model": "whisper-large-v3-turbo",
                "response_format": "text",
            }
            if not language or language.lower().startswith("fr"):
                data["prompt"] = "Transcription en français."
            if language:
                data["language"] = language
            resp = await client.post(url, headers=headers, files=files, data=data)
            if resp.status_code == 200:
                return resp.text.strip()
            logger.warning("Échec transcription Groq: HTTP %d - %s", resp.status_code, resp.text)
    return None


async def _transcribe_openai(
    file_path: Path, language: Optional[str]
) -> Optional[str]:
    """Transcrit l'audio via l'API OpenAI Whisper."""
    url = "https://api.openai.com/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        with open(file_path, "rb") as f:
            files = {"file": (file_path.name, f, "audio/wav")}
            data = {
                "model": "whisper-1",
                "response_format": "text",
            }
            if not language or language.lower().startswith("fr"):
                data["prompt"] = "Transcription en français."
            if language:
                data["language"] = language
            resp = await client.post(url, headers=headers, files=files, data=data)
            if resp.status_code == 200:
                return resp.text.strip()
            logger.warning("Échec transcription OpenAI: HTTP %d - %s", resp.status_code, resp.text)
    return None


def _transcribe_local_sync(
    file_path: Path, language: Optional[str]
) -> Tuple[Optional[str], Optional[str]]:
    """Exécute la transcription locale dans un thread bloquant séparé.

    Retourne (texte, langue_détectée).
    """
    model = _get_local_model()
    if model is None:
        return None, None

    try:
        kwargs = {
            "beam_size": 3,
            "vad_filter": True,  # Supprime les blancs/silences
        }
        if not language or language.lower().startswith("fr"):
            kwargs["initial_prompt"] = "Transcription en français."
        if language:
            kwargs["language"] = language
        segments, info = model.transcribe(str(file_path), **kwargs)
        text_parts = [seg.text.strip() for seg in segments if seg.text.strip()]
        detected = getattr(info, "language", None) or language
        probability = getattr(info, "language_probability", None)
        if detected and probability is not None:
            logger.info(
                "Langue détectée par Whisper : %s (%.0f%%)",
                detected,
                float(probability) * 100,
            )
        elif detected:
            logger.info("Langue de transcription : %s", detected)
        return " ".join(text_parts).strip(), detected
    except Exception as exc:
        logger.error("Erreur durant la transcription locale faster-whisper : %s", exc)
        return None, None


async def warmup_stt() -> None:
    """Préchauffe le modèle local pour éviter la latence du premier vocal."""
    if GROQ_API_KEY or OPENAI_API_KEY:
        logger.info("STT distant configuré : pas de préchauffage local nécessaire.")
        return

    logger.info(
        "Préchauffage du modèle faster-whisper local ('%s') en arrière-plan...",
        LOCAL_WHISPER_MODEL,
    )
    model = await asyncio.to_thread(_get_local_model)
    if model is not None:
        logger.info("Modèle faster-whisper local prêt.")


def _resolve_language(language: Optional[str]) -> Optional[str]:
    """Résout la langue demandée ; fallback sur STT_LANGUAGE ou 'fr'."""
    if language is not None and not language.strip():
        return None
    value = (language if language is not None else STT_LANGUAGE).strip().lower()
    if value in {"auto", "automatic", "détection", "detection"}:
        return None
    return value or None


async def transcribe_audio(
    file_path: Path, language: Optional[str] = None
) -> Tuple[Optional[str], str]:
    """Transcrit un fichier audio (ogg, mp3, wav, m4a).

    ``language`` vaut ``None`` par défaut : la langue est détectée
    automatiquement (ou forcée via ``WHISPER_LANGUAGE``).

    Retourne :
        (texte_transcrit, moteur_utilisé)
    """
    path = Path(file_path)
    if not path.exists():
        return None, "Fichier audio introuvable"

    try:
        size = path.stat().st_size
    except OSError:
        return None, "Fichier audio illisible"
    if size > MAX_AUDIO_BYTES:
        return (
            None,
            f"Fichier audio trop volumineux ({size / 1024 / 1024:.1f} Mo, "
            f"maximum {STT_MAX_AUDIO_MB} Mo)",
        )

    resolved_language = _resolve_language(language)

    # 1. Essai Groq (ultra-rapide)
    if GROQ_API_KEY:
        try:
            logger.info("Tentative de transcription audio via Groq Whisper...")
            text = await _transcribe_groq(path, resolved_language)
            if text:
                return text, f"groq-whisper:{resolved_language or 'auto'}"
        except Exception as exc:
            logger.warning("Erreur Groq: %s, repli vers le moteur suivant...", exc)

    # 2. Essai OpenAI
    if OPENAI_API_KEY:
        try:
            logger.info("Tentative de transcription audio via OpenAI Whisper...")
            text = await _transcribe_openai(path, resolved_language)
            if text:
                return text, f"openai-whisper:{resolved_language or 'auto'}"
        except Exception as exc:
            logger.warning("Erreur OpenAI: %s, repli vers le moteur suivant...", exc)

    # 3. Fallback Local faster-whisper (sur CPU dans un thread séparé)
    logger.info(
        "Transcription audio via faster-whisper local (modèle %s, langue %s)...",
        LOCAL_WHISPER_MODEL,
        resolved_language or "auto",
    )
    text, detected = await asyncio.to_thread(
        _transcribe_local_sync, path, resolved_language
    )
    if text:
        return text, f"local-whisper-{LOCAL_WHISPER_MODEL}:{detected or 'auto'}"

    return None, "Échec de transcription sur tous les moteurs disponibles"

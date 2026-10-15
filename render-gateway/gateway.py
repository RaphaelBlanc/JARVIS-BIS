"""
gateway.py — Client HTTP asynchrone vers le home-runner.

Fournit `forward_to_runner()`, qui envoie une requête POST au serveur maison
exposé via Cloudflare Tunnel et retourne la réponse normalisée.
"""

import httpx
import os
import logging

logger = logging.getLogger(__name__)

HOME_SERVER_URL: str = os.getenv("HOME_SERVER_URL", "")
SECRET_KEY: str = os.getenv("SECRET_KEY", "")
MAX_RESPONSE_LENGTH: int = 4000


async def forward_to_runner(prompt: str, timeout: int = 130) -> dict:
    """Transmet un prompt au home-runner et retourne sa réponse.

    Args:
        prompt: Le texte à envoyer au runner.
        timeout: Délai maximum en secondes avant abandon (défaut: 130).

    Returns:
        Un dictionnaire avec les clés:
            - ``success`` (bool): True si la requête a réussi.
            - ``output`` (str): La réponse du runner (tronquée si > 4000 chars).
            - ``error`` (str | None): Message d'erreur en cas d'échec.
    """
    if not HOME_SERVER_URL:
        logger.error("HOME_SERVER_URL n'est pas configuré.")
        return {"success": False, "output": "", "error": "HOME_SERVER_URL non configuré"}

    logger.info("Transmission du prompt au home-runner (%d chars).", len(prompt))

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{HOME_SERVER_URL}/run",
                json={"prompt": prompt, "timeout": 120},
                headers={"X-Secret-Key": SECRET_KEY},
            )
            resp.raise_for_status()
            data: dict = resp.json()

            output: str = data.get("output", "")
            if len(output) > MAX_RESPONSE_LENGTH:
                logger.info(
                    "Réponse tronquée: %d → %d chars.", len(output), MAX_RESPONSE_LENGTH
                )
                data["output"] = output[:MAX_RESPONSE_LENGTH] + "\n\n...(réponse tronquée)"

            logger.info("Réponse reçue — succès: %s.", data.get("success"))
            return data

    except httpx.TimeoutException:
        logger.warning("Timeout atteint (%ds) en attendant la réponse du home-runner.", timeout)
        return {
            "success": False,
            "output": "",
            "error": f"Le serveur maison n'a pas répondu à temps ({timeout}s)",
        }

    except httpx.HTTPStatusError as e:
        logger.error("Erreur HTTP %d depuis le home-runner.", e.response.status_code)
        return {
            "success": False,
            "output": "",
            "error": f"Erreur HTTP {e.response.status_code}",
        }

    except Exception as e:
        logger.exception("Erreur inattendue dans forward_to_runner.")
        return {"success": False, "output": "", "error": str(e)}

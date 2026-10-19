"""
core/tools/base.py — Définition de base pour l'architecture modulaire des outils (Tools).

Chaque outil (Moodle, Obsidian, Système PC, Budget, etc.) hérite de BaseTool
ou s'enregistre dans le ToolRegistry comme un plugin modulaire façon MCP.
"""

from abc import ABC
from typing import Any, Callable, Dict, List, Optional
import os
import logging

logger = logging.getLogger("core.tools")


class ToolParam:
    """Paramètre de configuration requis par un outil."""

    def __init__(
        self,
        name: str,
        label: str,
        description: str = "",
        default: Any = "",
        secret: bool = False,
        param_type: str = "str",  # "str", "int", "float", "bool", "path"
    ):
        self.name = name
        self.label = label
        self.description = description
        self.default = default
        self.secret = secret
        self.param_type = param_type


class BaseTool(ABC):
    """Classe de base pour un module / outil d'assistant personnel."""

    id: str = "base"
    name: str = "Base Tool"
    description: str = ""
    icon: str = "🔧"
    requires_local_hardware: bool = False  # Nécessite Wayland / display / micro / webcam locale
    params: List[ToolParam] = []

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self._enabled: bool = False

    @property
    def is_enabled(self) -> bool:
        """Indique si l'outil est activé dans la configuration ou l'environnement."""
        env_key = f"ENABLE_TOOL_{self.id.upper()}"
        env_val = os.getenv(env_key)
        if env_val is not None:
            return env_val.strip().lower() in ("1", "true", "yes", "on", "oui")
        return bool(self.config.get(f"enable_{self.id}", False))

    def get_config_val(self, key: str, default: Any = None) -> Any:
        """Récupère une valeur de configuration depuis l'environnement ou config dict."""
        return os.getenv(key, self.config.get(key, default))


class ToolRegistry:
    """Registre central des outils de l'assistant."""

    _tools: Dict[str, BaseTool] = {}

    @classmethod
    def register(cls, tool_instance: BaseTool) -> None:
        cls._tools[tool_instance.id] = tool_instance
        logger.debug(f"Outil enregistré: {tool_instance.id} ({tool_instance.name})")

    @classmethod
    def get_tool(cls, tool_id: str) -> Optional[BaseTool]:
        return cls._tools.get(tool_id)

    @classmethod
    def list_tools(cls) -> List[BaseTool]:
        return list(cls._tools.values())

    @classmethod
    def list_enabled_tools(cls) -> List[BaseTool]:
        return [t for t in cls._tools.values() if t.is_enabled]

    @classmethod
    def is_tool_enabled(cls, tool_id: str) -> bool:
        tool = cls.get_tool(tool_id)
        return tool.is_enabled if tool else False

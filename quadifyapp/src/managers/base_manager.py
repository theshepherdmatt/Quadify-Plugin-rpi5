# src/managers/base_manager.py
from abc import ABC, abstractmethod
import logging
import threading

try:
    from handlers.icon_provider import IconProvider
except Exception:
    IconProvider = None

class BaseManager(ABC):
    def __init__(self, display_manager, volumio_listener, mode_manager):
        self.display_manager = display_manager
        self.volumio_listener = volumio_listener
        self.mode_manager = mode_manager
        self.is_active = False
        self.on_mode_change_callbacks = []

        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.setLevel(logging.INFO)

        # Shared IconProvider: prefer ModeManager’s instance, else create one
        self.icon_provider = getattr(mode_manager, "icon_provider", None)
        if self.icon_provider is None and IconProvider:
            try:
                self.icon_provider = IconProvider()
            except Exception:
                self.icon_provider = None

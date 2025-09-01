# src/managers/menus/clock_menu.py

import logging
import time
from typing import List, Dict, Optional

from PIL import ImageFont
from managers.menus.base_manager import BaseManager


class ClockMenu(BaseManager):
    """
    Clock settings menu rendered via the central MenuManager list view.

    Menus:
      - main
          • Show Seconds  -> (On / Off)
          • Show Date     -> (On / Off)
          • Select Font   -> (Sans / Dots / Digital / Bold / Back)
          • Back
      - seconds           -> On / Off (applies immediately, returns to main)
      - date              -> On / Off (applies immediately, returns to main)
      - fonts             -> Sans / Dots / Digital / Bold / Back
    """

    def __init__(
        self,
        display_manager,
        mode_manager,
        menu_controller=None,
        window_size: int = 4,
        y_offset: int = 2,
        line_spacing: int = 15,
    ):
        super().__init__(display_manager, None, mode_manager)
        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.setLevel(logging.INFO)

        # Central Menu controller (MenuManager)
        self.menu_controller = (
            menu_controller
            or getattr(self.mode_manager, "menu_manager", None)
            or getattr(self.mode_manager, "menu_controller", None)
        )

        # Runtime
        self.is_active = False
        self.current_menu = "main"
        self.menu_stack: List[Dict] = []  # push {"menu": name} for back nav

        # UI bits (not used by MenuManager renderer but kept for consistency)
        self.window_size = window_size
        self.y_offset = y_offset
        self.line_spacing = line_spacing
        self.font_key = "menu_font"
        self.font: ImageFont.FreeTypeFont = display_manager.fonts.get(
            self.font_key, ImageFont.load_default()
        )

        # Debounce
        self._last_action = 0.0
        self._debounce = 0.25

        self.logger.info(
            "ClockMenu initialised. menu_controller=%s",
            type(self.menu_controller).__name__ if self.menu_controller else None
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start_mode(self):
        if self.is_active:
            return
        self.is_active = True
        self.menu_stack.clear()
        self.current_menu = "main"
        self._show_current_menu()

    def stop_mode(self):
        if not self.is_active:
            return
        self.is_active = False
        try:
            self.display_manager.clear_screen()
        except Exception:
            pass
        self.logger.info("ClockMenu: Stopped and cleared display.")

    # ------------------------------------------------------------------
    # Menu rendering via MenuManager
    # ------------------------------------------------------------------

    def _title_for_menu(self, menu_name: str) -> str:
        return {
            "main": "Clock",
            "seconds": "Show Seconds",
            "date": "Show Date",
            "fonts": "Select Font",
        }.get(menu_name, "Clock")

    def _items_for_menu(self, menu_name: str) -> List[Dict]:
        cfg = self.mode_manager.config

        if menu_name == "main":
            sec_state = "On" if cfg.get("show_seconds", False) else "Off"
            date_state = "On" if cfg.get("show_date", False) else "Off"
            font_key = cfg.get("clock_font_key", "clock_sans")
            font_label = {
                "clock_sans": "Sans",
                "clock_dots": "Dots",
                "clock_digital": "Digital",
                "clock_bold": "Bold",
            }.get(font_key, "Sans")

            return [
                {"title": f"Show Seconds: {sec_state}", "type": "submenu", "submenu": "seconds"},
                {"title": f"Show Date: {date_state}",    "type": "submenu", "submenu": "date"},
                {"title": f"Select Font: {font_label}",  "type": "submenu", "submenu": "fonts"},
                {"title": "Back",                         "type": "back"},
            ]

        if menu_name == "seconds":
            return [
                {"title": "On",  "type": "action", "action": "set_seconds", "value": True},
                {"title": "Off", "type": "action", "action": "set_seconds", "value": False},
                {"title": "Back", "type": "back"},
            ]

        if menu_name == "date":
            return [
                {"title": "On",  "type": "action", "action": "set_date", "value": True},
                {"title": "Off", "type": "action", "action": "set_date", "value": False},
                {"title": "Back", "type": "back"},
            ]

        if menu_name == "fonts":
            return [
                {"title": "Sans",    "type": "action", "action": "set_font", "value": "clock_sans"},
                {"title": "Dots",    "type": "action", "action": "set_font", "value": "clock_dots"},
                {"title": "Digital", "type": "action", "action": "set_font", "value": "clock_digital"},
                {"title": "Bold",    "type": "action", "action": "set_font", "value": "clock_bold"},
                {"title": "Back",    "type": "back"},
            ]

        return [{"title": "Back", "type": "back"}]

    def _show_current_menu(self):
        if not self.menu_controller:
            self.logger.warning("ClockMenu: Menu controller not available; cannot render list.")
            return

        items = self._items_for_menu(self.current_menu)
        title = self._title_for_menu(self.current_menu)

        self.menu_controller.show_list(
            title=title,
            items=items,
            on_select=self._on_list_select,
            on_back=self.back
        )

    # ------------------------------------------------------------------
    # Selection handlers
    # ------------------------------------------------------------------

    def _debounced(self) -> bool:
        now = time.time()
        if (now - self._last_action) < self._debounce:
            return True
        self._last_action = now
        return False

    def _on_list_select(self, item: Dict):
        if self._debounced() or not self.is_active:
            return

        typ = (item.get("type") or "").lower()
        title = item.get("title", "")
        action = item.get("action")
        value = item.get("value")
        submenu = item.get("submenu")

        if typ in ("info", "message"):
            return

        if typ == "back" or title.strip().lower() == "back":
            self.back()
            return

        if typ == "submenu" and submenu:
            self.menu_stack.append({"menu": self.current_menu})
            self.current_menu = submenu
            self._show_current_menu()
            return

        if typ == "action":
            if action == "set_seconds":
                self._set_seconds(bool(value))
                self._go_to_main()
                return
            if action == "set_date":
                self._set_date(bool(value))
                self._go_to_main()
                return
            if action == "set_font":
                self._set_font(str(value))
                self._go_to_main()
                return

            self.logger.warning("ClockMenu: Unknown action %r", action)
            return

        self.logger.warning("ClockMenu: Unknown item type %r for %r", typ, title)

    def back(self):
        if not self.is_active:
            return
        if self.menu_stack:
            prev = self.menu_stack.pop()
            self.current_menu = prev.get("menu", "main")
            self._show_current_menu()
        else:
            self.stop_mode()
            self.mode_manager.back()

    def _go_to_main(self):
        self.menu_stack.clear()
        self.current_menu = "main"
        self._show_current_menu()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _set_seconds(self, on: bool):
        self.mode_manager.config["show_seconds"] = on
        self.mode_manager.save_preferences()
        self.logger.info("ClockMenu: show_seconds => %s", "On" if on else "Off")

    def _set_date(self, on: bool):
        self.mode_manager.config["show_date"] = on
        self.mode_manager.save_preferences()
        self.logger.info("ClockMenu: show_date => %s", "On" if on else "Off")

    def _set_font(self, key: str):
        if key not in {"clock_sans", "clock_dots", "clock_digital", "clock_bold"}:
            self.logger.warning("ClockMenu: Unknown font key => %s", key)
            return
        self.mode_manager.config["clock_font_key"] = key
        self.mode_manager.save_preferences()
        self.logger.info("ClockMenu: clock_font_key => %s", key)

    # ------------------------------------------------------------------
    # Legacy adapters (MenuManager will normally handle these)
    # ------------------------------------------------------------------

    def scroll_selection(self, direction: int):
        if not self.is_active or not self.menu_controller:
            return
        if hasattr(self.menu_controller, "scroll_list"):
            try:
                self.menu_controller.scroll_list(direction)
                return
            except Exception:
                self.logger.exception("scroll_list failed")
        if hasattr(self.menu_controller, "scroll_selection"):
            try:
                self.menu_controller.scroll_selection(direction)
            except Exception:
                self.logger.exception("scroll_selection failed")

    def select_item(self):
        if not self.is_active or not self.menu_controller:
            return
        if hasattr(self.menu_controller, "select_current_in_list"):
            try:
                self.menu_controller.select_current_in_list()
                return
            except Exception:
                self.logger.exception("select_current_in_list failed")
        if hasattr(self.menu_controller, "select_item"):
            try:
                self.menu_controller.select_item()
            except Exception:
                self.logger.exception("select_item failed")

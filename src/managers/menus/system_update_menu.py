# src/managers/menus/system_update_menu.py

import logging
import time
import subprocess
from typing import List, Dict

from PIL import ImageFont
from managers.menus.base_manager import BaseManager


class SystemUpdateMenu(BaseManager):
    """
    System Update menu rendered via the central MenuManager list view.

    Menus:
      - main
          • Update from GitHub
          • Rollback last update
          • Back
      - confirm   (dynamic Yes/No for update or rollback)
    """

    UPDATE_SERVICE = ["sudo", "systemctl", "start", "quadify-update.service"]
    ROLLBACK_SCRIPT = "/home/volumio/Quadify/scripts/quadify_rollback.sh"

    def __init__(self, display_manager, mode_manager, menu_controller=None):
        super().__init__(display_manager, None, mode_manager)
        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.setLevel(logging.INFO)

        # Central Menu controller
        self.menu_controller = (
            menu_controller
            or getattr(self.mode_manager, "menu_manager", None)
            or getattr(self.mode_manager, "menu_controller", None)
        )

        # Runtime
        self.is_active = False
        self.current_menu = "main"
        self.menu_stack: List[Dict] = []

        # Debounce
        self._last_action = 0.0
        self._debounce = 0.25

        # UI (for splash screens)
        self.font_key = "menu_font"
        self.font: ImageFont.FreeTypeFont = display_manager.fonts.get(
            self.font_key, ImageFont.load_default()
        )

        # Which action are we confirming? ("update" | "rollback" | None)
        self._pending_action: str | None = None
        self._confirm_message: str = "Are you sure?"

        self.logger.info(
            "SystemUpdateMenu initialised. menu_controller=%s",
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
        self.logger.info("SystemUpdateMenu: Stopped and cleared display.")

    # ------------------------------------------------------------------
    # Menu building & rendering
    # ------------------------------------------------------------------

    def _title_for_menu(self, menu_name: str) -> str:
        return {
            "main": "System Update",
            "confirm": "Confirm",
        }.get(menu_name, "System Update")

    def _items_for_menu(self, menu_name: str) -> List[Dict]:
        if menu_name == "main":
            return [
                {"title": "Update from GitHub",   "type": "action",  "action": "confirm_update"},
                {"title": "Rollback last update", "type": "action",  "action": "confirm_rollback"},
                {"title": "Back",                 "type": "back"},
            ]

        if menu_name == "confirm":
            # Show the confirmation message as an info row, then Yes/No
            return [
                {"title": self._confirm_message, "type": "info"},
                {"title": "Yes", "type": "action", "action": "do_confirm_yes"},
                {"title": "No",  "type": "action", "action": "do_confirm_no"},
                {"title": "Back","type": "back"},
            ]

        return [{"title": "Back", "type": "back"}]

    def _show_current_menu(self):
        if not self.menu_controller:
            self.logger.warning("SystemUpdateMenu: Menu controller not available; cannot render list.")
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
    # Selection handling
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

        if typ in ("info", "message"):
            return

        if typ == "back" or title.strip().lower() == "back":
            self.back()
            return

        if typ == "action":
            if action == "confirm_update":
                self._pending_action = "update"
                self._confirm_message = "Update Quadify from GitHub?\nThis will restart the app."
                self.menu_stack.append({"menu": self.current_menu})
                self.current_menu = "confirm"
                self._show_current_menu()
                return

            if action == "confirm_rollback":
                self._pending_action = "rollback"
                self._confirm_message = "Rollback to the previous version?\nThis will restart the app."
                self.menu_stack.append({"menu": self.current_menu})
                self.current_menu = "confirm"
                self._show_current_menu()
                return

            if action == "do_confirm_yes":
                act = self._pending_action
                self._pending_action = None
                if act == "update":
                    self._perform_update()
                elif act == "rollback":
                    self._perform_rollback()
                else:
                    self._notice("Nothing to do.")
                return

            if action == "do_confirm_no":
                self._pending_action = None
                self._go_to_main()
                return

            self.logger.warning("SystemUpdateMenu: Unknown action %r", action)
            return

        self.logger.warning("SystemUpdateMenu: Unknown item type %r for %r", typ, title)

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

    def _perform_update(self):
        self._progress_splash("Updating from GitHub\nPlease wait")
        try:
            subprocess.run(self.UPDATE_SERVICE, check=True)
            self.logger.info("SystemUpdateMenu: quadify-update.service started.")
        except subprocess.CalledProcessError:
            self.logger.exception("Failed to start update service")
            self._error("Update failed to start.")
            return

        self._animate_for_seconds(3.0, base_text="Applying update")
        self._notice("Update triggered.\nQuadify will restart shortly.")
        time.sleep(2)
        self.display_manager.show_logo()
        self.stop_mode()

    def _perform_rollback(self):
        self._progress_splash("Rolling back to previous version\nPlease wait")
        try:
            subprocess.run(["bash", self.ROLLBACK_SCRIPT], check=True)
        except subprocess.CalledProcessError:
            self.logger.exception("Rollback script failed")
            self._error("Rollback failed.")
            return

        self._animate_for_seconds(2.0, base_text="Restoring backup")
        self._notice("Rollback complete.\nRestarting Quadify…")
        time.sleep(2)
        self.display_manager.show_logo()
        self.stop_mode()

    # ------------------------------------------------------------------
    # Splash / notices
    # ------------------------------------------------------------------

    def _message_screen(self, text, fill="white"):
        self.display_manager.clear_screen()
        def draw(draw_obj):
            y = 16
            for ln in str(text).split("\n"):
                draw_obj.text((8, y), ln[:22], font=self.font, fill=fill)
                y += 16
        self.display_manager.draw_custom(draw)

    def _progress_splash(self, text):
        self._message_screen(text, fill="white")

    def _animate_for_seconds(self, seconds: float, base_text="Working"):
        start = time.time()
        dots = ["", ".", "..", "..."]
        idx = 0
        while time.time() - start < seconds:
            self._message_screen(f"{base_text}{dots[idx % len(dots)]}")
            idx += 1
            time.sleep(0.35)

    def _notice(self, text):
        self._message_screen(text, fill="white")

    def _error(self, text):
        self._message_screen(text, fill="white")
        time.sleep(2)

    # ------------------------------------------------------------------
    # Legacy adapters (keep for hardware inputs)
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

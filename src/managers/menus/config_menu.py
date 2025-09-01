# src/managers/menus/config_menu.py

import logging
import time
from typing import List, Dict, Optional

from PIL import ImageFont
from managers.menus.base_manager import BaseManager


class ConfigMenu(BaseManager):
    """
    Configuration menu rendered via the central MenuManager list view.

    Sections:
      - Display Modes -> (VU-Meters -> Modern-VU Styles) | Simple
      - Brightness
      - Screensaver -> (Timer)
      - Update (hands off to SystemUpdateMenu)
    """

    def __init__(
        self,
        display_manager,
        mode_manager,
        window_size: int = 4,
        y_offset: int = 2,
        line_spacing: int = 15,
        menu_controller=None,
    ):
        super().__init__(display_manager, None, mode_manager)
        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.setLevel(logging.INFO)

        # Debounce
        self._last_action = 0.0
        self._debounce = 0.25

        # Central Menu controller (MenuManager)
        self.menu_controller = (
            menu_controller
            or getattr(self.mode_manager, "menu_manager", None)
            or getattr(self.mode_manager, "menu_controller", None)
        )

        # Runtime
        self.is_active = False
        self.current_menu = "main"
        self.menu_stack: List[Dict] = []   # push {"menu": name} for back nav

        # Display prefs / assets
        self.window_size = window_size
        self.y_offset = y_offset
        self.line_spacing = line_spacing
        self.font_key = "menu_font"
        self.font: ImageFont.FreeTypeFont = display_manager.fonts.get(self.font_key, ImageFont.load_default())
        self.spectrum_enabled = bool(mode_manager.config.get("cava_enabled", True))

        # Static options (labels only; converted to items when showing)
        self.vu_meter_modes = ["Modern-VU", "Classic-VU", "Digi-VU"]
        self.modern_vu_styles = ["Bars", "Dots", "Oscilloscope"]
        self.simple_modes = ["Minimal", "Original"]
        self.brightness_items = ["Low", "Medium", "High"]
        self.screensaver_items = ["None", "Snake", "Geo", "Quadify", "Timer", "Back"]
        self.screensaver_timer_items = [
            ("1 min",   60),
            ("2 min",   120),
            ("5 min",   300),
            ("10 min",  600),
            ("1 hour",  3600),
        ]

        self.logger.info(
            "ConfigMenu initialised. menu_controller=%s",
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
        self.logger.info("ConfigMenu: Stopped and cleared display.")

    # ------------------------------------------------------------------
    # Menu rendering via MenuManager
    # ------------------------------------------------------------------

    def _title_for_menu(self, menu_name: str) -> str:
        return {
            "main": "Configuration",
            "display_modes": "Display Modes",
            "vu_meters": "VU-Meters",
            "modern_vu_styles": "Modern-VU Styles",
            "simple": "Simple Modes",
            "brightness": "Brightness",
            "screensaver": "Screensaver",
            "screensaver_timer": "Screensaver Timer",
        }.get(menu_name, "Configuration")

    def _items_for_menu(self, menu_name: str) -> List[Dict]:
        # Build rows as dicts: {title, type, action? submenu? value?}
        if menu_name == "main":
            return [
                {"title": "Display Modes", "type": "submenu", "submenu": "display_modes"},
                {"title": "Clock",         "type": "action",  "action": "open_clock"},
                {"title": "Brightness",    "type": "submenu", "submenu": "brightness"},
                {"title": "Screensaver",   "type": "submenu", "submenu": "screensaver"},
                # Hand off to SystemUpdateMenu (no local submenu here)
                {"title": "Update",        "type": "action",  "action": "open_update_menu"},
                {"title": "Back",          "type": "back"},
            ]

        if menu_name == "display_modes":
            spectrum_label = f"Spectrum: {'On' if self.spectrum_enabled else 'Off'}"
            return [
                {"title": "VU-Meters",     "type": "submenu", "submenu": "vu_meters"},
                {"title": "Simple",        "type": "submenu", "submenu": "simple"},
                {"title": spectrum_label,  "type": "action",  "action": "toggle_spectrum"},
                {"title": "Back",          "type": "back"},
            ]

        if menu_name == "vu_meters":
            # Modern-VU goes to styles; other modes are direct actions
            rows = [{"title": "Modern-VU", "type": "submenu", "submenu": "modern_vu_styles"}]
            for name in ["Classic-VU", "Digi-VU"]:
                rows.append({"title": name, "type": "action", "action": "set_display_mode", "value": name})
            rows.append({"title": "Back", "type": "back"})
            return rows

        if menu_name == "modern_vu_styles":
            rows = [{"title": s, "type": "action", "action": "set_modern_vu_style", "value": s}
                    for s in self.modern_vu_styles]
            rows.append({"title": "Back", "type": "back"})
            return rows

        if menu_name == "simple":
            rows = [{"title": s, "type": "action", "action": "set_display_mode", "value": s}
                    for s in self.simple_modes]
            rows.append({"title": "Back", "type": "back"})
            return rows

        if menu_name == "brightness":
            rows = [{"title": b, "type": "action", "action": "set_brightness", "value": b}
                    for b in self.brightness_items]
            rows.append({"title": "Back", "type": "back"})
            return rows

        if menu_name == "screensaver":
            rows: List[Dict] = []
            for s in ["None", "Snake", "Geo", "Quadify", "Timer"]:
                if s == "Timer":
                    rows.append({"title": "Timer", "type": "submenu", "submenu": "screensaver_timer"})
                else:
                    rows.append({"title": s, "type": "action", "action": "set_screensaver", "value": s})
            rows.append({"title": "Back", "type": "back"})
            return rows

        if menu_name == "screensaver_timer":
            rows = [{"title": label, "type": "action", "action": "set_screensaver_timer", "value": seconds}
                    for (label, seconds) in self.screensaver_timer_items]
            rows.append({"title": "Back", "type": "back"})
            return rows

        # Fallback
        return [{"title": "Back", "type": "back"}]

    def _show_current_menu(self):
        if not self.menu_controller:
            self.logger.warning("Menu controller not available; cannot render list.")
            return
        self.logger.info("ConfigMenu: show_list -> %s", self._title_for_menu(self.current_menu))

        items = self._items_for_menu(self.current_menu)
        title = self._title_for_menu(self.current_menu)

        # Render
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

        # Back option
        if typ == "back" or title.strip().lower() == "back":
            self.back()
            return

        # Submenu option
        if typ == "submenu" and submenu:
            self.menu_stack.append({"menu": self.current_menu})
            self.current_menu = submenu
            self._show_current_menu()
            return

        # Action handling
        if typ == "action":
            if action == "open_clock":
                self._open_clock_menu()
                return

            elif action == "toggle_spectrum":
                self._toggle_spectrum()
                # stay in place to show updated label
                self._show_current_menu()
                return

            elif action == "set_display_mode":
                self._handle_display_mode(str(value))
                self._go_to_main()
                return

            elif action == "set_modern_vu_style":
                self._handle_modern_vu_style(str(value))
                self._go_to_main()
                return

            elif action == "set_brightness":
                self._handle_brightness(str(value))
                self._go_to_main()
                return

            elif action == "set_screensaver":
                self._handle_screensaver(str(value))
                self._go_to_main()
                return

            elif action == "set_screensaver_timer":
                self._handle_screensaver_timer_value(int(value))
                self._go_to_main()
                return

            elif action == "open_update_menu":
                # Hand off to SystemUpdateMenu
                self._open_update_menu()
                return

            else:
                self.logger.warning("ConfigMenu: Unknown action %r", action)
                return

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
    # Actions (business logic)
    # ------------------------------------------------------------------

    def _open_clock_menu(self):
        """
        Hand off to ClockMenu (clock_menu.py). Prefer ModeManager transition
        if present; otherwise start the clock_menu instance directly.
        """
        self.logger.info("ConfigMenu: Opening Clock menu...")
        # Stop ourselves for a clean handoff
        self.stop_mode()

        # Preferred: FSM transition if your ModeManager defines it
        to_clock = getattr(self.mode_manager, "to_clockmenu", None)
        if callable(to_clock):
            try:
                to_clock()
                return
            except Exception as e:
                self.logger.exception("ConfigMenu: to_clockmenu() failed: %s", e)

        # Fallback: call the instance directly if it exists
        clock_menu = getattr(self.mode_manager, "clock_menu", None)
        if clock_menu and hasattr(clock_menu, "start_mode"):
            try:
                clock_menu.start_mode()
                return
            except Exception as e:
                self.logger.exception("ConfigMenu: clock_menu.start_mode() failed: %s", e)

        self.logger.warning("ConfigMenu: ClockMenu not available on ModeManager.")

    def _open_update_menu(self):
        """
        Hand off to SystemUpdateMenu (system_update_menu.py).
        Prefer FSM transition if available.
        """
        self.logger.info("ConfigMenu: Opening System Update menu...")
        # Stop ourselves for a clean handoff
        self.stop_mode()

        # Preferred: FSM transition
        to_update = getattr(self.mode_manager, "to_systemupdate", None)
        if callable(to_update):
            try:
                to_update()
                return
            except Exception as e:
                self.logger.exception("ConfigMenu: to_systemupdate() failed: %s", e)

        # Fallback: call instance directly
        update_menu = getattr(self.mode_manager, "system_update_menu", None)
        if update_menu and hasattr(update_menu, "start_mode"):
            try:
                update_menu.start_mode()
                return
            except Exception as e:
                self.logger.exception("ConfigMenu: system_update_menu.start_mode() failed: %s", e)

        self.logger.warning("ConfigMenu: SystemUpdateMenu not available on ModeManager.")

    def _toggle_spectrum(self):
        self.spectrum_enabled = not self.spectrum_enabled
        self.mode_manager.config["cava_enabled"] = self.spectrum_enabled
        self.mode_manager.save_preferences()
        self.logger.info("ConfigMenu: Spectrum => %s", "On" if self.spectrum_enabled else "Off")

    def _handle_display_mode(self, mode_name: str):
        mapping = {
            "Modern-VU":   "modern",
            "Classic-VU":  "vuscreen",
            "Digi-VU":     "digitalvuscreen",
            "Minimal":     "minimal",
            "Original":    "original",
        }
        val = mapping.get(mode_name)
        if val:
            self.logger.info("ConfigMenu: Setting display mode => %s", val)
            self.mode_manager.set_display_mode(val)
        else:
            self.logger.warning("ConfigMenu: Unknown display mode => %s", mode_name)

    def _handle_modern_vu_style(self, style_name: str):
        style_map = {"Bars": "bars", "Dots": "dots", "Oscilloscope": "scope"}
        selected = style_map.get(style_name)
        if selected:
            self.logger.info("ConfigMenu: Setting Modern-VU spectrum mode => %s", selected)
            self.mode_manager.config["modern_spectrum_mode"] = selected
            self.mode_manager.set_display_mode("modern")
            self.mode_manager.save_preferences()
        else:
            self.logger.warning("ConfigMenu: Unknown Modern-VU style => %s", style_name)

    def _handle_brightness(self, level: str):
        brightness_map = {"Low": 50, "Medium": 150, "High": 255}
        val = brightness_map.get(level)
        if val is not None and hasattr(self.display_manager.oled, "contrast"):
            try:
                self.display_manager.oled.contrast(val)
                self.logger.info("ConfigMenu: Brightness => %s (%d)", level, val)
            except Exception as e:
                self.logger.error("ConfigMenu: Failed to set brightness => %s", e)
            self.mode_manager.config["oled_brightness"] = val
            self.mode_manager.save_preferences()
        else:
            self.logger.warning("ConfigMenu: Unknown brightness => %s", level)

    def _handle_screensaver(self, sel: str):
        saver_map = {"None": "none", "Snake": "snake", "Geo": "geo", "Quadify": "quadify"}
        chosen = saver_map.get(sel, "none")
        self.mode_manager.config["screensaver_type"] = chosen
        self.logger.info("ConfigMenu: screensaver_type => %s", chosen)
        self.mode_manager.save_preferences()

    def _handle_screensaver_timer_value(self, seconds: int):
        self.mode_manager.config["screensaver_timeout"] = seconds
        self.logger.info("ConfigMenu: screensaver_timeout => %ds", seconds)
        self.mode_manager.save_preferences()

    # ------------------------------------------------------------------
    # Legacy adapters (MenuManager will call these itself normally)
    # ------------------------------------------------------------------

    def scroll_selection(self, direction: int):
        # Central MenuManager handles scrolling; keep for compatibility
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
        # Keep compatibility with main loop mapping
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

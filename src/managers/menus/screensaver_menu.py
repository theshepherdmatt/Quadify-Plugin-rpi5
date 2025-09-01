import logging
import time
from PIL import ImageFont
from managers.menus.base_manager import BaseManager

class ScreensaverMenu(BaseManager):
    """
    A scrollable menu for choosing a screensaver (None, Snake, Geo, Quadify, Timer),
    plus a sub-menu (Timer) to pick your idle timeout. The sub-menu has no 'Back' item;
    picking a time automatically returns to the main menu.

    Main Items (6 total):
      1) None
      2) Snake
      3) Geo
      4) Quadify
      5) Timer
      6) Back  (goes to Config Menu)

    Timer Sub-Menu (no Back):
      [ "1 min", "2 min", "5 min", "10 min", "1 hour" ]
      Selecting one sets `screensaver_timeout` and returns to main.

    The highlight uses an anchored scrolling approach (like RadioManager).
    """

    def __init__(
        self,
        display_manager,
        mode_manager,
        window_size=4,
        y_offset=2,
        line_spacing=15
    ):
        super().__init__(display_manager, None, mode_manager)
        self.logger = logging.getLogger(self.__class__.__name__)
        self.is_active = False

        # Debounce
        self.last_action_time = 0
        self.debounce_interval = 0.3

        # Layout / scrolling
        self.window_size = window_size
        self.window_start_index = 0
        self.y_offset = y_offset
        self.line_spacing = line_spacing

        # Font
        self.font_key = "menu_font"
        self.font = display_manager.fonts.get(self.font_key) or ImageFont.load_default()

        # Main menu items (added "Back" for returning to config)
        self.main_items = [
            "None",
            "Snake",
            "Geo",
            "Quadify",
            "Timer",
            "Back"
        ]

        # Timer sub-menu items (no "Back" line)
        self.timer_items = [
            ("1 min",   60),
            ("2 min",   120),
            ("5 min",   300),
            ("10 min",  600),
            ("1 hour",  3600),
        ]

        # Track which menu is active: "main" or "timer"
        self.current_menu = "main"
        self.current_items = []  # We'll set this in start_mode()

        # Selection index
        self.current_index = 0

    # ----------------------------------------------------------------
    # Start / Stop
    # ----------------------------------------------------------------
    def start_mode(self):
        """Activate this menu and show the main items."""
        if self.is_active:
            self.logger.debug("ScreensaverMenu: Already active.")
            return

        self.is_active = True
        self.logger.info("ScreensaverMenu: Starting screensaver selection menu.")

        self.current_menu = "main"
        self.current_items = self.main_items
        self.current_index = 0
        self.window_start_index = 0
        self._draw_current_menu()

    def stop_mode(self):
        """Deactivate and clear the screen."""
        if self.is_active:
            self.is_active = False
            self.display_manager.clear_screen()
            self.logger.info("ScreensaverMenu: Stopped and cleared display.")

    # ----------------------------------------------------------------
    # Scrolling
    # ----------------------------------------------------------------
    def scroll_selection(self, direction):
        if not self.is_active:
            self.logger.warning("ScreensaverMenu: Scroll attempted while inactive.")
            return

        now = time.time()
        if now - self.last_action_time < self.debounce_interval:
            self.logger.debug("ScreensaverMenu: Scroll debounced.")
            return
        self.last_action_time = now

        old_index = self.current_index
        total = len(self.current_items)

        # Move up or down
        if direction > 0 and self.current_index < total - 1:
            self.current_index += 1
        elif direction < 0 and self.current_index > 0:
            self.current_index -= 1

        if self.current_index != old_index:
            self.logger.debug(
                f"ScreensaverMenu: Scrolled from {old_index} to {self.current_index}"
            )
            self._draw_current_menu()

    # ----------------------------------------------------------------
    # Selection
    # ----------------------------------------------------------------
    def select_item(self):
        if not self.is_active:
            self.logger.warning("ScreensaverMenu: Select attempted while inactive.")
            return

        now = time.time()
        if now - self.last_action_time < self.debounce_interval:
            self.logger.debug("ScreensaverMenu: Select debounced.")
            return
        self.last_action_time = now

        # Main menu or Timer sub-menu?
        if self.current_menu == "main":
            self._handle_main_selection()
        else:
            # Timer sub-menu
            self._handle_timer_selection()

    def _handle_main_selection(self):
        selected_name = self.current_items[self.current_index]
        self.logger.info(f"ScreensaverMenu: Selected => {selected_name} (main menu)")

        if selected_name == "Back":
            # Go back to the previous menu
            self.stop_mode()
            self.mode_manager.back()
            return

        elif selected_name == "Timer":
            # Switch to timer sub-menu
            self.current_menu = "timer"
            self.current_items = [pair[0] for pair in self.timer_items]  # e.g. ["1 min","2 min",...]
            self.current_index = 0
            self.window_start_index = 0
            self._draw_current_menu()
            return

        # Otherwise, it's "None", "Snake", "Stars", or "Quadify"
        # Save in config
        saver_map = {
            "None":    "none",
            "Snake":   "snake",
            "Geo":     "geo",
            "Quadify": "quadify"
        }
        chosen = saver_map.get(selected_name, "none")
        self.mode_manager.config["screensaver_type"] = chosen
        self.logger.info(f"ScreensaverMenu: screensaver_type => {chosen}")
        self.mode_manager.save_preferences()

        # (Optional) Return to the main screensaver menu or exit
        # For consistency with DisplayMenu, we’ll remain in the main menu:
        self._return_to_main()

    def _handle_timer_selection(self):
        """User selected an item from the Timer sub-menu (no 'Back' line)."""
        label, seconds = self.timer_items[self.current_index]
        self.logger.info(f"ScreensaverMenu: Timer => {label} ({seconds} seconds)")

        # Save in config
        self.mode_manager.config["screensaver_timeout"] = seconds
        self.mode_manager.save_preferences()

        # Now go back to the main menu automatically
        self._return_to_main()

    def _return_to_main(self):
        """Return from any sub-menu to the main screensaver list."""
        self.current_menu = "main"
        self.current_items = self.main_items
        self.current_index = 0
        self.window_start_index = 0
        self._draw_current_menu()

    # ----------------------------------------------------------------
    # Drawing with Anchored Scrolling
    # ----------------------------------------------------------------
    def _draw_current_menu(self):
        """Draw whichever list is currently active (main or timer)."""
        if not self.is_active:
            return

        visible = self._get_visible_slice(self.current_items)

        def draw(draw_obj):
            for i, item_name in enumerate(visible):
                actual_index = self.window_start_index + i
                selected = (actual_index == self.current_index)

                # If main menu & item is "Back" and selected => "<- ", else "-> "
                if self.current_menu == "main" and selected and item_name == "Back":
                    arrow = "<- "
                elif selected:
                    arrow = "-> "
                else:
                    arrow = "   "

                fill_color = "white" if selected else "gray"
                y_pos = self.y_offset + i * self.line_spacing
                draw_obj.text((5, y_pos), f"{arrow}{item_name}", font=self.font, fill=fill_color)

        self.display_manager.draw_custom(draw)

        self.logger.debug(
            f"ScreensaverMenu: current_menu={self.current_menu}, "
            f"items={self.current_items}, index={self.current_index}, "
            f"window_start={self.window_start_index}"
        )

    def _get_visible_slice(self, items):
        """
        Keep the selection in the middle if possible
        (anchored scrolling, same as RadioManager).
        """
        total = len(items)
        half_window = self.window_size // 2

        tentative_start = self.current_index - half_window

        if tentative_start < 0:
            self.window_start_index = 0
        elif tentative_start + self.window_size > total:
            self.window_start_index = max(total - self.window_size, 0)
        else:
            self.window_start_index = tentative_start

        end_index = self.window_start_index + self.window_size
        return items[self.window_start_index:end_index]

    def back(self):
        if self.is_active:
            self.stop_mode()
        self.mode_manager.back()

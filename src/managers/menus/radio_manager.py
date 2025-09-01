# src/managers/radio_manager.py

import logging
import threading
import time
from typing import Optional, List, Dict

from managers.base_manager import BaseManager


FRIENDLY_LABELS = {
    "radio": "Web Radio",
    "webradio": "Web Radio",
    "favourites": "Favourites",
    "favorites": "Favourites",
    "my webradios": "My Web Radios",
    "favorite radios": "Favourites",
    "volumio selection": "Volumio Selection",
    "top 500 radios": "Top 500",
    "by genre": "By Genre",
    "local radios": "Local Radios",
    "by country": "By Country",
    "popular radios": "Popular Radios",
    "best radios": "Best Radios",
}

# Preferred display order for categories (lowercased)
CATEGORY_ORDER = [
    "bbc radios",
    "volumio selection",
    "my web radios",
    "favorite radios",
    "favourite radios",  # just in case the API uses British spelling
    "top 500 radios",
    "by genre",
    "local radios",
    "by country",
    "popular radios",
    "best radios",
]


class RadioManager(BaseManager):
    """
    Radio controller refactored to use the centralised MenuManager list renderer.
    - Fetches navigation from Volumio (via VolumioListener)
    - Normalises items
    - Hands lists to MenuManager.show_list() for drawing/scrolling/highlight
    - Handles selection/back actions + playback
    """

    def __init__(self, display_manager, volumio_listener, mode_manager, menu_controller=None, loading_timeout_s: float = 6.0):
        super().__init__(display_manager, volumio_listener, mode_manager)

        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.setLevel(logging.INFO)

        self.volumio_listener = volumio_listener
        self.is_active = False

        # Navigation state
        self.menu_stack: List[Dict] = []        # stack of {"items":[...], "path":"..."} for Back
        self.current_menu_items: List[Dict] = []  # current visible list
        self.current_path: str = "radio"        # root for web radio

        self.loading_timeout_s = loading_timeout_s
        self._timeout_timer: Optional[threading.Timer] = None

        # Use passed-in menu_controller, fallback to ModeManager attributes if None
        self.menu_controller = (
            menu_controller
            or getattr(self.mode_manager, "menu_manager", None)
            or getattr(self.mode_manager, "menu_controller", None)
        )

        # Debounce to avoid double actions
        self._last_action = 0.0
        self._debounce_s = 0.20

        if hasattr(self.mode_manager, "add_on_mode_change_callback"):
            self.mode_manager.add_on_mode_change_callback(self.handle_mode_change)

        self.logger.info(
            "RadioManager initialised. menu_controller=%s",
            type(self.menu_controller).__name__ if self.menu_controller else None
        )

    # ---------------- lifecycle ----------------

    def handle_mode_change(self, current_mode: str):
        # Support both names your ModeManager may use
        if current_mode in ("radio", "webradio"):
            self.start_mode()
        elif self.is_active:
            self.stop_mode()

    def start_mode(self, start_uri: Optional[str] = None):
        if self.is_active:
            return
        self.is_active = True

        # Connect Volumio signals
        try:
            self.volumio_listener.navigation_received.connect(self.handle_navigation)
            self.volumio_listener.toast_message_received.connect(self.handle_toast_message)
        except Exception:
            pass  # already connected is fine

        self.menu_stack.clear()
        self.current_menu_items = []
        self.current_path = start_uri or "radio"

        self._show_loading_list()
        self.fetch_navigation(self.current_path)
        self._arm_timeout()

    def stop_mode(self):
        if not self.is_active:
            return
        self.is_active = False

        # Disconnect signals
        try:
            self.volumio_listener.navigation_received.disconnect(self.handle_navigation)
            self.volumio_listener.toast_message_received.disconnect(self.handle_toast_message)
        except Exception:
            pass

        self._cancel_timeout()
        try:
            self.display_manager.clear_screen()
        except Exception:
            pass

    # ---------------- fetch / navigation ----------------

    def fetch_navigation(self, uri: str):
        """Ask Volumio to browse a URI; results arrive via handle_navigation."""
        if not self.volumio_listener.is_connected():
            self._show_error_list("Connection Error", "Not connected to Volumio.")
            return
        try:
            self.logger.info("RadioManager: Fetching navigation for URI: %s", uri)
            self.volumio_listener.fetch_browse_library(uri)
        except Exception as e:
            self._show_error_list("Navigation Error", str(e))

    def handle_navigation(self, sender, navigation, service=None, uri=None, **kwargs):
        if not self.is_active:
            return

        try:
            lists = (navigation or {}).get("lists") or []
            combined: List[Dict] = []
            for lst in lists:
                combined.extend(lst.get("items") or [])

            if not combined:
                self._show_empty_list("No categories found." if self.current_path == "radio" else "No stations found.")
                return

            # Categories page when at root "radio"; otherwise stations
            if self.current_path == "radio":
                items = self._normalise_categories(combined)
            else:
                items = self._normalise_stations(combined)

            # Add a Back row and render
            items.append({"title": "Back", "type": "back", "uri": None})
            self.current_menu_items = items
            self._show_list(self.current_menu_items, title=self._title_for_current_path())

            self._cancel_timeout()
        except Exception as e:
            self._show_error_list("Parse Error", str(e))

    # ---------------- normalisers ----------------

    def _normalise_categories(self, items: List[Dict]) -> List[Dict]:
        # Remember original order for unknown categories (stable sort)
        original_index = {id(it): i for i, it in enumerate(items)}

        norm: List[Dict] = []
        for it in items:
            title = it.get("title") or it.get("name") or "Untitled"
            uri = it.get("uri") or it.get("link")
            norm.append({
                "title": title,
                "uri": uri,
                "type": "category",
                **it
            })

        order_map = {name: i for i, name in enumerate(CATEGORY_ORDER)}

        def sort_key(x: Dict):
            t = (x.get("title") or "").strip().lower()
            # Force any "bbc" title to the very front
            if "bbc" in t:
                return (-1, t, 0)
            # Known categories next, by our fixed order
            if t in order_map:
                return (order_map[t], t, 0)
            # Unknown categories keep their original order after known ones
            return (999, t, original_index.get(id(x), 0))

        norm.sort(key=sort_key)

        try:
            self.logger.info("Radio categories order: %s", " | ".join([n["title"] for n in norm]))
        except Exception:
            pass

        return norm

    def _normalise_stations(self, items: List[Dict]) -> List[Dict]:
        norm: List[Dict] = []
        for it in items:
            title = it.get("title") or it.get("name") or "Untitled"
            uri = it.get("uri") or it.get("link")
            typ = (it.get("type") or "").lower() or "webradio"
            norm.append({
                "title": title,
                "uri": uri,
                "type": typ,
                "albumart": it.get("albumart", "") or it.get("icon", ""),
                **it
            })
        return norm

    # ---------------- menu controller (central list view) ----------------

    def _title_for_current_path(self) -> str:
        key = (self.current_path or "").split("/")[-1].lower() or "webradio"
        return FRIENDLY_LABELS.get(key, "Web Radio")

    def _show_list(self, items: List[Dict], title: Optional[str] = None):
        if not self.menu_controller:
            self.logger.warning("Menu controller not available; cannot render list.")
            return
        normalised = [{"title": it.get("title") or it.get("label") or "Untitled", **it} for it in items]
        self.menu_controller.show_list(
            title=title or self._title_for_current_path(),
            items=normalised,
            on_select=self._on_list_select,
            on_back=self.back
        )

    def _show_loading_list(self):
        self._show_list([{"title": "Loading Web Radio…", "type": "info"}])

    def _show_empty_list(self, message: str = "No items"):
        self._show_list([
            {"title": message, "type": "info"},
            {"title": "Back", "type": "back"}
        ])

    def _show_error_list(self, title: str, message: str):
        self._show_list([
            {"title": f"Error: {title}", "type": "info"},
            {"title": message or "", "type": "info"},
            {"title": "Back", "type": "back"}
        ])

    # ---------------- selection / back ----------------

    def _debounced(self) -> bool:
        now = time.time()
        if (now - self._last_action) < self._debounce_s:
            return True
        self._last_action = now
        return False

    def _on_list_select(self, item: Dict):
        if self._debounced():
            return

        typ = (item.get("type") or "").lower()
        uri = item.get("uri")
        title = item.get("title", "")

        # Ignore info rows
        if typ in ("info", "message"):
            return

        # Back
        if typ == "back" or title.strip().lower() == "back":
            self.back()
            return

        # Station playback
        if typ in ("webradio", "station", "song", "track", "audio") or (uri and uri.endswith("/play")):
            self._play_station_from_item(item)
            return

        # Drill down (category -> stations)
        if uri:
            self.menu_stack.append({"items": self.current_menu_items[:], "path": self.current_path})
            self.current_path = uri
            self._show_loading_list()
            self.fetch_navigation(self.current_path)
            self._arm_timeout()
            return

        # Unknown
        self._show_error_list("Unknown Type", typ or "(none)")

    def back(self):
        if self.menu_stack:
            prev = self.menu_stack.pop()
            self.current_menu_items = prev["items"]
            self.current_path = prev.get("path", "radio")
            self._show_list(self.current_menu_items, title=self._title_for_current_path())
        else:
            self.stop_mode()
            self.mode_manager.back()

    # ---------------- playback ----------------

    def _play_station_from_item(self, item: Dict):
        title = item.get("title") or "Station"
        uri = item.get("uri")
        if not uri:
            self._show_error_list("Playback Error", "No URI")
            return

        if not self.volumio_listener.is_connected():
            self._show_error_list("Connection Error", "Not connected to Volumio.")
            return

        try:
            if hasattr(self.mode_manager, "suppress_state_change"):
                self.mode_manager.suppress_state_change()

            payload = {
                "title": title,
                "service": "webradio",
                "uri": uri,
                "type": "webradio",
                "albumart": item.get("albumart", "") or "",
                "icon": "fa fa-music",
            }
            self.logger.info("RadioManager: replaceAndPlay %s", uri)
            self.volumio_listener.socketIO.emit("replaceAndPlay", payload)

            if hasattr(self.mode_manager, "allow_state_change"):
                threading.Timer(1.0, self.mode_manager.allow_state_change).start()

            self._show_list([
                {"title": f"Playing: {title}", "type": "info"},
                {"title": "Back", "type": "back"}
            ])
        except Exception as e:
            self._show_error_list("Playback Error", str(e))

    # ---------------- toasts / timeout ----------------

    def handle_toast_message(self, sender, message, **kwargs):
        if not self.is_active:
            return
        try:
            mtype = (message or {}).get("type", "").lower()
            title = (message or {}).get("title", "Message")
            body = (message or {}).get("message", "")
            if mtype == "error":
                if (body or "").lower() == "no results":
                    self._show_empty_list("No results.")
                else:
                    self._show_error_list(title, body)
        except Exception as e:
            self.logger.exception("RadioManager: toast handling failed: %s", e)

    def _arm_timeout(self):
        self._cancel_timeout()
        self._timeout_timer = threading.Timer(self.loading_timeout_s, self._on_loading_timeout)
        self._timeout_timer.start()

    def _cancel_timeout(self):
        if self._timeout_timer:
            try:
                self._timeout_timer.cancel()
            except Exception:
                pass
            self._timeout_timer = None

    def _on_loading_timeout(self):
        if not self.current_menu_items:
            self._show_error_list("Timeout", "Radio did not load.")
            try:
                threading.Timer(2.5, self.mode_manager.to_menu).start()
            except Exception:
                pass

    # --- Legacy input adapters (match other managers) ---

    def scroll_selection(self, direction: int):
        if not self.is_active:
            return
        mc = self.menu_controller
        if not mc:
            return
        if hasattr(mc, "scroll_list"):
            try:
                mc.scroll_list(direction)
                return
            except Exception:
                self.logger.exception("scroll_list failed")
        if hasattr(mc, "scroll_selection"):
            try:
                mc.scroll_selection(direction)
            except Exception:
                self.logger.exception("scroll_selection failed")

    def select_item(self):
        if not self.is_active:
            return
        mc = self.menu_controller
        if not mc:
            return
        if hasattr(mc, "select_current_in_list"):
            try:
                mc.select_current_in_list()
                return
            except Exception:
                self.logger.exception("select_current_in_list failed")
        if hasattr(mc, "select_item"):
            try:
                mc.select_item()
            except Exception:
                self.logger.exception("select_item failed")

    def display_menu(self):
        """Legacy no-op: all drawing is centralised in MenuManager now."""
        pass

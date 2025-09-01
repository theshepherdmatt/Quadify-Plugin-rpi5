# src/managers/streaming_manager.py

import logging
import threading
from typing import Optional, List, Dict, Any

from managers.base_manager import BaseManager

class StreamingManager(BaseManager):
    """
    Thin controller for a streaming service (tidal/qobuz/spotify/etc).
    - Fetches navigation from Volumio
    - Normalises items
    - Hands lists to MenuManager for rendering/scrolling/highlight
    - Handles selection/back actions

    NOTE: No menu drawing here. All UI lists go via MenuManager.show_list().
    """

    def __init__(self, display_manager, volumio_listener, mode_manager,
                 service_name: str, root_uri: str,
                 loading_timeout_s: float = 6.0):
        super().__init__(display_manager, volumio_listener, mode_manager)
        self.service_name = str(service_name or "stream").lower()
        self.root_uri = root_uri

        self.accept_modes = {self.service_name, "streaming"}

        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.setLevel(logging.INFO)
        self.logger.info(f"{self.service_name.title()} StreamingManager initialised.")

        self.is_active = False
        self.menu_stack = []                 # stack of {"items": [...]} for back nav
        self.current_menu_items = []         # current list of dict rows

        self.loading_timeout_s = loading_timeout_s
        self._timeout_timer: Optional[threading.Timer] = None

        # Quick handle to the centralised menu controller
        self.menu_controller = getattr(self.mode_manager, "menu_manager", None) or \
                               getattr(self.mode_manager, "menu_controller", None)

        if hasattr(self.mode_manager, "add_on_mode_change_callback"):
            self.mode_manager.add_on_mode_change_callback(self.handle_mode_change)

    # ---------------- lifecycle ----------------

    def start_mode(self):
        if self.is_active:
            return
        self.is_active = True

        # Connect Volumio signals
        self.volumio_listener.navigation_received.connect(self.handle_navigation)
        self.volumio_listener.toast_message_received.connect(self.handle_toast_message)
        self.volumio_listener.state_changed.connect(self.handle_state_change)
        self.volumio_listener.track_changed.connect(self.handle_track_change)

        # Show loading list via MenuManager
        self._show_loading_list()
        self.fetch_navigation(self.root_uri)
        self._arm_timeout()

    def stop_mode(self):
        if not self.is_active:
            return
        self.is_active = False

        # Disconnect signals
        try:
            self.volumio_listener.navigation_received.disconnect(self.handle_navigation)
            self.volumio_listener.toast_message_received.disconnect(self.handle_toast_message)
            self.volumio_listener.state_changed.disconnect(self.handle_state_change)
            self.volumio_listener.track_changed.disconnect(self.handle_track_change)
        except Exception:
            pass

        self._cancel_timeout()

    def handle_mode_change(self, current_mode: str):
        if current_mode in self.accept_modes:
            self.start_mode()
        elif self.is_active:
            self.stop_mode()

    # ---------------- data fetch ----------------

    def fetch_navigation(self, uri: Optional[str]):
        if not uri:
            self._show_error_list("Navigation Error", "Missing URI")
            return
        if self.volumio_listener.is_connected():
            try:
                self.logger.info(f"{self.service_name.title()}Manager: Fetching navigation data for URI: {uri}")
                self.volumio_listener.fetch_browse_library(uri)
            except Exception as e:
                self._show_error_list("Navigation Error", str(e))
        else:
            self._show_error_list("Connection Error", "Not connected to Volumio.")

    def _service_aliases(self):
        s = self.service_name
        return {s, s.replace("_", ""), s.replace("_", "-")}

    def _uri_looks_like_ours(self, uri: str) -> bool:
        if not uri:
            return False
        root = self.root_uri.split("://", 1)[0] if "://" in self.root_uri else self.root_uri
        root = (root or "").lower()
        u = uri.lower()
        return root and (u.startswith(root) or root in u)

    def handle_navigation(self, sender, navigation, service, uri, **kwargs):
        if not self.is_active:
            return

        svc = (service or "").lower()
        u = (uri or "")

        is_ours = False
        if svc:
            is_ours = (svc in self._service_aliases())
        else:
            # Fallback when VolumioListener couldn’t identify service (e.g. radio_paradise)
            is_ours = self._uri_looks_like_ours(u)

        if not is_ours:
            self.logger.debug(
                f"Ignoring navigation for service='{service}' uri='{uri}' "
                f"(expecting {self.service_name} / root '{self.root_uri}')"
            )
            return

        self._cancel_timeout()
        self._update_menu_from_navigation(navigation)



    def _update_menu_from_navigation(self, navigation):
        lists = (navigation or {}).get("lists") or []
        combined = []
        for lst in lists:
            combined.extend(lst.get("items") or [])

        if not combined:
            self._show_empty_list()
            return

        # Normalise rows and add a Back row
        self.current_menu_items = self._normalise_items(combined)
        self.current_menu_items.append({"title": "Back", "uri": None, "type": "back"})

        self._show_list(self.current_menu_items)

    def _normalise_items(self, items):
        norm = []
        for it in items:
            title = it.get("title") or it.get("album") or it.get("name") or "Untitled"
            uri = it.get("uri")
            typ = (it.get("type") or "").lower()

            # Heuristic: Volumio sometimes uses /play or track URIs without type
            if not typ and uri:
                if uri.endswith("/play") or "/song/" in uri:
                    typ = "song"

            norm.append({
                "title": title,
                "uri": uri,
                "type": typ,
                **it,
            })
        return norm

    # ---------------- selection/back from MenuManager ----------------

    def _on_list_select(self, item: dict):
        typ = (item.get("type") or "").lower()
        uri = item.get("uri")
        svc = (item.get("service") or self.service_name or "").lower()

        # Ignore purely informational rows
        if typ in ("info", "message"):
            return

        # Back row
        if typ == "back" or (str(item.get("title", "")).strip().lower() == "back"):
            self.back()
            return

        # --- PLAYABLE CASES ---
        # Normal tracks
        if typ in ("song", "track") or (uri and uri.endswith("/play")):
            self.play_song(uri, service_override=svc)
            return

        # Radio Paradise (mywebradio) & generic webradio
        if typ in ("mywebradio", "webradio", "radio") or (uri and uri.startswith("webrp/")):
            # RP plugin expects 'radio_paradise' as service key
            if "radio_paradise" in svc or "radioparadise" in svc or (uri and uri.startswith("webrp/")):
                self.play_song(uri, service_override="radio_paradise")
            else:
                self.play_song(uri, service_override=svc or self.service_name)
            return

        # --- NAVIGATION (folders/playlists/albums) ---
        if uri:
            self.menu_stack.append({"items": self.current_menu_items.copy()})
            self._show_loading_list()
            self.fetch_navigation(uri)
            self._arm_timeout()


    def back(self):
        if self.menu_stack:
            previous = self.menu_stack.pop()
            self.current_menu_items = previous["items"]
            self._show_list(self.current_menu_items)
        else:
            # Exit streaming and return to previous mode
            self.stop_mode()
            self.mode_manager.back()

    # ---------------- playback/state ----------------

    def play_song(self, uri: Optional[str], service_override: Optional[str] = None):
        if not uri:
            return
        if self.volumio_listener.is_connected():
            try:
                service = (service_override or self.service_name or "").lower()
                self.logger.info(f"{(service or self.service_name).title()}Manager: replaceAndPlay {uri}")
                self.volumio_listener.socketIO.emit("replaceAndPlay", {
                    "item": {"service": service, "uri": uri}
                })
            except Exception as e:
                self._show_error_list("Playback Error", str(e))
        else:
            self._show_error_list("Connection Error", "Not connected to Volumio.")

    def handle_toast_message(self, sender, message, **kwargs):
        if not self.is_active:
            return
        if (message or {}).get("type") == "error":
            self._show_error_list(message.get("title", "Error"), message.get("message", ""))

    def handle_state_change(self, sender, state, **kwargs):
        # Keep for future enhancements (e.g. show playing indicator)
        pass

    def handle_track_change(self, sender, track, **kwargs):
        # Keep for future enhancements (e.g. auto-jump to Now Playing)
        pass

    # ---------------- timeouts ----------------

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
            self._show_error_list("Timeout", f"{self.service_name.title()} did not load.")
            # Soft return to main menu after a beat
            threading.Timer(2.5, self.mode_manager.to_menu).start()

    # ---------------- MenuManager integration (all UI goes here) ----------------

    def _show_list(self, items: List[Dict[str, Any]]):
        """Render a list via the central MenuManager."""
        if not self.menu_controller:
            self.logger.warning("Menu controller not available; cannot render list.")
            return
        # Ensure each item has a display label
        normalised = [{"title": it.get("title") or it.get("label") or "Untitled", **it} for it in items]
        self.menu_controller.show_list(
            title=self.service_name.title(),
            items=normalised,
            on_select=self._on_list_select,
            on_back=self.back
        )

    def _show_loading_list(self):
        self._show_list([{"title": f"Loading {self.service_name.title()}…", "type": "info"}])

    def _show_empty_list(self):
        self._show_list([{"title": f"No {self.service_name.title()} items", "type": "info"}, {"title": "Back", "type": "back"}])

    def _show_error_list(self, title: str, message: str):
        self._show_list([
            {"title": f"Error: {title}", "type": "info"},
            {"title": message or "", "type": "info"},
            {"title": "Back", "type": "back"}
        ])

    # --- Legacy input adapters (keep main.py happy) ---

    def _ensure_active(self):
        if not self.is_active and getattr(self.mode_manager, "get_mode", None):
            if self.mode_manager.get_mode() in self.accept_modes:
                self.start_mode()

    def scroll_selection(self, direction: int):
        self._ensure_active()
        if not self.is_active or not self.menu_controller:
            return
        if hasattr(self.menu_controller, "scroll_list"):
            try:
                self.menu_controller.scroll_list(direction); return
            except Exception:
                self.logger.exception("scroll_list failed")
        if hasattr(self.menu_controller, "scroll_selection"):
            try:
                self.menu_controller.scroll_selection(direction)
            except Exception:
                self.logger.exception("scroll_selection failed")

    def select_item(self):
        self._ensure_active()
        if not self.is_active or not self.menu_controller:
            return
        if hasattr(self.menu_controller, "select_current_in_list"):
            try:
                self.menu_controller.select_current_in_list(); return
            except Exception:
                self.logger.exception("select_current_in_list failed")
        if hasattr(self.menu_controller, "select_item"):
            try:
                self.menu_controller.select_item()
            except Exception:
                self.logger.exception("select_item failed")



    def display_menu(self):
        """
        Legacy no-op: drawing is centralised in MenuManager now.
        Present for compatibility if anything calls it.
        """
        pass


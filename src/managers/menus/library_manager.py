import logging
import threading
from typing import Optional, List, Dict
from threading import Thread
from requests.adapters import HTTPAdapter
from urllib.parse import quote, unquote
from urllib3.util.retry import Retry
import requests

from managers.base_manager import BaseManager

FRIENDLY_LABELS = {
    "music-library": "Music Library",
    "library": "Music Library",
    "artists": "Artists",
    "albums": "Albums",
    "genres": "Genres",
    "webradio": "Web Radio",
    "radio": "Web Radio",
    "radio_paradise": "Radio Paradise",
    "internal": "Internal",
    "config": "Config",
    "nas": "NAS",
    "usb": "USB",
    "playlists": "Playlists",
    "motherearth": "Mother Earth",
    "favourites": "Favourites",
    "favorites": "Favourites",
    "last_100": "Last 100",
    "mediaservers": "Media Servers",
    "upnp": "UPnP",
}


class LibraryManager(BaseManager):
    """
    Library controller refactored to use the central MenuManager list renderer
    (same approach as StreamingManager). All drawing goes through MenuManager.show_list().
    """

    def __init__(
        self,
        display_manager,
        volumio_config,
        mode_manager,
        volumio_listener,
        menu_controller=None,
        service_type: str = "library",
        root_uri: str = "music-library",
        loading_timeout_s: float = 6.0,
    ):
        super().__init__(display_manager, volumio_config, mode_manager)

        self.volumio_host = volumio_config.get("host", "localhost")
        self.volumio_port = volumio_config.get("port", 3000)
        self.base_url = f"http://{self.volumio_host}:{self.volumio_port}"
        self.volumio_listener = volumio_listener

        self.session = requests.Session()
        retries = Retry(total=3, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504])
        self.session.mount("http://", HTTPAdapter(max_retries=retries))

        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.setLevel(logging.INFO)

        self.is_active = False
        self.menu_stack: List[Dict] = []          # stack of {"items":[...], "path":"..."} for Back
        self.current_menu_items: List[Dict] = []  # current visible list

        self.service_type = str(service_type or "library").lower()
        self.root_uri = root_uri
        self.current_path = self.root_uri

        self.loading_timeout_s = loading_timeout_s
        self._timeout_timer: Optional[threading.Timer] = None

        # Prefer explicit menu_controller, else try to pick it from mode_manager
        self.menu_controller = menu_controller or \
                               getattr(self.mode_manager, "menu_manager", None) or \
                               getattr(self.mode_manager, "menu_controller", None)

        if hasattr(self.mode_manager, "add_on_mode_change_callback"):
            self.mode_manager.add_on_mode_change_callback(self.handle_mode_change)

    # ---------------- lifecycle ----------------

    def handle_mode_change(self, current_mode: str):
        if current_mode == self.service_type:
            self.start_mode()
        elif self.is_active:
            self.stop_mode()

    def start_mode(self, start_uri: Optional[str] = None):
        if self.is_active:
            return
        self.is_active = True

        self.menu_stack.clear()
        self.current_menu_items = []
        self.current_path = start_uri or self.root_uri

        self._show_loading_list()
        self.fetch_navigation(self.current_path)
        self._arm_timeout()

    def stop_mode(self):
        if not self.is_active:
            return
        self.is_active = False
        self._cancel_timeout()
        try:
            self.display_manager.clear_screen()
        except Exception:
            pass

    # ---------------- data fetch ----------------

    def fetch_navigation(self, uri: str):
        """Fetch a folder/list from Volumio HTTP API, then show it via MenuManager."""
        self.logger.info(f"[{self.service_type}] Fetching navigation for URI: {uri}")

        def _worker():
            try:
                resp = self.session.get(f"{self.base_url}/api/v1/browse?uri={quote(uri)}", timeout=6)
                if resp.status_code != 200:
                    self._show_error_list("Fetch Error", f"Status {resp.status_code}")
                    return

                nav = resp.json().get("navigation", {})
                lists = nav.get("lists") or []
                items: List[Dict] = []
                for lst in lists:
                    items.extend(lst.get("items") or [])

                if not items:
                    self._show_empty_list()
                    return

                # Normalise rows and add Back
                self.current_menu_items = self._normalise_items(items)
                self.current_menu_items.append({"title": "Back", "type": "back", "uri": None})

                self._show_list(self.current_menu_items)
            except Exception as e:
                self._show_error_list("Fetch Error", str(e))

        threading.Thread(target=_worker, daemon=True).start()

    def _normalise_items(self, items: List[Dict]) -> List[Dict]:
        norm: List[Dict] = []
        for it in items:
            title = it.get("title") or it.get("album") or it.get("name") or "Untitled"
            uri = it.get("uri")
            typ = (it.get("type") or "").lower()
            # Heuristic: sometimes missing types
            if not typ and uri and uri.endswith("/play"):
                typ = "song"
            norm.append({
                "title": title,
                "uri": uri,
                "type": typ,
                "service": (it.get("service") or "").lower(),
                **it,
            })
        return norm

    # ---------------- timeout ----------------

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
            self._show_error_list("Timeout", "Library did not load.")
            threading.Timer(2.5, self.mode_manager.to_menu).start()

    # ---------------- MenuManager integration (all UI goes here) ----------------

    def _title_for_current_path(self) -> str:
        # Try to give a friendly title, else path tail, else service display name
        candidates = [
            self.current_path.split("://")[0] if "://" in self.current_path else None,
            self.current_path.split("/")[-1] if "/" in self.current_path else self.current_path,
            self.service_type,
        ]
        for c in candidates:
            if c and c.lower() in FRIENDLY_LABELS:
                return FRIENDLY_LABELS[c.lower()]
        return (self.current_path or self.service_type or "Library").replace("_", " ").title()

    def _show_list(self, items: List[Dict]):
        if not self.menu_controller:
            self.logger.warning("Menu controller not available; cannot render list.")
            return
        normalised = [{"title": it.get("title") or it.get("label") or "Untitled", **it} for it in items]
        self.menu_controller.show_list(
            title=self._title_for_current_path(),
            items=normalised,
            on_select=self._on_list_select,
            on_back=self.back
        )

    def _show_loading_list(self):
        self._show_list([{"title": "Loading Library…", "type": "info"}])

    def _show_empty_list(self):
        self._show_list([
            {"title": "No items in this folder", "type": "info"},
            {"title": "Back", "type": "back"}
        ])

    def _show_error_list(self, title: str, message: str):
        self._show_list([
            {"title": f"Error: {title}", "type": "info"},
            {"title": message or "", "type": "info"},
            {"title": "Back", "type": "back"}
        ])

    # ---------------- selection/back from MenuManager ----------------

    def _on_list_select(self, item: Dict):
        typ = (item.get("type") or "").lower()
        uri = item.get("uri")

        # Ignore info-only rows
        if typ in ("info", "message"):
            return

        # Back row
        if typ == "back" or (str(item.get("title", "")).strip().lower() == "back"):
            self.back()
            return

        # Playlists need playPlaylist (Volumio quirk)
        if self.current_path.startswith("playlists") and typ == "playlist":
            playlist_name = item.get("title")
            if playlist_name and hasattr(self.volumio_listener, "socketIO"):
                try:
                    self.volumio_listener.socketIO.emit("playPlaylist", {"name": playlist_name})
                    self._show_list([
                        {"title": f"Playing playlist: {playlist_name}", "type": "info"},
                        {"title": "Back", "type": "back"}
                    ])
                except Exception as e:
                    self._show_error_list("Playlist Error", str(e))
            else:
                self._show_error_list("Playlist Error", "SocketIO unavailable or missing name.")
            return

        # Song / track
        if typ in ("song", "track", "audio", "file") or (uri and uri.endswith("/play")):
            self.replace_and_play(item)
            return

        # Folder-ish => drill down
        if typ in ("folder", "album", "internal-folder", "usb-folder", "nas-folder", "album_folder", "remdisk") or uri:
            if self._is_album_folder_fast(uri):
                # Show album actions as a small submenu list
                self._show_list([
                    {"title": "Play Album", "type": "action", "action": "play_album", "data": item},
                    {"title": "Select Songs", "type": "action", "action": "select_songs", "data": item},
                    {"title": "Back", "type": "back"}
                ])
                return
            # Push current list to stack, then fetch next
            self.menu_stack.append({"items": self.current_menu_items.copy(), "path": self.current_path})
            self.current_path = uri or self.current_path
            self._show_loading_list()
            self.fetch_navigation(self.current_path)
            self._arm_timeout()
            return

        # Action rows (from album submenu)
        if typ == "action":
            action = item.get("action")
            data = item.get("data") or {}
            self._perform_action(action, data)
            return

        # Unknown
        self._show_error_list("Unknown Type", typ or "(none)")

    def back(self):
        if self.menu_stack:
            prev = self.menu_stack.pop()
            self.current_menu_items = prev["items"]
            self.current_path = prev.get("path", self.current_path)
            self._show_list(self.current_menu_items)
        else:
            self.stop_mode()
            self.mode_manager.back()

    # ---------------- helpers (album/playback) ----------------

    def _perform_action(self, action: Optional[str], data: Dict):
        if action == "play_album":
            self.play_album_or_folder(data)
        elif action == "select_songs":
            uri = data.get("uri")
            if not uri:
                self._show_error_list("Navigation Error", "No album URI")
                return
            self.menu_stack.append({"items": self.current_menu_items.copy(), "path": self.current_path})
            self.current_path = uri
            self._show_loading_list()
            self.fetch_navigation(self.current_path)
            self._arm_timeout()
        elif action == "back":
            self.back()
        else:
            self._show_error_list("Invalid Action", str(action or ""))

    def _is_album_folder_fast(self, folder_uri: Optional[str]) -> bool:
        if not folder_uri:
            return False
        try:
            resp = self.session.get(f"{self.base_url}/api/v1/browse?uri={quote(folder_uri)}", timeout=6)
            items = resp.json().get("navigation", {}).get("lists", [{}])[0].get("items", [])
            has_songs = any((i.get("type") or "").lower() == "song" for i in items)
            has_folders = any((i.get("type") or "").lower() in ["folder", "album"] for i in items)
            return has_songs and not has_folders
        except Exception:
            return False

    def play_album_or_folder(self, folder_item: Dict):
        folder_uri = folder_item.get("uri")
        if not folder_uri:
            self._show_error_list("Playback Error", "No URI")
            return
        Thread(target=self._play_album_thread, args=(folder_uri, folder_item.get("title", "")), daemon=True).start()

    def _play_album_thread(self, album_uri: str, album_title: str):
        try:
            # Resolve custom albums:// scheme if present
            if album_uri.startswith("albums://"):
                parts = unquote(album_uri[9:]).split("/", 1)
                if len(parts) == 2:
                    artist, album = parts
                    album_uri = f"music-library/INTERNAL/Music/{artist}/{album}"
                else:
                    self._show_error_list("Playback Error", f"Could not resolve album index URI: {album_uri}")
                    return

            resp = self.session.get(f"{self.base_url}/api/v1/browse?uri={quote(album_uri)}", timeout=8)
            if resp.status_code != 200:
                self._show_error_list("Playback Error", f"Fetch album failed: {resp.status_code}")
                return

            items = resp.json().get("navigation", {}).get("lists", [{}])[0].get("items", [])
            playable = [it for it in items if (it.get("type") in ("song", "track", "audio", "file")) and it.get("uri")]
            if not playable:
                self._show_error_list("Playback Error", f"No tracks in: {album_title}")
                return

            first = playable[0]
            self.session.post(f"{self.base_url}/api/v1/replaceAndPlay",
                              json={"name": album_title, "service": "mpd", "uri": first.get("uri")})
            for it in playable[1:]:
                self.session.post(f"{self.base_url}/api/v1/addToQueue",
                                  json={"name": album_title, "service": "mpd", "uri": it.get("uri")})
            self.session.post(f"{self.base_url}/api/v1/commands", json={"cmd": "play"})

            self._show_list([
                {"title": f"Playing: {album_title}", "type": "info"},
                {"title": "Back", "type": "back"}
            ])
        except Exception as e:
            self._show_error_list("Playback Error", str(e))

    def replace_and_play(self, item: Dict):
        uri = item.get("uri")
        if not uri:
            self._show_error_list("Playback Error", "No URI")
            return
        try:
            data = {"name": item.get("title", ""), "service": item.get("service", self.service_type), "uri": uri}
            resp = self.session.post(f"{self.base_url}/api/v1/replaceAndPlay", json=data, timeout=8)
            if resp.status_code == 200:
                self._show_list([
                    {"title": f"Playing: {item.get('title','')}", "type": "info"},
                    {"title": "Back", "type": "back"}
                ])
            else:
                self._show_error_list("Playback Error", f"Failed: {resp.status_code}")
        except Exception as e:
            self._show_error_list("Playback Error", str(e))

    # --- Legacy input adapters (match StreamingManager) ---

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
        """
        Legacy no-op: all drawing is centralised in MenuManager now.
        Present for compatibility if anything calls it.
        """
        pass

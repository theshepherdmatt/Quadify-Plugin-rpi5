# src/managers/menu_manager.py

import logging
import threading
import time
import os
import glob
from PIL import Image, ImageDraw, ImageFont
from network.service_listener import get_available_services

class MenuManager:
    def __init__(self, display_manager, volumio_listener, mode_manager, window_size=5, menu_type="icon_row"):
        self.display_manager = display_manager
        self.volumio_listener = volumio_listener
        self.mode_manager = mode_manager
        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.setLevel(logging.INFO)

        # --- icon-row (home) state ---
        self.menu_stack = []
        self.current_menu_items = []          # list[str] of labels
        self.current_selection_index = 0
        self.window_start_index = 0
        self.is_active = False
        self.window_size = window_size
        self.menu_type = menu_type
        self.font_key = 'menu_font'
        self.bold_font_key = 'menu_font_bold'
        self.lock = threading.Lock()
        self.config = getattr(self.display_manager, "config", {}) or {}
        self.icon = lambda k: (self.config.get("icon_urls", {}) or {}).get(k)

        # NEW: discovered services metadata
        # label -> { name, plugin, uri, albumart, icon_url, label }
        self.services_by_label = {}
        self.discovered_order = []            # preserve order from discovery

        # --- centralised list view (for managers) ---
        self.active_view = "icon"             # "icon" | "list"
        self.list_title = ""
        self.list_items = []                  # list[dict]: {"label","type","uri", ...}
        self.list_index = 0
        self.list_offset = 0
        self.list_page_size = 3               # tuned for small OLEDs
        self.on_list_select = None
        self.on_list_back = None

        # Display label mapping
        self.label_map = {
            "MUSIC_LIBRARY": "Library",
            "ARTISTS": "Artists",
            "ALBUMS": "Albums",
            "GENRES": "Genres",
            "WEB_RADIO": "Radio",
            "RADIO_PARADISE": "Radio\nParadise",
            "TIDAL": "Tidal",
            "INTERNAL": "Internal",
            "CONFIG": "Config",
            "NAS": "NAS",
            "USB": "USB",
            "PLAYLISTS": "Playlists",
            "SPOTIFY": "Spotify",
            "QOBUZ": "Qobuz",
            "MOTHER_EARTH_RADIO": "Mother\nEarth",
            "FAVOURITES": "Favourites",
            "LAST_100": "Last 100",
            "MEDIA_SERVERS": "Media\nServers",
            "SOUNDCLOUD": "Sound\nCloud",
            "UPNP": "UPnP",
        }

        # Static icon fallbacks (keys here are labels)
        self.static_icons = {
            "STREAM": self.icon("stream"),
            "LIBRARY": self.icon("library"),
            "RADIO": self.icon("webradio"),
            "PLAYLISTS": self.icon("playlists"),
            "TIDAL": self.icon("tidal"),
            "QOBUZ": self.icon("qobuz"),
            "SPOTIFY": self.icon("spop"),
            "INTERNAL": self.icon("mpd"),
            "NAS": self.icon("nas"),
            "USB": self.icon("usb"),
            "CONFIG": self.icon("config"),
            "ORIGINAL": self.icon("display"),
            "MODERN": self.icon("display"),
            "LAST_100": self.icon("history"),
            "MEDIA_SERVERS": self.icon("mediaservers"),
        }

        # Load cached PNG icons (produced by your icon fetcher)
        self.icon_cache = {}
        self.local_icon_dir = '/data/plugins/system_hardware/quadify/quadifyapp/src/assets/pngs'
        for icon_path in glob.glob(os.path.join(self.local_icon_dir, '*.png')):
            try:
                key = os.path.splitext(os.path.basename(icon_path))[0].upper()
                img = Image.open(icon_path).convert("RGBA")
                self.icon_cache[key] = img
                self.logger.info(f"Loaded local icon: {key} from {icon_path}")
            except Exception as e:
                self.logger.warning(f"Failed to load local icon {icon_path}: {e}")

        # Remap fallbacks to friendly keys
        if "LIBRARY" not in self.icon_cache and "MUSIC_LIBRARY" in self.icon_cache:
            self.icon_cache["LIBRARY"] = self.icon_cache["MUSIC_LIBRARY"]
        if "RADIO" not in self.icon_cache and "WEB_RADIO" in self.icon_cache:
            self.icon_cache["RADIO"] = self.icon_cache["WEB_RADIO"]
        if "RADIO-P" not in self.icon_cache and "RADIO_PARADISE" in self.icon_cache:
            self.icon_cache["RADIO-P"] = self.icon_cache["RADIO_PARADISE"]

        if hasattr(self.mode_manager, "add_on_mode_change_callback"):
            self.mode_manager.add_on_mode_change_callback(self.handle_mode_change)

    # ---------------- mode lifecycle ----------------

    def handle_mode_change(self, current_mode):
        self.logger.info(f"MenuManager handling mode change to: {current_mode}")
        if current_mode == "menu":
            self.start_mode()
        elif self.is_active:
            self.stop_mode()

    def start_mode(self, skip_initial_draw=False):
        self.is_active = True
        self.active_view = "icon"
        self.refresh_main_menu()
        if not skip_initial_draw:
            # draw once in a thread to avoid blocking signal thread
            threading.Thread(target=self.display_menu, daemon=True).start()

    def stop_mode(self):
        if not self.is_active:
            return
        self.is_active = False
        with self.lock:
            self.display_manager.clear_screen()
        self.logger.info("MenuManager: Stopped menu mode and cleared display.")

    # ---------------- discovery -> home menu ----------------

    def refresh_main_menu(self):
        """
        Rebuilds the home (icon row) from discovered services.
        Fills self.services_by_label for dynamic routing on select.
        """
        services_labels = []
        self.services_by_label = {}
        self.discovered_order = []

        try:
            raw_services = get_available_services()  # [{name, plugin, uri, icon_url, label, ...}]
            for svc in raw_services:
                label = self._map_plugin_to_label(svc['name'], svc['plugin'])
                # Keep a canonical label for lookup/drawing
                can_label = label.strip().upper()
                # Skip pure library subentries on the home row
                if can_label in ("INTERNAL", "NAS", "LIBRARY"):
                    continue
                if can_label not in self.services_by_label:
                    self.services_by_label[can_label] = svc
                    self.discovered_order.append(can_label)
                    services_labels.append(can_label)
        except Exception as e:
            self.logger.error(f"Error getting available services: {e}")
            services_labels = ["WEB_RADIO", "PLAYLISTS"]

        # Ensure special groups appear if present
        if any(lbl in ("MEDIA_SERVERS", "UPNP") for lbl in services_labels):
            if "MEDIA_SERVERS" not in services_labels:
                services_labels.append("MEDIA_SERVERS")

        # Always include config
        if "CONFIG" not in services_labels:
            services_labels.append("CONFIG")

        self.current_menu_items = services_labels
        self.current_selection_index = 0
        self.window_start_index = 0

    def _map_plugin_to_label(self, name, plugin):
        """
        Convert Volumio {name, plugin} to a stable label used for icons & routing.
        """
        plugin = (plugin or "").lower()
        normalised_name = (name or "").lower().replace(' ', '-')

        if plugin == "mpd":
            mapping = {
                "music-library": "MUSIC_LIBRARY",
                "artists": "ARTISTS",
                "albums": "ALBUMS",
                "internal": "INTERNAL",
                "nas": "NAS",
                "usb": "USB",
            }
            return mapping.get(normalised_name, (name or "UNKNOWN").upper().replace(' ', '_'))

        mapping = {
            "tidal": "TIDAL",
            "qobuz": "QOBUZ",
            "spop": "SPOTIFY",
            "webradio": "WEB_RADIO",
            "radio_paradise": "RADIO_PARADISE",
            "motherearthradio": "MOTHER_EARTH_RADIO",
            "playlists": "PLAYLISTS",
            "favourites": "FAVOURITES",
            "favorites": "FAVOURITES",
            "last_100": "LAST_100",
            "mediaservers": "MEDIA_SERVERS",
            "upnp": "UPNP",
        }
        # Default: use the name as a label (e.g., MIXCLOUD -> MIXCLOUD)
        return mapping.get(plugin, (name or "UNKNOWN").upper().replace(' ', '_'))

    # ---------------- drawing: home (icon row) ----------------

    def display_menu(self):
        self.draw_menu(offset_x=0)

    def draw_menu(self, offset_x=0):
        with self.lock:
            visible_items = self.get_visible_window(self.current_menu_items, self.window_size)
            icon_size = 45
            spacing = -2
            total_width = self.display_manager.oled.width
            total_height = self.display_manager.oled.height
            total_icons_width = len(visible_items) * icon_size + (len(visible_items) - 1) * spacing
            x_offset = (total_width - total_icons_width) // 2 + offset_x
            y_position = (total_height - icon_size) // 2 - 10

            base_image = Image.new("RGB", self.display_manager.oled.size, "black")
            draw_obj = ImageDraw.Draw(base_image)

            for i, item in enumerate(visible_items):
                actual_index = self.window_start_index + i
                icon = self.icon_cache.get(item) or self.static_icons.get(item)
                if not icon:
                    self.logger.warning(f"No icon cached for {item}, skipping.")
                    continue
                if icon.mode == "RGBA":
                    background = Image.new("RGB", icon.size, (0, 0, 0))
                    background.paste(icon, mask=icon.split()[3])
                    icon = background
                icon = icon.resize((icon_size, icon_size), Image.LANCZOS)
                x = x_offset + i * (icon_size + spacing)
                y_adjustment = -5 if actual_index == self.current_selection_index else 0
                base_image.paste(icon, (x, y_position + y_adjustment))

                label = self.label_map.get(item, item.title().replace('_', ' '))
                font = self.display_manager.fonts.get(
                    self.bold_font_key if actual_index == self.current_selection_index else self.font_key,
                    ImageFont.load_default(),
                )
                text_color = "white" if actual_index == self.current_selection_index else "black"

                lines = label.split('\n')
                line_height = font.getsize('A')[1]
                total_h = line_height * len(lines)
                text_y = y_position + icon_size + 2 - total_h // 2

                for j, line in enumerate(lines):
                    tw, th = draw_obj.textsize(line, font=font)
                    text_x = x + (icon_size - tw) // 2
                    draw_obj.text((text_x, text_y + j * line_height), line, font=font, fill=text_color)

            base_image = base_image.convert(self.display_manager.oled.mode)
            self.display_manager.oled.display(base_image)

    def get_visible_window(self, items, window_size):
        half = window_size // 2
        self.window_start_index = self.current_selection_index - half
        if self.window_start_index < 0:
            self.window_start_index = 0
        elif self.window_start_index + window_size > len(items):
            self.window_start_index = max(len(items) - window_size, 0)
        return items[self.window_start_index: self.window_start_index + window_size]

    # ---------------- central list view (used by feature managers) ----------------

    def show_list(self, title, items, on_select=None, on_back=None):
        self.active_view = "list"
        self.list_title = title or ""
        self.list_items = [{
            "label": it.get("title") or it.get("label") or "Untitled",
            "type": (it.get("type") or "").lower(),
            "uri": it.get("uri"),
            **it
        } for it in (items or [])]
        self.list_index = 0
        self.list_offset = 0
        self.on_list_select = on_select
        self.on_list_back = on_back
        self._render_list()

    def _list_set_bounds(self):
        total = len(self.list_items)
        if total <= 0:
            self.list_index = 0
            self.list_offset = 0
            return
        self.list_index = max(0, min(self.list_index, total - 1))
        if total <= self.list_page_size:
            self.list_offset = 0
            return
        if self.list_index <= 1:
            self.list_offset = 0
        elif self.list_index >= total - 2:
            self.list_offset = total - self.list_page_size
        else:
            self.list_offset = self.list_index - 1

    def _render_list(self):
        with self.lock:
            w, h = self.display_manager.oled.size
            img = Image.new("RGB", (w, h), "black")
            draw = ImageDraw.Draw(img)
            font = self.display_manager.fonts.get(self.font_key, ImageFont.load_default())
            font_bold = self.display_manager.fonts.get(self.bold_font_key, font)
            th = self._list_theme()

            margin = th["margin"]
            y_offset = th.get("y_offset", 0)

            # Title
            title_y = max(0, margin + th.get("title_offset", 0) + y_offset)
            title_h = 0
            if self.list_title:
                draw.text((margin, title_y), self.list_title, font=font_bold, fill=th["text"])
                try:
                    ascent, descent = font_bold.getmetrics()
                    title_h = ascent + descent
                except Exception:
                    _, title_h = self._text_wh(draw, self.list_title, font_bold)
                div_y = title_y + title_h
                draw.line((margin, div_y, w - margin, div_y), fill=th["divider_colour"], width=1)
                rows_y = div_y + th.get("header_gap", 0)
            else:
                rows_y = title_y

            # Rows
            total = len(self.list_items)
            self._list_set_bounds()
            start = self.list_offset
            end = min(start + self.list_page_size, total)
            visible = self.list_items[start:end]

            if total <= self.list_page_size:
                focus_row = self.list_index
            elif self.list_offset == 0:
                focus_row = min(self.list_index, 1)
            elif self.list_offset == total - self.list_page_size:
                focus_row = self.list_index - self.list_offset
            else:
                focus_row = 1

            base_line_h = self._text_wh(draw, "A", font)[1]
            line_h = base_line_h + th["row_gap"]
            text_x = margin + 12

            for i, row in enumerate(visible):
                row_y = rows_y + i * line_h
                is_focus = (i == focus_row)
                if is_focus:
                    draw.rectangle((margin - 2, row_y - 1, w - margin + 2, row_y + line_h - 2),
                                   fill=th["focus_bg"])
                chev = th["chevron_focus"] if is_focus else th["chevron_dim"]
                draw.text((margin, row_y), chev,
                          font=font_bold if is_focus else font,
                          fill=th["text"] if is_focus else th["text_dim"])

                label = row.get("label") or row.get("title") or "Untitled"
                max_w = (w - margin) - text_x
                label_draw = self._truncate_to_width(draw, label,
                                                     font_bold if is_focus else font, max_w)
                draw.text((text_x, row_y), label_draw,
                          font=font_bold if is_focus else font,
                          fill=th["text"] if is_focus else th["text_dim"])

                if i < len(visible) - 1:
                    dy = row_y + line_h - 1
                    draw.line((margin, dy, w - margin, dy), fill=th["divider_colour"])

            if self.list_offset > 0:
                draw.text((w - margin - 8, rows_y - 12), "^", font=font, fill=th["text_dim"])
            if self.list_offset + self.list_page_size < total:
                draw.text((w - margin - 8, h - margin - 12), "v", font=font, fill=th["text_dim"])

            self.display_manager.oled.display(img)

    def _scroll_list(self, delta):
        if not self.list_items:
            return
        self.list_index = max(0, min(self.list_index + delta, len(self.list_items) - 1))
        self._render_list()

    def _select_list_item(self):
        if not self.list_items:
            return
        item = self.list_items[self.list_index]

        if (item.get("type") == "back") or (str(item.get("label", "")).strip().lower() == "back"):
            if callable(self.on_list_back):
                try:
                    self.on_list_back()
                except Exception as e:
                    self.logger.exception("on_list_back failed: %s", e)
            return

        if callable(self.on_list_select):
            try:
                self.on_list_select(item)
            except Exception as e:
                self.logger.exception("on_list_select failed: %s", e)

    def scroll_list(self, delta: int):
        self._scroll_list(delta)

    def select_current_in_list(self):
        self._select_list_item()

    # ---------------- input routing (legacy-safe) ----------------

    def scroll_selection(self, direction):
        if not self.is_active:
            return
        if self.active_view == "list":
            self._scroll_list(direction)
            return
        if not self.current_menu_items:
            return
        next_index = self.current_selection_index + direction
        next_index = max(0, min(next_index, len(self.current_menu_items) - 1))
        if self.current_menu_items[next_index] != "":
            self.current_selection_index = next_index
        self.display_menu()

    def select_item(self):
        if not self.is_active:
            return
        if self.active_view == "list":
            self._select_list_item()
            return
        if not self.current_menu_items:
            return
        selected = self.current_menu_items[self.current_selection_index]
        self.logger.info(f"MenuManager: Selected menu item: {selected}")
        threading.Thread(target=self._handle_selection, args=(selected,), daemon=True).start()

    def _handle_selection(self, selected_item):
        time.sleep(0.15)  # tiny debounce / UX polish
        key = str(selected_item).strip().upper()

        # Known routes first (fast paths)
        if key in ["MUSIC_LIBRARY", "LIBRARY"]:
            self.mode_manager.to_library(); return
        if key == "WEB_RADIO":
            self.mode_manager.to_radio(); return
        if key == "PLAYLISTS":
            self.mode_manager.to_playlists(); return
        if key == "CONFIG":
            self.mode_manager.to_configmenu(); return
        if key == "TIDAL":
            self.mode_manager.trigger("to_streaming", service_name="tidal", start_uri="tidal://"); return
        if key == "QOBUZ":
            self.mode_manager.trigger("to_streaming", service_name="qobuz", start_uri="qobuz://"); return
        if key == "SPOTIFY":
            self.mode_manager.trigger("to_streaming", service_name="spotify", start_uri="spotify://"); return
        if key in ["RADIO_PARADISE", "RADIO-P"]:
            self.mode_manager.to_radioparadise(); return
        if key in ["MOTHEREARTH", "MOTHER-E"]:
            self.mode_manager.to_motherearthradio(); return
        if key == "ALBUMS":
            self.mode_manager.to_albums(); return
        if key == "ARTISTS":
            self.mode_manager.to_artists(); return
        if key == "GENRES":
            self.mode_manager.to_genres(); return
        if key in ["FAVOURITES", "FAVORITES"]:
            self.mode_manager.to_favourites(); return
        if key == "LAST_100":
            self.mode_manager.to_last100(); return
        if key in ["MEDIA_SERVERS", "UPNP"]:
            self.mode_manager.to_mediaservers(); return

        # Dynamic fallback: open any discovered plugin
        svc = self.services_by_label.get(key)
        if not svc:
            self.logger.warning(f"MenuManager: Unhandled menu selection key '{key}' and no discovered meta.")
            return

        plugin = (svc.get("plugin") or "").lower()
        uri = (svc.get("uri") or "").strip()

        # Route common non-streaming plugins
        if plugin == "webradio":
            self.mode_manager.to_radio(); return
        if plugin in ("playlists", "playlist"):
            self.mode_manager.to_playlists(); return
        if plugin == "mpd":
            # Library entries should be opened via the library manager
            self.mode_manager.to_library(); return

        # Everything else -> generic streaming manager
        # Choose a sensible start URI:
        start_uri = uri or f"{plugin}://"
        self.logger.info(f"MenuManager: Opening dynamic streaming plugin '{plugin}' with start_uri='{start_uri}'")
        self.mode_manager.trigger("to_streaming", service_name=plugin, start_uri=start_uri)

    # ---- list theme & text helpers ----

    def _list_theme(self):
        return {
            "y_offset": -12,
            "margin": 2,
            "title_offset": 5,
            "header_gap": 5,
            "row_gap": 8,
            "divider_colour": (40, 40, 40),
            "focus_bg": (50, 50, 50),
            "text": (255, 255, 255),
            "text_dim": (170, 170, 170),
            "chevron_focus": "›",
            "chevron_dim": "›",
        }

    def _text_wh(self, draw, text, font):
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        return right - left, bottom - top

    def _truncate_to_width(self, draw, text, font, max_w):
        w, _ = self._text_wh(draw, text, font)
        if w <= max_w:
            return text
        ell = "…"
        s = text
        low, high = 0, len(s)
        while low < high:
            mid = (low + high) // 2
            cand = s[:mid] + ell
            cw, _ = self._text_wh(draw, cand, font)
            if cw <= max_w:
                low = mid + 1
            else:
                high = mid
        mid = max(1, low - 1)
        return s[:mid] + ell

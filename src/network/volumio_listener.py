# src/network/volumio_listener.py

import socketio
import logging
import time
import threading
from blinker import Signal

class VolumioListener:
    def __init__(self, host='localhost', port=3000, reconnect_delay=5):
        """
        Initialize the VolumioListener.
        """
        self.logger = logging.getLogger("VolumioListener")

        self.logger.setLevel(logging.DEBUG)  # Set to DEBUG for detailed logs
        self.logger.debug("[VolumioListener] Initializing...")

        self.host = host
        self.port = port
        self.reconnect_delay = reconnect_delay
        self.socketIO = socketio.Client(logger=False, engineio_logger=False, reconnection=True)

        # Define Blinker signals
        self.connected = Signal('connected')
        self.disconnected = Signal('disconnected')
        self.state_changed = Signal('state_changed')
        self.track_changed = Signal('track_changed')
        self.toast_message_received = Signal('toast_message_received')
        self.navigation_received = Signal()

        # Navigation signals for managers
        self.playlists_navigation_received = Signal('playlists_navigation_received')
        self.webradio_navigation_received = Signal('webradio_navigation_received')
        self.motherearth_navigation_received = Signal('motherearth_navigation_received')
        self.radioparadise_navigation_received = Signal('radioparadise_navigation_received')
        self.qobuz_navigation_received = Signal('qobuz_navigation_received')
        self.tidal_navigation_received = Signal('tidal_navigation_received')
        self.spotify_navigation_received = Signal('spotify_navigation_received')
        self.library_navigation_received = Signal('library_navigation_received')
        self.usb_library_navigation_received = Signal('usb_library_navigation_received')
        self.albums_navigation_received = Signal('albums_navigation_received')

        # Internal state
        self.current_state = {}
        self.state_lock = threading.Lock()
        self.current_volume = None 
        self._running = True
        self._reconnect_attempt = 1

        # Tracking browseLibrary requests
        self.browse_lock = threading.Lock()
        self.last_browse_service = None
        self.last_browse_uri = None

        self.register_socketio_events()
        self.connect()

    def register_socketio_events(self):
        """Register events to listen to from the SocketIO server."""
        self.logger.info("[VolumioListener] Registering SocketIO events...")
        self.socketIO.on('connect', self.on_connect)
        self.socketIO.on('disconnect', self.on_disconnect)
        self.socketIO.on('pushState', self.on_push_state)
        self.socketIO.on('pushBrowseLibrary', self.on_push_browse_library)
        self.socketIO.on('pushTrack', self.on_push_track)
        self.socketIO.on('pushToastMessage', self.on_push_toast_message)
        self.socketIO.on('volume', self.set_volume)
        self.socketIO.on('pushBrowseSources', self.on_push_browse_sources)

    
    def set_volume(self, value):
        """Set the volume to a specific value, increase/decrease, or mute/unmute."""
        valid_values = ['+', '-', 'mute', 'unmute']
        if isinstance(value, int) and 0 <= value <= 100:
            self.logger.info(f"[VolumioListener] Setting volume to: {value}")
            self.socketIO.emit('volume', value)
            # Update our local volume tracker
            self.current_volume = value
        elif value in valid_values:
            self.logger.info(f"[VolumioListener] Sending volume command: {value}")
            self.socketIO.emit('volume', value)
        else:
            self.logger.warning(f"[VolumioListener] Invalid volume value: {value}")

    def increase_volume_by(self, step=5):
        # If current_volume is None, log a warning and do nothing
        if self.current_volume is None:
            self.logger.warning("Current volume not set; skipping volume increase.")
            return
        new_volume = min(100, self.current_volume + step)
        self.logger.info(f"[VolumioListener] Increasing volume from {self.current_volume} to {new_volume}")
        self.set_volume(new_volume)

    def decrease_volume_by(self, step=5):
        if self.current_volume is None:
            self.logger.warning("Current volume not set; skipping volume decrease.")
            return
        new_volume = max(0, self.current_volume - step)
        self.logger.info(f"[VolumioListener] Decreasing volume from {self.current_volume} to {new_volume}")
        self.set_volume(new_volume)


    # You can now update your existing methods to use the new step-based methods.
    def increase_volume(self):
        """Increase the volume by 5 (by default)."""
        self.increase_volume_by(5)

    def decrease_volume(self):
        """Decrease the volume by 5 (by default)."""
        self.decrease_volume_by(5)

    def mute_volume(self):
        """Mute the volume."""
        self.set_volume('mute')

    def unmute_volume(self):
        """Unmute the volume."""
        self.set_volume('unmute')

    def on_push_toast_message(self, data):
        """Handle 'pushToastMessage' events."""
        self.logger.info("[VolumioListener] Received pushToastMessage event.")
        if data:
            self.logger.debug(f"Toast Message Data: {data}")
            self.toast_message_received.send(self, message=data)
        else:
            self.logger.warning("[VolumioListener] Received empty toast message.")

    def connect(self):
        """Connect to the Volumio server."""
        if self.socketIO.connected:
            self.logger.info("[VolumioListener] Already connected.")
            return
        try:
            self.logger.info(f"[VolumioListener] Connecting to Volumio at {self.host}:{self.port}...")
            self.socketIO.connect(f"http://{self.host}:{self.port}")
            self.logger.info("[VolumioListener] Successfully connected.")
        except Exception as e:
            self.logger.error(f"[VolumioListener] Connection error: {e}")
            self.schedule_reconnect()

    def on_connect(self):
        self.connected.send(self)
        self.logger.info("[VolumioListener] Connected to Volumio.")
        self._reconnect_attempt = 1  # Reset reconnect attempts
        self.socketIO.emit('getState')
        # Explicitly browse root after connect (wait a fraction of a second for socket to settle)
        threading.Timer(0.2, lambda: self.fetch_browse_library("")).start()


    def is_connected(self):
        """Check if the client is connected to Volumio."""
        return self.socketIO.connected

    def on_disconnect(self):
        """Handle disconnection."""
        self.disconnected.send(self)
        self.logger.warning("[VolumioListener] Disconnected from Volumio.")
        self.schedule_reconnect()

    def on_push_browse_sources(self, data):
        self.logger.info("[VolumioListener] Received pushBrowseSources event.")
        # Optionally: log the new sources for debug
        self.logger.debug(f"[VolumioListener] Sources: {data}")
        # Now trigger your menu to refresh.
        # This could be via a direct reference, signal, or callback—see below!
        if hasattr(self, "menu_manager"):
            self.menu_manager.refresh_main_menu()
            self.menu_manager.display_menu()
        else:
            # Or use a signal if you wire it that way
            if hasattr(self, "sources_changed"):
                self.sources_changed.send(self, sources=data)

    def schedule_reconnect(self):
        """Schedule a reconnection attempt."""
        delay = min(self.reconnect_delay * self._reconnect_attempt, 60)
        self.logger.info(f"[VolumioListener] Reconnecting in {delay} seconds...")
        threading.Thread(target=self._reconnect_after_delay, args=(delay,), daemon=True).start()

    def _reconnect_after_delay(self, delay):
        """Reconnect after a specified delay."""
        time.sleep(delay)
        if not self.socketIO.connected and self._running:
            self._reconnect_attempt += 1
            self.connect()

    def on_push_state(self, data):
        self.logger.info("[VolumioListener] Received pushState event.")
        with self.state_lock:
            self.current_state = data  # Store the current state
            if "volume" in data:
                self.current_volume = data["volume"]
        self.state_changed.send(self, state=data)

    def extract_streaming_services(self, navigation):
        STREAMING_SERVICES = {"tidal", "qobuz", "spotify"}
        found = []
        lists = navigation.get("lists", [])
        for item in lists:
            name = item.get("plugin_name", "").lower()
            if name in STREAMING_SERVICES:
                found.append(name)
        return found

    def on_push_browse_library(self, data):
        self.logger.info("[VolumioListener] Received pushBrowseLibrary event.")
        navigation = data.get("navigation", {})
        if not navigation:
            self.logger.warning("[VolumioListener] No navigation data received.")
            return

        # (Optional) root streaming services scrape omitted for brevity ...

        # Pull tracked browse info (set in fetch_browse_library)
        with self.browse_lock:
            tracked_service = self.last_browse_service
            tracked_uri = self.last_browse_uri
            self.last_browse_service = None
            self.last_browse_uri = None

        # Best-guess event URI (Volumio often omits this)
        event_uri = (navigation.get('uri') or data.get('uri') or '').strip().lower()

        # Choose the most reliable values
        chosen_uri = tracked_uri or event_uri
        chosen_service = tracked_service or self.get_service_from_uri(chosen_uri)

        if not chosen_service:
            # Fallback: derive from payload items (helps dynamic/unknown plugins)
            chosen_service = self._infer_service_from_navigation(navigation)

        self.logger.debug(f"[VolumioListener] Using URI: {chosen_uri}, Service: {chosen_service}")

        # Emit one generic navigation signal that includes what manager(s) need
        self.navigation_received.send(self, navigation=navigation, service=chosen_service, uri=chosen_uri)

    def on_push_track(self, data):
        """Handle 'pushTrack' events."""
        self.logger.info("[VolumioListener] Received pushTrack event.")
        track_info = self.extract_track_info(data)
        self.track_changed.send(self, track_info=track_info)

    def extract_track_info(self, data):
        """Extract track info."""
        track = data.get('track', {})
        return {
            'title': track.get('title', 'Unknown Title'),
            'artist': track.get('artist', 'Unknown Artist'),
            'albumart': track.get('albumart', ''),
            'uri': track.get('uri', '')
        }

    def get_current_state(self):
        with self.state_lock:
            return self.current_state.copy()  # Return a copy to prevent external modifications

    def stop(self):
        """Stop the VolumioListener."""
        self._running = False
        self.socketIO.disconnect()
        self.logger.info("[VolumioListener] Listener stopped.")

    def fetch_browse_library(self, uri):
        if self.socketIO.connected:
            # DEFAULT TO 'music-library' IF BLANK OR NONE
            uri = uri or "music-library"
            service = self.get_service_from_uri(uri)
            with self.browse_lock:
                self.last_browse_service = service
                self.last_browse_uri = uri
                self.logger.debug(f"[VolumioListener] Tracking browseLibrary URI: {uri}, Service: {service}")
            self.socketIO.emit("browseLibrary", {"uri": uri})
            self.logger.debug(f"[VolumioListener] Emitted 'browseLibrary' for URI: {uri}")
        else:
            self.logger.warning("[VolumioListener] Cannot emit 'browseLibrary' - not connected to Volumio.")

    def get_service_from_uri(self, uri):
        self.logger.debug(f"Determining service for URI: {uri}")
        u = (uri or '').lower()

        if u.startswith("spotify") or u.startswith("spop"):
            return 'spotify'
        if u.startswith("qobuz://"):
            return 'qobuz'
        if u.startswith("tidal://"):
            return 'tidal'
        if u.startswith("radio/"):
            return 'webradio'

        # Radio Paradise
        if u in ("rparadise", "radio_paradise", "radio-paradise") or u.startswith("rparadise/"):
            return 'radioparadise'
        if u.startswith("webrp/"):                
            return 'radioparadise'                

        # Mother Earth Radio
        if u in ("mer", "motherearthradio", "mother_earth_radio"): 
            return "motherearthradio"
        if u.startswith("webmer/"):                
            return "motherearthradio"             

        if u.startswith("playlists") or u.startswith("playlist://"):
            return 'playlists'
        if u.startswith("music-library/nas"):
            return 'library'
        if u.startswith("music-library/usb"):
            return 'usblibrary'
        if u == "music-library":
            return 'library'

        self.logger.warning(f"Unrecognized URI scheme: {uri}")
        return None

    def _infer_service_from_navigation(self, navigation):
        """Best-effort: read 'service' or 'plugin_name' off items in the list."""
        try:
            for lst in navigation.get('lists', []):
                for it in lst.get('items', []):
                    svc = (it.get('service') or it.get('plugin_name') or '').lower()
                    if svc:
                        # normalize a couple of common variants
                        if svc in ('radio_paradise', 'radio-paradise', 'radioparadise'):
                            return 'radioparadise'
                        if svc in ('mother_earth_radio', 'motherearthradio', 'mother-earth-radio'):
                            return 'motherearthradio'
                        return svc
        except Exception:
            pass
        return None

        

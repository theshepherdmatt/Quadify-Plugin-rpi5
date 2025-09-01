# src/handlers/icon_provider.py
#
# Lightweight icon lookup that pulls from your cached PNGs and (optionally)
# the icons manifest written by your icon_fetcher. No dependency on
# display_manager.icons.

import os
import json
from typing import Optional, Dict, List
from PIL import Image

ASSETS_PNG_DIR = "/home/volumio/Quadify/src/assets/pngs"
ASSETS_MANIFEST = "/home/volumio/Quadify/src/assets/icons_manifest.json"


def _norm_label(s):  # type: (str) -> str
    s = (s or "").strip()
    return s.upper().replace(" ", "_").replace("-", "_").replace("/", "_")


def _variants(s):  # type: (str) -> List[str]
    """Generate a few spelling/casing variants that commonly appear."""
    s = (s or "").strip()
    if not s:
        return []
    lo = s.lower()
    up = _norm_label(s)
    return list({lo, lo.replace("_", "-"), lo.replace("-", "_"), up})


class IconProvider:
    """
    Loads icons from the PNG cache (and optional JSON manifest).
    Use:
        ip = IconProvider()
        img = ip.get_icon("spotify", size=20)
        img2 = ip.get_service_icon_from_state(state, size=18)
    """

    def __init__(self, assets_dir=ASSETS_PNG_DIR, manifest_path=ASSETS_MANIFEST):
        self.assets_dir = assets_dir
        self.manifest_path = manifest_path
        self._cache = {}        # type: Dict[str, Image.Image]  # base images by UPPER label
        self._index = {}        # type: Dict[str, str]           # UPPER label -> absolute path
        self._manifest = {}     # type: Dict[str, str]           # UPPER label -> absolute path
        self.reload()

    # ----------------------- public API -----------------------

    def reload(self):
        """(Re)load directory index and manifest (does not clear decoded image cache)."""
        self._build_index()
        self._load_manifest()

    def get_icon(self, key, size=None):  # type: (str, Optional[int]) -> Optional[Image.Image]
        """
        Return a PIL.Image (RGBA) for 'key' (e.g. 'qobuz', 'RADIO_PARADISE').
        If size is provided, returns a resized COPY; base image stays cached.
        """
        for v in _variants(key):
            base = self._load_base(_norm_label(v))
            if base is not None:
                if size:
                    img = base.copy()
                    img = img.resize((size, size), Image.LANCZOS)
                    return img
                return base
        return None

    def get_service_icon_from_state(self, state, size=None):  # type: (dict, Optional[int]) -> Optional[Image.Image]
        """
        Try multiple hints from a Volumio state: service, trackType, plugin, stream.
        Includes a few normalised aliases (spop->SPOTIFY, radio_paradise->RADIO_PARADISE, etc).
        """
        service    = (state.get("service") or "").strip()
        track_type = (state.get("trackType") or "").strip()
        plugin     = (state.get("plugin") or "").strip()
        stream     = (state.get("stream") or "").strip()

        candidates = []  # type: List[str]
        for v in (service, track_type, plugin, stream):
            candidates.extend(_variants(v))

        # Common aliases
        alias_map = {
            "spop": "SPOTIFY",
            "radio_paradise": "RADIO_PARADISE",
            "radioparadise": "RADIO_PARADISE",
            "mother_earth_radio": "MOTHER_EARTH_RADIO",
            "motherearthradio": "MOTHER_EARTH_RADIO",
        }
        for c in list(candidates):
            if c in alias_map:
                candidates.append(alias_map[c])

        for c in candidates:
            img = self.get_icon(c, size=size)
            if img is not None:
                return img
        return None

    # ----------------------- internals ------------------------

    def _build_index(self):
        self._index.clear()
        if not os.path.isdir(self.assets_dir):
            return
        for name in os.listdir(self.assets_dir):
            if not name.lower().endswith(".png"):
                continue
            label = _norm_label(os.path.splitext(name)[0])
            self._index[label] = os.path.join(self.assets_dir, name)

    def _load_manifest(self):
        self._manifest.clear()
        try:
            if os.path.exists(self.manifest_path):
                with open(self.manifest_path, "r") as f:
                    data = json.load(f)

                # support both list-of-entries and {label:path} formats
                if isinstance(data, list):
                    for entry in data:
                        label = _norm_label(entry.get("label", ""))
                        path = entry.get("path")
                        if label and path:
                            self._manifest[label] = path
                elif isinstance(data, dict):
                    for k, v in data.items():
                        self._manifest[_norm_label(k)] = v
        except Exception:
            # Manifest is optional; ignore errors.
            pass

    def _load_base(self, label_upper):  # type: (str) -> Optional[Image.Image]
        if not label_upper:
            return None
        if label_upper in self._cache:
            return self._cache[label_upper]

        path = self._manifest.get(label_upper) or self._index.get(label_upper)
        if not path or not os.path.exists(path):
            return None

        try:
            img = Image.open(path).convert("RGBA")
            self._cache[label_upper] = img
            return img
        except Exception:
            return None

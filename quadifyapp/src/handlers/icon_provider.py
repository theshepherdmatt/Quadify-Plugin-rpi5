# src/handlers/icon_provider.py

import os, json
from typing import Optional, Dict, List
from PIL import Image

# Resolve plugin paths relative to this file
_HERE = os.path.dirname(__file__)
_SRC  = os.path.abspath(os.path.join(_HERE, ".."))  # .../quadifyapp/src

_DEFAULT_PNG_DIR   = os.path.join(_SRC, "assets", "pngs")
_DEFAULT_MANIFEST  = os.path.join(_SRC, "assets", "icons_manifest.json")

def _first_existing(paths):
    for p in paths:
        if p and os.path.exists(p):
            return p
    return None

def _norm_label(s):  # same as before
    s = (s or "").strip()
    return s.upper().replace(" ", "_").replace("-", "_").replace("/", "_")

def _variants(s):  # same as before
    s = (s or "").strip()
    if not s:
        return []
    lo = s.lower()
    up = _norm_label(s)
    return list({lo, lo.replace("_", "-"), lo.replace("-", "_"), up})

# Candidate locations (env overrides first, then plugin defaults, then common fallbacks)
CANDIDATE_PNG_DIRS = [
    os.environ.get("QUADIFY_ICON_DIR") or os.environ.get("QUADIFY_ASSETS_PNG_DIR"),
    _DEFAULT_PNG_DIR,
    "/data/plugins/system_hardware/quadify/quadifyapp/src/assets/pngs",
    "/home/volumio/Quadify/src/assets/pngs",
]
CANDIDATE_PNG_DIRS = [p for p in CANDIDATE_PNG_DIRS if p]

CANDIDATE_MANIFESTS = [
    os.environ.get("QUADIFY_ICON_MANIFEST"),
    _DEFAULT_MANIFEST,
    "/data/plugins/system_hardware/quadify/quadifyapp/src/assets/icons_manifest.json",
    "/home/volumio/Quadify/src/assets/icons_manifest.json",
]
CANDIDATE_MANIFESTS = [p for p in CANDIDATE_MANIFESTS if p]

class IconProvider:
    def __init__(self, assets_dir: Optional[str] = None, manifest_path: Optional[str] = None):
        # Pick first existing unless explicit args were passed
        self.assets_dir    = assets_dir    or _first_existing(CANDIDATE_PNG_DIRS)    or _DEFAULT_PNG_DIR
        self.manifest_path = manifest_path or _first_existing(CANDIDATE_MANIFESTS)   or _DEFAULT_MANIFEST
        self._cache: Dict[str, Image.Image] = {}
        self._index: Dict[str, str] = {}
        self._manifest: Dict[str, str] = {}
        self.reload()

    def reload(self):
        self._build_index()
        self._load_manifest()

    def get_icon(self, key: str, size: Optional[int] = None) -> Optional[Image.Image]:
        for v in _variants(key):
            base = self._load_base(_norm_label(v))
            if base is not None:
                return base.resize((size, size), Image.LANCZOS) if size else base
        return None

    def get_service_icon_from_state(self, state: dict, size: Optional[int] = None) -> Optional[Image.Image]:
        service    = (state.get("service") or "").strip()
        track_type = (state.get("trackType") or "").strip()
        plugin     = (state.get("plugin") or "").strip()
        stream     = (state.get("stream") or "").strip()

        candidates: List[str] = []
        for v in (service, track_type, plugin, stream):
            candidates.extend(_variants(v))

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

    # --- internals (unchanged except using self.assets_dir / self.manifest_path) ---
    def _build_index(self):
        self._index.clear()
        if not os.path.isdir(self.assets_dir):
            return
        for name in os.listdir(self.assets_dir):
            if name.lower().endswith(".png"):
                label = _norm_label(os.path.splitext(name)[0])
                self._index[label] = os.path.join(self.assets_dir, name)

    def _load_manifest(self):
        self._manifest.clear()
        if not self.manifest_path or not os.path.exists(self.manifest_path):
            return
        try:
            with open(self.manifest_path, "r") as f:
                data = json.load(f)
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
            pass

    def _load_base(self, label_upper: str) -> Optional[Image.Image]:
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

"""Runtime settings.

Values come from (lowest to highest priority):
  1. built-in defaults
  2. a ``.env`` file in the working directory and real environment variables
  3. ``<data_dir>/settings.json`` (written by the dashboard's Settings page)
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

VALID_SOURCES = ("auto", "api", "browser")

# Settings the dashboard is allowed to change.
EDITABLE_KEYS = (
    "places_api_key",
    "source",
    "headless",
    "render_js",
    "respect_robots",
    "site_concurrency",
    "request_timeout",
    "use_browser_profile",
    "anthropic_api_key",
    "ai_model",
    "search_delay",
    "maps_concurrency",
)


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, # comments, optional quotes.

    Real environment variables always win over the file.
    """
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _as_bool(value: str | bool | None, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data")
    profiles_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "profiles")
    places_api_key: str = ""
    source: str = "auto"
    headless: bool = True
    # Render JavaScript-heavy websites (Wix, Squarespace...) with Chromium when plain HTTP
    # returns almost no text.
    render_js: bool = True
    respect_robots: bool = True
    site_concurrency: int = 8
    request_timeout: float = 15.0
    # Keep Chromium's cookies between runs (data/browser-profile). Lets you sign in to Google
    # once (`prospector browser-login`) so Maps shows its full view instead of the limited one.
    use_browser_profile: bool = False
    # Optional AI fit notes.
    anthropic_api_key: str = ""
    ai_model: str = "claude-opus-5-5"
    # Pause between Google Maps searches (seconds) — gentler on Google, fewer CAPTCHAs.
    search_delay: float = 2.0
    # Listings opened in parallel by the browser source.
    maps_concurrency: int = 4
    chromium_path: str = ""
    host: str = "127.0.0.1"
    port: int = 8000

    @property
    def db_path(self) -> Path:
        return self.data_dir / "prospector.db"

    @property
    def browser_profile_dir(self) -> Path | None:
        return self.data_dir / "browser-profile" if self.use_browser_profile else None

    @property
    def settings_file(self) -> Path:
        return self.data_dir / "settings.json"

    def resolved_source(self, requested: str | None = None) -> str:
        """Turn 'auto' into a concrete source name."""
        choice = (requested or self.source or "auto").lower()
        if choice not in VALID_SOURCES:
            raise ValueError(f"Unknown source {choice!r}; use one of {', '.join(VALID_SOURCES)}")
        if choice == "auto":
            return "api" if self.places_api_key else "browser"
        return choice

    def public_dict(self) -> dict:
        """Settings safe to send to the browser (API key masked)."""
        data = {k: getattr(self, k) for k in EDITABLE_KEYS}
        for name in ("places_api_key", "anthropic_api_key"):
            key = getattr(self, name)
            data[name] = (key[:4] + "…" + key[-4:]) if len(key) > 10 else ("set" if key else "")
            data[f"has_{name}"] = bool(key)
        data["data_dir"] = str(self.data_dir)
        data["profiles_dir"] = str(self.profiles_dir)
        return data

    def save_overrides(self, updates: dict) -> None:
        """Persist dashboard edits to settings.json and apply them in memory."""
        current = _read_overrides(self.settings_file)
        for key, value in updates.items():
            if key not in EDITABLE_KEYS:
                continue
            if key == "source" and value not in VALID_SOURCES:
                raise ValueError(f"source must be one of {', '.join(VALID_SOURCES)}")
            current[key] = value
        self.settings_file.parent.mkdir(parents=True, exist_ok=True)
        self.settings_file.write_text(json.dumps(current, indent=2), encoding="utf-8")
        _apply(self, current)


def _read_overrides(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _apply(settings: Settings, values: dict) -> None:
    for key, value in values.items():
        if key not in EDITABLE_KEYS:
            continue
        if key in ("headless", "render_js", "respect_robots", "use_browser_profile"):
            value = _as_bool(value, getattr(settings, key))
        elif key == "site_concurrency":
            value = max(1, min(32, int(value)))
        elif key == "request_timeout":
            value = max(3.0, min(120.0, float(value)))
        elif key == "search_delay":
            value = max(0.0, min(60.0, float(value)))
        elif key == "maps_concurrency":
            value = max(1, min(8, int(value)))
        else:
            value = str(value or "").strip()
        setattr(settings, key, value)


def load_settings(env_file: Path | None = None) -> Settings:
    _load_dotenv(env_file or Path.cwd() / ".env")
    if env_file is None and Path.cwd() != PROJECT_ROOT:
        _load_dotenv(PROJECT_ROOT / ".env")

    s = Settings()
    env = os.environ
    if env.get("PROSPECTOR_DATA_DIR"):
        p = Path(env["PROSPECTOR_DATA_DIR"]).expanduser()
        s.data_dir = p if p.is_absolute() else PROJECT_ROOT / p
    if env.get("PROSPECTOR_PROFILES_DIR"):
        p = Path(env["PROSPECTOR_PROFILES_DIR"]).expanduser()
        s.profiles_dir = p if p.is_absolute() else PROJECT_ROOT / p
    s.places_api_key = env.get("GOOGLE_PLACES_API_KEY", "").strip()
    s.source = (env.get("PROSPECTOR_SOURCE") or "auto").strip().lower()
    s.headless = _as_bool(env.get("PROSPECTOR_HEADLESS"), True)
    s.render_js = _as_bool(env.get("PROSPECTOR_RENDER_JS"), True)
    s.respect_robots = _as_bool(env.get("PROSPECTOR_RESPECT_ROBOTS"), True)
    s.anthropic_api_key = env.get("ANTHROPIC_API_KEY", "").strip()
    s.ai_model = (env.get("PROSPECTOR_AI_MODEL") or s.ai_model).strip()
    s.use_browser_profile = _as_bool(env.get("PROSPECTOR_BROWSER_PROFILE"), False)
    s.chromium_path = env.get("PROSPECTOR_CHROMIUM_PATH", "").strip()
    s.host = env.get("PROSPECTOR_HOST", s.host)
    s.port = int(env.get("PROSPECTOR_PORT", s.port))

    _apply(s, _read_overrides(s.settings_file))
    s.data_dir.mkdir(parents=True, exist_ok=True)
    s.profiles_dir.mkdir(parents=True, exist_ok=True)
    return s


__all__ = ["Settings", "load_settings", "VALID_SOURCES", "asdict"]

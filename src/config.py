"""Plugin configuration, Redis key constants, and field definitions.

Single source of truth for:
  - PLUGIN_CONFIG: loaded from plugin.json
  - Redis key names used by every module
  - PLUGIN_FIELDS: the settings schema shared by the Plugin class and the handler
"""

import json
import os


# ── Hard-coded defaults ─────────────────────────────────────────────────────
DEFAULT_PORT: int = 9193
DEFAULT_HOST: str = "0.0.0.0"
AUTO_START_DEFAULT: bool = True
DEFAULT_CLEANUP_TIMEOUT: int = 30  # seconds

# Key used to look up this plugin's settings in Dispatcharr's PluginConfig
# table.  Dispatcharr derives the key from the zip folder name, which may be
# "emby_stream_cleanup" or "emby-stream-cleanup" depending on the build.
PLUGIN_DB_KEY: str = "emby_stream_cleanup"


def _load_plugin_config() -> dict:
    """Load plugin configuration from plugin.json."""
    config_path = os.path.join(os.path.dirname(__file__), 'plugin.json')
    with open(config_path, 'r') as f:
        return json.load(f)


PLUGIN_CONFIG = _load_plugin_config()

# ── Redis key names ──────────────────────────────────────────────────────────
REDIS_KEY_RUNNING = "emby_cleanup:server_running"
REDIS_KEY_HOST    = "emby_cleanup:server_host"
REDIS_KEY_PORT    = "emby_cleanup:server_port"
REDIS_KEY_STOP    = "emby_cleanup:stop_requested"
REDIS_KEY_LEADER  = "emby_cleanup:leader"

# Prefix for per-channel viewer tracking sets.
# Full key: emby_cleanup:viewers:{channel_number}
REDIS_KEY_VIEWERS_PREFIX = "emby_cleanup:viewers:"

# Keys to wipe on startup (leader key intentionally excluded so the winning
# worker keeps its claim after cleanup).
CLEANUP_REDIS_KEYS = [
    REDIS_KEY_RUNNING,
    REDIS_KEY_HOST,
    REDIS_KEY_PORT,
    REDIS_KEY_STOP,
]

# Complete set of every key ever written by this plugin (excluding viewer keys)
ALL_PLUGIN_REDIS_KEYS = CLEANUP_REDIS_KEYS + [REDIS_KEY_LEADER]

# Leader election TTL.  The winner holds this key for up to LEADER_TTL seconds.
LEADER_TTL = 60  # seconds

# TTL for viewer tracking sets — auto-expire stale data if the plugin
# restarts without receiving stop events.
VIEWER_SET_TTL = 86400  # 24 hours

# ── Plugin field definitions ─────────────────────────────────────────────────
PLUGIN_FIELDS = [
    {
        "id": "auto_start",
        "label": "Auto-Start Webhook Server",
        "type": "boolean",
        "default": AUTO_START_DEFAULT,
        "description": "Automatically start the webhook server when plugin loads (recommended)",
    },
    {
        "id": "suppress_access_logs",
        "label": "Suppress Access Logs",
        "type": "boolean",
        "default": True,
        "description": "Suppress HTTP access logs for webhook requests",
    },
    {
        "id": "port",
        "label": "Webhook Server Port",
        "type": "number",
        "default": DEFAULT_PORT,
        "description": "Port for the webhook HTTP server",
        "placeholder": "9193",
    },
    {
        "id": "host",
        "label": "Webhook Server Host",
        "type": "string",
        "default": DEFAULT_HOST,
        "description": "Host address to bind to (0.0.0.0 for all interfaces, 127.0.0.1 for localhost only)",
        "placeholder": "0.0.0.0",
    },
    {
        "id": "emby_identifier",
        "label": "Emby Identifier",
        "type": "string",
        "default": "",
        "description": (
            "IP address, hostname, or XC username that Emby uses to connect to Dispatcharr. "
            "Matched against client IP and username in Dispatcharr to identify Emby connections."
        ),
        "placeholder": "192.168.1.100",
    },
    {
        "id": "cleanup_timeout",
        "label": "Cleanup Timeout (seconds)",
        "type": "number",
        "default": DEFAULT_CLEANUP_TIMEOUT,
        "description": (
            "Seconds to wait after the last Emby viewer stops watching a channel "
            "before terminating the Dispatcharr connection"
        ),
        "placeholder": "30",
    },
]

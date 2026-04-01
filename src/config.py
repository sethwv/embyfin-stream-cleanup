"""Plugin configuration, Redis key constants, and field definitions.

Single source of truth for:
  - PLUGIN_CONFIG: loaded from plugin.json
  - Redis key names used by every module
  - PLUGIN_FIELDS: the settings schema shared by the Plugin class and the monitor
"""

import json
import os


# ── Hard-coded defaults ─────────────────────────────────────────────────────
DEFAULT_PORT: int = 9193
DEFAULT_HOST: str = "0.0.0.0"
DEFAULT_CLEANUP_TIMEOUT: int = 30  # seconds
DEFAULT_POLL_INTERVAL: int = 10    # seconds

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
REDIS_KEY_RUNNING  = "emby_cleanup:server_running"
REDIS_KEY_HOST     = "emby_cleanup:server_host"
REDIS_KEY_PORT     = "emby_cleanup:server_port"
REDIS_KEY_STOP     = "emby_cleanup:stop_requested"
REDIS_KEY_LEADER   = "emby_cleanup:leader"
REDIS_KEY_MONITOR  = "emby_cleanup:monitor_running"

# Keys to wipe on startup (leader key intentionally excluded so the winning
# worker keeps its claim after cleanup).
CLEANUP_REDIS_KEYS = [
    REDIS_KEY_RUNNING,
    REDIS_KEY_HOST,
    REDIS_KEY_PORT,
    REDIS_KEY_STOP,
    REDIS_KEY_MONITOR,
]

# Complete set of every key ever written by this plugin
ALL_PLUGIN_REDIS_KEYS = CLEANUP_REDIS_KEYS + [REDIS_KEY_LEADER]

# Leader election TTL.  The winner holds this key for up to LEADER_TTL seconds.
LEADER_TTL = 60  # seconds

# Heartbeat TTL for "running" Redis keys.  The monitor and server refresh
# their keys on every loop iteration.  If the process dies, the keys expire
# and autostart can proceed on the next startup.
HEARTBEAT_TTL = 30  # seconds

# ── Plugin field definitions ─────────────────────────────────────────────────
PLUGIN_FIELDS = [
    {
        "id": "client_identifier",
        "label": "Client Identifier",
        "type": "string",
        "default": "",
        "description": (
            "IP address, hostname, or XC username used by the target client to connect to Dispatcharr. "
            "Comma-separated list for multiple values (e.g. '192.168.1.100, media-server'). "
            "Use 'ALL' to match every client. "
            "Matched against client IP and username to identify connections."
        ),
        "placeholder": "192.168.1.100, media-server, or ALL",
    },
    {
        "id": "cleanup_timeout",
        "label": "Idle Timeout (seconds)",
        "type": "number",
        "default": DEFAULT_CLEANUP_TIMEOUT,
        "description": (
            "Seconds a matching client must be idle (no data flowing) before "
            "its Dispatcharr connection is terminated. "
            "During stream failover or buffering the timer is paused automatically"
        ),
        "placeholder": "30",
    },
    {
        "id": "poll_interval",
        "label": "Poll Interval (seconds)",
        "type": "number",
        "default": DEFAULT_POLL_INTERVAL,
        "description": "How often to check client activity",
        "placeholder": "10",
    },
    {
        "id": "emby_url",
        "label": "Emby Server URL",
        "type": "string",
        "default": "",
        "description": (
            "Base URL of the Emby server (e.g. http://192.168.1.100:8096). "
            "When set, the plugin polls Emby's Sessions API to detect orphaned "
            "connections that Emby failed to close. Leave blank to rely solely "
            "on idle detection"
        ),
        "placeholder": "http://192.168.1.100:8096",
    },
    {
        "id": "emby_api_key",
        "label": "Emby API Key",
        "type": "string",
        "default": "",
        "description": (
            "API key for the Emby server. "
            "Generate one in Emby under Settings > API Keys"
        ),
        "placeholder": "your-emby-api-key",
    },
    {
        "id": "enable_debug_server",
        "label": "Enable Debug Server",
        "type": "boolean",
        "default": False,
        "description": "Start an HTTP server for the debug dashboard (optional)",
    },
    {
        "id": "suppress_access_logs",
        "label": "Suppress Access Logs",
        "type": "boolean",
        "default": True,
        "description": "Suppress HTTP access logs for the debug server",
    },
    {
        "id": "port",
        "label": "Debug Server Port",
        "type": "number",
        "default": DEFAULT_PORT,
        "description": "Port for the debug HTTP server",
        "placeholder": "9193",
    },
    {
        "id": "host",
        "label": "Debug Server Host",
        "type": "string",
        "default": DEFAULT_HOST,
        "description": "Host address to bind the debug server to (0.0.0.0 for all interfaces)",
        "placeholder": "0.0.0.0",
    },
]

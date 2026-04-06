"""Emby Stream Cleanup - package root.

Dispatcharr discovers the plugin by importing this package and looking for
the ``Plugin`` class.  The stream monitor, debug server, and auto-start
logic live in their own modules; this file only contains the plugin API.
"""

import logging
import time

from .config import (
    PLUGIN_CONFIG, PLUGIN_FIELDS, build_plugin_fields, PLUGIN_DB_KEY,
    REDIS_KEY_RUNNING, REDIS_KEY_HOST, REDIS_KEY_PORT, REDIS_KEY_STOP,
    REDIS_KEY_MONITOR,
    DEFAULT_PORT, DEFAULT_HOST,
)
from .handler import StreamMonitor
from .server import DebugServer, get_current_server
from .autostart import attempt_autostart
from .utils import get_redis_client, read_redis_flag, normalize_host, redis_decode

logger = logging.getLogger(__name__)

# Module-level monitor instance shared across actions
_monitor = StreamMonitor()


class Plugin:
    """Dispatcharr Plugin - Emby stream cleanup via activity monitoring."""

    name        = PLUGIN_CONFIG["name"]
    description = PLUGIN_CONFIG["description"]
    version     = PLUGIN_CONFIG["version"]
    author      = PLUGIN_CONFIG["author"]

    @property
    def fields(self):
        """Build fields dynamically based on saved media_server_count."""
        try:
            from apps.plugins.models import PluginConfig
            cfg = PluginConfig.objects.get(key=PLUGIN_DB_KEY)
            count = int(cfg.settings.get("media_server_count", 1))
        except Exception:
            count = 1
        return build_plugin_fields(count)

    actions = [
        {
            "id": "restart_monitor",
            "label": "Restart Monitor",
            "description": "Restart the stream monitor to apply config changes",
            "button_label": "Restart Monitor",
            "button_variant": "filled",
            "button_color": "orange",
        },
        {
            "id": "start_debug_server",
            "label": "Start Debug Server",
            "description": "Start the debug dashboard HTTP server",
            "button_label": "Start Server",
            "button_variant": "filled",
            "button_color": "green",
        },
        {
            "id": "stop_debug_server",
            "label": "Stop Debug Server",
            "description": "Stop the debug dashboard HTTP server",
            "button_label": "Stop Server",
            "button_variant": "filled",
            "button_color": "red",
        },
        {
            "id": "status",
            "label": "Status",
            "description": "Check monitor and debug server status",
            "button_label": "Check Status",
            "button_variant": "filled",
            "button_color": "blue",
        },
    ]

    # -- Initialisation --------------------------------------------------------

    def __init__(self):
        attempt_autostart(_monitor)

    # -- Action dispatcher -----------------------------------------------------

    def run(self, action: str, params: dict, context: dict):
        """Execute a plugin action and return a result dict."""
        logger_ctx = context.get("logger", logger)
        settings   = context.get("settings", {})

        # -- restart_monitor ---------------------------------------------------
        if action == "restart_monitor":
            try:
                if _monitor.is_running():
                    _monitor.stop()
                    # Brief pause so Redis keys are cleaned up before restart
                    time.sleep(0.5)

                if _monitor.start(settings=settings):
                    return {"status": "success", "message": "Stream monitor restarted with current settings"}
                return {"status": "error", "message": "Failed to start stream monitor"}
            except Exception as e:
                logger_ctx.error(f"Error restarting monitor: {e}", exc_info=True)
                return {"status": "error", "message": f"Failed to restart monitor: {str(e)}"}

        # -- start_debug_server ------------------------------------------------
        elif action == "start_debug_server":
            server = get_current_server()
            if server and server.is_running():
                return {
                    "status": "error",
                    "message": f"Debug server is already running on http://{server.host}:{server.port}/debug",
                }

            # Check Redis for remote instance
            redis_client = get_redis_client()
            if redis_client and read_redis_flag(redis_client, REDIS_KEY_RUNNING):
                rhost = redis_decode(redis_client.get(REDIS_KEY_HOST)) or DEFAULT_HOST
                rport = redis_decode(redis_client.get(REDIS_KEY_PORT)) or str(DEFAULT_PORT)
                return {
                    "status": "error",
                    "message": f"Debug server is already running on http://{rhost}:{rport}/debug (another worker)",
                }

            port = int(settings.get("port", DEFAULT_PORT))
            host = normalize_host(settings.get("host", DEFAULT_HOST), DEFAULT_HOST)
            server = DebugServer(_monitor, port=port, host=host)
            if server.start(settings=settings):
                return {
                    "status": "success",
                    "message": f"Debug server started on http://{host}:{port}/debug",
                }
            return {"status": "error", "message": "Failed to start debug server. Port may be in use."}

        # -- stop_debug_server -------------------------------------------------
        elif action == "stop_debug_server":
            # Flag manual stop so autostart won't re-launch during this runtime.
            # The flag is cleared on fresh Dispatcharr boot (CLEANUP_REDIS_KEYS).
            redis_client = get_redis_client()
            if redis_client:
                try:
                    from .config import REDIS_KEY_MANUAL_STOP
                    redis_client.set(REDIS_KEY_MANUAL_STOP, "1")
                except Exception:
                    pass

            server = get_current_server()
            if server and server.is_running():
                server.stop()
                return {"status": "success", "message": "Debug server stopped"}

            # Signal remote worker
            if redis_client and read_redis_flag(redis_client, REDIS_KEY_RUNNING):
                redis_client.set(REDIS_KEY_STOP, "1")
                for _ in range(50):
                    if not read_redis_flag(redis_client, REDIS_KEY_RUNNING):
                        return {"status": "success", "message": "Debug server stopped"}
                    time.sleep(0.1)
                redis_client.delete(REDIS_KEY_RUNNING, REDIS_KEY_HOST, REDIS_KEY_PORT, REDIS_KEY_STOP)
                return {"status": "warning", "message": "Stop signal sent but server did not confirm. Redis keys cleared."}

            return {"status": "error", "message": "Debug server is not running"}

        # -- status ------------------------------------------------------------
        elif action == "status":
            monitor_running = _monitor.is_running()
            server = get_current_server()
            server_running = server and server.is_running()

            redis_client = get_redis_client()
            remote_monitor = False
            remote_server = False
            if redis_client:
                try:
                    remote_monitor = read_redis_flag(redis_client, REDIS_KEY_MONITOR)
                except Exception:
                    pass
                try:
                    remote_server = read_redis_flag(redis_client, REDIS_KEY_RUNNING)
                except Exception:
                    pass

            parts = []
            if monitor_running or remote_monitor:
                parts.append("Monitor: running")
            else:
                parts.append("Monitor: stopped")

            if server_running:
                parts.append(f"Debug server: http://{server.host}:{server.port}/debug")
            elif remote_server:
                rhost = redis_decode(redis_client.get(REDIS_KEY_HOST)) or DEFAULT_HOST
                rport = redis_decode(redis_client.get(REDIS_KEY_PORT)) or str(DEFAULT_PORT)
                parts.append(f"Debug server: http://{rhost}:{rport}/debug (another worker)")
            else:
                parts.append("Debug server: stopped")

            return {
                "status": "success",
                "message": " | ".join(parts),
                "running": monitor_running or remote_monitor,
            }

        return {"status": "error", "message": f"Unknown action: {action}"}

    def stop(self, context: dict):
        """Called when the plugin is disabled or Dispatcharr is shutting down.

        After a ``force_reload`` the module-level ``_monitor`` and
        ``get_current_server()`` references point to *new* (idle) instances
        because the module was re-imported.  The *old* running daemon threads
        are still alive but unreachable by direct reference.  We fall back to
        Redis signaling so the old poll loops detect the stop flag and exit.
        """
        stopped_monitor = False
        if _monitor.is_running():
            logger.info("Plugin stopping, shutting down monitor")
            _monitor.stop()
            stopped_monitor = True

        server = get_current_server()
        stopped_server = False
        if server and server.is_running():
            logger.info("Plugin stopping, shutting down debug server")
            server.stop()
            stopped_server = True

        # Redis fallback: signal orphaned threads from a previous module load
        if not stopped_monitor or not stopped_server:
            redis_client = get_redis_client()
            if redis_client:
                if not stopped_monitor and read_redis_flag(redis_client, REDIS_KEY_MONITOR):
                    logger.info("Plugin stopping, sending Redis stop signal to orphaned monitor")
                    redis_client.set(REDIS_KEY_STOP, "1")
                if not stopped_server and read_redis_flag(redis_client, REDIS_KEY_RUNNING):
                    logger.info("Plugin stopping, sending Redis stop signal to orphaned debug server")
                    redis_client.set(REDIS_KEY_STOP, "1")

        # Clear leader election and dedup keys so the next discovery can re-autostart
        try:
            rc = get_redis_client()
            if rc:
                from .config import REDIS_KEY_LEADER
                rc.delete(REDIS_KEY_LEADER, REDIS_KEY_LEADER + ":autostart_dedup")
        except Exception:
            pass

"""Emby Stream Cleanup - package root.

Dispatcharr discovers the plugin by importing this package and looking for
the ``Plugin`` class.  Webhook handling, server management, and auto-start
logic live in their own modules; this file only contains the plugin API.
"""

import logging
import time

from .config import (
    PLUGIN_CONFIG, PLUGIN_FIELDS,
    REDIS_KEY_RUNNING, REDIS_KEY_HOST, REDIS_KEY_PORT, REDIS_KEY_STOP,
    DEFAULT_PORT, DEFAULT_HOST,
)
from .handler import WebhookHandler
from .server import WebhookServer, get_current_server
from .autostart import attempt_autostart
from .utils import get_redis_client, read_redis_flag, normalize_host, redis_decode

logger = logging.getLogger(__name__)


class Plugin:
    """Dispatcharr Plugin - Emby stream cleanup via webhooks."""

    name        = PLUGIN_CONFIG["name"]
    description = PLUGIN_CONFIG["description"]
    version     = PLUGIN_CONFIG["version"]
    author      = PLUGIN_CONFIG["author"]

    fields  = PLUGIN_FIELDS

    actions = [
        {
            "id": "start_server",
            "label": "Start Webhook Server",
            "description": "Start the HTTP webhook server",
            "button_label": "Start Server",
            "button_variant": "filled",
            "button_color": "green",
        },
        {
            "id": "stop_server",
            "label": "Stop Webhook Server",
            "description": "Stop the HTTP webhook server",
            "button_label": "Stop Server",
            "button_variant": "filled",
            "button_color": "red",
        },
        {
            "id": "restart_server",
            "label": "Restart Webhook Server",
            "description": "Restart the HTTP webhook server",
            "button_label": "Restart Server",
            "button_variant": "filled",
            "button_color": "orange",
        },
        {
            "id": "server_status",
            "label": "Server Status",
            "description": "Check if the webhook server is running",
            "button_label": "Check Status",
            "button_variant": "filled",
            "button_color": "blue",
        },
    ]

    # ── Initialisation ───────────────────────────────────────────────────────

    def __init__(self):
        self.handler = WebhookHandler()
        attempt_autostart(self.handler)

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _get_redis_server_state(self):
        """Return (redis_client, server_running, server_host, server_port)."""
        redis_client = get_redis_client()
        server_running = False
        server_host = None
        server_port = None

        try:
            if redis_client:
                server_running = read_redis_flag(redis_client, REDIS_KEY_RUNNING)
                if server_running:
                    server_host = redis_decode(redis_client.get(REDIS_KEY_HOST)) or DEFAULT_HOST
                    server_port = redis_decode(redis_client.get(REDIS_KEY_PORT)) or str(DEFAULT_PORT)
        except Exception as e:
            logger.debug(f"Could not read Redis server state: {e}")

        return redis_client, server_running, server_host, server_port

    # ── Action dispatcher ────────────────────────────────────────────────────

    def run(self, action: str, params: dict, context: dict):
        """Execute a plugin action and return a result dict."""
        logger_ctx = context.get("logger", logger)
        settings   = context.get("settings", {})

        redis_client, server_running_redis, server_host, server_port = self._get_redis_server_state()
        current_server = get_current_server()

        # ── start_server ─────────────────────────────────────────────────
        if action == "start_server":
            try:
                import gevent  # noqa: F401
                from gevent import pywsgi  # noqa: F401
            except ImportError:
                return {
                    "status": "error",
                    "message": "gevent is not installed (unexpected - it is a Dispatcharr dependency)",
                }

            try:
                port = int(settings.get("port", DEFAULT_PORT))
                host = normalize_host(settings.get("host", DEFAULT_HOST), DEFAULT_HOST)
                logger_ctx.info(f"Starting webhook server with host='{host}', port={port}")

                if server_running_redis:
                    return {
                        "status": "error",
                        "message": f"Webhook server is already running on http://{server_host}:{server_port}/webhook",
                    }
                if current_server and current_server.is_running():
                    return {
                        "status": "error",
                        "message": f"Webhook server is already running on http://{current_server.host}:{current_server.port}/webhook",
                    }

                server = WebhookServer(self.handler, port=port, host=host)
                if server.start(settings=settings):
                    return {
                        "status": "success",
                        "message": "Webhook server started successfully",
                        "webhook_url": f"http://{host}:{port}/webhook",
                        "health_check": f"http://{host}:{port}/health",
                    }
                return {
                    "status": "error",
                    "message": "Failed to start webhook server. Port may already be in use.",
                }

            except Exception as e:
                logger_ctx.error(f"Error starting webhook server: {e}", exc_info=True)
                return {"status": "error", "message": f"Failed to start server: {str(e)}"}

        # ── stop_server ──────────────────────────────────────────────────
        elif action == "stop_server":
            try:
                if current_server and current_server.is_running():
                    if current_server.stop():
                        return {"status": "success", "message": "Webhook server stopped successfully"}

                if redis_client:
                    try:
                        logger_ctx.info("Sending stop signal via Redis")
                        redis_client.set(REDIS_KEY_STOP, "1")

                        for _ in range(50):  # wait up to 5 s
                            if not read_redis_flag(redis_client, REDIS_KEY_RUNNING):
                                logger_ctx.info("Server confirmed shutdown via Redis")
                                return {"status": "success", "message": "Webhook server stopped successfully"}
                            time.sleep(0.1)

                        logger_ctx.warning("Server did not confirm shutdown within 5s, force-cleaning Redis keys")
                        redis_client.delete(REDIS_KEY_RUNNING, REDIS_KEY_HOST, REDIS_KEY_PORT, REDIS_KEY_STOP)
                        return {
                            "status": "warning",
                            "message": "Stop signal sent but server did not confirm. Redis keys cleared.",
                        }
                    except Exception as e:
                        return {"status": "error", "message": f"Failed to signal stop: {str(e)}"}

                return {"status": "error", "message": "No running server found"}

            except Exception as e:
                logger_ctx.error(f"Error stopping webhook server: {e}", exc_info=True)
                return {"status": "error", "message": f"Failed to stop server: {str(e)}"}

        # ── restart_server ───────────────────────────────────────────────
        elif action == "restart_server":
            try:
                if current_server and current_server.is_running():
                    current_server.stop()

                if redis_client:
                    try:
                        redis_client.set(REDIS_KEY_STOP, "1")
                        stopped = False
                        for _ in range(50):
                            if not read_redis_flag(redis_client, REDIS_KEY_RUNNING):
                                stopped = True
                                break
                            time.sleep(0.1)
                        if not stopped:
                            logger_ctx.warning("Server did not confirm shutdown within 5s during restart")
                            redis_client.delete(REDIS_KEY_RUNNING, REDIS_KEY_HOST, REDIS_KEY_PORT, REDIS_KEY_STOP)
                    except Exception as e:
                        return {"status": "error", "message": f"Failed to stop server: {str(e)}"}

                time.sleep(0.5)

                if redis_client:
                    try:
                        redis_client.delete(REDIS_KEY_STOP)
                    except Exception:
                        pass

                time.sleep(0.5)

                port = int(settings.get("port", DEFAULT_PORT))
                host = normalize_host(settings.get("host", DEFAULT_HOST), DEFAULT_HOST)

                if redis_client and read_redis_flag(redis_client, REDIS_KEY_RUNNING):
                    return {"status": "error", "message": "Server is still running after stop attempt"}

                server = WebhookServer(self.handler, port=port, host=host)
                if server.start(settings=settings):
                    return {
                        "status": "success",
                        "message": "Webhook server restarted successfully",
                        "webhook_url": f"http://{host}:{port}/webhook",
                    }
                return {"status": "error", "message": "Server stopped but failed to restart"}

            except Exception as e:
                logger_ctx.error(f"Error restarting webhook server: {e}", exc_info=True)
                return {"status": "error", "message": f"Failed to restart server: {str(e)}"}

        # ── server_status ────────────────────────────────────────────────
        elif action == "server_status":
            if current_server and current_server.is_running():
                return {
                    "status": "success",
                    "message": f"Webhook server is running on http://{current_server.host}:{current_server.port}/webhook",
                    "running": True,
                }
            if server_running_redis:
                return {
                    "status": "success",
                    "message": f"Webhook server is running on http://{server_host}:{server_port}/webhook (another worker)",
                    "running": True,
                }
            return {
                "status": "success",
                "message": "Webhook server is not running",
                "running": False,
            }

        return {"status": "error", "message": f"Unknown action: {action}"}

    def stop(self, context: dict):
        """Called when the plugin is disabled or Dispatcharr is shutting down."""
        current_server = get_current_server()
        if current_server and current_server.is_running():
            logger.info("Plugin stopping — shutting down webhook server")
            current_server.stop()

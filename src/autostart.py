"""Auto-start logic for the Emby Stream Cleanup webhook server.

Uses Redis leader election (SET NX EX) so only one uWSGI worker starts the
server, even across multiple processes.

  1. Each worker calls ``attempt_autostart()`` from ``Plugin.__init__``.
  2. Background thread waits for Django ORM, reads plugin config.
  3. Races all workers with SET NX EX on a leader key.
  4. Winner clears stale state and starts the WebhookServer.
"""

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

# Per-process guard: only one autostart thread may be spawned per process.
_autostart_launched = False
_autostart_lock = threading.Lock()

_STARTUP_WAIT = 5   # seconds before the first config-read attempt
_RETRY_DELAY  = 3   # seconds between subsequent attempts
_MAX_ATTEMPTS = 8   # total attempts to read PluginConfig from the DB


def attempt_autostart(handler) -> None:
    """Entry point from ``Plugin.__init__``.

    Spawns a daemon thread (at most once per OS process) that races via Redis
    NX to become the autostart leader and start the webhook server.
    """
    global _autostart_launched
    with _autostart_lock:
        if _autostart_launched:
            logger.debug("Emby stream cleanup: auto-start already launched in this process, skipping")
            return
        _autostart_launched = True

    threading.Thread(
        target=_autostart_worker,
        args=(handler,),
        daemon=True,
        name="emby-cleanup-autostart",
    ).start()


def cleanup_stale_state(redis_client) -> None:
    """Delete plugin Redis keys left over from a previous container lifecycle."""
    from .config import CLEANUP_REDIS_KEYS
    try:
        if redis_client:
            deleted = redis_client.delete(*CLEANUP_REDIS_KEYS)
            if deleted:
                logger.info(f"Startup cleanup: removed {deleted} stale plugin Redis key(s)")
            else:
                logger.debug("Startup cleanup: no stale Redis keys found")
    except Exception as e:
        logger.warning(f"Startup cleanup failed: {e}")


def _autostart_worker(handler) -> None:
    """Background thread body."""
    from .config import (
        PLUGIN_CONFIG, REDIS_KEY_LEADER, LEADER_TTL,
        DEFAULT_PORT, DEFAULT_HOST, AUTO_START_DEFAULT, PLUGIN_DB_KEY,
    )
    from .utils import get_redis_client, normalize_host

    # Try both key forms (underscore and hyphen)
    _plugin_keys = [PLUGIN_DB_KEY, PLUGIN_DB_KEY.replace('_', '-')]

    settings_dict: dict = {}
    auto_start_enabled = False

    for attempt in range(_MAX_ATTEMPTS):
        time.sleep(_STARTUP_WAIT if attempt == 0 else _RETRY_DELAY)
        try:
            from apps.plugins.models import PluginConfig
            config = None
            for _key in _plugin_keys:
                config = PluginConfig.objects.filter(key=_key).first()
                if config is not None:
                    break
            if config is None:
                logger.debug(
                    f"Emby stream cleanup: PluginConfig not found yet "
                    f"(attempt {attempt + 1}/{_MAX_ATTEMPTS}, tried keys: {_plugin_keys})"
                )
                continue
            settings_dict = config.settings or {}
            auto_start_enabled = bool(
                config.enabled
                and settings_dict.get('auto_start', AUTO_START_DEFAULT)
            )
            logger.debug(
                f"Emby stream cleanup: auto-start config read on attempt {attempt + 1}: "
                f"plugin_enabled={config.enabled}, auto_start={auto_start_enabled}"
            )
            break
        except Exception as e:
            logger.debug(
                f"Emby stream cleanup: auto-start attempt {attempt + 1} could not read config: {e}"
            )
    else:
        logger.warning(
            "Emby stream cleanup: could not read plugin config after all attempts, aborting auto-start"
        )
        return

    if not auto_start_enabled:
        logger.debug("Emby stream cleanup: auto-start disabled in settings")
        return

    # ── Leader election via Redis SET NX ─────────────────────────────────────
    redis_client = get_redis_client()
    if redis_client is None:
        logger.warning("Emby stream cleanup: cannot connect to Redis, aborting auto-start")
        return

    worker_id = f"{os.getpid()}-{threading.get_ident()}"
    won = redis_client.set(REDIS_KEY_LEADER, worker_id, nx=True, ex=LEADER_TTL)
    if not won:
        logger.debug("Emby stream cleanup: another worker won leader election, skipping auto-start")
        return

    logger.debug(f"Emby stream cleanup: won leader election (worker {worker_id})")

    # ── Clean stale state then start server ──────────────────────────────────
    cleanup_stale_state(redis_client)

    port = int(settings_dict.get('port', DEFAULT_PORT))
    host = normalize_host(
        settings_dict.get('host', DEFAULT_HOST),
        DEFAULT_HOST,
    )

    from .server import WebhookServer
    server = WebhookServer(handler, port=port, host=host)
    if server.start(settings=settings_dict):
        logger.info(
            f"Emby stream cleanup: auto-start successful on http://{host}:{port}/webhook"
        )
    else:
        try:
            redis_client.delete(REDIS_KEY_LEADER)
        except Exception:
            pass
        logger.warning(
            "Emby stream cleanup: auto-start failed to start server. "
            "Use 'Start Server' button to start manually."
        )

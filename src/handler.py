"""Webhook handler and stream cleanup logic.

Tracks active Emby viewers per channel using Redis sets keyed by
PlaySessionId.  When the last viewer stops watching a channel, schedules
a delayed cleanup that terminates the matching Dispatcharr client
connection after a configurable timeout.
"""

import logging
import socket
import time

from .config import (
    REDIS_KEY_VIEWERS_PREFIX, VIEWER_SET_TTL, DEFAULT_CLEANUP_TIMEOUT,
)
from .utils import get_redis_client, redis_decode

logger = logging.getLogger(__name__)

# Track pending cleanup greenlets so we can cancel if a viewer reconnects.
# Maps channel_number (str) -> gevent.Greenlet
_pending_cleanups = {}

# Metadata for pending cleanups (for debug page).
# Maps channel_number (str) -> {"scheduled_at": float, "timeout": int, "fires_at": float}
_cleanup_metadata = {}


class WebhookHandler:
    """Processes Emby webhook events and manages stream cleanup."""

    def handle_webhook(self, event_data, settings):
        """Process an incoming Emby webhook payload.

        Returns a dict suitable for JSON serialisation as the HTTP response.
        """
        event = event_data.get("Event", "")
        if event not in ("playback.start", "playback.stop"):
            return {"status": "ignored", "reason": f"Unhandled event: {event}"}

        item = event_data.get("Item", {})
        if item.get("Type") != "TvChannel":
            return {"status": "ignored", "reason": "Not a TvChannel item"}

        channel_number = item.get("ChannelNumber") or item.get("Number")
        if not channel_number:
            return {"status": "ignored", "reason": "No channel number in payload"}
        channel_number = str(channel_number)

        playback_info = event_data.get("PlaybackInfo", {})
        session_id = playback_info.get("PlaySessionId")
        if not session_id:
            return {"status": "ignored", "reason": "No PlaySessionId in payload"}

        user_name = event_data.get("User", {}).get("Name", "unknown")
        item_name = item.get("Name", "unknown")

        if event == "playback.start":
            return self._handle_start(channel_number, session_id, user_name, item_name, settings)
        else:
            return self._handle_stop(channel_number, session_id, user_name, item_name, settings)

    def _handle_start(self, channel_number, session_id, user_name, item_name, settings):
        """Register a new viewer on the channel."""
        redis_client = get_redis_client()
        if not redis_client:
            logger.error("Cannot track viewer: Redis unavailable")
            return {"status": "error", "reason": "Redis unavailable"}

        viewer_key = f"{REDIS_KEY_VIEWERS_PREFIX}{channel_number}"
        redis_client.sadd(viewer_key, session_id)
        redis_client.expire(viewer_key, VIEWER_SET_TTL)

        viewer_count = redis_client.scard(viewer_key)
        logger.info(
            f"Viewer started: user={user_name}, channel={channel_number} ({item_name}), "
            f"session={session_id[:12]}..., viewers={viewer_count}"
        )

        # Cancel any pending cleanup for this channel since someone is watching
        self._cancel_pending_cleanup(channel_number)

        return {
            "status": "ok",
            "event": "playback.start",
            "channel": channel_number,
            "viewers": viewer_count,
        }

    def _handle_stop(self, channel_number, session_id, user_name, item_name, settings):
        """Remove a viewer and schedule cleanup if no viewers remain."""
        redis_client = get_redis_client()
        if not redis_client:
            logger.error("Cannot track viewer: Redis unavailable")
            return {"status": "error", "reason": "Redis unavailable"}

        viewer_key = f"{REDIS_KEY_VIEWERS_PREFIX}{channel_number}"
        redis_client.srem(viewer_key, session_id)

        viewer_count = redis_client.scard(viewer_key)
        logger.info(
            f"Viewer stopped: user={user_name}, channel={channel_number} ({item_name}), "
            f"session={session_id[:12]}..., viewers_remaining={viewer_count}"
        )

        if viewer_count == 0:
            timeout = int(settings.get("cleanup_timeout", DEFAULT_CLEANUP_TIMEOUT))
            emby_identifier = (settings.get("emby_identifier") or "").strip()

            if not emby_identifier:
                logger.warning(
                    f"Channel {channel_number} has 0 viewers but no emby_identifier configured — "
                    f"skipping cleanup. Set the Emby Identifier in plugin settings."
                )
                return {
                    "status": "ok",
                    "event": "playback.stop",
                    "channel": channel_number,
                    "viewers": 0,
                    "cleanup": "skipped",
                    "reason": "No emby_identifier configured",
                }

            logger.info(
                f"Channel {channel_number} has 0 viewers, scheduling cleanup in {timeout}s"
            )
            self._schedule_cleanup(channel_number, timeout, emby_identifier)

            return {
                "status": "ok",
                "event": "playback.stop",
                "channel": channel_number,
                "viewers": 0,
                "cleanup": "scheduled",
                "timeout_seconds": timeout,
            }

        return {
            "status": "ok",
            "event": "playback.stop",
            "channel": channel_number,
            "viewers": viewer_count,
            "cleanup": "not_needed",
        }

    def _schedule_cleanup(self, channel_number, timeout, emby_identifier):
        """Spawn a gevent greenlet to run cleanup after *timeout* seconds."""
        self._cancel_pending_cleanup(channel_number)

        try:
            from gevent import spawn, sleep

            now = time.time()
            _cleanup_metadata[channel_number] = {
                "scheduled_at": now,
                "timeout": timeout,
                "fires_at": now + timeout,
            }

            def _delayed_cleanup():
                try:
                    sleep(timeout)
                    # Re-check viewer count — someone may have started watching
                    redis_client = get_redis_client()
                    if redis_client:
                        viewer_key = f"{REDIS_KEY_VIEWERS_PREFIX}{channel_number}"
                        current_viewers = redis_client.scard(viewer_key)
                        if current_viewers > 0:
                            logger.info(
                                f"Cleanup cancelled for channel {channel_number}: "
                                f"{current_viewers} viewer(s) reconnected during timeout"
                            )
                            return

                    self._execute_cleanup(channel_number, emby_identifier)
                except Exception as e:
                    logger.error(f"Error in delayed cleanup for channel {channel_number}: {e}", exc_info=True)
                finally:
                    _pending_cleanups.pop(channel_number, None)
                    _cleanup_metadata.pop(channel_number, None)

            greenlet = spawn(_delayed_cleanup)
            _pending_cleanups[channel_number] = greenlet

        except ImportError:
            # gevent not available — fall through to synchronous (should not happen
            # since server.py already imports gevent, but handle gracefully)
            logger.warning("gevent not available for delayed cleanup, executing immediately")
            self._execute_cleanup(channel_number, emby_identifier)

    def _cancel_pending_cleanup(self, channel_number):
        """Cancel a pending cleanup greenlet for the given channel, if any."""
        greenlet = _pending_cleanups.pop(channel_number, None)
        _cleanup_metadata.pop(channel_number, None)
        if greenlet is not None:
            try:
                greenlet.kill(block=False)
                logger.debug(f"Cancelled pending cleanup for channel {channel_number}")
            except Exception:
                pass

    def get_debug_state(self, emby_identifier=""):
        """Return current tracking state for the debug endpoint.

        Includes Dispatcharr client details and identifier matching info so
        the debug page can clearly show what would/wouldn't be terminated.
        """
        redis_client = get_redis_client()
        now = time.time()

        channels = {}

        # Resolve emby_identifier hostnames to IPs once
        identifier_lower = emby_identifier.lower().strip() if emby_identifier else ""
        resolved_ips = set()
        if identifier_lower:
            try:
                resolved_ips = {
                    info[4][0]
                    for info in socket.getaddrinfo(emby_identifier.strip(), None)
                }
            except (socket.gaierror, OSError):
                pass

        # Build a map of channel_number -> Channel model info
        channel_model_cache = {}
        try:
            from apps.channels.models import Channel
            channel_model_cache = {
                str(ch.channel_number): {"name": ch.name, "uuid": str(ch.uuid)}
                for ch in Channel.objects.only("channel_number", "name", "uuid")
            }
        except Exception:
            pass

        # Scan Redis for all viewer sets
        if redis_client:
            try:
                prefix = REDIS_KEY_VIEWERS_PREFIX
                for key in redis_client.scan_iter(match=f"{prefix}*"):
                    key_str = key.decode('utf-8') if isinstance(key, bytes) else key
                    channel_number = key_str[len(prefix):]
                    sessions = redis_client.smembers(key_str)
                    ttl = redis_client.ttl(key_str)

                    model_info = channel_model_cache.get(channel_number, {})

                    channels[channel_number] = {
                        "viewers": len(sessions),
                        "sessions": sorted(
                            s.decode('utf-8') if isinstance(s, bytes) else s
                            for s in sessions
                        ),
                        "ttl": ttl if ttl > 0 else None,
                        "channel_name": model_info.get("name", ""),
                        "channel_uuid": model_info.get("uuid", ""),
                        "dispatcharr_clients": [],
                    }
            except Exception as e:
                logger.debug(f"Debug state: error scanning Redis: {e}")

        # Enrich with Dispatcharr client data for each tracked channel
        if redis_client:
            try:
                from apps.proxy.ts_proxy.redis_keys import RedisKeys
            except ImportError:
                RedisKeys = None

            if RedisKeys:
                for ch_num, ch_data in channels.items():
                    uuid = ch_data.get("channel_uuid")
                    if not uuid:
                        continue
                    try:
                        client_ids = redis_client.smembers(RedisKeys.clients(uuid)) or []
                        for raw_id in client_ids:
                            client_id = raw_id.decode("utf-8") if isinstance(raw_id, bytes) else str(raw_id)
                            meta_key = RedisKeys.client_metadata(uuid, client_id)
                            cdata = redis_client.hgetall(meta_key)
                            if not cdata:
                                continue

                            ip = redis_decode(cdata.get(b"ip_address") or cdata.get("ip_address"))
                            username = redis_decode(cdata.get(b"username") or cdata.get("username"))
                            user_agent = redis_decode(cdata.get(b"user_agent") or cdata.get("user_agent"))
                            connected_at = redis_decode(cdata.get(b"connected_at") or cdata.get("connected_at"))
                            last_active = redis_decode(cdata.get(b"last_active") or cdata.get("last_active"))
                            bytes_sent = redis_decode(cdata.get(b"bytes_sent") or cdata.get("bytes_sent"))

                            # Determine if this client matches the emby identifier
                            is_match = False
                            match_reason = ""
                            if identifier_lower:
                                if ip.lower() == identifier_lower:
                                    is_match = True
                                    match_reason = "IP match"
                                elif username.lower() == identifier_lower:
                                    is_match = True
                                    match_reason = "username match"
                                elif ip in resolved_ips:
                                    is_match = True
                                    match_reason = "hostname resolves to IP"

                            # Calculate connected duration
                            connected_duration = ""
                            try:
                                if connected_at:
                                    dur = now - float(connected_at)
                                    if dur >= 3600:
                                        connected_duration = f"{int(dur // 3600)}h {int((dur % 3600) // 60)}m"
                                    elif dur >= 60:
                                        connected_duration = f"{int(dur // 60)}m {int(dur % 60)}s"
                                    else:
                                        connected_duration = f"{int(dur)}s"
                            except (ValueError, TypeError):
                                pass

                            ch_data["dispatcharr_clients"].append({
                                "client_id": client_id,
                                "ip": ip,
                                "username": username,
                                "user_agent": user_agent,
                                "connected_duration": connected_duration,
                                "last_active": last_active,
                                "bytes_sent": bytes_sent,
                                "is_emby_match": is_match,
                                "match_reason": match_reason,
                            })
                    except Exception as e:
                        logger.debug(f"Debug state: error reading clients for channel {ch_num}: {e}")

        # Add pending cleanup info
        pending = {}
        for ch, meta in _cleanup_metadata.items():
            remaining = meta["fires_at"] - now
            pending[ch] = {
                "timeout": meta["timeout"],
                "remaining_seconds": round(max(0, remaining), 1),
                "scheduled_at": meta["scheduled_at"],
            }
            if ch in channels:
                channels[ch]["cleanup_pending"] = True
                channels[ch]["cleanup_remaining"] = round(max(0, remaining), 1)
            else:
                channels[ch] = {
                    "viewers": 0,
                    "sessions": [],
                    "channel_name": "",
                    "channel_uuid": "",
                    "dispatcharr_clients": [],
                    "cleanup_pending": True,
                    "cleanup_remaining": round(max(0, remaining), 1),
                }

        return {
            "channels": channels,
            "pending_cleanups": pending,
            "identifier_configured": bool(identifier_lower),
            "resolved_ips": sorted(resolved_ips) if resolved_ips else [],
        }

    def _execute_cleanup(self, channel_number, emby_identifier):
        """Terminate Dispatcharr client connections for Emby on the given channel."""
        try:
            from apps.channels.models import Channel

            try:
                channel = Channel.objects.get(channel_number=float(channel_number))
            except Channel.DoesNotExist:
                logger.warning(f"Cleanup: channel number {channel_number} not found in Dispatcharr")
                return
            except (ValueError, Channel.MultipleObjectsReturned) as e:
                logger.warning(f"Cleanup: error looking up channel {channel_number}: {e}")
                return

            channel_uuid = str(channel.uuid)
            self._stop_matching_clients(channel_uuid, channel_number, emby_identifier)

        except ImportError:
            logger.error("Cannot import Dispatcharr models — is the plugin running inside Dispatcharr?")

    def _stop_matching_clients(self, channel_uuid, channel_number, emby_identifier):
        """Find and stop Dispatcharr clients matching the Emby identifier."""
        redis_client = get_redis_client()
        if not redis_client:
            logger.error("Cleanup: Redis unavailable, cannot stop clients")
            return

        try:
            from apps.proxy.ts_proxy.redis_keys import RedisKeys
            from apps.proxy.ts_proxy.services.channel_service import ChannelService
        except ImportError:
            logger.error("Cannot import Dispatcharr proxy modules")
            return

        client_set_key = RedisKeys.clients(channel_uuid)
        client_ids = redis_client.smembers(client_set_key) or []

        if not client_ids:
            logger.debug(f"Cleanup: no active clients on channel {channel_number} ({channel_uuid})")
            return

        stopped_count = 0
        for raw_id in client_ids:
            client_id = raw_id.decode("utf-8") if isinstance(raw_id, bytes) else str(raw_id)
            meta_key = RedisKeys.client_metadata(channel_uuid, client_id)
            client_data = redis_client.hgetall(meta_key)

            if not client_data:
                continue

            ip_address = redis_decode(client_data.get(b"ip_address") or client_data.get("ip_address"))
            username = redis_decode(client_data.get(b"username") or client_data.get("username"))
            user_agent = redis_decode(client_data.get(b"user_agent") or client_data.get("user_agent"))

            # Match against emby_identifier (check IP, resolved IP, and username)
            identifier_lower = emby_identifier.lower()
            match = (
                ip_address.lower() == identifier_lower
                or username.lower() == identifier_lower
            )
            # If identifier looks like a hostname (not purely an IP), resolve it
            if not match:
                try:
                    resolved_ips = {
                        info[4][0]
                        for info in socket.getaddrinfo(emby_identifier, None)
                    }
                    match = ip_address in resolved_ips
                except (socket.gaierror, OSError):
                    pass
            if not match:
                continue

            logger.info(
                f"Cleanup: stopping client {client_id} on channel {channel_number} "
                f"(ip={ip_address}, user={username}, ua={user_agent})"
            )

            try:
                result = ChannelService.stop_client(channel_uuid, client_id)
                if result.get("status") == "success":
                    stopped_count += 1
                    logger.info(f"Cleanup: successfully stopped client {client_id}")
                else:
                    logger.warning(f"Cleanup: stop_client returned: {result}")
            except Exception as e:
                logger.error(f"Cleanup: error stopping client {client_id}: {e}", exc_info=True)

        if stopped_count > 0:
            logger.info(
                f"Cleanup complete: stopped {stopped_count} Emby client(s) on channel {channel_number}"
            )
        else:
            logger.debug(
                f"Cleanup: no matching Emby clients found on channel {channel_number} "
                f"for identifier '{emby_identifier}'"
            )

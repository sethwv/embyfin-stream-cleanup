"""Stream monitor that polls Dispatcharr client activity.

Periodically scans all active channels for clients matching the configured
client identifier(s).  When a matching client's ``last_active`` timestamp
exceeds the idle timeout, the connection is terminated via
``ChannelService.stop_client()``.

Optionally cross-references with an Emby/Jellyfin Sessions API to detect
orphaned connections that the media server failed to close.
"""

import json
import logging
import socket
import threading
import time
import urllib.request
import urllib.error

from .config import (
    DEFAULT_CLEANUP_TIMEOUT, DEFAULT_POLL_INTERVAL,
    REDIS_KEY_MONITOR, REDIS_KEY_STOP,
    HEARTBEAT_TTL,
)
from .utils import get_redis_client, read_redis_flag, redis_decode

logger = logging.getLogger(__name__)

# Channel states that indicate the stream is mid-failover or still starting up.
# Clients appear idle during these states because no data is flowing yet.
_GRACE_STATES = frozenset({"initializing", "connecting", "buffering", "waiting_for_clients"})

# NowPlayingItem.Type values that indicate a live TV stream.
# Emby uses "TvChannel"; Jellyfin may use either "TvChannel" or "LiveTvChannel".
_LIVE_TV_TYPES = frozenset({"TvChannel", "LiveTvChannel"})


def _get_failover_grace():
    """Return the failover grace period (seconds) from Dispatcharr proxy config.

    During a stream switch Dispatcharr allows up to
    ``FAILOVER_GRACE_PERIOD + BUFFERING_TIMEOUT`` before disconnecting
    clients.  We use the same window so we don't kill sessions that are
    just waiting for a new upstream to stabilise.
    """
    try:
        from apps.proxy.config import TSConfig
        settings = TSConfig.get_proxy_settings()
        failover = getattr(TSConfig, "FAILOVER_GRACE_PERIOD", 20)
        buffering = settings.get("buffering_timeout", 15)
        return failover + buffering
    except Exception:
        return 35  # safe default: 20 + 15


def _resolve_username(user_id_str, cache):
    """Resolve a Redis user_id string to a Django username.

    Uses *cache* (dict) to avoid repeated DB hits within one poll cycle.
    """
    try:
        uid = int(user_id_str)
        if uid <= 0:
            return ""
        if uid not in cache:
            from apps.accounts.models import User
            cache[uid] = User.objects.get(id=uid).username
        return cache[uid]
    except Exception:
        return ""


class StreamMonitor:
    """Background poller that watches Dispatcharr client activity and
    terminates idle connections matching the configured identifier."""

    def __init__(self):
        self._thread = None
        self._running = False
        self._settings = {}
        # Per-client tracking: {(channel_uuid, client_id): first_idle_ts}
        # Records when we first noticed a client was idle so we can
        # measure idle duration across poll cycles.
        self._idle_since = {}
        # Per-client orphan tracking: {(channel_uuid, client_id): first_orphan_ts}
        self._orphaned_since = {}
        # Snapshot for the debug page (updated each poll cycle)
        self._last_scan = {}
        self._last_scan_time = 0
        self._stopped_log = []  # recent terminations for debug display
        # Media server session state (updated each poll cycle)
        self._emby_active_count = None  # None=not configured, int=session count
        self._emby_error = None  # last error message, if any
        self._media_server_status = []  # per-server status dicts for debug page

    # ── Identifier helpers ───────────────────────────────────────────────────

    @staticmethod
    def _parse_identifiers(client_identifier):
        """Split a comma-separated identifier string into lowercased values."""
        if not client_identifier:
            return []
        return [v.strip().lower() for v in client_identifier.split(",") if v.strip()]

    @staticmethod
    def _resolve_identifiers(identifiers):
        """Resolve any hostnames in *identifiers* to IP addresses."""
        resolved = set()
        for ident in identifiers:
            try:
                for info in socket.getaddrinfo(ident, None):
                    resolved.add(info[4][0])
            except (socket.gaierror, OSError):
                pass
        return resolved

    @staticmethod
    def _match_client(ip, username, identifiers, resolved_ips):
        """Check if a client matches any configured identifier.
        Returns (matched: bool, reason: str)."""
        if "all" in identifiers:
            return True, "ALL (matches every client)"
        ip_lower = ip.lower()
        uname_lower = username.lower()
        for ident in identifiers:
            if ip_lower == ident:
                return True, f"IP match ({ident})"
            if uname_lower == ident:
                return True, f"username match ({ident})"
        if ip in resolved_ips:
            return True, "hostname resolves to IP"
        return False, ""

    # ── Media server session helpers ─────────────────────────────────────────

    def _get_media_server_configs(self):
        """Return a list of (url, api_key) tuples for all configured servers."""
        count = max(1, int(self._settings.get("media_server_count", 1)))
        servers = []
        for n in range(1, count + 1):
            suffix = f"_{n}" if n > 1 else ""
            url = (self._settings.get(f"media_server_url{suffix}") or "").strip().rstrip("/")
            key = (self._settings.get(f"media_server_api_key{suffix}") or "").strip()
            # Migrate legacy field names from single-server config
            if n == 1 and not url:
                url = (self._settings.get("emby_url") or "").strip().rstrip("/")
            if n == 1 and not key:
                key = (self._settings.get("emby_api_key") or "").strip()
            if url and key:
                servers.append((url, key))
        return servers

    @staticmethod
    def _detect_server_type(url):
        """Probe /System/Info/Public to determine Emby vs Jellyfin."""
        try:
            req = urllib.request.Request(
                f"{url}/System/Info/Public",
                headers={"Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                info = json.loads(resp.read().decode("utf-8"))
                # Jellyfin includes ProductName; Emby does not
                product = info.get("ProductName", "")
                if "jellyfin" in product.lower():
                    return "Jellyfin"
                return "Emby"
        except Exception:
            return None

    def _fetch_media_server_sessions(self):
        """Fetch active sessions from all configured Emby/Jellyfin servers.

        Returns a list of session dicts, or ``None`` if no servers are configured.
        Sets ``self._emby_error`` on failure.
        """
        servers = self._get_media_server_configs()
        if not servers:
            self._media_server_status = []
            return None

        all_sessions = []
        errors = []
        per_server = []
        for idx, (url, api_key) in enumerate(servers, 1):
            endpoint = f"{url}/Sessions"
            # Detect server type on first encounter or after error
            server_type = getattr(self, "_server_types", {}).get(url)
            if server_type is None:
                server_type = self._detect_server_type(url)
                if not hasattr(self, "_server_types"):
                    self._server_types = {}
                if server_type:
                    self._server_types[url] = server_type
            try:
                req = urllib.request.Request(endpoint, headers={
                    "Accept": "application/json",
                    # Emby accepts ?api_key; Jellyfin accepts X-Emby-Token header.
                    # Sending both ensures compatibility with either server.
                    "X-Emby-Token": api_key,
                })
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    sessions = data if isinstance(data, list) else []
                    live = [s for s in sessions
                            if s.get("NowPlayingItem", {}).get("Type") in _LIVE_TV_TYPES]
                    all_sessions.extend(live)
                    active = len(live)
                    per_server.append({"num": idx, "url": url, "type": server_type, "active": active, "error": None})
            except Exception as e:
                errors.append(f"Server {idx}: {e}")
                logger.warning(f"Failed to fetch media server sessions from server {idx}: {e}")
                per_server.append({"num": idx, "url": url, "type": server_type, "active": None, "error": str(e)})

        self._media_server_status = per_server
        self._emby_error = "; ".join(errors) if errors else None
        return all_sessions

    @staticmethod
    def _count_active_streams(sessions):
        """Count live TV sessions with an active NowPlayingItem."""
        if not sessions:
            return 0
        return sum(1 for s in sessions
                   if s.get("NowPlayingItem", {}).get("Type") in _LIVE_TV_TYPES)

    def _detect_orphans(self, scan_result, sessions, now, ChannelService):
        """Compare Dispatcharr matched connections against active media server
        sessions by channel number.  Connections on channels the media server
        is no longer watching are orphan candidates.

        Orphans must also be idle and are confirmed over multiple poll cycles
        before termination to avoid race conditions during channel switches.
        """
        # Build set of channel numbers the media server is actively watching
        active_channel_numbers = set()
        for s in (sessions or []):
            npi = s.get("NowPlayingItem", {})
            ch_num = npi.get("ChannelNumber")
            if ch_num:
                # Normalize: strip leading zeros, trailing .0
                ch_num = str(ch_num).strip()
                try:
                    num = float(ch_num)
                    ch_num = str(int(num)) if num == int(num) else ch_num
                except (ValueError, TypeError):
                    pass
                active_channel_numbers.add(ch_num)

        # Collect all matched clients across all channels (skip grace channels)
        all_matched = []
        for ch_uuid, ch_data in scan_result.items():
            if ch_data.get("in_grace"):
                continue
            for client in ch_data.get("clients", []):
                if client.get("is_target_match"):
                    all_matched.append((ch_uuid, ch_data, client))

        if not all_matched:
            return

        # Determine orphan candidates: clients on channels the media server
        # is NOT actively watching
        orphan_candidates = []
        non_orphans = []
        for item in all_matched:
            ch_num = item[1].get("channel_number", "")
            if ch_num in active_channel_numbers:
                non_orphans.append(item)
            else:
                orphan_candidates.append(item)

        # Clear tracking for non-orphans
        for ch_uuid, _, client in non_orphans:
            self._orphaned_since.pop((ch_uuid, client["client_id"]), None)

        if not orphan_candidates:
            return

        poll_interval = max(int(self._settings.get("poll_interval", DEFAULT_POLL_INTERVAL)), 1)
        # Require orphan candidates to be confirmed across multiple poll cycles
        confirm_threshold = poll_interval * 2

        for ch_uuid, ch_data, client in orphan_candidates:
            ck = (ch_uuid, client["client_id"])
            client["is_orphan"] = True

            # Never terminate a client that is actively receiving data
            idle = client.get("idle_seconds") or 0
            if idle < poll_interval:
                self._orphaned_since.pop(ck, None)
                continue

            if ck not in self._orphaned_since:
                self._orphaned_since[ck] = now
                logger.info(
                    f"Potential orphan: client {client['client_id']} on "
                    f"CH {ch_data.get('channel_number', '?')} "
                    f"(no matching media server session, idle {idle:.0f}s, "
                    f"connected {client.get('connected_duration', '?')})"
                )
                continue

            orphan_age = now - self._orphaned_since[ck]
            if orphan_age < confirm_threshold:
                continue  # not yet confirmed

            channel_number = ch_data.get("channel_number", "?")
            channel_name = ch_data.get("channel_name", "")
            logger.info(
                f"Terminating orphaned client {client['client_id']} on CH "
                f"{channel_number} ({channel_name}): "
                f"no active media server session for {orphan_age:.0f}s "
                f"(ip={client.get('ip', '?')}, user={client.get('username', '?')})"
            )
            try:
                result = ChannelService.stop_client(ch_uuid, client["client_id"])
                if result.get("status") == "success":
                    logger.info(f"Successfully terminated orphaned client {client['client_id']}")
                    self._stopped_log.append({
                        "time": now,
                        "channel": f"CH {channel_number} ({channel_name})",
                        "ip": client.get("ip", ""),
                        "username": client.get("username", ""),
                        "idle_seconds": round(client.get("idle_seconds") or 0),
                        "reason": "orphan",
                    })
                    if len(self._stopped_log) > 20:
                        self._stopped_log = self._stopped_log[-20:]
                else:
                    logger.warning(f"stop_client returned: {result}")
            except Exception as e:
                logger.error(f"Error stopping orphaned client: {e}", exc_info=True)
            self._orphaned_since.pop(ck, None)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def start(self, settings=None):
        """Start the background polling thread."""
        if self._running:
            logger.warning("Stream monitor is already running")
            return False

        self._settings = settings or {}
        self._running = True
        self._idle_since.clear()
        self._orphaned_since.clear()
        self._stopped_log.clear()
        self._emby_active_count = None
        self._emby_error = None

        # Mark as running in Redis (with heartbeat TTL so the key expires
        # if this process dies without cleaning up).
        redis_client = get_redis_client()
        if redis_client:
            redis_client.set(REDIS_KEY_MONITOR, "1", ex=HEARTBEAT_TTL)
            # Clear any stale stop signal
            redis_client.delete(REDIS_KEY_STOP)

        self._thread = threading.Thread(
            target=self._poll_loop,
            daemon=True,
            name="emby-stream-monitor",
        )
        self._thread.start()
        logger.info("Stream monitor started")
        return True

    def stop(self):
        """Stop the background polling thread."""
        if not self._running:
            return True
        self._running = False
        redis_client = get_redis_client()
        if redis_client:
            redis_client.delete(REDIS_KEY_MONITOR)
        # Thread will exit on next cycle check
        logger.info("Stream monitor stopping")
        return True

    def is_running(self):
        return self._running

    def update_settings(self, settings):
        """Update settings without restarting."""
        self._settings = settings or {}

    # ── Poll loop ────────────────────────────────────────────────────────────

    def _poll_loop(self):
        """Main polling loop. Runs in a daemon thread."""
        logger.info("Stream monitor poll loop started")
        while self._running:
            try:
                # Check for Redis stop signal (cross-worker shutdown)
                redis_client = get_redis_client()
                if redis_client and read_redis_flag(redis_client, REDIS_KEY_STOP):
                    logger.info("Stream monitor received stop signal via Redis")
                    self._running = False
                    redis_client.delete(REDIS_KEY_MONITOR, REDIS_KEY_STOP)
                    break

                # Refresh heartbeat so the key doesn't expire while we're alive
                if redis_client:
                    redis_client.set(REDIS_KEY_MONITOR, "1", ex=HEARTBEAT_TTL)

                self._poll_once()
            except Exception as e:
                logger.error(f"Stream monitor poll error: {e}", exc_info=True)

            interval = int(self._settings.get("poll_interval", DEFAULT_POLL_INTERVAL))
            interval = max(1, interval)
            time.sleep(interval)

        logger.info("Stream monitor poll loop exited")

    def _poll_once(self):
        """Single poll cycle: scan channels, check idle matched clients, terminate."""
        redis_client = get_redis_client()
        if not redis_client:
            return

        client_identifier = (self._settings.get("client_identifier") or "").strip()
        if not client_identifier:
            return

        identifiers = self._parse_identifiers(client_identifier)
        if not identifiers:
            return

        resolved_ips = self._resolve_identifiers(identifiers)
        timeout = int(self._settings.get("cleanup_timeout", DEFAULT_CLEANUP_TIMEOUT))
        now = time.time()

        # Read Dispatcharr proxy settings for failover grace period
        failover_grace = _get_failover_grace()

        try:
            from apps.proxy.ts_proxy.redis_keys import RedisKeys
            from apps.proxy.ts_proxy.services.channel_service import ChannelService
        except ImportError:
            return

        # Build channel model cache for names
        channel_model_cache = {}
        _user_cache = {}  # per-scan cache: user_id int -> username str
        try:
            from apps.channels.models import Channel
            for ch in Channel.objects.only("channel_number", "name", "uuid"):
                channel_model_cache[str(ch.uuid)] = {
                    "name": ch.name,
                    "number": str(int(ch.channel_number)) if ch.channel_number == int(ch.channel_number) else str(ch.channel_number),
                }
        except Exception:
            pass

        # Find all active channels by scanning channel_stream:* keys
        scan_result = {}
        active_keys = set()

        # Fetch media server sessions early so idle termination can cross-check
        sessions = self._fetch_media_server_sessions()
        media_server_channel_numbers = None
        if sessions is not None:
            media_server_channel_numbers = set()
            for s in sessions:
                npi = s.get("NowPlayingItem", {})
                ch_num = npi.get("ChannelNumber")
                if ch_num:
                    ch_num = str(ch_num).strip()
                    try:
                        num = float(ch_num)
                        ch_num = str(int(num)) if num == int(num) else ch_num
                    except (ValueError, TypeError):
                        pass
                    media_server_channel_numbers.add(ch_num)

        try:
            for key in redis_client.scan_iter(match="channel_stream:*"):
                key_str = key.decode("utf-8") if isinstance(key, bytes) else key
                # key format: channel_stream:{channel_id}
                parts = key_str.split(":", 1)
                if len(parts) < 2:
                    continue
                channel_id_raw = parts[1]

                # channel_id_raw might be numeric ID, need to find UUID
                # Look up UUID from channel model cache by checking all channels
                channel_uuid = None
                channel_name = ""
                channel_number = ""

                # Try treating channel_id_raw as a UUID directly
                if channel_id_raw in channel_model_cache:
                    channel_uuid = channel_id_raw
                    channel_name = channel_model_cache[channel_id_raw]["name"]
                    channel_number = channel_model_cache[channel_id_raw]["number"]
                else:
                    # It's a numeric ID; look up the channel
                    try:
                        ch = Channel.objects.filter(pk=int(channel_id_raw)).only("uuid", "name", "channel_number").first()
                        if ch:
                            channel_uuid = str(ch.uuid)
                            channel_name = ch.name
                            channel_number = str(int(ch.channel_number)) if ch.channel_number == int(ch.channel_number) else str(ch.channel_number)
                    except (ValueError, Exception):
                        pass

                if not channel_uuid:
                    continue

                # ── Failover / buffering protection ──────────────────────────
                # Read channel metadata to check if the stream is mid-failover
                # or still buffering.  Clients appear idle during these states
                # because no data is flowing, so we must not terminate them.
                ch_meta_key = RedisKeys.channel_metadata(channel_uuid)
                ch_meta = redis_client.hgetall(ch_meta_key) or {}
                ch_state = redis_decode(
                    ch_meta.get(b"state") or ch_meta.get("state")
                ).lower()
                in_grace = ch_state in _GRACE_STATES

                if not in_grace:
                    # Even if state is "active", a recent stream switch means
                    # data may have just resumed and last_active hasn't caught up.
                    switch_raw = redis_decode(
                        ch_meta.get(b"stream_switch_time") or ch_meta.get("stream_switch_time")
                    )
                    try:
                        switch_ts = float(switch_raw) if switch_raw else 0
                    except (ValueError, TypeError):
                        switch_ts = 0
                    if switch_ts and (now - switch_ts) < failover_grace:
                        in_grace = True

                # Read clients for this channel
                client_ids = redis_client.smembers(RedisKeys.clients(channel_uuid)) or []
                if not client_ids:
                    continue

                channel_clients = []
                for raw_id in client_ids:
                    client_id = raw_id.decode("utf-8") if isinstance(raw_id, bytes) else str(raw_id)
                    meta_key = RedisKeys.client_metadata(channel_uuid, client_id)
                    cdata = redis_client.hgetall(meta_key)
                    if not cdata:
                        continue

                    ip = redis_decode(cdata.get(b"ip_address") or cdata.get("ip_address"))
                    user_id_str = redis_decode(cdata.get(b"user_id") or cdata.get("user_id"))
                    username = _resolve_username(user_id_str, _user_cache)
                    user_agent = redis_decode(cdata.get(b"user_agent") or cdata.get("user_agent"))
                    connected_at = redis_decode(cdata.get(b"connected_at") or cdata.get("connected_at"))
                    last_active_raw = redis_decode(cdata.get(b"last_active") or cdata.get("last_active"))
                    bytes_sent = redis_decode(cdata.get(b"bytes_sent") or cdata.get("bytes_sent"))

                    matched, match_reason = self._match_client(ip, username, identifiers, resolved_ips)

                    # Calculate last_active age
                    try:
                        last_active_ts = float(last_active_raw) if last_active_raw else 0
                    except (ValueError, TypeError):
                        last_active_ts = 0
                    idle_seconds = (now - last_active_ts) if last_active_ts > 0 else None

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

                    client_info = {
                        "client_id": client_id,
                        "ip": ip,
                        "username": username,
                        "user_agent": user_agent,
                        "connected_at_raw": connected_at,
                        "connected_duration": connected_duration,
                        "bytes_sent": bytes_sent,
                        "is_target_match": matched,
                        "match_reason": match_reason,
                        "idle_seconds": round(idle_seconds, 1) if idle_seconds is not None else None,
                        "in_grace": in_grace,
                        "is_orphan": False,
                    }
                    channel_clients.append(client_info)

                    # Track and act on matched clients
                    if matched:
                        ck = (channel_uuid, client_id)
                        active_keys.add(ck)
                        should_terminate = False
                        reason = ""

                        if not in_grace:
                            # Check media server pool (if configured)
                            if media_server_channel_numbers is not None:
                                if channel_number not in media_server_channel_numbers:
                                    # Track how long absent from pool
                                    if ck not in self._idle_since:
                                        self._idle_since[ck] = now
                                        logger.debug(
                                            f"Client {client_id} on CH {channel_number} "
                                            f"not in media server pool - tracking"
                                        )
                                    else:
                                        absent_seconds = (now - self._idle_since[ck]).total_seconds()
                                        if absent_seconds >= timeout:
                                            should_terminate = True
                                            reason = (
                                                f"absent from media server pool "
                                                f"{absent_seconds:.0f}s >= {timeout}s timeout"
                                            )

                            # Check idle_seconds (always, regardless of media server)
                            if not should_terminate and idle_seconds is not None and idle_seconds >= timeout:
                                should_terminate = True
                                reason = f"idle {idle_seconds:.0f}s >= {timeout}s timeout"

                        if should_terminate:
                            logger.info(
                                f"Terminating client {client_id} on CH {channel_number} "
                                f"({channel_name}): {reason} "
                                f"(ip={ip}, user={username})"
                            )
                            try:
                                result = ChannelService.stop_client(channel_uuid, client_id)
                                if result.get("status") == "success":
                                    logger.info(f"Successfully terminated client {client_id}")
                                    self._stopped_log.append({
                                        "time": now,
                                        "channel": f"CH {channel_number} ({channel_name})",
                                        "ip": ip,
                                        "username": username,
                                        "reason": reason,
                                    })
                                    if len(self._stopped_log) > 20:
                                        self._stopped_log = self._stopped_log[-20:]
                                    self._idle_since.pop(ck, None)
                                else:
                                    logger.warning(f"stop_client returned: {result}")
                            except Exception as e:
                                logger.error(f"Error stopping client {client_id}: {e}", exc_info=True)
                        elif not in_grace:
                            # Not terminating - if channel is in pool and not idle, clear tracking
                            in_pool = (media_server_channel_numbers is not None
                                       and channel_number in media_server_channel_numbers)
                            not_idle = (idle_seconds is not None and idle_seconds < timeout)
                            if in_pool and not_idle:
                                self._idle_since.pop(ck, None)
                            elif idle_seconds is None and in_pool:
                                # No idle data but channel in pool - safe
                                self._idle_since.pop(ck, None)
                            elif media_server_channel_numbers is None:
                                # No media server configured, track idle start
                                if idle_seconds is not None and ck not in self._idle_since:
                                    self._idle_since[ck] = now

                if channel_clients:
                    scan_result[channel_uuid] = {
                        "channel_name": channel_name,
                        "channel_number": channel_number,
                        "channel_state": ch_state,
                        "in_grace": in_grace,
                        "clients": channel_clients,
                    }

        except Exception as e:
            logger.error(f"Error during poll scan: {e}", exc_info=True)

        # Prune idle_since entries for clients that disappeared
        stale = [k for k in self._idle_since if k not in active_keys]
        for k in stale:
            self._idle_since.pop(k, None)

        # ── Media server orphan detection ────────────────────────────────
        if sessions is not None:
            emby_active = self._count_active_streams(sessions)
            self._emby_active_count = emby_active
            self._detect_orphans(scan_result, sessions, now, ChannelService)
        elif self._get_media_server_configs():
            # Configured but fetch failed -- keep last count, don't orphan-kill
            pass
        else:
            self._emby_active_count = None

        # Prune orphaned_since entries for clients that disappeared
        stale_orphans = [k for k in self._orphaned_since if k not in active_keys]
        for k in stale_orphans:
            self._orphaned_since.pop(k, None)

        self._last_scan = scan_result
        self._last_scan_time = now

    # ── Debug state ──────────────────────────────────────────────────────────

    def get_debug_state(self):
        """Return current state for the debug page."""
        client_identifier = (self._settings.get("client_identifier") or "").strip()
        identifiers = self._parse_identifiers(client_identifier)
        resolved_ips = self._resolve_identifiers(identifiers)
        timeout = int(self._settings.get("cleanup_timeout", DEFAULT_CLEANUP_TIMEOUT))
        poll_interval = int(self._settings.get("poll_interval", DEFAULT_POLL_INTERVAL))

        return {
            "running": self._running,
            "scan": self._last_scan,
            "scan_time": self._last_scan_time,
            "idle_timeout": timeout,
            "poll_interval": poll_interval,
            "identifier_configured": bool(identifiers),
            "resolved_ips": sorted(resolved_ips) if resolved_ips else [],
            "stopped_log": list(self._stopped_log),
            "emby_configured": bool(self._get_media_server_configs()),
            "emby_active_count": self._emby_active_count,
            "emby_error": self._emby_error,
            "media_servers": list(self._media_server_status),
        }

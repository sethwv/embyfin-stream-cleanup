"""Debug HTTP server (optional).

Serves a debug dashboard showing which Dispatcharr clients match the
configured identifier and their idle status.  The stream monitor runs
independently in a background thread.

Routes:
  GET  /        Landing page
  GET  /debug   Live debug dashboard (auto-refreshes)
  GET  /health  Health check
"""

import logging
import socket
import threading
import time

from .config import (
    PLUGIN_CONFIG, REDIS_KEY_RUNNING, REDIS_KEY_HOST, REDIS_KEY_PORT,
    REDIS_KEY_STOP, DEFAULT_PORT, DEFAULT_HOST,
    HEARTBEAT_TTL,
)
from .utils import get_redis_client, read_redis_flag, normalize_host

logger = logging.getLogger(__name__)

# Module-level reference to the currently running server instance (per process).
_debug_server = None


def get_current_server():
    """Return the active DebugServer instance for this process, or None."""
    return _debug_server


def set_current_server(server):
    """Set the active DebugServer instance for this process."""
    global _debug_server
    _debug_server = server


class DebugServer:
    """Lightweight gevent WSGI server for the debug dashboard."""

    def __init__(self, monitor, port=None, host=None):
        self.monitor = monitor
        self.port = port if port is not None else DEFAULT_PORT
        self.host = normalize_host(host, DEFAULT_HOST)
        logger.info(f"DebugServer initialised with host='{self.host}', port={self.port}")
        self.server_thread = None
        self.server = None
        self.running = False
        self.settings = {}

    # -- Port verification ----------------------------------------------------

    def _verify_stopped(self, timeout=3):
        """Block until the server port is confirmed free (up to *timeout* seconds)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.settimeout(0.5)
                sock.bind((self.host, self.port))
                sock.close()
                logger.info(f"Verified port {self.port} is free after server stop")
                return True
            except OSError:
                try:
                    sock.close()
                except Exception:
                    pass
                time.sleep(0.2)

        logger.warning(
            f"Port {self.port} still in use after {timeout}s - server may not have stopped cleanly"
        )
        return False

    # -- WSGI application -----------------------------------------------------

    def wsgi_app(self, environ, start_response):
        """Handle a single HTTP request."""
        path = environ.get('PATH_INFO', '/')

        if path == '/health':
            start_response('200 OK', [('Content-Type', 'text/plain')])
            return [b"OK\n"]

        elif path == '/debug':
            return self._serve_debug_page(start_response)

        elif path == '/':
            return self._serve_landing_page(start_response)

        else:
            start_response('404 Not Found', [('Content-Type', 'text/plain')])
            return [b"Not Found\n"]

    # -- Debug page ------------------------------------------------------------

    def _serve_debug_page(self, start_response):
        try:
            debug_state = self.monitor.get_debug_state()
            now = time.time()

            plugin_name = PLUGIN_CONFIG.get('name', 'Emby Stream Cleanup')
            identifier = self.settings.get("client_identifier", "") or ""
            identifier_display = identifier or "(not set)"
            timeout = debug_state.get("idle_timeout", 30)
            poll_interval = debug_state.get("poll_interval", 10)
            monitor_running = debug_state.get("running", False)

            # Resolved IPs info
            resolved_ips = debug_state.get("resolved_ips", [])
            resolved_html = ""
            if resolved_ips and identifier:
                resolved_html = f' &rarr; <span>{", ".join(resolved_ips)}</span>'

            # Monitor status
            if monitor_running:
                monitor_badge = '<span class="badge active">Running</span>'
            else:
                monitor_badge = '<span class="badge idle">Stopped</span>'

            # Media server status
            emby_configured = debug_state.get("emby_configured", False)
            emby_active_count = debug_state.get("emby_active_count")
            emby_error = debug_state.get("emby_error")
            emby_html = ""
            if emby_configured:
                if emby_error:
                    emby_html = f'<tr><td>Media Server</td><td><span class="warn">Error: {emby_error}</span></td></tr>'
                elif emby_active_count is not None:
                    emby_html = f'<tr><td>Media Server</td><td><span>{emby_active_count} active session(s)</span></td></tr>'
                else:
                    emby_html = '<tr><td>Media Server</td><td><span>Connecting...</span></td></tr>'

            # Build channel cards from last scan
            scan = debug_state.get("scan", {})
            channels_html = ""
            if scan:
                for ch_uuid, ch_data in sorted(scan.items(), key=lambda x: x[1].get("channel_number", "")):
                    channel_name = ch_data.get("channel_name", "")
                    channel_number = ch_data.get("channel_number", "?")
                    ch_in_grace = ch_data.get("in_grace", False)
                    ch_state = ch_data.get("channel_state", "")
                    clients = ch_data.get("clients", [])

                    matched_clients = [c for c in clients if c.get("is_target_match")]
                    other_clients = [c for c in clients if not c.get("is_target_match")]

                    # Determine card status based on idle state of matched clients
                    has_idle = any(
                        (c.get("idle_seconds") or 0) >= timeout
                        for c in matched_clients
                    )

                    if ch_in_grace:
                        status_class = "grace"
                        status_label = f"Grace period ({ch_state})"
                        status_desc = "Channel is buffering or switching streams &mdash; terminations paused"
                    elif has_idle:
                        status_class = "pending"
                        status_label = "Idle matched clients detected"
                        status_desc = "Matching clients will be terminated when idle timeout expires"
                    elif matched_clients:
                        status_class = "active"
                        status_label = f"{len(matched_clients)} matched client(s) active"
                        status_desc = "Clients are streaming data normally"
                    else:
                        status_class = "idle"
                        status_label = "No matched clients"
                        status_desc = "No clients on this channel match the configured identifier"

                    name_html = f' <span class="channel-name">{channel_name}</span>' if channel_name else ""
                    card_html = f'''
                    <div class="card {status_class}">
                        <div class="card-header">
                            <span class="channel-num">CH {channel_number}{name_html}</span>
                            <span class="badge {status_class}">{status_label}</span>
                        </div>
                        <div class="status-desc">{status_desc}</div>'''

                    if matched_clients:
                        card_html += f'<div class="section-label target">Matched Clients ({len(matched_clients)})</div>'
                        if ch_in_grace:
                            card_html += '<div class="client-note grace-note">Terminations PAUSED during failover/buffering</div>'
                        else:
                            card_html += '<div class="client-note target-note">Idle clients WILL be terminated after timeout</div>'
                        for c in matched_clients:
                            card_html += self._render_client_row(c, is_match=True, timeout=timeout)

                    if other_clients:
                        card_html += f'<div class="section-label safe">Other Clients ({len(other_clients)})</div>'
                        card_html += '<div class="client-note safe-note">These connections will NOT be affected</div>'
                        for c in other_clients:
                            card_html += self._render_client_row(c, is_match=False, timeout=timeout)

                    card_html += '</div>'
                    channels_html += card_html
            else:
                channels_html = '<div class="empty">No active channels with clients found.</div>'

            # Recent terminations
            stopped_log = debug_state.get("stopped_log", [])
            log_html = ""
            if stopped_log:
                log_html = '<h2>Recent Terminations</h2>'
                for entry in reversed(stopped_log):
                    from datetime import datetime, timezone
                    ts = entry.get("time", 0)
                    ts_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%H:%M:%S UTC')
                    ago = int(now - ts)
                    reason = entry.get("reason", "idle")
                    reason_label = '<span class="orphan-warn">[ORPHAN]</span> ' if reason == "orphan" else ""
                    log_html += (
                        f'<div class="log-entry">'
                        f'<span class="log-time">{ts_str} ({ago}s ago)</span> '
                        f'{reason_label}'
                        f'{entry.get("channel", "?")} '
                        f'<span class="log-detail">ip={entry.get("ip", "?")} '
                        f'user={entry.get("username", "?")} '
                        f'idle={entry.get("idle_seconds", "?")}s</span>'
                        f'</div>'
                    )

            # Last scan time
            scan_time = debug_state.get("scan_time", 0)
            scan_ago = f"{int(now - scan_time)}s ago" if scan_time > 0 else "never"

            refresh_interval = min(poll_interval, 5)

            html = self._debug_html(
                plugin_name, monitor_badge, identifier_display, resolved_html,
                timeout, poll_interval, scan_ago, channels_html, log_html,
                refresh_interval, emby_html
            )

            start_response('200 OK', [('Content-Type', 'text/html; charset=utf-8')])
            return [html.encode('utf-8')]
        except Exception as e:
            logger.error(f"Error generating debug page: {e}", exc_info=True)
            start_response('500 Internal Server Error', [('Content-Type', 'text/plain')])
            return [b"Error generating debug page\n"]

    @staticmethod
    def _debug_html(plugin_name, monitor_badge, identifier_display, resolved_html,
                    timeout, poll_interval, scan_ago, channels_html, log_html,
                    refresh_interval, emby_html):
        return f"""<!DOCTYPE html>
<html>
<head>
    <title>{plugin_name} - Debug</title>
    <meta http-equiv="refresh" content="{refresh_interval}">
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
            max-width: 800px;
            margin: 40px auto;
            padding: 20px;
            background: #1a1a2e;
            color: #e0e0e0;
        }}
        .container {{
            background: #16213e;
            padding: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.4);
        }}
        h1 {{ margin-top: 0; color: #e0e0e0; font-size: 22px; }}
        h2 {{ color: #a0a0b0; font-size: 16px; margin-top: 25px; border-bottom: 1px solid #2a2a4a; padding-bottom: 8px; }}
        .nav {{ margin-bottom: 20px; font-size: 13px; }}
        a {{ color: #64b5f6; text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}
        .config-table {{ font-size: 13px; color: #a0a0b0; margin-bottom: 20px; width: 100%; }}
        .config-table td {{ padding: 3px 0; }}
        .config-table td:first-child {{ color: #707090; width: 140px; }}
        .config-table span {{ color: #e0e0e0; font-weight: 500; }}
        .explainer {{
            background: #1a2744;
            border: 1px solid #2a3a5a;
            border-radius: 6px;
            padding: 14px 16px;
            font-size: 13px;
            color: #90b0d0;
            margin-bottom: 20px;
            line-height: 1.6;
        }}
        .explainer strong {{ color: #b0d0f0; }}
        .card {{
            border: 1px solid #2a2a4a;
            border-radius: 6px;
            padding: 14px 18px;
            margin-bottom: 12px;
            background: #1c2541;
        }}
        .card.active {{ border-left: 4px solid #4caf50; }}
        .card.pending {{ border-left: 4px solid #ff9800; }}
        .card.idle {{ border-left: 4px solid #555; }}
        .card.grace {{ border-left: 4px solid #42a5f5; }}
        .card-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        .channel-num {{ font-weight: 600; font-size: 15px; color: #e0e0e0; }}
        .channel-name {{ font-weight: 400; color: #707090; font-size: 13px; margin-left: 6px; }}
        .status-desc {{ font-size: 12px; color: #707090; margin-top: 4px; font-style: italic; }}
        .badge {{
            font-size: 12px;
            padding: 3px 10px;
            border-radius: 12px;
            font-weight: 500;
            white-space: nowrap;
        }}
        .badge.active {{ background: #1b3a1b; color: #66bb6a; }}
        .badge.pending {{ background: #3a2a10; color: #ffb74d; }}
        .badge.idle {{ background: #2a2a2a; color: #888; }}
        .badge.grace {{ background: #1a2a3a; color: #64b5f6; }}
        .grace-note {{ color: #64b5f6; }}
        .section-label {{
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-top: 12px;
            margin-bottom: 4px;
            padding-top: 10px;
            border-top: 1px solid #2a2a4a;
        }}
        .section-label.target {{ color: #ffb74d; }}
        .section-label.safe {{ color: #66bb6a; }}
        .client-note {{
            font-size: 11px;
            font-style: italic;
            margin-bottom: 6px;
        }}
        .target-note {{ color: #ffb74d; }}
        .safe-note {{ color: #66bb6a; }}
        .client-row {{
            font-size: 12px;
            padding: 6px 10px;
            margin: 3px 0;
            border-radius: 4px;
            font-family: monospace;
        }}
        .client-row.match {{
            background: #2a2010;
            border: 1px solid #4a3a1a;
        }}
        .client-row.safe {{
            background: #1a2a1a;
            border: 1px solid #2a4a2a;
        }}
        .client-detail {{
            display: flex;
            flex-wrap: wrap;
            gap: 6px 16px;
        }}
        .client-field {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
            font-size: 11px;
        }}
        .client-field .label {{ color: #707090; }}
        .client-field .value {{ color: #e0e0e0; font-weight: 500; }}
        .match-reason {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
            font-size: 11px;
            color: #ffb74d;
            font-weight: 500;
        }}
        .safe-label {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
            font-size: 11px;
            color: #66bb6a;
            font-weight: 500;
        }}
        .idle-warn {{
            color: #ffb74d;
            font-weight: 600;
        }}
        .orphan-warn {{
            color: #ef5350;
            font-weight: 600;
        }}
        .empty {{ color: #707090; font-style: italic; padding: 20px 0; text-align: center; }}
        .refresh-note {{ font-size: 11px; color: #505060; text-align: center; margin-top: 15px; }}
        .warn {{ color: #ffb74d; font-weight: 500; }}
        .log-entry {{
            font-size: 12px;
            padding: 4px 0;
            border-bottom: 1px solid #2a2a4a;
        }}
        .log-time {{ color: #707090; font-size: 11px; }}
        .log-detail {{ color: #a0a0b0; font-family: monospace; font-size: 11px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="nav"><a href="/">&larr; Home</a></div>
        <h1>Debug {monitor_badge}</h1>

        <table class="config-table">
            <tr><td>Client Identifier</td><td><span>{identifier_display}</span>{resolved_html}</td></tr>
            <tr><td>Idle Timeout</td><td><span>{timeout}s</span></td></tr>
            <tr><td>Poll Interval</td><td><span>{poll_interval}s</span></td></tr>
            <tr><td>Last Scan</td><td><span>{scan_ago}</span></td></tr>
            {emby_html}
        </table>

        <div class="explainer">
            <strong>How it works:</strong>
            The monitor polls all active Dispatcharr channels every <strong>{poll_interval}s</strong>.
            Clients matching the identifier <strong>{identifier_display}</strong> are tracked.
            If a matching client stops receiving data for <strong>{timeout}s</strong>, its connection is terminated.
            When an Emby/Jellyfin server URL is configured, the plugin also cross-references active
            media server sessions to detect <strong>orphaned</strong> connections that the server failed to close.
            Non-matching clients are <strong>never</strong> affected.
        </div>

        <h2>Active Channels</h2>
        {channels_html}
        {log_html}
        <div class="refresh-note">Auto-refreshes every {refresh_interval} seconds</div>
    </div>
</body>
</html>"""

    # -- Landing page ----------------------------------------------------------

    def _serve_landing_page(self, start_response):
        plugin_name = PLUGIN_CONFIG.get('name', 'Emby Stream Cleanup')
        plugin_version = PLUGIN_CONFIG.get('version', 'unknown version').lstrip('-')
        plugin_description = PLUGIN_CONFIG.get('description', '')
        repo_url = PLUGIN_CONFIG.get('repo_url', 'https://github.com/sethwv/emby-stream-cleanup')

        monitor_status = "Running" if self.monitor.is_running() else "Stopped"

        html = f"""<!DOCTYPE html>
<html>
<head>
    <title>{plugin_name}</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
            max-width: 600px;
            margin: 100px auto;
            padding: 20px;
            background: #1a1a2e;
            color: #e0e0e0;
        }}
        .container {{
            background: #16213e;
            padding: 40px;
            border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.4);
        }}
        h1 {{ margin-top: 0; color: #e0e0e0; }}
        .version {{ color: #707090; font-size: 14px; margin-top: -10px; margin-bottom: 20px; }}
        p {{ color: #a0a0b0; line-height: 1.6; }}
        a {{ color: #64b5f6; text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}
        .links {{ margin-top: 30px; padding-top: 20px; border-top: 1px solid #2a2a4a; }}
        .links a {{ display: inline-block; margin-right: 20px; font-weight: 500; }}
        .status {{ font-size: 13px; color: #a0a0b0; margin-top: 10px; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>{plugin_name}</h1>
        <div class="version">{plugin_version}</div>
        <p>{plugin_description}</p>
        <p class="status">Monitor: <strong>{monitor_status}</strong></p>
        <div class="links">
            <a href="/debug">Debug Dashboard</a>
            <a href="/health">Health Check</a>
            <a href="{repo_url}" target="_blank">GitHub</a>
        </div>
    </div>
</body>
</html>"""
        start_response('200 OK', [('Content-Type', 'text/html; charset=utf-8')])
        return [html.encode('utf-8')]

    # -- Client row rendering --------------------------------------------------

    @staticmethod
    def _render_client_row(client, is_match, timeout=30):
        """Render a single Dispatcharr client as an HTML row."""
        row_class = "match" if is_match else "safe"
        ip = client.get("ip", "?")
        username = client.get("username", "")
        user_agent = client.get("user_agent", "")
        duration = client.get("connected_duration", "")
        match_reason = client.get("match_reason", "")
        idle_seconds = client.get("idle_seconds")
        in_grace = client.get("in_grace", False)

        label_html = ""
        if is_match:
            if client.get("is_orphan"):
                label_html = '<span class="match-reason orphan-warn">ORPHAN (no active media server session &mdash; will terminate)</span>'
            elif in_grace and idle_seconds is not None and idle_seconds >= timeout:
                label_html = f'<span class="match-reason" style="color:#1565c0">GRACE PERIOD (idle {int(idle_seconds)}s &mdash; termination paused)</span>'
            elif idle_seconds is not None and idle_seconds >= timeout:
                label_html = f'<span class="match-reason idle-warn">WILL TERMINATE (idle {int(idle_seconds)}s / {timeout}s timeout)</span>'
            elif idle_seconds is not None:
                label_html = f'<span class="match-reason">MONITORED ({match_reason}) - idle {int(idle_seconds)}s</span>'
            else:
                label_html = f'<span class="match-reason">MONITORED ({match_reason})</span>'
        else:
            label_html = '<span class="safe-label">SAFE - not affected</span>'

        fields = [f'<span class="client-field"><span class="label">IP:</span> <span class="value">{ip}</span></span>']
        if username:
            fields.append(f'<span class="client-field"><span class="label">User:</span> <span class="value">{username}</span></span>')
        if user_agent:
            ua_short = user_agent[:60] + ("..." if len(user_agent) > 60 else "")
            fields.append(f'<span class="client-field"><span class="label">UA:</span> <span class="value">{ua_short}</span></span>')
        if duration:
            fields.append(f'<span class="client-field"><span class="label">Connected:</span> <span class="value">{duration}</span></span>')

        return f'''<div class="client-row {row_class}">
            {label_html}
            <div class="client-detail">{"".join(fields)}</div>
        </div>'''

    # -- Lifecycle -------------------------------------------------------------

    def start(self, settings=None) -> bool:
        """Start the debug server in a background thread."""
        if self.running:
            logger.warning("Debug server is already running")
            return False

        # Guard against duplicate servers across workers via Redis
        redis_client = get_redis_client()
        if redis_client and read_redis_flag(redis_client, REDIS_KEY_RUNNING):
            logger.warning("Another debug server instance is already running (detected via Redis)")
            return False

        current = get_current_server()
        if current and current.is_running():
            logger.warning("Another debug server instance is already running in this process")
            return False

        # Validate host / port binding
        logger.info(f"Attempting to bind debug server to host='{self.host}', port={self.port}")
        try:
            try:
                socket.getaddrinfo(self.host, self.port, socket.AF_INET, socket.SOCK_STREAM)
            except socket.gaierror as e:
                logger.error(
                    f"Cannot resolve host '{self.host}': {e}. "
                    f"In Docker, use '0.0.0.0' to bind to all interfaces."
                )
                return False

            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.host, self.port))
            sock.close()
        except OSError as e:
            if e.errno == -2 or 'Name or service not known' in str(e):
                logger.error(
                    f"Cannot resolve host '{self.host}': {e}. "
                    f"In Docker, use '0.0.0.0' to bind to all interfaces."
                )
            else:
                logger.error(f"Cannot bind to {self.host}:{self.port}: {e}")
            return False

        self.settings = settings or {}

        try:
            from gevent import pywsgi

            def run_server():
                try:
                    suppress_logs = self.settings.get('suppress_access_logs', True)
                    server_kwargs = {
                        'listener': (self.host, self.port),
                        'application': self.wsgi_app,
                    }
                    if suppress_logs:
                        server_kwargs['log'] = None

                    self.server = pywsgi.WSGIServer(**server_kwargs)
                    self.running = True
                    set_current_server(self)

                    # Announce via Redis (with heartbeat TTL)
                    _rc = get_redis_client()
                    if _rc:
                        try:
                            _rc.set(REDIS_KEY_RUNNING, "1", ex=HEARTBEAT_TTL)
                            _rc.set(REDIS_KEY_HOST, self.host, ex=HEARTBEAT_TTL)
                            _rc.set(REDIS_KEY_PORT, str(self.port), ex=HEARTBEAT_TTL)
                        except Exception as e:
                            logger.warning(f"Could not set Redis running flags: {e}")

                    logger.info(f"Debug server started on http://{self.host}:{self.port}/")

                    from gevent import spawn, sleep
                    spawn(self.server.serve_forever)

                    # Monitor for Redis stop signal
                    monitor_redis = get_redis_client()
                    while self.running:
                        try:
                            if monitor_redis and read_redis_flag(monitor_redis, REDIS_KEY_STOP):
                                logger.info("Debug server stop signal detected via Redis")
                                self.running = False
                                try:
                                    self.server.stop(timeout=5)
                                except Exception as e:
                                    logger.warning(f"Error during server.stop(): {e}")
                                self._verify_stopped(timeout=3)
                                break
                            elif not monitor_redis:
                                monitor_redis = get_redis_client()
                        except Exception as e:
                            logger.warning(f"Error checking stop signal: {e}")
                            monitor_redis = get_redis_client()

                        # Refresh heartbeat so keys don't expire while alive
                        if monitor_redis:
                            try:
                                monitor_redis.set(REDIS_KEY_RUNNING, "1", ex=HEARTBEAT_TTL)
                                monitor_redis.expire(REDIS_KEY_HOST, HEARTBEAT_TTL)
                                monitor_redis.expire(REDIS_KEY_PORT, HEARTBEAT_TTL)
                            except Exception:
                                pass

                        sleep(1)

                    # Cleanup Redis flags
                    _rc = get_redis_client()
                    if _rc:
                        try:
                            _rc.delete(REDIS_KEY_RUNNING, REDIS_KEY_HOST, REDIS_KEY_PORT, REDIS_KEY_STOP)
                        except Exception as e:
                            logger.warning(f"Could not clear Redis flags on shutdown: {e}")

                    set_current_server(None)
                    logger.info("Debug server stopped and cleaned up")

                except Exception as e:
                    logger.error(f"Error running debug server: {e}", exc_info=True)
                    self.running = False

            self.server_thread = threading.Thread(target=run_server, daemon=True)
            self.server_thread.start()

            time.sleep(0.5)
            return self.running

        except ImportError:
            logger.error("gevent is not installed")
            return False

    def stop(self) -> bool:
        """Stop the debug server."""
        if not self.running:
            return False

        logger.info("Stopping debug server...")

        if self.server:
            try:
                self.server.stop(timeout=5)
            except Exception as e:
                logger.warning(f"Error during server.stop(): {e}")
            self._verify_stopped(timeout=3)

        self.running = False
        set_current_server(None)

        redis_client = get_redis_client()
        if redis_client:
            try:
                redis_client.delete(REDIS_KEY_RUNNING, REDIS_KEY_HOST, REDIS_KEY_PORT)
            except Exception as e:
                logger.warning(f"Could not clear Redis flags: {e}")

        return True

    def is_running(self) -> bool:
        return self.running and self.server_thread is not None and self.server_thread.is_alive()

"""gevent WSGI webhook server.

Binds to a configurable host:port and serves:
  GET  /          Landing page with webhook URL info
  POST /webhook   Receive Emby webhook payloads
  GET  /health    Simple health check
"""

import json
import logging
import socket
import threading
import time

from .config import (
    PLUGIN_CONFIG, REDIS_KEY_RUNNING, REDIS_KEY_HOST, REDIS_KEY_PORT,
    REDIS_KEY_STOP, DEFAULT_PORT, DEFAULT_HOST,
)
from .utils import get_redis_client, read_redis_flag, normalize_host, get_dispatcharr_version, compare_versions

logger = logging.getLogger(__name__)

# Module-level reference to the currently running server instance (per process).
_webhook_server = None

# Store the last raw webhook received on /debug/webhook for inspection.
_last_debug_webhook = None


def get_current_server():
    """Return the active WebhookServer instance for this process, or None."""
    return _webhook_server


def set_current_server(server):
    """Set the active WebhookServer instance for this process."""
    global _webhook_server
    _webhook_server = server


class WebhookServer:
    """Lightweight gevent WSGI server that receives Emby webhooks."""

    def __init__(self, handler, port=None, host=None):
        self.handler = handler
        self.port = port if port is not None else DEFAULT_PORT
        self.host = normalize_host(host, DEFAULT_HOST)
        logger.info(f"WebhookServer initialised with host='{self.host}', port={self.port}")
        self.server_thread = None
        self.server = None
        self.running = False
        self.settings = {}

    # ── Port verification ────────────────────────────────────────────────────

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

    # ── WSGI application ─────────────────────────────────────────────────────

    def wsgi_app(self, environ, start_response):
        """Handle a single HTTP request."""
        path = environ.get('PATH_INFO', '/')
        method = environ.get('REQUEST_METHOD', 'GET')

        if path == '/webhook':
            if method != 'POST':
                start_response('405 Method Not Allowed', [
                    ('Content-Type', 'text/plain'),
                    ('Allow', 'POST'),
                ])
                return [b"Method Not Allowed. Use POST.\n"]

            try:
                content_length = int(environ.get('CONTENT_LENGTH', 0) or 0)
                if content_length == 0:
                    start_response('400 Bad Request', [('Content-Type', 'application/json')])
                    return [json.dumps({"error": "Empty request body"}).encode('utf-8')]

                body = environ['wsgi.input'].read(content_length)
                event_data = json.loads(body)
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning(f"Invalid JSON in webhook request: {e}")
                start_response('400 Bad Request', [('Content-Type', 'application/json')])
                return [json.dumps({"error": "Invalid JSON"}).encode('utf-8')]

            try:
                result = self.handler.handle_webhook(event_data, self.settings)
                start_response('200 OK', [('Content-Type', 'application/json')])
                return [json.dumps(result).encode('utf-8')]
            except Exception as e:
                logger.error(f"Error processing webhook: {e}", exc_info=True)
                start_response('500 Internal Server Error', [('Content-Type', 'application/json')])
                return [json.dumps({"error": "Internal server error"}).encode('utf-8')]

        elif path == '/health':
            start_response('200 OK', [('Content-Type', 'text/plain')])
            return [b"OK\n"]

        elif path == '/debug/webhook':
            global _last_debug_webhook
            if method == 'POST':
                try:
                    content_length = int(environ.get('CONTENT_LENGTH', 0) or 0)
                    body = environ['wsgi.input'].read(content_length) if content_length else b''
                    payload = json.loads(body) if body else {}
                    import time as _t
                    _last_debug_webhook = {
                        "received_at": _t.time(),
                        "payload": payload,
                    }
                    logger.debug(f"Debug webhook received: {payload.get('Event', 'unknown')}")
                    start_response('200 OK', [('Content-Type', 'application/json')])
                    return [json.dumps({"status": "received"}).encode('utf-8')]
                except (json.JSONDecodeError, ValueError):
                    start_response('400 Bad Request', [('Content-Type', 'application/json')])
                    return [json.dumps({"error": "Invalid JSON"}).encode('utf-8')]

            # GET — show last received webhook
            import time as _time
            plugin_name = PLUGIN_CONFIG.get('name', 'Emby Stream Cleanup')
            if _last_debug_webhook:
                ts = _last_debug_webhook["received_at"]
                ago = _time.time() - ts
                from datetime import datetime, timezone
                ts_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
                payload_json = json.dumps(_last_debug_webhook["payload"], indent=2)
                # Escape HTML
                payload_html = payload_json.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                content_html = f'''<div class="meta">Received: {ts_str} ({int(ago)}s ago)</div>
                    <pre>{payload_html}</pre>'''
            else:
                content_html = '<div class="empty">No webhook received yet. POST any JSON to this URL.</div>'

            html = f"""<!DOCTYPE html>
<html>
<head>
    <title>{plugin_name} - Debug Webhook</title>
    <meta http-equiv="refresh" content="5">
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
            max-width: 800px;
            margin: 40px auto;
            padding: 20px;
            background: #f5f5f5;
        }}
        .container {{
            background: white;
            padding: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        h1 {{ margin-top: 0; color: #333; font-size: 22px; }}
        .nav {{ margin-bottom: 20px; font-size: 13px; }}
        a {{ color: #0066cc; text-decoration: none; }}
        .meta {{ font-size: 13px; color: #888; margin-bottom: 10px; }}
        pre {{
            background: #f8f8f8;
            border: 1px solid #e0e0e0;
            border-radius: 6px;
            padding: 16px;
            font-size: 12px;
            overflow-x: auto;
            max-height: 600px;
            overflow-y: auto;
        }}
        .empty {{ color: #999; font-style: italic; padding: 20px 0; text-align: center; }}
        .webhook-url {{
            background: #e3f2fd;
            padding: 10px 15px;
            border-radius: 4px;
            font-family: monospace;
            font-size: 13px;
            margin: 10px 0 15px;
        }}
        .refresh-note {{ font-size: 11px; color: #bbb; text-align: center; margin-top: 15px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="nav"><a href="/">&larr; Home</a> &nbsp;|&nbsp; <a href="/debug">Debug</a></div>
        <h1>Debug Webhook</h1>
        <p style="color:#666; font-size: 14px;">Point any webhook here to inspect the payload:</p>
        <div class="webhook-url">POST http://&lt;host&gt;:{self.port}/debug/webhook</div>
        {content_html}
        <div class="refresh-note">Auto-refreshes every 5 seconds</div>
    </div>
</body>
</html>"""
            start_response('200 OK', [('Content-Type', 'text/html; charset=utf-8')])
            return [html.encode('utf-8')]

        elif path == '/debug':
            try:
                identifier = self.settings.get("emby_identifier", "") or ""
                timeout = self.settings.get("cleanup_timeout", 30)
                debug_state = self.handler.get_debug_state(emby_identifier=identifier)

                import time as _time
                now = _time.time()

                plugin_name = PLUGIN_CONFIG.get('name', 'Emby Stream Cleanup')
                identifier_display = identifier or "(not set)"

                # Resolved IPs info
                resolved_ips = debug_state.get("resolved_ips", [])
                resolved_html = ""
                if resolved_ips and identifier:
                    resolved_html = f' &rarr; <span>{", ".join(resolved_ips)}</span>'

                # Build channel cards
                channels_html = ""
                if debug_state.get("channels"):
                    for ch_num, ch_data in sorted(debug_state["channels"].items(), key=lambda x: x[0]):
                        viewers = ch_data.get("viewers", 0)
                        sessions = ch_data.get("sessions", [])
                        cleanup_pending = ch_data.get("cleanup_pending", False)
                        cleanup_remaining = ch_data.get("cleanup_remaining")
                        channel_name = ch_data.get("channel_name", "")
                        clients = ch_data.get("dispatcharr_clients", [])

                        # Status determination
                        status_class = "active" if viewers > 0 else ("pending" if cleanup_pending else "idle")

                        if viewers > 0:
                            status_label = f"{viewers} Emby viewer(s) watching"
                            status_desc = "Cleanup will NOT run while viewers are active"
                        elif cleanup_pending:
                            status_label = f"Cleanup in {cleanup_remaining}s"
                            status_desc = "Last Emby viewer stopped — countdown to terminate matching clients"
                        else:
                            status_label = "Idle"
                            status_desc = "No active Emby viewers tracked"

                        # Channel header
                        name_html = f' <span class="channel-name">{channel_name}</span>' if channel_name else ""
                        card_html = f'''
                        <div class="card {status_class}">
                            <div class="card-header">
                                <span class="channel-num">CH {ch_num}{name_html}</span>
                                <span class="badge {status_class}">{status_label}</span>
                            </div>
                            <div class="status-desc">{status_desc}</div>'''

                        # Emby sessions
                        if sessions:
                            card_html += '<div class="section-label">Emby PlaySessionIds</div>'
                            for s in sessions:
                                card_html += f'<div class="session">{s}</div>'

                        # Dispatcharr clients
                        if clients:
                            emby_clients = [c for c in clients if c.get("is_emby_match")]
                            other_clients = [c for c in clients if not c.get("is_emby_match")]

                            if emby_clients:
                                card_html += f'<div class="section-label target">Dispatcharr Clients Matching Identifier ({len(emby_clients)})</div>'
                                card_html += '<div class="client-note target-note">These connections WILL be terminated when cleanup fires</div>'
                                for c in emby_clients:
                                    card_html += self._render_client_row(c, is_match=True)

                            if other_clients:
                                card_html += f'<div class="section-label safe">Other Dispatcharr Clients ({len(other_clients)})</div>'
                                card_html += '<div class="client-note safe-note">These connections will NOT be affected</div>'
                                for c in other_clients:
                                    card_html += self._render_client_row(c, is_match=False)
                        elif ch_data.get("channel_uuid"):
                            card_html += '<div class="no-clients">No active Dispatcharr clients on this channel</div>'

                        card_html += '</div>'
                        channels_html += card_html
                else:
                    channels_html = '<div class="empty">No channels being tracked — waiting for Emby webhook events</div>'

                html = f"""<!DOCTYPE html>
<html>
<head>
    <title>{plugin_name} - Debug</title>
    <meta http-equiv="refresh" content="5">
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
            max-width: 800px;
            margin: 40px auto;
            padding: 20px;
            background: #f5f5f5;
        }}
        .container {{
            background: white;
            padding: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        h1 {{ margin-top: 0; color: #333; font-size: 22px; }}
        h2 {{ color: #555; font-size: 16px; margin-top: 25px; border-bottom: 1px solid #eee; padding-bottom: 8px; }}
        .nav {{ margin-bottom: 20px; font-size: 13px; }}
        a {{ color: #0066cc; text-decoration: none; }}

        .config-table {{ font-size: 13px; color: #666; margin-bottom: 20px; width: 100%; }}
        .config-table td {{ padding: 3px 0; }}
        .config-table td:first-child {{ color: #999; width: 140px; }}
        .config-table span {{ color: #333; font-weight: 500; }}

        .explainer {{
            background: #f0f7ff;
            border: 1px solid #d0e3f7;
            border-radius: 6px;
            padding: 14px 16px;
            font-size: 13px;
            color: #2c5282;
            margin-bottom: 20px;
            line-height: 1.6;
        }}
        .explainer strong {{ color: #1a365d; }}

        .card {{
            border: 1px solid #e0e0e0;
            border-radius: 6px;
            padding: 14px 18px;
            margin-bottom: 12px;
        }}
        .card.active {{ border-left: 4px solid #4caf50; }}
        .card.pending {{ border-left: 4px solid #ff9800; }}
        .card.idle {{ border-left: 4px solid #9e9e9e; }}
        .card-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        .channel-num {{ font-weight: 600; font-size: 15px; }}
        .channel-name {{ font-weight: 400; color: #888; font-size: 13px; margin-left: 6px; }}
        .status-desc {{ font-size: 12px; color: #888; margin-top: 4px; font-style: italic; }}
        .badge {{
            font-size: 12px;
            padding: 3px 10px;
            border-radius: 12px;
            font-weight: 500;
            white-space: nowrap;
        }}
        .badge.active {{ background: #e8f5e9; color: #2e7d32; }}
        .badge.pending {{ background: #fff3e0; color: #e65100; }}
        .badge.idle {{ background: #f5f5f5; color: #757575; }}

        .section-label {{
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-top: 12px;
            margin-bottom: 4px;
            padding-top: 10px;
            border-top: 1px solid #f0f0f0;
        }}
        .section-label.target {{ color: #e65100; }}
        .section-label.safe {{ color: #2e7d32; }}

        .client-note {{
            font-size: 11px;
            font-style: italic;
            margin-bottom: 6px;
        }}
        .target-note {{ color: #e65100; }}
        .safe-note {{ color: #388e3c; }}

        .session {{
            font-family: monospace;
            font-size: 12px;
            color: #666;
            padding: 2px 0;
        }}

        .client-row {{
            font-size: 12px;
            padding: 6px 10px;
            margin: 3px 0;
            border-radius: 4px;
            font-family: monospace;
        }}
        .client-row.match {{
            background: #fff3e0;
            border: 1px solid #ffe0b2;
        }}
        .client-row.safe {{
            background: #f1f8e9;
            border: 1px solid #dcedc8;
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
        .client-field .label {{ color: #999; }}
        .client-field .value {{ color: #333; font-weight: 500; }}
        .match-reason {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
            font-size: 11px;
            color: #e65100;
            font-weight: 500;
        }}
        .safe-label {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
            font-size: 11px;
            color: #388e3c;
            font-weight: 500;
        }}

        .no-clients {{
            font-size: 12px;
            color: #bbb;
            font-style: italic;
            margin-top: 10px;
            padding-top: 10px;
            border-top: 1px solid #f0f0f0;
        }}

        .empty {{ color: #999; font-style: italic; padding: 20px 0; text-align: center; }}
        .refresh-note {{ font-size: 11px; color: #bbb; text-align: center; margin-top: 15px; }}
        .warn {{ color: #e65100; font-weight: 500; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="nav"><a href="/">&larr; Home</a> &nbsp;|&nbsp; <a href="/debug/webhook">Webhook Inspector</a></div>
        <h1>Debug</h1>

        <table class="config-table">
            <tr><td>Emby Identifier</td><td><span>{identifier_display}</span>{resolved_html}</td></tr>
            <tr><td>Cleanup Timeout</td><td><span>{timeout}s</span></td></tr>
        </table>

        <div class="explainer">
            <strong>How cleanup works:</strong>
            When all Emby viewers stop watching a channel, a <strong>{timeout}s countdown</strong> starts.
            If no Emby viewer reconnects before it expires, only Dispatcharr clients
            matching the identifier <strong>{identifier_display}</strong> are terminated.
            Non-matching clients are <strong>never</strong> affected.
        </div>

        <h2>Tracked Channels</h2>
        {channels_html}
        <div class="refresh-note">Auto-refreshes every 5 seconds</div>
    </div>
</body>
</html>"""
                start_response('200 OK', [('Content-Type', 'text/html; charset=utf-8')])
                return [html.encode('utf-8')]
            except Exception as e:
                logger.error(f"Error generating debug page: {e}", exc_info=True)
                start_response('500 Internal Server Error', [('Content-Type', 'text/plain')])
                return [b"Error generating debug page\n"]

        elif path == '/':
            plugin_name = PLUGIN_CONFIG.get('name', 'Emby Stream Cleanup')
            plugin_version = PLUGIN_CONFIG.get('version', 'unknown version').lstrip('-')
            plugin_description = PLUGIN_CONFIG.get('description', '')
            repo_url = PLUGIN_CONFIG.get('repo_url', 'https://github.com/sethwv/emby-stream-cleanup')

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
            background: #f5f5f5;
        }}
        .container {{
            background: white;
            padding: 40px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        h1 {{ margin-top: 0; color: #333; }}
        .version {{ color: #999; font-size: 14px; margin-top: -10px; margin-bottom: 20px; }}
        p {{ color: #666; line-height: 1.6; }}
        a {{ color: #0066cc; text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}
        .links {{ margin-top: 30px; padding-top: 20px; border-top: 1px solid #eee; }}
        .links a {{ display: inline-block; margin-right: 20px; font-weight: 500; }}
        code {{
            background: #f0f0f0;
            padding: 2px 6px;
            border-radius: 3px;
            font-size: 13px;
        }}
        .webhook-url {{
            background: #e8f5e9;
            padding: 10px 15px;
            border-radius: 4px;
            font-family: monospace;
            margin: 15px 0;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>{plugin_name}</h1>
        <div class="version">{plugin_version}</div>
        <p>{plugin_description}</p>
        <p>Configure this URL in Emby's webhook settings:</p>
        <div class="webhook-url">POST http://&lt;dispatcharr-host&gt;:{self.port}/webhook</div>
        <div class="links">
            <a href="/debug">Debug</a>
            <a href="/debug/webhook">Webhook Inspector</a>
            <a href="/health">Health Check</a>
            <a href="{repo_url}" target="_blank">GitHub</a>
        </div>
    </div>
</body>
</html>"""
            start_response('200 OK', [('Content-Type', 'text/html; charset=utf-8')])
            return [html.encode('utf-8')]

        else:
            start_response('404 Not Found', [('Content-Type', 'text/plain')])
            return [b"Not Found\n"]

    @staticmethod
    def _render_client_row(client, is_match):
        """Render a single Dispatcharr client as an HTML row."""
        row_class = "match" if is_match else "safe"
        ip = client.get("ip", "?")
        username = client.get("username", "")
        user_agent = client.get("user_agent", "")
        duration = client.get("connected_duration", "")
        match_reason = client.get("match_reason", "")

        label_html = ""
        if is_match:
            label_html = f'<span class="match-reason">WILL TERMINATE ({match_reason})</span>'
        else:
            label_html = '<span class="safe-label">SAFE — won\'t be affected</span>'

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

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def start(self, settings=None) -> bool:
        """Start the webhook server in a background thread.

        Returns True on success, False if the server could not be started.
        """
        if self.running:
            logger.warning("Webhook server is already running")
            return False

        # Guard against duplicate servers across workers via Redis
        redis_client = get_redis_client()
        if redis_client and read_redis_flag(redis_client, REDIS_KEY_RUNNING):
            logger.warning("Another webhook server instance is already running (detected via Redis)")
            return False

        # Guard against a duplicate in the same process
        current = get_current_server()
        if current and current.is_running():
            logger.warning("Another webhook server instance is already running in this process")
            return False

        # Check Dispatcharr version
        min_version = PLUGIN_CONFIG.get("min_dispatcharr_version", "1.0.0")
        try:
            dispatcharr_version, dispatcharr_timestamp, full_version = get_dispatcharr_version()
            if dispatcharr_version != "unknown":
                if dispatcharr_timestamp:
                    logger.info(f"Dev build detected ({full_version}), skipping version check")
                elif not compare_versions(dispatcharr_version, min_version):
                    logger.error(
                        f"Dispatcharr {dispatcharr_version} does not meet minimum requirement {min_version}"
                    )
                    return False
                else:
                    logger.info(f"Dispatcharr {dispatcharr_version} meets minimum requirement {min_version}")
            else:
                logger.warning("Could not determine Dispatcharr version, skipping check")
        except Exception as e:
            logger.warning(f"Could not verify Dispatcharr version: {e}. Proceeding anyway.")

        # Validate host / port binding
        logger.info(f"Attempting to bind to host='{self.host}', port={self.port}")
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
                    logger.debug(f"Starting gevent WSGI server on {self.host}:{self.port}")

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

                    # Announce via Redis
                    _rc = get_redis_client()
                    if _rc:
                        try:
                            _rc.set(REDIS_KEY_RUNNING, "1")
                            _rc.set(REDIS_KEY_HOST, self.host)
                            _rc.set(REDIS_KEY_PORT, str(self.port))
                        except Exception as e:
                            logger.warning(f"Could not set Redis running flags: {e}")

                    logger.info(f"Webhook server started on http://{self.host}:{self.port}/webhook")

                    from gevent import spawn, sleep
                    spawn(self.server.serve_forever)

                    # Monitor for Redis stop signal
                    monitor_redis = get_redis_client()
                    check_count = 0
                    while self.running:
                        try:
                            if monitor_redis and read_redis_flag(monitor_redis, REDIS_KEY_STOP):
                                logger.info("Stop signal detected via Redis, shutting down")
                                self.running = False
                                try:
                                    self.server.stop(timeout=5)
                                except Exception as e:
                                    logger.warning(f"Error during server.stop(): {e}")
                                self._verify_stopped(timeout=3)
                                break
                            elif not monitor_redis:
                                monitor_redis = get_redis_client()

                            check_count += 1
                            if check_count % 60 == 0:
                                logger.debug(
                                    f"Stop signal monitor alive (check #{check_count}), "
                                    f"server running on {self.host}:{self.port}"
                                )
                        except Exception as e:
                            logger.warning(f"Error checking stop signal (check #{check_count}): {e}")
                            monitor_redis = get_redis_client()

                        sleep(1)

                    # Cleanup Redis flags after stopping
                    _rc = get_redis_client()
                    if _rc:
                        try:
                            _rc.delete(REDIS_KEY_RUNNING, REDIS_KEY_HOST, REDIS_KEY_PORT, REDIS_KEY_STOP)
                        except Exception as e:
                            logger.warning(f"Could not clear Redis flags on shutdown: {e}")

                    set_current_server(None)
                    logger.info("Webhook server stopped and cleaned up")

                except Exception as e:
                    logger.error(f"Error running webhook server: {e}", exc_info=True)
                    self.running = False

            self.server_thread = threading.Thread(target=run_server, daemon=True)
            self.server_thread.start()

            # Brief wait for the server to bind and set running=True
            time.sleep(0.5)

            if self.running:
                return True
            else:
                return False

        except ImportError:
            logger.error("gevent is not installed")
            return False

    def stop(self) -> bool:
        """Stop the webhook server."""
        if not self.running:
            return False

        logger.info("Stopping webhook server...")

        if self.server:
            try:
                self.server.stop(timeout=5)
            except Exception as e:
                logger.warning(f"Error during server.stop(): {e}")
            self._verify_stopped(timeout=3)

        self.running = False
        set_current_server(None)

        # Clear Redis flags
        redis_client = get_redis_client()
        if redis_client:
            try:
                redis_client.delete(REDIS_KEY_RUNNING, REDIS_KEY_HOST, REDIS_KEY_PORT)
            except Exception as e:
                logger.warning(f"Could not clear Redis flags: {e}")

        return True

    def is_running(self) -> bool:
        """Return True if the server thread is alive and the server is marked running."""
        return self.running and self.server_thread is not None and self.server_thread.is_alive()

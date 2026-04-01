# Emby Stream Cleanup

A [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr) plugin that automatically terminates orphaned Dispatcharr connections when Emby users stop watching live TV.

## The Problem

When Emby connects to Dispatcharr as a live TV source, it opens a persistent client connection for each channel. When an Emby user stops watching, Emby often keeps that connection alive for a period of time, occupying one of your provider's limited connection slots. This plugin listens for Emby's playback webhooks and terminates the matching Dispatcharr connection after a configurable timeout once the last Emby viewer stops watching.

## How It Works

1. Emby sends **playback.start** and **playback.stop** webhook events to the plugin
2. The plugin tracks active Emby viewers per channel using Redis sets (keyed by PlaySessionId)
3. When the **last** viewer stops watching a channel, a countdown timer starts
4. If no viewer reconnects before the timer expires, the plugin finds and terminates only the Dispatcharr client connection that matches the configured Emby identifier
5. Non-Emby clients on the same channel are **never** affected

## Requirements

- Dispatcharr **v0.22.0** or later
- Emby Server with webhook support

## Installation

1. Download the latest release zip from [Releases](https://github.com/sethwv/emby-stream-cleanup/releases)
2. In Dispatcharr, go to **Plugins** and upload the zip
3. Restart Dispatcharr
4. Enable the plugin and configure settings

## Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| **Auto-Start Webhook Server** | `true` | Automatically start the webhook server when the plugin loads |
| **Webhook Server Port** | `9193` | Port for the webhook HTTP server |
| **Webhook Server Host** | `0.0.0.0` | Bind address (`0.0.0.0` for all interfaces) |
| **Emby Identifier** | *(required)* | IP address, hostname, or Xtream Codes username that Emby uses to connect to Dispatcharr. Used to identify which Dispatcharr client connections belong to Emby |
| **Cleanup Timeout** | `30` seconds | How long to wait after the last Emby viewer stops watching before terminating the connection |
| **Suppress Access Logs** | `true` | Hide per-request HTTP logging |

### Finding Your Emby Identifier

The plugin needs to know how Emby connects to Dispatcharr so it can match the right client connections. Look at the active clients in Dispatcharr's dashboard — Emby's connections will show up with either:

- **An IP address** — Emby server's IP (e.g. `192.168.1.100`)
- **A hostname** — Emby server's hostname (the plugin resolves hostnames to IPs for matching)
- **An XC username** — if Emby connects through Xtream Codes credentials

Use the **/debug** page (see below) to verify which clients match your identifier before relying on cleanup.

## Emby Webhook Setup

1. In Emby, go to **Settings → Notifications → Webhooks** (or install the webhooks plugin)
2. Add a new webhook with the URL:
   ```
   http://<dispatcharr-host>:9193/webhook
   ```
3. Enable the **Playback Start** and **Playback Stop** events
4. Save

### Docker Users

Make sure port `9193` is exposed in your `docker-compose.yml`:

```yaml
ports:
  - "9193:9193"
```

If Emby and Dispatcharr are on the same Docker network, use the container name as the host in the webhook URL.

## Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Landing page with plugin info and links |
| `/webhook` | POST | Receives Emby webhook events |
| `/health` | GET | Health check (returns `OK`) |
| `/debug` | GET | Debug dashboard showing tracked channels, viewer counts, Dispatcharr clients, cleanup countdowns, and identifier matching |
| `/debug/webhook` | POST/GET | Webhook inspector — POST any JSON to inspect it, GET to view the last received payload |

## Debug Page

The **/debug** page auto-refreshes every 5 seconds and shows:

- **Current configuration** — identifier, resolved IPs (for hostnames), cleanup timeout
- **How cleanup works** — a summary of the cleanup logic
- **Per tracked channel:**
  - Channel number and name
  - Number of active Emby viewers and their PlaySessionIds
  - Cleanup countdown (when last viewer stops)
  - **Dispatcharr clients matching your identifier** — marked as "WILL TERMINATE" with match reason (IP match, username match, or hostname resolution)
  - **Other Dispatcharr clients** — marked as "SAFE — won't be affected"

This makes it easy to verify your identifier is correct and see exactly what would happen during cleanup.

## Safety Guarantees

- **Only Emby-identified connections are terminated** — the plugin matches clients against the configured Emby Identifier. Non-matching clients are never touched.
- **Only when ALL Emby viewers stop** — cleanup only triggers when the last Emby viewer stops watching a channel. If multiple Emby users are watching the same channel, the connection stays alive until they all stop.
- **Timeout for reconnection** — after the last viewer stops, the configurable timeout (default 30s) gives time for users to reconnect (e.g. channel surfing) before the connection is terminated.

## License

MIT
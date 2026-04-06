# Embyfin Stream Cleanup

A [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr) plugin that monitors client activity and automatically terminates idle Emby/Jellyfin connections in Dispatcharr.

## The Problem

When Emby or Jellyfin connects to Dispatcharr for live TV, client connections persist even after users stop watching. This wastes provider stream slots and can hit connection limits.

## How It Works

1. Configure a client identifier (IP address, hostname, or XC username) to tell the plugin which connections belong to your media server.
2. A background monitor polls active Dispatcharr channels on a configurable interval (default: 10s).
3. If a matching client's `last_active` timestamp goes stale longer than the idle timeout (default: 30s), the connection is terminated via `ChannelService.stop_client()`.
4. When media server URLs are configured, the plugin also polls the Sessions API to detect **orphaned** connections - streams the media server considers stopped but Dispatcharr is still serving. These are terminated after confirmation over two consecutive poll cycles.

No webhooks or external configuration needed. The plugin watches Dispatcharr's own activity data directly.

## Safety

- Only connections matching the configured identifier are ever affected.
- Non-matching clients on the same channel are never touched.
- Active connections (still receiving data) are never terminated.
- The plugin only reads Dispatcharr's existing client metadata from Redis; it does not modify any upstream state.

## Installation

1. Download the latest release zip from the [releases page](https://github.com/sethwv/emby-stream-cleanup/releases).
2. In Dispatcharr, go to **Plugins** and upload the zip.
3. Restart Dispatcharr.
4. Enable the plugin and configure settings.

## Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| Client Identifier | _(empty)_ | IP, hostname, or XC username used to connect. Comma-separated for multiple values. Use `ALL` to match every client. |
| Idle Timeout | `30` | Seconds a matching client must be idle before termination |
| Poll Interval | `10` | How often (seconds) to scan for idle clients |
| Number of Media Servers | `1` | Number of Emby/Jellyfin servers to monitor for orphan detection. After changing, save and click the blue refresh button on the My Plugins page to see the new fields. |
| Media Server URL | _(empty)_ | Base URL (e.g. `http://192.168.1.100:8096`). Polls Sessions API for orphan detection. Leave blank to disable. |
| Media Server API Key | _(empty)_ | API key for the media server (Settings > API Keys) |
| Enable Debug Server | `false` | Start an HTTP debug dashboard (optional) |
| Mask Sensitive Data | `false` | Hide usernames, IPs, and URLs in the debug dashboard |
| Debug Server Port | `9193` | Port for the debug server |
| Debug Server Host | `0.0.0.0` | Host address to bind the debug server to |

Multiple media servers are supported. Increase the server count to add additional URL/API key pairs.

### Finding Your Client Identifier

Check Dispatcharr's active connections while your media server is streaming. The IP address or XC username shown for its connection is what you enter here. Multiple values can be comma-separated (e.g. `192.168.1.100, media-server`).

Hostnames are automatically resolved to IP addresses.

## Docker

If running in Docker, expose the debug server port to use the dashboard:

```yaml
ports:
  - "9193:9193"
```

## Debug Dashboard

When enabled, visit `http://<host>:9193/debug` to see:

- Media server pool status (per-server connectivity and active sessions)
- All active channels with connected clients
- Which clients match the configured identifier (highlighted)
- Current idle duration per client
- Recent termination history

The page auto-refreshes at the poll interval rate.

## Plugin Actions

In the Dispatcharr plugin settings page:

- **Restart Monitor** - Restart the background activity monitor (applies config changes)
- **Start / Stop Server** - Toggle the debug dashboard HTTP server
- **Check Status** - Show whether the monitor and debug server are running

## Requirements

- Dispatcharr v0.22.0 or later

## Building from Source

```bash
./package.sh
```

Creates a versioned zip file ready to upload to Dispatcharr.

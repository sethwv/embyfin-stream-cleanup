# Embyfin Stream Cleanup

A [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr) plugin that automatically terminates stale Emby/Jellyfin connections in Dispatcharr.

## The Problem

When Emby or Jellyfin connects to Dispatcharr for live TV, client connections persist even after users stop watching. This wastes provider stream slots and can hit connection limits.

## How It Works

1. Configure one or more media servers with their URL, API key, and the client identifier they use when connecting to Dispatcharr.
2. A background monitor polls active Dispatcharr channels on a configurable interval (default: 10s).
3. Connections are terminated when either condition is met:
   - **Idle**: no data has flowed for longer than the timeout (default: 30s)
   - **Orphaned**: the channel is no longer in the media server's active session pool for longer than the timeout
4. During stream failover or buffering the timer pauses automatically.

Only connections matching a configured identifier are ever affected. Non-matching clients are never touched.

## Installation

1. Download the latest release zip from the [releases page](https://github.com/sethwv/emby-stream-cleanup/releases).
2. In Dispatcharr, go to **Plugins** and upload the zip.
3. Restart Dispatcharr.
4. Enable the plugin and configure settings.

## Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| Timeout | `30` | Seconds before a matching connection is terminated (idle or absent from media server pool) |
| Poll Interval | `10` | How often (seconds) to scan for stale clients |
| Number of Media Servers | `1` | How many Emby/Jellyfin servers to monitor. Save and refresh the page to see new fields |
| Media Server URL | _(empty)_ | Base URL (e.g. `http://192.168.1.100:8096`) |
| Media Server API Key | _(empty)_ | API key (Settings > API Keys in Emby/Jellyfin) |
| Media Server Client Identifier | _(empty)_ | IP, hostname, or XC username the server uses when connecting to Dispatcharr. Comma-separated for multiple values |
| Enable Debug Server | `false` | Start an HTTP debug dashboard |
| Debug Server Port | `9193` | Port for the debug server |
| Debug Server Host | `0.0.0.0` | Bind address for the debug server |

### Finding Your Client Identifier

Check Dispatcharr's active connections while your media server is streaming. The IP address or XC username shown for its connection is what you enter as the identifier for that server.

## Plugin Actions

- **Restart Monitor** — Apply config changes (restarts monitor and debug server)
- **Check Status** — Show whether the monitor and debug server are running
- **Reset All Settings** — Wipe all saved settings and Redis keys

## Debug Dashboard

When enabled, visit `http://<host>:9193/debug` to see active channels, matched clients, media server pool status, and recent terminations.

## Requirements

- Dispatcharr v0.22.0 or later

## Building from Source

```bash
./package.sh
```

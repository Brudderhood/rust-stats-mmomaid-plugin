# Rust Stats Plugin (MMO Maid)

Adds a `/statscheck` slash command that pulls a Rust player profile from
[ruststats.io](https://ruststats.io) by Steam ID and posts a formatted embed
back into Discord.

## Usage

Two ways to invoke it:

**Slash command** (Discord shows the `steamid:` field — that label is part
of Discord's UI and can't be hidden on slash commands):

```
/statscheck steamid:76561198254115883
```

**Chat command** (no labels — just the prefix and the value):

```
!statscheck 76561198254115883
!statscheck https://steamcommunity.com/profiles/76561198254115883
!statscheck https://steamcommunity.com/id/somename
!statscheck somename
```

`?statscheck` and `.statscheck` work as alternate prefixes. Vanity names and
`steamcommunity.com/id/...` URLs are auto-resolved to a SteamID64 via
Steam's public XML endpoint (no API key required).

Either invocation returns an embed with overview, PvP, kills, deaths,
gathering, and accuracy stats — plus links back to Steam and ruststats.io.

## Packaging & uploading

1. Zip the plugin folder (do **not** include `sdk_extracted/` or the `.whl`):

   ```powershell
   Compress-Archive -Path __main__.py,manifest.json,requirements.txt,README.md -DestinationPath rust_stats_plugin.zip -Force
   ```

2. Upload `rust_stats_plugin.zip` in the [MMO Maid Developer Portal](https://mmomaid.com/dev).

3. On the upload form, declare:

   - **Capabilities:**
     - `proxy:http` (allowed domains: `ruststats.io`, `steamcommunity.com`)
     - `discord:send_message` (for `!statscheck` chat replies)
     - `events:message_content` (to read the message text)

   The `/statscheck` slash command itself is declared in `manifest.json`,
   not the upload form.

4. Click **Submit for Review**.

## Files

```
rust_stats_plugin/
├── __main__.py        Entry point — slash command handler
├── manifest.json      Declares the /statscheck slash command
├── requirements.txt   SDK dependency
└── README.md          This file
```

## Sandbox

Plugin runs in the standard MMO Maid Docker sandbox: 128 MB RAM, 0.25 CPU,
read-only filesystem, no direct network (HTTP only via the proxy capability).

## Docs

Full SDK documentation: <https://mmomaid.com/dev/docs>

# Rust Stats Plugin (MMO Maid)

Adds a `/statscheck` slash command that pulls a Rust player profile from
[ruststats.io](https://ruststats.io) by Steam ID and posts a formatted embed
back into Discord.

## Usage

In any channel the bot is in:

```
/statscheck steamid:76561198254115883
```

The plugin replies with a deferred embed containing overview, PvP, kills,
deaths, gathering, and accuracy stats — plus profile links.

## Packaging & uploading

1. Zip the plugin folder (do **not** include `sdk_extracted/` or the `.whl`):

   ```powershell
   Compress-Archive -Path __main__.py,requirements.txt,README.md -DestinationPath rust_stats_plugin.zip -Force
   ```

2. Upload `rust_stats_plugin.zip` in the [MMO Maid Developer Portal](https://mmomaid.com/dev).

3. On the upload form, declare:

   - **Capability:** `proxy:http`
   - **Allowed domain:** `ruststats.io`
   - **Slash command:** `statscheck`
     - Option: `steamid` — type `string`, required, description "SteamID64 (17 digits)"

4. Click **Submit for Review**.

## Files

```
rust_stats_plugin/
├── __main__.py        Entry point — slash command handler
├── requirements.txt   SDK dependency
└── README.md          This file
```

## Sandbox

Plugin runs in the standard MMO Maid Docker sandbox: 128 MB RAM, 0.25 CPU,
read-only filesystem, no direct network (HTTP only via the proxy capability).

## Docs

Full SDK documentation: <https://mmomaid.com/dev/docs>

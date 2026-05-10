# Rust Stats + Raid Plugin (MMO Maid)

Two slash commands for Rust players in Discord:

- `/statscheck` — pulls a player profile from
  [ruststats.io](https://ruststats.io) by Steam ID and posts a formatted embed.
- `/raidcheck` — calculates raid cost (single-tool counts and the cheapest
  C4/Rocket combo) for a wall or door, with sulfur totals and tool icons.

## Usage

### `/statscheck`

Slash command (Discord shows the `steamid:` field — that label is part of
Discord's UI and can't be hidden):

```
/statscheck steamid:76561198254115883
```

Chat fallback (literal text, useful for paste/forward):

```
/statscheck 76561198254115883
/statscheck https://steamcommunity.com/profiles/76561198254115883
/statscheck https://steamcommunity.com/id/somename
/statscheck somename
```

Vanity names and `steamcommunity.com/id/...` URLs are auto-resolved to a
SteamID64 via Steam's public XML endpoint (no API key required).

### `/raidcheck`

```
/raidcheck target:hqmwall
/raidcheck target:armoreddoor quantity:4
/raidcheck target:stonewall
```

Chat fallback:

```
/raidcheck hqmwall
/raidcheck armoreddoor 4
/raidcheck garagedoor 2
```

Targets (case/space/dash-insensitive aliases accepted):

| Target                   | Aliases                                     |
| ------------------------ | ------------------------------------------- |
| Sheet Metal Door         | `sheetdoor`, `smdoor`, `sheet`              |
| Garage Door              | `garagedoor`, `garage`, `gd`                |
| Armored Door             | `armoreddoor`, `armoured`, `armored`, `ad`  |
| Stone Wall (hard side)   | `stonewall`, `stone`, `swhard`, `sw`        |
| Stone Wall (soft side)   | `stonewallsoft`, `softside`, `sws`          |
| Sheet Metal Wall         | `metalwall`, `sheetmetalwall`, `metal`, `smw` |
| Armored Wall (HQM)       | `hqmwall`, `hqm`, `armoredwall`, `aw`       |

The embed shows:

- 💣 **Raid combo (C4/Rocket)** — the cheapest 1- or 2-tool mix using C4
  and/or Rocket (e.g. HQM wall = `7 C4 + 1 Rocket = 16,800 sulfur`).
- 💸 **Cheapest (incl. bullets/throwables)** — only shown when bullets or
  throwables actually beat the C4/Rocket combo (often a slow grind).
- 📊 **Single-tool options table** — count and sulfur for every viable tool.

Damage values are calibrated against [rusthelp.com](https://rusthelp.com).
If a future patch changes a tool's damage, edit only the affected
target's `damage` array in `RAID_TARGETS`.

## Packaging & uploading

1. Zip the plugin folder (do **not** include `sdk_extracted/`, `.whl`, or
   `script.py`):

   ```powershell
   Compress-Archive -Path __main__.py,manifest.json,requirements.txt,README.md -DestinationPath rust_stats_plugin.zip -Force
   ```

2. Upload `rust_stats_plugin.zip` in the [MMO Maid Developer Portal](https://mmomaid.com/dev).

3. On the upload form, declare:

   - **Capabilities:**
     - `proxy:http` (allowed domains: `ruststats.io`, `steamcommunity.com`)
     - `discord:send_message` (for chat-text replies)
     - `events:message_content` (to read `/statscheck` and `/raidcheck` text)

   The `/statscheck` and `/raidcheck` slash commands are declared in
   `manifest.json`, not the upload form.

4. Click **Submit for Review**.

> The running Discord bot serves whatever zip is currently published in the
> portal — pushing to git alone does **not** update the bot. Re-upload
> after every code change.

## Files

```
rust_stats_plugin/
├── __main__.py        Entry point — slash + chat handlers, raid calculator
├── manifest.json      Declares /statscheck and /raidcheck slash commands
├── requirements.txt   SDK dependency
└── README.md          This file
```

## Sandbox

Plugin runs in the standard MMO Maid Docker sandbox: 128 MB RAM, 0.25 CPU,
read-only filesystem, no direct network (HTTP only via the proxy capability).

## Docs

Full SDK documentation: <https://mmomaid.com/dev/docs>

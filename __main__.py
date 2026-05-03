"""
Rust Stats Plugin
=================

Adds a /statscheck slash command that fetches a Rust player profile from
ruststats.io by Steam ID and posts a formatted embed back to Discord.

Capabilities required:
  - proxy:http   (allowed domain: ruststats.io)

Slash commands to register in the Dev Portal:
  - statscheck (option: steamid, type=string, required)

Docs: https://mmomaid.com/dev/docs
"""
import json
import re

from mmo_maid_sdk import Plugin, Context

plugin = Plugin()

RUSTSTATS_URL = "https://ruststats.io/api/rpc/get_profile"
STEAMID_RE = re.compile(r"^\d{17}$")

# Discord embed color (Rust orange-ish)
EMBED_COLOR = 0xCD412B


@plugin.on_ready
def on_ready(ctx: Context):
    ctx.log("Rust Stats plugin ready")


def _extract_option(event: dict, name: str) -> str:
    """Pull a slash command option value out of the event payload.

    The runner may surface options as either {"options": {"name": value}}
    or {"options": [{"name": ..., "value": ...}, ...]}, so handle both.
    """
    opts = event.get("options")
    if isinstance(opts, dict):
        val = opts.get(name)
        if val is not None:
            return str(val).strip()
    if isinstance(opts, list):
        for item in opts:
            if isinstance(item, dict) and item.get("name") == name:
                return str(item.get("value", "")).strip()
    # Last resort: top-level key on the event itself
    val = event.get(name)
    return str(val).strip() if val is not None else ""


def _format_kv(d: dict, keys: list) -> str:
    """Render selected key/value pairs from a stats sub-dict, skipping missing."""
    lines = []
    for key, label in keys:
        v = d.get(key)
        if v in (None, "", "0"):
            continue
        lines.append(f"**{label}:** {v}")
    return "\n".join(lines) if lines else "—"


def _build_embed(profile: dict) -> dict:
    """Turn the ruststats.io profile JSON into a Discord embed."""
    name = profile.get("personaname") or "Unknown"
    steamid = profile.get("steamid", "")
    avatar = profile.get("avatar_full_url") or profile.get("avatar_url") or ""
    is_banned = profile.get("is_banned")
    is_private = profile.get("is_private")

    overview = profile.get("overview") or {}
    pvp = profile.get("pvp_stats") or {}
    kills = profile.get("kills") or {}
    deaths = profile.get("deaths") or {}
    gathered = profile.get("gathered") or {}
    bullets = profile.get("bullets_hit") or {}

    fields = []

    # Overview
    overview_text = _format_kv(overview, [
        ("time_played", "Time Played"),
        ("played_last_2weeks", "Last 2 Weeks"),
        ("account_created", "Account Created"),
        ("achievement_count", "Achievements"),
    ])
    fields.append({"name": "Overview", "value": overview_text, "inline": True})

    # PvP
    pvp_text = _format_kv(pvp, [
        ("kdr", "K/D"),
        ("kills", "Kills"),
        ("deaths", "Deaths"),
        ("headshots", "Headshots"),
        ("headshot_percent", "Headshot %"),
        ("bullets_fired", "Bullets Fired"),
        ("bullets_hit_percent", "Hit %"),
    ])
    fields.append({"name": "PvP", "value": pvp_text, "inline": True})

    # Kills breakdown
    kills_text = _format_kv(kills, [
        ("players", "Players"),
        ("scientists", "Scientists"),
        ("bears", "Bears"),
        ("wolves", "Wolves"),
        ("boars", "Boars"),
        ("deer", "Deer"),
        ("chickens", "Chickens"),
        ("horses", "Horses"),
    ])
    fields.append({"name": "Kills", "value": kills_text, "inline": True})

    # Deaths
    deaths_text = _format_kv(deaths, [
        ("total", "Total"),
        ("suicide", "Suicides"),
        ("self_inflicted", "Self-Inflicted"),
        ("fall", "Fall"),
    ])
    fields.append({"name": "Deaths", "value": deaths_text, "inline": True})

    # Gathered
    gathered_text = _format_kv(gathered, [
        ("wood", "Wood"),
        ("stone", "Stone"),
        ("metal_ore", "Metal Ore"),
        ("scrap", "Scrap"),
        ("cloth", "Cloth"),
        ("leather", "Leather"),
        ("low_grade_fuel", "Low Grade"),
    ])
    fields.append({"name": "Gathered", "value": gathered_text, "inline": True})

    # Combat accuracy
    bullets_text = _format_kv(bullets, [
        ("players", "Hits on Players"),
        ("buildings", "Hits on Buildings"),
        ("bears", "Hits on Bears"),
        ("wolves", "Hits on Wolves"),
    ])
    fields.append({"name": "Bullets Hit", "value": bullets_text, "inline": True})

    # Header line: status flags
    status_bits = []
    if is_banned:
        status_bits.append("⛔ BANNED")
    if is_private:
        status_bits.append("🔒 Private profile")
    description_parts = []
    if status_bits:
        description_parts.append(" • ".join(status_bits))
    description_parts.append(f"Steam ID: `{steamid}`")
    description_parts.append(f"[Steam Profile](https://steamcommunity.com/profiles/{steamid}) • [ruststats.io](https://ruststats.io/profile/{steamid})")

    embed = {
        "title": f"🎯 {name} — Rust Stats",
        "url": f"https://ruststats.io/profile/{steamid}",
        "description": "\n".join(description_parts),
        "color": EMBED_COLOR,
        "fields": fields,
    }
    if avatar:
        embed["thumbnail"] = {"url": avatar}

    last_update = profile.get("last_update_at")
    if last_update:
        embed["footer"] = {"text": f"Last updated: {last_update}"}

    return embed


@plugin.on_slash_command("statscheck")
def handle_statscheck(ctx: Context, event: dict):
    steamid = _extract_option(event, "steamid")

    if not steamid:
        ctx.interaction.respond(
            content="❌ Please provide a Steam ID. Example: `/statscheck steamid:76561198254115883`",
            ephemeral=True,
        )
        return

    if not STEAMID_RE.match(steamid):
        ctx.interaction.respond(
            content=f"❌ `{steamid}` doesn't look like a SteamID64 (17 digits).",
            ephemeral=True,
        )
        return

    # Fetch can take a moment; defer so we don't blow the 3s response window.
    ctx.interaction.defer()

    try:
        resp = ctx.http.post(
            RUSTSTATS_URL,
            body=json.dumps({"id": steamid}),
            headers={
                "Content-Type": "application/json",
                "Accept": "*/*",
                "Origin": "https://ruststats.io",
                "Referer": f"https://ruststats.io/profile/{steamid}",
                "User-Agent": "Mozilla/5.0 (compatible; MMOMaidPlugin/1.0)",
            },
        )
    except Exception as exc:
        ctx.log(f"ruststats.io request failed: {exc}", level="error")
        ctx.interaction.followup(
            content="⚠️ Failed to reach ruststats.io. Try again in a moment.",
            ephemeral=True,
        )
        return

    status = resp.get("status", 0)
    body = resp.get("body") or resp.get("body_bytes") or ""

    if status != 200:
        ctx.log(f"ruststats.io non-200 ({status}) for {steamid}", level="warning")
        ctx.interaction.followup(
            content=f"⚠️ ruststats.io returned status {status}. The profile may not exist.",
            ephemeral=True,
        )
        return

    try:
        profile = json.loads(body) if isinstance(body, str) else body
    except (ValueError, TypeError) as exc:
        ctx.log(f"Failed to parse ruststats.io JSON: {exc}", level="error")
        ctx.interaction.followup(
            content="⚠️ Got a bad response from ruststats.io.",
            ephemeral=True,
        )
        return

    if not isinstance(profile, dict) or not profile.get("steamid"):
        ctx.interaction.followup(
            content=f"⚠️ No profile found for Steam ID `{steamid}`.",
            ephemeral=True,
        )
        return

    embed = _build_embed(profile)
    ctx.interaction.followup(embeds=[embed])


# ── This must be the last line ────────────────────────────────────────────────
plugin.run()

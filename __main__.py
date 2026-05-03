"""
Rust Stats Plugin
=================

Adds a /statscheck slash command that fetches a Rust player profile from
ruststats.io and posts a formatted embed back to Discord.

Accepted input forms (any of these work):
  - SteamID64               76561198254115883
  - Profile URL             https://steamcommunity.com/profiles/7656...
  - Vanity URL              https://steamcommunity.com/id/somename
  - Vanity name             somename

Capabilities required:
  - proxy:http   (allowed domains: ruststats.io, steamcommunity.com)

Slash command (declared in manifest.json):
  - statscheck (option: steamid, type=string, required)

Docs: https://mmomaid.com/dev/docs
"""
import json
import re

from mmo_maid_sdk import Plugin, Context

plugin = Plugin()

RUSTSTATS_URL = "https://ruststats.io/api/rpc/get_profile"

STEAMID64_RE     = re.compile(r"^\d{17}$")
PROFILE_URL_RE   = re.compile(r"steamcommunity\.com/profiles/(\d{17})", re.I)
VANITY_URL_RE    = re.compile(r"steamcommunity\.com/id/([A-Za-z0-9_\-]+)", re.I)
VANITY_NAME_RE   = re.compile(r"^[A-Za-z0-9_\-]{2,32}$")
STEAMID64_TAG_RE = re.compile(r"<steamID64>(\d{17})</steamID64>")
DIGITS17_RE      = re.compile(r"\b(\d{17})\b")

EMBED_COLOR = 0xCD412B  # Rust orange-ish


@plugin.on_ready
def on_ready(ctx: Context):
    ctx.log("Rust Stats plugin ready")


def _extract_option(event: dict, name: str) -> str:
    """Pull a slash command option value from the event payload.

    The runner's exact event shape isn't documented, so try every reasonable
    container we might see: dict-of-options, list-of-{name,value} dicts,
    Discord-raw "data.options", and top-level keys.
    """
    containers = [
        event.get("options"),
        event.get("params"),
        event.get("arguments"),
        event.get("args"),
        (event.get("data") or {}).get("options") if isinstance(event.get("data"), dict) else None,
    ]

    for opts in containers:
        if isinstance(opts, dict):
            v = opts.get(name)
            if v not in (None, ""):
                return str(v).strip()
        elif isinstance(opts, list):
            for item in opts:
                if isinstance(item, dict) and item.get("name") == name:
                    v = item.get("value")
                    if v not in (None, ""):
                        return str(v).strip()

    # Top-level fallback (some runners flatten options onto the event)
    v = event.get(name)
    if v not in (None, ""):
        return str(v).strip()

    return ""


def _resolve_to_steamid64(ctx: Context, raw: str) -> tuple[str, str | None]:
    """Resolve raw input → (steamid64, error_message_or_None)."""
    s = raw.strip()
    if not s:
        return "", "missing input"

    # Already a SteamID64
    if STEAMID64_RE.match(s):
        return s, None

    # Profile URL containing the SteamID64
    m = PROFILE_URL_RE.search(s)
    if m:
        return m.group(1), None

    # Vanity URL → extract vanity name
    m = VANITY_URL_RE.search(s)
    if m:
        vanity = m.group(1)
    elif VANITY_NAME_RE.match(s):
        vanity = s
    else:
        # Last try: any 17-digit run anywhere in the string
        m = DIGITS17_RE.search(s)
        if m:
            return m.group(1), None
        return "", "couldn't parse that as a Steam ID, profile URL, or vanity name"

    # Resolve vanity → SteamID64 via Steam's public XML endpoint (no API key needed)
    try:
        resp = ctx.http.get(
            f"https://steamcommunity.com/id/{vanity}/?xml=1",
            headers={"User-Agent": "Mozilla/5.0 (compatible; MMOMaidPlugin/1.0)"},
        )
    except Exception as exc:
        ctx.log(f"vanity resolve failed for {vanity!r}: {exc}", level="error")
        return "", "couldn't reach steamcommunity.com to resolve that name"

    body = resp.get("body") or resp.get("body_bytes") or ""
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="ignore")

    m = STEAMID64_TAG_RE.search(body)
    if m:
        return m.group(1), None

    return "", f"no Steam profile found for `{vanity}`"


def _format_kv(d: dict, keys: list) -> str:
    lines = []
    for key, label in keys:
        v = d.get(key)
        if v in (None, "", "0"):
            continue
        lines.append(f"**{label}:** {v}")
    return "\n".join(lines) if lines else "—"


def _build_embed(profile: dict) -> dict:
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

    fields.append({"name": "Overview", "value": _format_kv(overview, [
        ("time_played", "Time Played"),
        ("played_last_2weeks", "Last 2 Weeks"),
        ("account_created", "Account Created"),
        ("achievement_count", "Achievements"),
    ]), "inline": True})

    fields.append({"name": "PvP", "value": _format_kv(pvp, [
        ("kdr", "K/D"),
        ("kills", "Kills"),
        ("deaths", "Deaths"),
        ("headshots", "Headshots"),
        ("headshot_percent", "Headshot %"),
        ("bullets_fired", "Bullets Fired"),
        ("bullets_hit_percent", "Hit %"),
    ]), "inline": True})

    fields.append({"name": "Kills", "value": _format_kv(kills, [
        ("players", "Players"),
        ("scientists", "Scientists"),
        ("bears", "Bears"),
        ("wolves", "Wolves"),
        ("boars", "Boars"),
        ("deer", "Deer"),
        ("chickens", "Chickens"),
        ("horses", "Horses"),
    ]), "inline": True})

    fields.append({"name": "Deaths", "value": _format_kv(deaths, [
        ("total", "Total"),
        ("suicide", "Suicides"),
        ("self_inflicted", "Self-Inflicted"),
        ("fall", "Fall"),
    ]), "inline": True})

    fields.append({"name": "Gathered", "value": _format_kv(gathered, [
        ("wood", "Wood"),
        ("stone", "Stone"),
        ("metal_ore", "Metal Ore"),
        ("scrap", "Scrap"),
        ("cloth", "Cloth"),
        ("leather", "Leather"),
        ("low_grade_fuel", "Low Grade"),
    ]), "inline": True})

    fields.append({"name": "Bullets Hit", "value": _format_kv(bullets, [
        ("players", "Hits on Players"),
        ("buildings", "Hits on Buildings"),
        ("bears", "Hits on Bears"),
        ("wolves", "Hits on Wolves"),
    ]), "inline": True})

    status_bits = []
    if is_banned:
        status_bits.append("⛔ BANNED")
    if is_private:
        status_bits.append("🔒 Private profile")
    description_parts = []
    if status_bits:
        description_parts.append(" • ".join(status_bits))
    description_parts.append(f"Steam ID: `{steamid}`")
    description_parts.append(
        f"[Steam Profile](https://steamcommunity.com/profiles/{steamid}) • "
        f"[ruststats.io](https://ruststats.io/profile/{steamid})"
    )

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
    raw = _extract_option(event, "steamid")

    if not raw:
        # Log the event keys so we can see what shape options arrive in
        ctx.log(
            f"statscheck: no steamid option found. event keys: {list(event.keys())} "
            f"event preview: {json.dumps(event, default=str)[:1500]}",
            level="warning",
        )
        ctx.interaction.respond(
            content=(
                "❌ Please provide a Steam ID, profile URL, or vanity name.\n"
                "Examples:\n"
                "• `/statscheck 76561198254115883`\n"
                "• `/statscheck https://steamcommunity.com/id/somename`\n"
                "• `/statscheck somename`"
            ),
            ephemeral=True,
        )
        return

    # Defer immediately — vanity resolution + ruststats.io call can exceed 3s
    ctx.interaction.defer()

    steamid, err = _resolve_to_steamid64(ctx, raw)
    if err:
        ctx.interaction.followup(content=f"❌ {err}: `{raw}`", ephemeral=True)
        return

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
        profile = json.loads(body) if isinstance(body, (str, bytes)) else body
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

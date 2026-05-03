"""
Rust Stats Plugin
=================

Two ways to look up a Rust player's stats from ruststats.io:

  1. Slash command:   /statscheck steamid:<value>
  2. Chat command:    !statscheck <value>

Accepted input forms (any of these work for either command):
  - SteamID64               76561198254115883
  - Profile URL             https://steamcommunity.com/profiles/7656...
  - Vanity URL              https://steamcommunity.com/id/somename
  - Vanity name             somename

Capabilities required:
  - proxy:http               (allowed domains: ruststats.io, steamcommunity.com)
  - discord:send_message     (for chat-command replies)
  - events:message_content   (to read !statscheck messages)

Slash command is declared in manifest.json.

Docs: https://mmomaid.com/dev/docs
"""
import gzip
import json
import re
import time

from mmo_maid_sdk import Plugin, Context, RateLimitError

plugin = Plugin()

RUSTSTATS_URL = "https://ruststats.io/api/rpc/get_profile"

STEAMID64_RE       = re.compile(r"^\d{17}$")
PROFILE_URL_RE     = re.compile(r"steamcommunity\.com/profiles/(\d{17})", re.I)
VANITY_URL_RE      = re.compile(r"steamcommunity\.com/id/([A-Za-z0-9_\-]+)", re.I)
VANITY_NAME_RE     = re.compile(r"^[A-Za-z0-9_\-]{2,32}$")
STEAMID64_TAG_RE   = re.compile(r"<steamID64>(\d{17})</steamID64>")
STEAMID_JSON_RE    = re.compile(r'"steamid"\s*:\s*"(\d{17})"')
DIGITS17_RE        = re.compile(r"\b(\d{17})\b")

EMBED_COLOR = 0xCD412B  # Rust orange-ish

PROXY_RETRY_ATTEMPTS = 3   # extra tries after the first, on RateLimitError
PROXY_RETRY_CAP_S    = 65  # per-attempt sleep cap (proxy quota window is 1 min)


def _proxy_call(ctx: Context, label: str, fn, *args, **kwargs):
    """Run an http proxy call, sleeping + retrying when the proxy quota trips.

    The SDK raises RateLimitError with a retry_after hint computed from the
    proxy's "remaining=X.X/min" message (60s when fully exhausted). We honour
    it so a near-empty quota doesn't fail the user-visible request.
    """
    last_exc: RateLimitError | None = None
    for attempt in range(PROXY_RETRY_ATTEMPTS + 1):
        try:
            return fn(*args, **kwargs)
        except RateLimitError as exc:
            last_exc = exc
            if attempt >= PROXY_RETRY_ATTEMPTS:
                break
            delay = min(max(int(getattr(exc, "retry_after", 0) or 5), 1), PROXY_RETRY_CAP_S)
            ctx.log(
                f"{label}: proxy quota exhausted ({exc}); sleeping {delay}s "
                f"and retrying ({attempt + 1}/{PROXY_RETRY_ATTEMPTS})",
                level="warning",
            )
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


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


def _http_get_text(ctx: Context, url: str) -> tuple[int, str]:
    """GET a URL via the proxy, returning (status, body_as_text).

    Forces Accept-Encoding: identity so Steam doesn't gzip the response
    (the proxy hands compressed bodies through as binary-looking text).
    Also tries to decompress gzip in-process as a belt-and-braces fallback.
    """
    resp = _proxy_call(
        ctx,
        f"GET {url}",
        ctx.http.get,
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) MMOMaidPlugin/1.0",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "identity",  # please don't compress, the proxy can't decode it
        },
    )
    status = int(resp.get("status", 0) or 0)
    body = resp.get("body") or resp.get("body_bytes") or ""

    # If the body still looks gzip-ish (proxy ignored Accept-Encoding), try to decompress.
    if isinstance(body, (bytes, bytearray)):
        if body[:2] == b"\x1f\x8b":
            try:
                body = gzip.decompress(bytes(body))
            except Exception:
                pass
        body = bytes(body).decode("utf-8", errors="replace")
    elif isinstance(body, str) and body.startswith("\x1f"):
        # Body arrived as a string with binary gzip bytes lossily decoded as text.
        # We can't recover the original bytes, but flag it so the caller can log.
        ctx.log(
            f"_http_get_text: body looks gzip-compressed but was lossily decoded "
            f"to text by the proxy — cannot decompress. url={url}",
            level="warning",
        )

    return status, body


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

    # ── Strategy 1: XML endpoint — returns <steamID64>...</steamID64> ─────
    xml_url = f"https://steamcommunity.com/id/{vanity}/?xml=1"
    try:
        status, body = _http_get_text(ctx, xml_url)
    except Exception as exc:
        ctx.log(f"vanity XML lookup failed for {vanity!r}: {exc}", level="error")
        status, body = 0, ""

    if status == 200 and body:
        m = STEAMID64_TAG_RE.search(body)
        if m:
            return m.group(1), None
        if "could not be found" in body.lower() or "no users matching" in body.lower():
            return "", f"no Steam profile with vanity name `{vanity}`"

    # If non-200 or unparseable, log the diagnostics for debugging
    if status != 200 or not body:
        ctx.log(
            f"vanity XML non-200 for {vanity!r}: status={status} body_len={len(body)} "
            f"preview={body[:300]!r}",
            level="warning",
        )
        if status in (403, 451, 0):
            return "", (
                f"couldn't resolve `{vanity}` — make sure `steamcommunity.com` is "
                "in the plugin's allowed domains (Dev Portal upload form)."
            )

    # ── Strategy 2: scrape HTML page — has g_rgProfileData JS with steamid ─
    html_url = f"https://steamcommunity.com/id/{vanity}/"
    try:
        status, body = _http_get_text(ctx, html_url)
    except Exception as exc:
        ctx.log(f"vanity HTML lookup failed for {vanity!r}: {exc}", level="error")
        return "", f"couldn't reach steamcommunity.com to resolve `{vanity}`"

    if status == 200 and body:
        m = STEAMID_JSON_RE.search(body)
        if m:
            return m.group(1), None

    ctx.log(
        f"vanity HTML resolve: no steamid found for {vanity!r}: status={status} "
        f"body_len={len(body)} preview={body[:300]!r}",
        level="warning",
    )
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
        f"[Steam Profile](https://steamcommunity.com/profiles/{steamid})"
    )

    embed = {
        "title": f"🎯 {name} — Rust Stats",
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


TRANSIENT_HTTP_STATUSES = (500, 502, 503, 504)
TRANSIENT_RETRY_DELAYS_S = (3, 8, 20)  # ruststats.io 5xx are usually transient


def _fetch_profile(ctx: Context, steamid: str) -> tuple[dict | None, str | None]:
    """Hit ruststats.io for the profile. Returns (profile, error_message).

    Retries on transient HTTP errors (5xx) — observed in the wild that
    ruststats.io occasionally returns 500 on the first hit and succeeds
    on the next call seconds later.
    """
    last_status = 0
    last_body: object = ""

    for attempt in range(len(TRANSIENT_RETRY_DELAYS_S) + 1):
        try:
            resp = _proxy_call(
                ctx,
                f"ruststats.io profile {steamid}",
                ctx.http.post,
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
        except RateLimitError as exc:
            ctx.log(f"ruststats.io rate-limited after retries: {exc}", level="error")
            return None, "Stats service is rate-limited right now. Try again in about a minute."
        except Exception as exc:
            ctx.log(
                f"ruststats.io request failed: {type(exc).__name__}: {exc!r}",
                level="error",
            )
            return None, "Couldn't reach the stats service. Try again in a moment."

        try:
            status = int(resp.get("status", 0) or 0)
        except (AttributeError, ValueError, TypeError):
            status = 0
        body = resp.get("body") or resp.get("body_bytes") or ""
        last_status, last_body = status, body

        if status == 200:
            break

        if status in TRANSIENT_HTTP_STATUSES and attempt < len(TRANSIENT_RETRY_DELAYS_S):
            delay = TRANSIENT_RETRY_DELAYS_S[attempt]
            ctx.log(
                f"ruststats.io transient {status} for {steamid}; sleeping {delay}s "
                f"and retrying ({attempt + 1}/{len(TRANSIENT_RETRY_DELAYS_S)})",
                level="warning",
            )
            time.sleep(delay)
            continue

        # Non-transient status, or transient retries exhausted.
        ctx.log(
            f"ruststats.io non-200 ({status}) for {steamid} after {attempt + 1} "
            f"attempt(s); body preview={str(body)[:300]!r}",
            level="warning",
        )
        if status in TRANSIENT_HTTP_STATUSES:
            return None, "Stats service is having a moment. Try again shortly."
        return None, f"Stats service returned status {status}. The profile may not exist."

    try:
        profile = json.loads(last_body) if isinstance(last_body, (str, bytes)) else last_body
    except (ValueError, TypeError) as exc:
        ctx.log(f"Failed to parse ruststats.io JSON: {exc}", level="error")
        return None, "Got a bad response from the stats service."

    if not isinstance(profile, dict) or not profile.get("steamid"):
        return None, f"No profile found for Steam ID `{steamid}`."

    return profile, None


USAGE_HELP = (
    "❌ Please provide a Steam ID, profile URL, or vanity name.\n"
    "Examples:\n"
    "• `!statscheck 76561198254115883`\n"
    "• `!statscheck https://steamcommunity.com/id/somename`\n"
    "• `!statscheck somename`"
)


@plugin.on_slash_command("statscheck")
def handle_statscheck_slash(ctx: Context, event: dict):
    raw = _extract_option(event, "steamid")

    if not raw:
        ctx.log(
            f"statscheck: no steamid option found. event keys: {list(event.keys())} "
            f"event preview: {json.dumps(event, default=str)[:1500]}",
            level="warning",
        )
        ctx.interaction.respond(content=USAGE_HELP, ephemeral=True)
        return

    ctx.interaction.defer()

    steamid, err = _resolve_to_steamid64(ctx, raw)
    if err:
        ctx.interaction.followup(content=f"❌ {err}: `{raw}`", ephemeral=True)
        return

    profile, err = _fetch_profile(ctx, steamid)
    if err:
        ctx.interaction.followup(content=f"⚠️ {err}", ephemeral=True)
        return

    ctx.interaction.followup(embeds=[_build_embed(profile)])


CHAT_PREFIXES = ("!statscheck", "?statscheck", ".statscheck")


@plugin.on_event("message_create")
def handle_statscheck_chat(ctx: Context, event: dict):
    content = (event.get("content") or "").strip()
    if not content:
        return

    parts = content.split(None, 1)
    cmd = parts[0].lower()
    if cmd not in CHAT_PREFIXES:
        return

    channel_id = event.get("channel_id")
    if not channel_id:
        return

    if len(parts) < 2 or not parts[1].strip():
        ctx.discord.send_message(channel_id=channel_id, content=USAGE_HELP)
        return

    raw = parts[1].strip()

    # Immediate ack so the user knows we received it. Resolution + fetch can
    # take a while when the proxy quota is near-empty (we sleep + retry).
    ctx.discord.send_message(
        channel_id=channel_id,
        content=f"🔍 Looking up stats for `{raw}`…",
    )

    steamid, err = _resolve_to_steamid64(ctx, raw)
    if err:
        ctx.discord.send_message(channel_id=channel_id, content=f"❌ {err}: `{raw}`")
        return

    profile, err = _fetch_profile(ctx, steamid)
    if err:
        ctx.discord.send_message(channel_id=channel_id, content=f"⚠️ {err}")
        return

    ctx.discord.send_message(channel_id=channel_id, embeds=[_build_embed(profile)])


# ── This must be the last line ────────────────────────────────────────────────
plugin.run()

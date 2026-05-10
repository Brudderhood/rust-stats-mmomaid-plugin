"""
Rust Stats + Raid Plugin
========================

Slash commands:
  /statscheck steamid:<value>          look up a player's ruststats.io profile
  /raidcheck  target:<id> [quantity:N] raid-cost table for a wall or door

Chat commands (mirrors of the slash commands, for users who type literal text):
  /statscheck <value>
  /raidcheck  <target> [quantity]

Accepted statscheck input forms:
  - SteamID64               76561198254115883
  - Profile URL             https://steamcommunity.com/profiles/7656...
  - Vanity URL              https://steamcommunity.com/id/somename
  - Vanity name             somename

Raidcheck targets (case/space/dash-insensitive, common aliases accepted):
  sheetdoor, garagedoor, armoreddoor,
  stonewall (hard), stonewallsoft, metalwall, hqmwall

Capabilities required:
  - proxy:http               (allowed domains: ruststats.io, steamcommunity.com)
  - discord:send_message     (for chat-command replies)
  - events:message_content   (to read /statscheck and /raidcheck messages)

Slash commands declared in manifest.json.

Docs: https://mmomaid.com/dev/docs
"""
import gzip
import json
import math
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
    "• `/statscheck 76561198254115883`\n"
    "• `/statscheck https://steamcommunity.com/id/somename`\n"
    "• `/statscheck somename`"
)


# ── Raid calculator ────────────────────────────────────────────────────────────
# Per-unit damage values from RustLabs. Counts and sulfur match rusthelp.com,
# and pair-wise combos (e.g. 7 C4 + 1 Rocket on an HQM wall) are solved as a
# bounded knapsack so we surface the cheapest mix, not just pure stacks.

RAID_TOOLS: list[dict] = [
    # (display name, short label, emoji, sulfur cost per unit)
    {"name": "Timed Explosive Charge (C4)", "short": "C4",       "emoji": "💣", "sulfur": 2200},
    {"name": "Rocket",                      "short": "Rocket",   "emoji": "🚀", "sulfur": 1400},
    {"name": "Satchel Charge",              "short": "Satchel",  "emoji": "📦", "sulfur":  480},
    {"name": "Beancan Grenade",             "short": "Beancan",  "emoji": "🥫", "sulfur":   60},
    {"name": "F1 Grenade",                  "short": "F1",       "emoji": "💥", "sulfur":   30},
    {"name": "Explosive 5.56 Rifle Ammo",   "short": "Explo556", "emoji": "🔫", "sulfur":   10},
    {"name": "High Velocity Rocket",        "short": "HVRocket", "emoji": "⚡", "sulfur":  100},
]

# Damage per tool against each target (calibrated so ceil(HP/dmg) reproduces
# the count rusthelp.com shows AND mixed-tool combos check out against rusthelp's
# recommended recipes). None = tool not viable / not listed.
# Order: C4, Rocket, Satchel, Beancan, F1, Explo5.56, HVRocket.
RAID_TARGETS: list[dict] = [
    # ── Doors ─────────────────────────────────────────────────────────────
    {
        "name": "Wooden Door", "emoji": "🚪", "hp": 200,
        "damage": [1000.0, 600.0, 210.0, 30.0,   5.0,   12.5,   50.0],
        "aliases": ["woodendoor", "wooddoor", "wd"],
    },
    {
        "name": "Wood Double Door", "emoji": "🚪", "hp": 200,
        "damage": [1000.0, 600.0, 210.0, 30.0,   5.0,   12.5,   50.0],
        "aliases": ["wooddoubledoor", "woodendoubledoor", "wdd"],
    },
    {
        "name": "Sheet Metal Door", "emoji": "🚪", "hp": 250,
        "damage": [275.0, 275.0,  70.0, 14.0,    5.0,    4.0,   24.0],
        "aliases": ["sheetmetaldoor", "sheetdoor", "smdoor", "sheet"],
    },
    {
        "name": "Sheet Metal Double Door", "emoji": "🚪", "hp": 250,
        "damage": [275.0, 275.0,  70.0, 12.0,    5.0,    4.0,   24.0],
        "aliases": ["sheetmetaldoubledoor", "smdd", "metaldoubledoor"],
    },
    {
        "name": "Garage Door", "emoji": "🚛", "hp": 600,
        "damage": [400.0, 200.0,  70.0, 12.3,    0.5,    4.0,   None],
        "aliases": ["garagedoor", "garage", "gd"],
    },
    {
        "name": "Armored Door", "emoji": "🛡️", "hp": 1000,
        "damage": [400.0, 200.0,  70.0, 12.2,    5.0,    4.0,   None],
        "aliases": ["armoreddoor", "armoureddoor", "armoured", "armored", "ad"],
    },
    {
        "name": "Armored Double Door", "emoji": "🛡️", "hp": 1000,
        "damage": [400.0, 200.0,  70.0, 14.5,    5.0,    4.0,   24.0],
        "aliases": ["armoreddoubledoor", "armoureddoubledoor", "add"],
    },
    {
        "name": "Ladder Hatch", "emoji": "🪜", "hp": 250,
        "damage": [275.0, 275.0,  70.0, 14.0,    5.0,    4.0,   24.0],
        "aliases": ["ladderhatch", "hatch", "lh"],
    },
    # ── Walls ─────────────────────────────────────────────────────────────
    {
        "name": "Wooden Wall", "emoji": "🪵", "hp": 250,
        "damage": [1000.0, 600.0, 210.0, 30.0,   5.0,   5.32,  50.0],
        "aliases": ["woodenwall", "woodwall", "ww"],
    },
    {
        "name": "Stone Wall (hard side)", "emoji": "🧱", "hp": 500,
        "damage": [125.0, 125.0,  50.0, 11.0,    None,   2.71,   None],
        "aliases": ["stonewall", "stone", "stonewallhard", "swhard", "sw"],
    },
    {
        "name": "Stone Wall (soft side)", "emoji": "🧱", "hp": 500,
        "damage": [275.0, 275.0, 100.0, 22.0,    None,   5.4,    None],
        "aliases": ["stonewallsoft", "softside", "sws", "stonesoft"],
    },
    {
        "name": "Sheet Metal Wall", "emoji": "🔩", "hp": 1000,
        "damage": [250.0, 143.0,  44.0,  7.64,   1.008,  2.5,    None],
        "aliases": ["sheetmetalwall", "metalwall", "metal", "smw"],
    },
    {
        "name": "Armored Wall (HQM)", "emoji": "💎", "hp": 2000,
        "damage": [275.0, 137.0,  44.0,  7.65,   1.0075, 2.504,  None],
        "aliases": ["armoredwall", "armouredwall", "hqmwall", "hqm", "aw"],
    },
    # ── Compound / external ───────────────────────────────────────────────
    {
        "name": "High External Wooden Wall", "emoji": "🌲", "hp": 500,
        "damage": [1000.0, 250.0,  84.0, 30.0,   None,   6.0,    None],
        "aliases": ["highexternalwoodwall", "highextwoodwall", "hewoodwall", "hewood"],
    },
    {
        "name": "High External Stone Wall", "emoji": "⛰️", "hp": 500,
        "damage": [250.0, 125.0,  50.0, 11.0,    2.75,   2.9,   16.0],
        "aliases": ["highexternalstonewall", "highextstonewall", "hestonewall", "hestone"],
    },
    # ── Deployables ───────────────────────────────────────────────────────
    {
        "name": "Tool Cupboard", "emoji": "🛠️", "hp": 1000,
        "damage": [250.0, 143.0,  44.0,  7.64,   1.008,  2.5,    None],
        "aliases": ["toolcupboard", "tc", "cupboard"],
    },
    {
        "name": "Auto Turret", "emoji": "🔫", "hp": 1000,
        "damage": [500.0, 334.0, 250.0, 50.0,  100.0,    9.5,  334.0],
        "aliases": ["autoturret", "turret", "at"],
    },
    {
        "name": "SAM Site", "emoji": "📡", "hp": 1000,
        "damage": [500.0, 334.0, 250.0, 62.5,   50.0,    6.76, 30.0],
        "aliases": ["samsite", "sam"],
    },
    {
        "name": "Locker", "emoji": "🗄️", "hp": 500,
        "damage": [500.0, 250.0, 100.0, 30.0,   72.0,   10.21, 167.0],
        "aliases": ["locker"],
    },
    {
        "name": "Vending Machine", "emoji": "🏪", "hp": 1250,
        "damage": [250.0, 139.0,  44.0,  9.0,    1.008,  2.51, 14.9],
        "aliases": ["vendingmachine", "vending", "vm"],
    },
    {
        "name": "Large Wood Box", "emoji": "📦", "hp": 300,
        "damage": [1000.0, 600.0, 210.0, 30.0,  75.0,   10.0, 150.0],
        "aliases": ["largewoodbox", "largebox", "lwb"],
    },
    {
        "name": "Wood Storage Box", "emoji": "📦", "hp": 150,
        "damage": [1000.0, 600.0, 210.0, 30.0,  75.0,   10.0, 150.0],
        "aliases": ["woodstoragebox", "woodbox", "smallbox", "wsb"],
    },
    {
        "name": "Furnace", "emoji": "🔥", "hp": 500,
        "damage": [500.0, 250.0, 100.0, 25.0,   55.5,   10.21, 50.0],
        "aliases": ["furnace"],
    },
]

_NORM_RE = re.compile(r"[\s_\-]+")
_DMG_EPS = 1e-6  # float tolerance when checking "did the combo cover HP?"


def _norm(s: str) -> str:
    return _NORM_RE.sub("", s.strip().lower())


def _find_raid_target(query: str) -> dict | None:
    q = _norm(query)
    if not q:
        return None
    for t in RAID_TARGETS:
        if _norm(t["name"]) == q or q in (_norm(a) for a in t["aliases"]):
            return t
    matches = [
        t for t in RAID_TARGETS
        if q in _norm(t["name"]) or any(q in _norm(a) for a in t["aliases"])
    ]
    return matches[0] if len(matches) == 1 else None


def _count_for(damage: float | None, hp: float) -> int | None:
    if damage is None or damage <= 0:
        return None
    return math.ceil(hp / damage - _DMG_EPS)


def _solve_combo(hp: float, damages: list[float | None], costs: list[int],
                 allow: list[int] | None = None) -> tuple[int, list[tuple[int, int]]] | None:
    """Cheapest single-tool or two-tool combo that deals >= hp damage.

    Returns (total_sulfur, [(tool_idx, count), ...]) sorted by count desc, or
    None if no viable tool exists in `allow`. `allow` restricts the tool set
    (used to compute "explosives only" alongside an "anything" cheapest).
    """
    n = len(damages)
    indices = list(range(n)) if allow is None else [i for i in allow if 0 <= i < n]
    indices = [i for i in indices if damages[i] is not None and damages[i] > 0]
    if not indices:
        return None

    best: tuple[int, list[tuple[int, int]]] | None = None

    def consider(cost: int, parts: list[tuple[int, int]]):
        nonlocal best
        parts = [(i, c) for i, c in parts if c > 0]
        if not parts:
            return
        if best is None or cost < best[0]:
            best = (cost, parts)

    # Single-tool baselines.
    for i in indices:
        c = _count_for(damages[i], hp)
        assert c is not None
        consider(c * costs[i], [(i, c)])

    # Two-tool combos. We iterate `a` over how many of tool i we use, then
    # take the smallest b for tool j to cover the remaining damage.
    for i in indices:
        max_i = _count_for(damages[i], hp) or 0
        for j in indices:
            if j == i:
                continue
            for a in range(max_i + 1):
                rem = hp - a * damages[i]
                if rem <= _DMG_EPS:
                    b = 0
                else:
                    b = math.ceil(rem / damages[j] - _DMG_EPS)
                cost = a * costs[i] + b * costs[j]
                consider(cost, [(i, a), (j, b)])

    if best is None:
        return None
    parts = sorted(best[1], key=lambda p: -p[1])
    return best[0], parts


def _format_combo(parts: list[tuple[int, int]], qty: int = 1) -> str:
    # Tool order is canonical (C4 first, then Rocket, …) so the combo reads
    # naturally regardless of how the solver discovered it.
    parts = sorted(parts, key=lambda p: p[0])
    return " + ".join(
        f"{RAID_TOOLS[i]['emoji']} {RAID_TOOLS[i]['short']} ×{c * qty:,}"
        for i, c in parts
    )


# Tool-set restrictions for the headline combos.
_C4_IDX, _ROCKET_IDX, _EXPLO_IDX = 0, 1, 5
_C4_ROCKET_IDXS = [_C4_IDX, _ROCKET_IDX]


def _build_raid_embed(target: dict, qty: int) -> dict:
    qty = max(1, int(qty))
    hp = float(target["hp"])  # solve per single structure, then × qty
    damages = list(target["damage"])
    costs = [t["sulfur"] for t in RAID_TOOLS]

    # Single-tool table rows (counts already × qty for the user).
    table_lines = [
        f"{'Tool':<14} {'Qty':>7}  {'Sulfur':>10}",
        "─" * 36,
    ]
    for tool, dmg in zip(RAID_TOOLS, damages):
        if dmg is None:
            table_lines.append(f"{tool['emoji']} {tool['short']:<11} {'n/a':>7}  {'—':>10}")
            continue
        per = _count_for(dmg, hp) or 0
        c = per * qty
        s = c * tool["sulfur"]
        table_lines.append(f"{tool['emoji']} {tool['short']:<11} {c:>7,}  {s:>10,}")
    table = "```\n" + "\n".join(table_lines) + "\n```"

    # Headline combos (each solved per single structure):
    #   - "raid combo": cheapest mix of C4 and Rocket — what most raiders use
    #   - "cheapest":   cheapest mix of any tool incl. ammo (often a slow grind)
    raid_combo = _solve_combo(hp, damages, costs, allow=_C4_ROCKET_IDXS)
    cheapest   = _solve_combo(hp, damages, costs)

    # Discord renders embed `fields` with real visual separation, which is
    # what we want — putting these inside the description squashes them.
    fields: list[dict] = []

    if raid_combo:
        cost_each, parts = raid_combo
        fields.append({
            "name": "💣 Raid combo (C4/Rocket)",
            "value": f"{_format_combo(parts, qty)}\nTotal: **{cost_each * qty:,} sulfur**",
            "inline": False,
        })

    if cheapest and (not raid_combo or cheapest[0] < raid_combo[0]):
        cost_each, parts = cheapest
        fields.append({
            "name": "💸 Cheapest (incl. bullets/throwables)",
            "value": f"{_format_combo(parts, qty)}\nTotal: **{cost_each * qty:,} sulfur**",
            "inline": False,
        })

    fields.append({
        "name": "📊 Single-tool options",
        "value": table,
        "inline": False,
    })

    return {
        "title": "💥 Rust Raid Calculator",
        "description": (
            f"{target['emoji']} **{target['name']}**  ×{qty}  ·  "
            f"**{int(hp * qty):,} HP**"
        ),
        "color": EMBED_COLOR,
        "fields": fields,
    }


def _raid_help_text() -> str:
    aliases_preview = ", ".join(
        f"{t['emoji']} `{t['aliases'][0]}`" for t in RAID_TARGETS
    )
    return (
        "❌ Please pick a target.\n"
        f"Examples:\n"
        f"• `/raidcheck stonewall`\n"
        f"• `/raidcheck armoreddoor 4`\n"
        f"• `/raidcheck hqmwall 2`\n"
        f"\nValid targets: {aliases_preview}"
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


@plugin.on_slash_command("raidcheck")
def handle_raidcheck_slash(ctx: Context, event: dict):
    target_q = _extract_option(event, "target")
    qty_raw = _extract_option(event, "quantity")

    if not target_q:
        ctx.interaction.respond(content=_raid_help_text(), ephemeral=True)
        return

    try:
        qty = max(1, int(qty_raw)) if qty_raw else 1
    except (TypeError, ValueError):
        qty = 1

    target = _find_raid_target(target_q)
    if not target:
        ctx.interaction.respond(
            content=f"❌ Unknown target `{target_q}`.\n{_raid_help_text()}",
            ephemeral=True,
        )
        return

    ctx.interaction.respond(embeds=[_build_raid_embed(target, qty)])


# Chat-command prefixes — users typing the literal text in a Discord channel.
STATSCHECK_PREFIXES = ("/statscheck",)
RAIDCHECK_PREFIXES  = ("/raidcheck",)


def _do_statscheck_chat(ctx: Context, channel_id: str, args: str):
    if not args:
        ctx.discord.send_message(channel_id=channel_id, content=USAGE_HELP)
        return

    steamid, err = _resolve_to_steamid64(ctx, args)
    if err:
        ctx.discord.send_message(channel_id=channel_id, content=f"❌ {err}: `{args}`")
        return

    profile, err = _fetch_profile(ctx, steamid)
    if err:
        ctx.discord.send_message(channel_id=channel_id, content=f"⚠️ {err}")
        return

    ctx.discord.send_message(channel_id=channel_id, embeds=[_build_embed(profile)])


def _do_raidcheck_chat(ctx: Context, channel_id: str, args: str):
    tokens = args.split()
    if not tokens:
        ctx.discord.send_message(channel_id=channel_id, content=_raid_help_text())
        return

    qty = 1
    if len(tokens) >= 2 and tokens[-1].isdigit():
        qty = max(1, int(tokens[-1]))
        target_query = " ".join(tokens[:-1])
    else:
        target_query = " ".join(tokens)

    target = _find_raid_target(target_query)
    if not target:
        ctx.discord.send_message(
            channel_id=channel_id,
            content=f"❌ Unknown target `{target_query}`.\n{_raid_help_text()}",
        )
        return

    ctx.discord.send_message(channel_id=channel_id, embeds=[_build_raid_embed(target, qty)])


@plugin.on_event("message_create")
def handle_message_create(ctx: Context, event: dict):
    content = (event.get("content") or "").strip()
    if not content:
        return

    parts = content.split(None, 1)
    cmd = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""

    channel_id = event.get("channel_id")
    if not channel_id:
        return

    if cmd in STATSCHECK_PREFIXES:
        _do_statscheck_chat(ctx, channel_id, rest)
    elif cmd in RAIDCHECK_PREFIXES:
        _do_raidcheck_chat(ctx, channel_id, rest)


# ── This must be the last line ────────────────────────────────────────────────
plugin.run()

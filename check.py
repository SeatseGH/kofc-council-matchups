"""
KoC Sports Checker — GitHub Actions Edition (v3)
=================================================
Posts to Discord:
  1. WEEKLY PREVIEW  — Monday morning, all KoC matchups for the coming week
  2. DAY-BEFORE      — the day before each individual game
  3. FINAL SCORE     — when each game ends
  4. WEEKLY RECAP    — Sunday night, all results from that week

All messages include council numbers, start time, TV channel, and venue
(with home team or neutral-site noted), plus custom server emoji if available.

Environment variables (GitHub Secrets):
  DISCORD_WEBHOOK_URL   (required) — channel webhook URL
  DISCORD_BOT_TOKEN     (optional) — enables automatic custom-emoji lookup
  DISCORD_GUILD_ID      (optional) — server ID, used with the bot token

Pure Python stdlib — nothing to pip install.
"""

import json
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
WEBHOOK_URL  = os.environ.get("DISCORD_WEBHOOK_URL", "")
BOT_TOKEN    = os.environ.get("DISCORD_BOT_TOKEN", "")
GUILD_ID     = os.environ.get("DISCORD_GUILD_ID", "")

_HERE = Path(__file__).resolve().parent
# schools.json sits next to check.py; state.json defaults there too but can be
# pointed elsewhere so it stays at a fixed, cacheable location.
SCHOOLS_FILE = Path(os.environ.get("KOC_SCHOOLS_FILE") or (_HERE / "schools.json"))
STATE_FILE   = Path(os.environ.get("KOC_STATE_FILE")   or (_HERE / "state.json"))
ESPN_BASE    = "https://site.api.espn.com/apis/site/v2/sports"

ESPN_LEAGUES = [
    ("football",   "football/college-football"),
    ("basketball", "basketball/mens-college-basketball"),
]

# "" = all games; groups=100 = NCAA tournament bracket
GROUP_VARIANTS = ["", "&groups=100"]

SPORT_EMOJI = {"football": "\U0001F3C8", "basketball": "\U0001F3C0"}

# Weekly post scheduling (Python weekday(): Mon=0 ... Sun=6), US Eastern
WEEKLY_PREVIEW_DAY  = 0    # Monday
WEEKLY_PREVIEW_HOUR = 9    # from 9 AM ET onward
WEEKLY_RECAP_DAY    = 6    # Sunday
WEEKLY_RECAP_HOUR   = 22   # from 10 PM ET onward, after games finish

# State retention
DAILY_STATE_MAX_AGE_DAYS  = 5
WEEKLY_STATE_MAX_AGE_DAYS = 60

# ESPN schedules by US Eastern calendar date
try:
    from zoneinfo import ZoneInfo
    SCHEDULE_TZ = ZoneInfo("America/New_York")
except Exception:
    SCHEDULE_TZ = timezone.utc

# ── Helpers ───────────────────────────────────────────────────────────────────
def log(msg: str):
    print(msg, flush=True)

def fetch_json(url: str, headers: dict = None) -> dict:
    h = {"User-Agent": "KoC-Sports-Bot/3.0"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())

def post_webhook(payload: dict) -> bool:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        WEBHOOK_URL,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "DiscordBot (https://github.com, 3.0)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            log(f"     -> Webhook posted (HTTP {r.status})")
            return True
    except urllib.error.HTTPError as e:
        log(f"     -> Webhook error {e.code}: {e.read().decode()[:200]}")
        return False
    except Exception as e:
        log(f"     -> Webhook failed: {e}")
        return False

# ── Custom emoji ──────────────────────────────────────────────────────────────
FILLER_WORDS = {
    "the", "of", "at", "and", "university", "college", "institute",
    "technology", "polytechnic", "school",
}

def _emoji_key(text: str) -> str:
    """Normalise a name for emoji matching: lowercase, letters/digits only."""
    return re.sub(r"[^a-z0-9]", "", text.lower())

def fetch_guild_emojis() -> dict:
    """
    Return {normalised_emoji_name: '<:name:id>'} for the server's custom emoji.
    Requires DISCORD_BOT_TOKEN + DISCORD_GUILD_ID; returns {} if not configured.
    """
    if not (BOT_TOKEN and GUILD_ID):
        return {}
    url = f"https://discord.com/api/v10/guilds/{GUILD_ID}/emojis"
    try:
        data = fetch_json(url, {"Authorization": f"Bot {BOT_TOKEN}"})
    except urllib.error.HTTPError as e:
        log(f"(emoji lookup failed: HTTP {e.code} — check bot token / guild ID / bot is in server)")
        return {}
    except Exception as e:
        log(f"(emoji lookup failed: {e})")
        return {}

    out = {}
    for em in data:
        name, eid = em.get("name"), em.get("id")
        if not (name and eid):
            continue
        prefix = "a" if em.get("animated") else ""
        out[_emoji_key(name)] = f"<{prefix}:{name}:{eid}>"
    log(f"Loaded {len(out)} custom server emoji")
    return out

def emoji_candidates(school: dict) -> list:
    """Possible emoji names for a school, best guess first."""
    keys = []

    def add(k):
        if k and k not in keys:
            keys.append(k)

    if school.get("emoji_name"):
        add(_emoji_key(school["emoji_name"]))

    for name in [school["name"]] + school.get("alt_names", []):
        add(_emoji_key(name))
        words = [w for w in re.findall(r"[a-z]+", name.lower())
                 if w not in FILLER_WORDS]
        if words:
            add("".join(words))
            if len(words[0]) >= 3:
                add(words[0])
            if len(words) >= 2:
                add(words[0] + words[1])
    return keys

def resolve_emoji(school: dict, guild_emojis: dict) -> str:
    """
    Return the renderable emoji string for a school, or "".
    A manual "emoji" field in schools.json always wins.
    """
    manual = (school.get("emoji") or "").strip()
    if manual:
        if re.fullmatch(r"<a?:\w+:\d+>", manual):
            return manual
        if re.fullmatch(r":\w+:", manual):
            log(f"  ! {school['name']}: emoji '{manual}' is a shortcode; "
                f"webhooks need the <:name:id> form — ignoring")
            return ""
        return manual  # standard unicode emoji, renders fine
    for key in emoji_candidates(school):
        if key in guild_emojis:
            return guild_emojis[key]
    return ""

# ── State persistence ─────────────────────────────────────────────────────────
STATE_KEYS = ("preview", "final", "weekly_preview", "weekly_recap")

def load_state() -> dict:
    state = {k: {} for k in STATE_KEYS}
    if STATE_FILE.exists():
        try:
            loaded = json.loads(STATE_FILE.read_text())
            for key in STATE_KEYS:
                if isinstance(loaded.get(key), dict):
                    state[key] = loaded[key]
        except Exception as e:
            log(f"(could not read state.json, starting fresh: {e})")
    return state

def save_state(state: dict):
    now_ts = datetime.now(timezone.utc).timestamp()
    daily_cutoff  = now_ts - DAILY_STATE_MAX_AGE_DAYS * 86400
    weekly_cutoff = now_ts - WEEKLY_STATE_MAX_AGE_DAYS * 86400
    for key in ("preview", "final"):
        state[key] = {k: v for k, v in state[key].items() if v > daily_cutoff}
    for key in ("weekly_preview", "weekly_recap"):
        state[key] = {k: v for k, v in state[key].items() if v > weekly_cutoff}
    STATE_FILE.write_text(json.dumps(state, indent=2))

# ── School loading & matching ─────────────────────────────────────────────────
def load_koc_schools() -> list:
    try:
        return json.loads(SCHOOLS_FILE.read_text())
    except json.JSONDecodeError as e:
        log(f"ERROR: schools.json has a syntax error: {e}")
        log("Paste the file into https://jsonlint.com to find the problem.")
        raise SystemExit(1)

def build_name_map(schools: list) -> dict:
    name_map = {}
    for s in schools:
        name_map[s["name"].lower()] = s
        for a in s.get("alt_names", []):
            name_map[a.lower()] = s
    return name_map

def _tokenize(name: str) -> set:
    stopwords = FILLER_WORDS | {"state", "saint", "mount", "our", "lady"}
    return {w for w in re.findall(r"[a-z]+", name.lower())
            if len(w) >= 3 and w not in stopwords}

def find_koc_school(team_name: str, token_map: dict, name_map: dict):
    """
    Match an ESPN team name to a KoC school record, or None.
      1. Exact match
      2. Substring match (both 8+ chars, so 'kansas' can't match 'arkansas')
      3. Token match: all significant words of the KoC name appear in the
         ESPN name (needs 2+ tokens, blocks single-word false positives)
    """
    tn = team_name.lower().strip()
    tn_tokens = _tokenize(tn)
    for kn, kn_tokens in token_map.items():
        if kn == tn:
            return name_map[kn]
        if len(kn) >= 8 and len(tn) >= 8 and (kn in tn or tn in kn):
            return name_map[kn]
        if len(kn_tokens) >= 2 and kn_tokens.issubset(tn_tokens):
            return name_map[kn]
    return None

# ── ESPN parsing ──────────────────────────────────────────────────────────────
def parse_games(data: dict, sport: str) -> list:
    games = []
    for event in data.get("events", []):
        try:
            comp  = event["competitions"][0]
            teams = {c["homeAway"]: c for c in comp["competitors"]}
            home, away = teams.get("home", {}), teams.get("away", {})

            tv = ", ".join(
                nm for b in comp.get("broadcasts", []) for nm in b.get("names", [])
            ) or "TBD"

            venue = comp.get("venue", {})
            addr  = venue.get("address", {})

            try:
                start_dt = datetime.fromisoformat(
                    event.get("date", "").replace("Z", "+00:00")
                )
            except Exception:
                start_dt = None

            games.append({
                "id":         event["id"],
                "sport":      sport,
                "home":       home.get("team", {}).get("displayName", ""),
                "away":       away.get("team", {}).get("displayName", ""),
                "home_score": int(home.get("score", 0) or 0),
                "away_score": int(away.get("score", 0) or 0),
                "status":     comp.get("status", {}).get("type", {}).get("name", ""),
                "clock":      comp.get("status", {}).get("displayClock", ""),
                "period":     comp.get("status", {}).get("period", 0),
                "venue":      venue.get("fullName", "Venue TBD"),
                "venue_city": ", ".join(filter(None, [addr.get("city"), addr.get("state")])),
                "start_time": start_dt,
                "tv":         tv,
                "neutral":    comp.get("neutralSite", False),
            })
        except Exception as e:
            log(f"  [parse error] {e}")
    return games

def fetch_koc_games(date_strs, token_map, name_map) -> list:
    """Fetch the given Eastern dates and return only KoC-vs-KoC games."""
    found, seen = [], set()
    for sport, league_path in ESPN_LEAGUES:
        for date_str in date_strs:
            for variant in GROUP_VARIANTS:
                url = (f"{ESPN_BASE}/{league_path}/scoreboard"
                       f"?dates={date_str}&limit=500{variant}")
                try:
                    games = parse_games(fetch_json(url), sport)
                except urllib.error.HTTPError as e:
                    if e.code != 404:
                        log(f"[{sport} {date_str}] HTTP {e.code}")
                    continue
                except Exception as e:
                    log(f"[{sport} {date_str}] fetch error: {e}")
                    continue

                for game in games:
                    gid = f"{sport}:{game['id']}"
                    if gid in seen:
                        continue
                    seen.add(gid)
                    aw = find_koc_school(game["away"], token_map, name_map)
                    hm = find_koc_school(game["home"], token_map, name_map)
                    if aw and hm:
                        game["gid"] = gid
                        game["away_school"] = aw
                        game["home_school"] = hm
                        found.append(game)
    found.sort(key=lambda g: g["start_time"] or datetime.max.replace(tzinfo=timezone.utc))
    return found

# ── Message building ──────────────────────────────────────────────────────────
def team_label(espn_name: str, school: dict, emoji_map: dict) -> str:
    """'<:emoji:id> Team Name (Council #1234)'"""
    parts = []
    em = emoji_map.get(id(school)) or ""
    if em:
        parts.append(em)
    parts.append(espn_name)
    council = (school or {}).get("council", "")
    if council and council not in ("N/A", "TBD", ""):
        parts.append(f"(Council #{council})")
    return " ".join(parts)

def location_text(game: dict) -> str:
    city = f", {game['venue_city']}" if game["venue_city"] else ""
    if game["neutral"]:
        return f"\U0001F4CD **{game['venue']}**{city} *(neutral site)*"
    return f"\U0001F4CD **{game['venue']}**{city} *(home: {game['home']})*"

def send_chunked_embed(title: str, header: str, blocks: list, color: int, footer: str):
    """Split blocks across embeds so no description exceeds Discord's limit."""
    if not blocks:
        blocks = ["*No matchups found.*"]

    chunks, current, length = [], [], 0
    for b in blocks:
        if length + len(b) > 3500 and current:
            chunks.append(current)
            current, length = [], 0
        current.append(b)
        length += len(b) + 2
    if current:
        chunks.append(current)

    ok = True
    for i, chunk in enumerate(chunks):
        desc = ("" if i else header + "\n\n") + "\n\n".join(chunk)
        embed = {
            "title":       title if i == 0 else f"{title} (cont. {i+1})",
            "description": desc,
            "color":       color,
            "footer":      {"text": footer},
        }
        ok = post_webhook({"embeds": [embed]}) and ok
    return ok

# ── Individual game messages ──────────────────────────────────────────────────
def preview_payload(game, emoji_map):
    emoji = SPORT_EMOJI[game["sport"]]
    ts    = int(game["start_time"].timestamp())
    desc = (
        f"**{team_label(game['away'], game['away_school'], emoji_map)}**\n"
        f"@ **{team_label(game['home'], game['home_school'], emoji_map)}**\n\n"
        f"\U0001F550 <t:{ts}:F>\n"
        f"\U0001F4FA TV: **{game['tv']}**\n"
        f"{location_text(game)}"
    )
    return {"embeds": [{
        "title":       f"{emoji} KoC Matchup Tomorrow!",
        "description": desc,
        "color":       0xFFD700,
        "footer":      {"text": f"Men's College {game['sport'].capitalize()} \u2022 Knights of Columbus College Councils"},
    }]}

def final_payload(game, emoji_map):
    emoji = SPORT_EMOJI[game["sport"]]
    away_label = team_label(game["away"], game["away_school"], emoji_map)
    home_label = team_label(game["home"], game["home_school"], emoji_map)

    if game["home_score"] > game["away_score"]:
        result = f"\U0001F389 **{home_label}** wins!"
    elif game["away_score"] > game["home_score"]:
        result = f"\U0001F389 **{away_label}** wins!"
    else:
        result = "**TIE**"

    desc = (
        f"**{away_label}** - {game['away_score']}\n"
        f"**{home_label}** - {game['home_score']}\n\n"
        f"{result}\n"
        f"\U0001F4FA TV: **{game['tv']}**\n"
        f"{location_text(game)}"
    )
    return {"embeds": [{
        "title":       f"{emoji} Final Score \u2014 KoC Matchup",
        "description": desc,
        "color":       0x1A6B3A,
        "footer":      {"text": f"Men's College {game['sport'].capitalize()} \u2022 Knights of Columbus College Councils"},
    }]}

# ── Weekly messages ───────────────────────────────────────────────────────────
def weekly_preview_block(game, emoji_map) -> str:
    sport_em = SPORT_EMOJI[game["sport"]]
    ts = int(game["start_time"].timestamp()) if game["start_time"] else None
    day = game["start_time"].astimezone(SCHEDULE_TZ).strftime("%a %b %-d") if game["start_time"] else "TBD"
    lines = [
        f"{sport_em} **{day}**",
        f"**{team_label(game['away'], game['away_school'], emoji_map)}**",
        f"@ **{team_label(game['home'], game['home_school'], emoji_map)}**",
    ]
    if ts:
        lines.append(f"\U0001F550 <t:{ts}:t>  \u2022  \U0001F4FA {game['tv']}")
    else:
        lines.append(f"\U0001F4FA {game['tv']}")
    lines.append(location_text(game))
    return "\n".join(lines)

def weekly_recap_block(game, emoji_map) -> str:
    sport_em = SPORT_EMOJI[game["sport"]]
    day = game["start_time"].astimezone(SCHEDULE_TZ).strftime("%a %b %-d") if game["start_time"] else ""
    away_label = team_label(game["away"], game["away_school"], emoji_map)
    home_label = team_label(game["home"], game["home_school"], emoji_map)

    a, h = game["away_score"], game["home_score"]
    if h > a:
        away_s, home_s = f"{a}", f"**{h}**"
        winner = f"\U0001F389 {home_label}"
    elif a > h:
        away_s, home_s = f"**{a}**", f"{h}"
        winner = f"\U0001F389 {away_label}"
    else:
        away_s, home_s = f"{a}", f"{h}"
        winner = "Tie"

    return (
        f"{sport_em} **{day}**\n"
        f"{away_label} {away_s} \u2014 {home_s} {home_label}\n"
        f"{winner}"
    )

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    if not WEBHOOK_URL:
        log("ERROR: DISCORD_WEBHOOK_URL secret is not set.")
        sys.exit(1)

    now       = datetime.now(timezone.utc)
    now_ts    = now.timestamp()
    now_local = now.astimezone(SCHEDULE_TZ)

    today_str    = now_local.strftime("%Y%m%d")
    tomorrow_str = (now_local + timedelta(days=1)).strftime("%Y%m%d")

    # Monday..Sunday of the current Eastern week
    week_start = now_local - timedelta(days=now_local.weekday())
    week_dates = [(week_start + timedelta(days=i)).strftime("%Y%m%d") for i in range(7)]
    iso = now_local.isocalendar()
    week_key = f"{iso[0]}-W{iso[1]:02d}"
    week_label = (f"{week_start.strftime('%b %-d')} \u2013 "
                  f"{(week_start + timedelta(days=6)).strftime('%b %-d, %Y')}")

    schools   = load_koc_schools()
    name_map  = build_name_map(schools)
    token_map = {kn: _tokenize(kn) for kn in name_map if _tokenize(kn)}
    state     = load_state()

    # Resolve custom emoji once, keyed by school object identity
    guild_emojis = fetch_guild_emojis()
    emoji_map = {}
    for s in schools:
        em = resolve_emoji(s, guild_emojis)
        if em:
            emoji_map[id(s)] = em
    if emoji_map:
        log(f"Matched emoji for {len(emoji_map)} of {len(schools)} schools")

    log(f"Run at {now.isoformat()} UTC")
    log(f"Eastern: {now_local.strftime('%a %Y-%m-%d %H:%M')}  week={week_key}")
    log(f"Tracking {len(schools)} schools ({len(name_map)} name variants)")

    # ── 1. Weekly preview (Monday) ────────────────────────────────────────────
    if (now_local.weekday() == WEEKLY_PREVIEW_DAY
            and now_local.hour >= WEEKLY_PREVIEW_HOUR
            and week_key not in state["weekly_preview"]):
        log(f"\n== Weekly preview due for {week_key} ==")
        games = fetch_koc_games(week_dates, token_map, name_map)
        upcoming = [g for g in games if g["status"] not in ("STATUS_FINAL", "STATUS_FULL_TIME")]
        log(f"   {len(upcoming)} matchups this week")
        blocks = [weekly_preview_block(g, emoji_map) for g in upcoming]
        if send_chunked_embed(
            "\U0001F4C5 KoC Matchups This Week",
            f"**{week_label}**",
            blocks, 0xFFD700,
            "Knights of Columbus College Councils \u2022 Men's Basketball & Football",
        ):
            state["weekly_preview"][week_key] = now_ts

    # ── 2. Weekly recap (Sunday) ──────────────────────────────────────────────
    if (now_local.weekday() == WEEKLY_RECAP_DAY
            and now_local.hour >= WEEKLY_RECAP_HOUR
            and week_key not in state["weekly_recap"]):
        log(f"\n== Weekly recap due for {week_key} ==")
        games = fetch_koc_games(week_dates, token_map, name_map)
        finished = [g for g in games if g["status"] in ("STATUS_FINAL", "STATUS_FULL_TIME")]
        log(f"   {len(finished)} completed matchups this week")
        blocks = [weekly_recap_block(g, emoji_map) for g in finished]
        if send_chunked_embed(
            "\U0001F4CA KoC Results This Week",
            f"**{week_label}**",
            blocks, 0x1A6B3A,
            "Knights of Columbus College Councils \u2022 Men's Basketball & Football",
        ):
            state["weekly_recap"][week_key] = now_ts

    # ── 3. Per-game: day-before preview and final score ───────────────────────
    log("")
    games = fetch_koc_games([today_str, tomorrow_str], token_map, name_map)
    log(f"{len(games)} KoC matchup(s) today/tomorrow")

    for game in games:
        gid = game["gid"]
        log(f"  \u2705 {game['away']} @ {game['home']} [{game['status']}]")

        if game["status"] == "STATUS_SCHEDULED" and game["start_time"]:
            game_date = game["start_time"].astimezone(SCHEDULE_TZ).strftime("%Y%m%d")
            if game_date != tomorrow_str:
                log(f"     (scheduled {game_date}, not tomorrow - no preview)")
            elif gid in state["preview"]:
                log("     (preview already sent)")
            else:
                log("     -> Sending day-before preview!")
                if post_webhook(preview_payload(game, emoji_map)):
                    state["preview"][gid] = now_ts

        if game["status"] in ("STATUS_FINAL", "STATUS_FULL_TIME"):
            if gid in state["final"]:
                log("     (final already sent)")
            else:
                log("     -> Sending final score!")
                if post_webhook(final_payload(game, emoji_map)):
                    state["final"][gid] = now_ts

        if game["status"] == "STATUS_IN_PROGRESS":
            log(f"     (live: {game['away_score']}-{game['home_score']}, {game['clock']})")

    save_state(state)
    log(f"\nDone. Tracked: {len(state['preview'])} previews, {len(state['final'])} finals, "
        f"{len(state['weekly_preview'])} weekly previews, {len(state['weekly_recap'])} weekly recaps.")

if __name__ == "__main__":
    main()

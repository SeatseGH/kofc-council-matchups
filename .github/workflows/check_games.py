"""
KoC Sports Checker — GitHub Actions Edition
============================================
Posts to Discord:
  • A PREVIEW the day before any game between two KoC College Council schools
  • A FINAL SCORE when that game ends

Both messages include council numbers, start time, TV channel, and venue
(with home team or neutral-site noted).

Required environment variable (set as a GitHub Secret):
  DISCORD_WEBHOOK_URL

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
SCHOOLS_FILE = Path(__file__).parent / "schools.json"
STATE_FILE   = Path(__file__).parent / "state.json"
ESPN_BASE    = "https://site.api.espn.com/apis/site/v2/sports"

# Leagues to check: (sport_label, espn_path)
ESPN_LEAGUES = [
    ("football",   "football/college-football"),
    ("basketball", "basketball/mens-college-basketball"),
]

# Extra query variants per league. "" = all games; groups=100 = NCAA tournament.
GROUP_VARIANTS = ["", "&groups=100"]

SPORT_EMOJI = {"football": "🏈", "basketball": "🏀"}

# Keep sent-alert records this many days before pruning
STATE_MAX_AGE_DAYS = 5

# ESPN schedules by US Eastern calendar date
try:
    from zoneinfo import ZoneInfo
    SCHEDULE_TZ = ZoneInfo("America/New_York")
except Exception:
    SCHEDULE_TZ = timezone.utc

# ── Helpers ───────────────────────────────────────────────────────────────────
def log(msg: str):
    print(msg, flush=True)

def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "KoC-Sports-Bot/2.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())

def post_webhook(payload: dict) -> bool:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        WEBHOOK_URL,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "DiscordBot (https://github.com, 2.0)",
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

# ── State persistence ─────────────────────────────────────────────────────────
def load_state() -> dict:
    state = {"preview": {}, "final": {}}
    if STATE_FILE.exists():
        try:
            loaded = json.loads(STATE_FILE.read_text())
            for key in ("preview", "final"):
                if isinstance(loaded.get(key), dict):
                    state[key] = loaded[key]
        except Exception as e:
            log(f"(could not read state.json, starting fresh: {e})")
    return state

def save_state(state: dict):
    cutoff = datetime.now(timezone.utc).timestamp() - STATE_MAX_AGE_DAYS * 86400
    for key in ("preview", "final"):
        state[key] = {gid: ts for gid, ts in state[key].items() if ts > cutoff}
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
    """Map every lowercase name/alt_name -> its school record."""
    name_map = {}
    for s in schools:
        name_map[s["name"].lower()] = s
        for a in s.get("alt_names", []):
            name_map[a.lower()] = s
    return name_map

def _tokenize(name: str) -> set:
    """Significant words in a name, ignoring filler words."""
    stopwords = {
        "the", "of", "at", "and", "university", "college", "state",
        "institute", "technology", "polytechnic", "school", "saint",
        "mount", "our", "lady",
    }
    return {w for w in re.findall(r"[a-z]+", name.lower())
            if len(w) >= 3 and w not in stopwords}

def find_koc_school(team_name: str, token_map: dict, name_map: dict):
    """
    Match an ESPN team name to a KoC school record, or return None.
      1. Exact match
      2. Substring match (both names 8+ chars, avoids 'kansas' matching 'arkansas')
      3. Token match: all significant words of the KoC name appear in the ESPN
         name (needs 2+ tokens, avoids single-word false positives)
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

# ── Message building ──────────────────────────────────────────────────────────
def school_label(espn_name: str, school: dict) -> str:
    """Team name with council number appended when known."""
    council = (school or {}).get("council", "")
    if council and council not in ("N/A", "TBD", ""):
        return f"{espn_name} (Council #{council})"
    return espn_name

def location_text(game: dict) -> str:
    city = f", {game['venue_city']}" if game["venue_city"] else ""
    if game["neutral"]:
        return f"📍 **{game['venue']}**{city} *(neutral site)*"
    return f"📍 **{game['venue']}**{city} *(home: {game['home']})*"

def preview_payload(game: dict, away_school: dict, home_school: dict) -> dict:
    emoji = SPORT_EMOJI[game["sport"]]
    ts    = int(game["start_time"].timestamp())
    desc = (
        f"**{school_label(game['away'], away_school)}**\n"
        f"@ **{school_label(game['home'], home_school)}**\n\n"
        f"🕐 <t:{ts}:F>\n"
        f"📺 TV: **{game['tv']}**\n"
        f"{location_text(game)}"
    )
    return {"embeds": [{
        "title":       f"{emoji} KoC Matchup Tomorrow!",
        "description": desc,
        "color":       0xFFD700,
        "footer":      {"text": f"Men's College {game['sport'].capitalize()} • Knights of Columbus College Councils"},
    }]}

def final_payload(game: dict, away_school: dict, home_school: dict) -> dict:
    emoji = SPORT_EMOJI[game["sport"]]
    away_label = school_label(game["away"], away_school)
    home_label = school_label(game["home"], home_school)

    if game["home_score"] > game["away_score"]:
        result = f"🎉 **{home_label}** wins!"
    elif game["away_score"] > game["home_score"]:
        result = f"🎉 **{away_label}** wins!"
    else:
        result = "**TIE**"

    desc = (
        f"**{away_label}** - {game['away_score']}\n"
        f"**{home_label}** - {game['home_score']}\n\n"
        f"{result}\n"
        f"📺 TV: **{game['tv']}**\n"
        f"{location_text(game)}"
    )
    return {"embeds": [{
        "title":       f"{emoji} Final Score — KoC Matchup",
        "description": desc,
        "color":       0x1A6B3A,
        "footer":      {"text": f"Men's College {game['sport'].capitalize()} • Knights of Columbus College Councils"},
    }]}

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

    schools   = load_koc_schools()
    name_map  = build_name_map(schools)
    token_map = {kn: _tokenize(kn) for kn in name_map if _tokenize(kn)}
    state     = load_state()

    log(f"Run at {now.isoformat()} UTC")
    log(f"Eastern date: today={today_str}  tomorrow={tomorrow_str}")
    log(f"Tracking {len(schools)} schools ({len(name_map)} name variants)")

    seen = set()

    for sport, league_path in ESPN_LEAGUES:
        for date_str in (today_str, tomorrow_str):
            for variant in GROUP_VARIANTS:
                url = f"{ESPN_BASE}/{league_path}/scoreboard?dates={date_str}&limit=500{variant}"
                try:
                    games = parse_games(fetch_json(url), sport)
                except urllib.error.HTTPError as e:
                    if e.code != 404:
                        log(f"[{sport} {date_str}] HTTP {e.code}")
                    continue
                except Exception as e:
                    log(f"[{sport} {date_str}] fetch error: {e}")
                    continue

                if not games:
                    continue
                log(f"\n[{sport} {date_str}{variant}] {len(games)} games")

                for game in games:
                    gid = f"{sport}:{game['id']}"
                    if gid in seen:
                        continue
                    seen.add(gid)

                    away_school = find_koc_school(game["away"], token_map, name_map)
                    home_school = find_koc_school(game["home"], token_map, name_map)
                    if not (away_school and home_school):
                        continue

                    log(f"  ✅ {game['away']} @ {game['home']} [{game['status']}]")

                    # ── Preview: game is on tomorrow's Eastern calendar date ──
                    if (
                        game["status"] == "STATUS_SCHEDULED"
                        and game["start_time"] is not None
                    ):
                        game_date = game["start_time"].astimezone(SCHEDULE_TZ).strftime("%Y%m%d")
                        if game_date != tomorrow_str:
                            log(f"     (scheduled {game_date}, not tomorrow - no preview)")
                        elif gid in state["preview"]:
                            log("     (preview already sent)")
                        else:
                            log("     -> Sending preview!")
                            if post_webhook(preview_payload(game, away_school, home_school)):
                                state["preview"][gid] = now_ts

                    # ── Final score ───────────────────────────────────────────
                    if game["status"] in ("STATUS_FINAL", "STATUS_FULL_TIME"):
                        if gid in state["final"]:
                            log("     (final already sent)")
                        else:
                            log("     -> Sending final score!")
                            if post_webhook(final_payload(game, away_school, home_school)):
                                state["final"][gid] = now_ts

                    if game["status"] == "STATUS_IN_PROGRESS":
                        log(f"     (live: {game['away_score']}-{game['home_score']}, {game['clock']})")

    save_state(state)
    log(f"\nDone. State: {len(state['preview'])} previews, {len(state['final'])} finals tracked.")

if __name__ == "__main__":
    main()

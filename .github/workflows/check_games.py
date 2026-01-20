#!/usr/bin/env python3
"""
check_games.py

Run periodically (GitHub Actions).  - Fetch ESPN scoreboards (basketball, football)
- Match games where both teams map to a known council school.
- Post messages to Discord via webhook.
- Persist posted game IDs to a GitHub Issue (so we don't repost).
"""

import os
import requests
import json
import sys
from datetime import datetime
import pytz
import traceback

# === Configuration / environment variables ===
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")  # required
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")  # provided by Actions by default
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY")  # owner/repo
POSTED_STORE_ISSUE_TITLE = os.getenv("POSTED_STORE_ISSUE_TITLE", "KofC-posted-games-store")
TIMEZONE = os.getenv("TIMEZONE", "US/Central")

COUNCIL_FILE = os.getenv("COUNCIL_FILE", "council_schools.json")
# Optionally provide a JSON map of council number -> discord role id
# Example: {"2782": "987654321234567890", "4396": "123456789012345678"}
ROLE_ID_MAP_JSON = os.getenv("ROLE_ID_MAP_JSON", "")

# ESPN endpoints
BASKETBALL_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard"
FOOTBALL_URL = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard"

# === Helpers ===
def load_councils(path):
    if not os.path.exists(path):
        print("ERROR: council file not found:", path)
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    # Expected shape: { "canonical_id": { "official_name":"...", "council":2782, "aliases": {"espn":["Illinois", ...], "common": [...]} , "espn_ids": [123, ...] } }
    return data

def normalize(s: str):
    if not s:
        return ""
    s = s.lower().strip()
    replace = {
        "&": " and ",
        ".": "",
        ",": "",
        "-": " ",
        "—": " ",
        "'": "",
        "’": ""
    }
    for k,v in replace.items():
        s = s.replace(k, v)
    # collapse spaces
    s = " ".join(s.split())
    return s

# Build lookup structures for quick resolution
def build_lookup(councils):
    alias_lookup = {}  # normalized alias -> canonical_id
    espn_id_lookup = {}  # espn team id string -> canonical_id
    council_by_number = {}  # number -> canonical_id
    for cid, info in councils.items():
        num = str(info.get("council", ""))
        council_by_number[num] = cid
        aliases = info.get("aliases", {})
        # flatten alias groups
        for group in aliases.values():
            for a in group:
                alias_lookup[normalize(a)] = cid
        # include official_name and canonical id name as aliases
        official = info.get("official_name") or info.get("official", "")
        if official:
            alias_lookup[normalize(official)] = cid
        # espn ids if present
        for espn_id in info.get("espn_ids", []):
            espn_id_lookup[str(espn_id)] = cid
    return alias_lookup, espn_id_lookup, council_by_number

# Resolve a team (by display name or espn id) -> canonical id (or None)
def resolve_team(team_obj, alias_lookup, espn_id_lookup):
    # team_obj is ESPN team structure: { "team": {"id": "xxx", "displayName": "Illinois"} ... }
    team = team_obj.get("team", {})
    team_id = team.get("id")
    display = team.get("displayName", "") or team.get("shortDisplayName", "")
    # try espn id first
    if team_id and str(team_id) in espn_id_lookup:
        return espn_id_lookup[str(team_id)]
    # try display
    norm = normalize(display)
    if norm in alias_lookup:
        return alias_lookup[norm]
    # fallback: try full team name fields
    for key in ("name", "displayName", "shortDisplayName", "location", "abbreviation"):
        v = team.get(key)
        if v and normalize(v) in alias_lookup:
            return alias_lookup[normalize(v)]
    return None

# GitHub Issue persistence helpers
def find_or_create_issue(title):
    # returns (issue_number, body_text)
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        raise RuntimeError("GITHUB_TOKEN and GITHUB_REPOSITORY must be set")
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    owner_repo = GITHUB_REPOSITORY
    # list issues
    url = f"https://api.github.com/repos/{owner_repo}/issues"
    params = {"state": "open", "per_page": 100}
    r = requests.get(url, headers=headers, params=params, timeout=30)
    r.raise_for_status()
    issues = r.json()
    for issue in issues:
        if issue.get("title") == title:
            return issue["number"], issue.get("body") or ""
    # not found -> create
    create_url = f"https://api.github.com/repos/{owner_repo}/issues"
    payload = {"title": title, "body": "[]"}
    r2 = requests.post(create_url, headers=headers, json=payload, timeout=30)
    r2.raise_for_status()
    issue = r2.json()
    return issue["number"], issue.get("body") or ""

def read_posted_set_from_issue(issue_number):
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    owner_repo = GITHUB_REPOSITORY
    url = f"https://api.github.com/repos/{owner_repo}/issues/{issue_number}"
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    body = r.json().get("body") or ""
    try:
        data = json.loads(body)
        if not isinstance(data, list):
            return set()
        return set(data)
    except Exception:
        return set()

def write_posted_set_to_issue(issue_number, posted_set):
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    owner_repo = GITHUB_REPOSITORY
    url = f"https://api.github.com/repos/{owner_repo}/issues/{issue_number}"
    body = json.dumps(sorted(list(posted_set)), indent=2)
    payload = {"body": body}
    r = requests.patch(url, headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    return True

def format_time(iso_time):
    # ESPN returns ISO timestamps like "2025-11-01T00:00Z" or with offset
    try:
        dt = datetime.fromisoformat(iso_time.replace("Z", "+00:00"))
        tz = pytz.timezone(TIMEZONE)
        local = dt.astimezone(tz)
        return local.strftime("%b %d, %Y %I:%M %p %Z").lstrip("0")
    except Exception:
        return iso_time

def get_tv_channel(competition):
    broadcasts = competition.get("broadcasts", [])
    if not broadcasts:
        return "TBD"
    names = []
    for b in broadcasts:
        for n in b.get("names", []):
            names.append(n)
    return ", ".join(names) if names else "TBD"

def build_role_mentions(council_home_num, council_away_num, councils, role_id_map):
    mentions = []
    # prefer explicit role ids if provided
    if role_id_map:
        home_id = role_id_map.get(str(council_home_num))
        away_id = role_id_map.get(str(council_away_num))
        if home_id:
            mentions.append(f"<@&{home_id}>")
        else:
            # fallback to textual name (server must allow mention by name)
            name = f"Council {council_home_num} ({councils[council_home_num]['official_name']})"
            mentions.append(name)
        if away_id:
            mentions.append(f"<@&{away_id}>")
        else:
            name = f"Council {council_away_num} ({councils[council_away_num]['official_name']})"
            mentions.append(name)
    else:
        # no role ids provided — use textual role names (server must allow mention by name)
        name1 = f"Council {council_home_num} ({councils[council_home_num]['official_name']})"
        name2 = f"Council {council_away_num} ({councils[council_away_num]['official_name']})"
        mentions = [name1, name2]
    return " ".join(mentions)

# Post to Discord webhook
def post_to_discord(webhook_url, content):
    payload = {"content": content}
    headers = {"Content-Type": "application/json"}
    r = requests.post(webhook_url, json=payload, headers=headers, timeout=30)
    if r.status_code >= 400:
        print("Discord webhook failed:", r.status_code, r.text)
        r.raise_for_status()
    return r

# Single-run main logic
def main():
    if not DISCORD_WEBHOOK:
        print("ERROR: DISCORD_WEBHOOK env var required.")
        sys.exit(1)
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        print("ERROR: GITHUB_TOKEN and GITHUB_REPOSITORY env vars required (GITHUB_TOKEN provided automatically in Actions).")
        sys.exit(1)

    councils = load_councils(COUNCIL_FILE)
    alias_lookup, espn_id_lookup, council_by_number = build_lookup(councils)

    role_id_map = {}
    if ROLE_ID_MAP_JSON:
        try:
            role_id_map = json.loads(ROLE_ID_MAP_JSON)
        except Exception:
            print("WARNING: invalid ROLE_ID_MAP_JSON; ignoring role id map.")

    # find or create issue to store posted ids
    issue_num, _ = find_or_create_issue(POSTED_STORE_ISSUE_TITLE)
    posted = read_posted_set_from_issue(issue_num)
    print("Loaded posted count:", len(posted))

    to_post = []  # list of (game_id, message)

    for url, sport in [(BASKETBALL_URL, "🏀 Men's Basketball"),
                       (FOOTBALL_URL, "🏈 Football")]:
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print("Failed to fetch", url, e)
            continue

        for event in data.get("events", []):
            game_id = str(event.get("id"))
            if not game_id or game_id in posted:
                continue

            comp = event.get("competitions", [{}])[0]
            competitors = comp.get("competitors", [])
            if len(competitors) < 2:
                continue

            # find home & away
            home = None
            away = None
            for t in competitors:
                if t.get("homeAway") == "home":
                    home = t
                elif t.get("homeAway") == "away":
                    away = t
            if not home or not away:
                # fallback: first/second
                home = competitors[0]
                away = competitors[1]

            # try to resolve to canonical ids
            home_cid = resolve_team(home, alias_lookup, espn_id_lookup)
            away_cid = resolve_team(away, alias_lookup, espn_id_lookup)

            if not home_cid or not away_cid:
                # not both councils
                continue

            # both teams are in councils -> prepare message
            home_info = councils[home_cid]
            away_info = councils[away_cid]
            home_cnum = str(home_info.get("council"))
            away_cnum = str(away_info.get("council"))

            neutral = comp.get("neutralSite", False)
            venue = comp.get("venue", {}) or {}
            stadium = venue.get("fullName") or venue.get("name") or "TBD"
            city = venue.get("address", {}).get("city", "")
            state = venue.get("address", {}).get("state", "")
            location = f"{stadium} ({city}, {state})" if city else stadium

            site_label = "🏟️ Neutral Site" if neutral else f"🏠 {home_info.get('official_name')} home game"

            start_iso = comp.get("startDate")
            start_str = format_time(start_iso) if start_iso else "TBD"
            tv = get_tv_channel(comp)

            role_mentions = build_role_mentions(home_cnum, away_cnum, councils, role_id_map)

            # Build a friendly message
            msg_lines = [
                "⚔️ **COLLEGE COUNCIL MATCHUP**",
                "",
                f"{sport}",
                f"🕖 {start_str}",
                f"📺 {tv}",
                f"📍 {location}",
                f"{site_label}",
                "",
                f"🎓 {home_info.get('official_name')} (Council {home_cnum})",
                "vs",
                f"🎓 {away_info.get('official_name')} (Council {away_cnum})",
                "",
                f"{role_mentions}"
            ]
            message = "\n".join(msg_lines)
            to_post.append((game_id, message))

    # Post messages (if any) and update posted set
    if not to_post:
        print("No new council matchups found.")
    else:
        print(f"Posting {len(to_post)} messages...")
        for game_id, message in to_post:
            try:
                post_to_discord(DISCORD_WEBHOOK, message)
                posted.add(game_id)
                print("Posted game", game_id)
            except Exception:
                print("Failed to post game", game_id)
                traceback.print_exc()

    # persist posted set
    try:
        write_posted_set_to_issue(issue_num, posted)
        print("Updated persisted posted set (count):", len(posted))
    except Exception:
        print("Failed to write posted set to issue")
        traceback.print_exc()


if __name__ == "__main__":
    main()

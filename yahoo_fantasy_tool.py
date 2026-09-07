#!/usr/bin/env python3
"""
Yahoo Fantasy Football tool
============================
Fetch league settings, draft results, and team rosters from the Yahoo
Fantasy Sports API via OAuth2.

ONE-TIME SETUP
--------------
1. Register an app at https://developer.yahoo.com/apps/create/
     - Application Name : anything, e.g. "My Fantasy Tool"
     - Homepage URL      : anything, e.g. https://localhost
     - Redirect URI(s)   : oob     <-- exactly this, lowercase
     - API Permissions   : check "Fantasy Sports" -> Read
2. Copy the Client ID (Consumer Key) and Client Secret (Consumer Secret)
   shown on the app's page.
3. Run:
       python yahoo_fantasy_tool.py auth
   It prints a URL. Open it, log in with the Yahoo account that owns
   your league, approve access, and paste the code Yahoo shows you
   back into the terminal.

USAGE
-----
    python yahoo_fantasy_tool.py leagues
    python yahoo_fantasy_tool.py settings --league-id 917220 --season 2027
    python yahoo_fantasy_tool.py draft    --league-id 917220 --season 2027
    python yahoo_fantasy_tool.py roster   --league-id 917220 --season 2027 --team-id 4

From your URL (https://football.fantasysports.yahoo.com/f1/917220/4):
    league-id = 917220
    team-id   = 4

NOTES
-----
- Yahoo assigns a new internal "game_key" every NFL season (e.g. 449 for
  the 2025 season). A renewed private league normally keeps the SAME
  league_id across seasons, so this tool looks up the right game_key for
  whatever --season you ask for and combines it with --league-id for you.
- A season only shows up once Yahoo has opened that year's game on their
  side (usually mid-summer). Run `leagues` any time to see what's live.
- Yahoo's JSON responses are deeply nested with numeric-string keys. If
  the parsing here breaks on a shape it doesn't expect, it falls back to
  dumping the raw JSON so you can see exactly what came back and adjust.
"""

import argparse
import base64
import json
import os
import sys
import time
import webbrowser
from urllib.parse import urlencode

import requests

CONFIG_PATH = os.path.expanduser("~/.yahoo_fantasy_config.json")
TOKEN_PATH = os.path.expanduser("~/.yahoo_fantasy_tokens.json")

AUTH_URL = "https://api.login.yahoo.com/oauth2/request_auth"
TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
API_BASE = "https://fantasysports.yahooapis.com/fantasy/v2"


# ---------- small local storage helpers ----------

def load_json(path):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(path, 0o600)  # keep tokens/secrets readable only by you


# ---------- OAuth2 (three-legged, "oob" redirect) ----------

def get_client_credentials():
    config = load_json(CONFIG_PATH)
    if "client_id" not in config or "client_secret" not in config:
        print("First-time setup: enter your Yahoo app credentials")
        print("(from https://developer.yahoo.com/apps/)\n")
        config["client_id"] = input("Client ID (Consumer Key): ").strip()
        config["client_secret"] = input("Client Secret (Consumer Secret): ").strip()
        save_json(CONFIG_PATH, config)
    return config["client_id"], config["client_secret"]


def do_auth():
    client_id, client_secret = get_client_credentials()
    params = {
        "client_id": client_id,
        "redirect_uri": "oob",
        "response_type": "code",
        "language": "en-us",
    }
    url = f"{AUTH_URL}?{urlencode(params)}"
    print("\nOpen this URL, log in, and approve access:\n")
    print(url)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    code = input("\nPaste the code Yahoo shows you here: ").strip()

    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = requests.post(
        TOKEN_URL,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "authorization_code",
            "redirect_uri": "oob",
            "code": code,
        },
    )
    resp.raise_for_status()
    tokens = resp.json()
    tokens["obtained_at"] = time.time()
    save_json(TOKEN_PATH, tokens)
    print("Authorized. Tokens saved to", TOKEN_PATH)


def refresh_access_token(client_id, client_secret, tokens):
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = requests.post(
        TOKEN_URL,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "refresh_token",
            "redirect_uri": "oob",
            "refresh_token": tokens["refresh_token"],
        },
    )
    resp.raise_for_status()
    new_tokens = resp.json()
    new_tokens["obtained_at"] = time.time()
    new_tokens.setdefault("refresh_token", tokens["refresh_token"])
    save_json(TOKEN_PATH, new_tokens)
    return new_tokens


def get_access_token():
    client_id, client_secret = get_client_credentials()
    tokens = load_json(TOKEN_PATH)
    if not tokens:
        print("Not authorized yet. Run: python yahoo_fantasy_tool.py auth")
        sys.exit(1)
    age = time.time() - tokens.get("obtained_at", 0)
    if age > tokens.get("expires_in", 3600) - 60:
        tokens = refresh_access_token(client_id, client_secret, tokens)
    return tokens["access_token"]


# ---------- API access ----------

def api_get(path):
    token = get_access_token()
    url = f"{API_BASE}/{path}"
    sep = "&" if "?" in url else "?"
    url = f"{url}{sep}format=json"
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"})
    resp.raise_for_status()
    return resp.json()


def iter_collection(container):
    """Yahoo returns collections as a dict of '0', '1', ..., 'count' keys.
    This yields just the item values in order, skipping 'count'."""
    for key, val in container.items():
        if key == "count":
            continue
        yield val


def find_game_key(season, game_code="nfl"):
    """Look up the Yahoo game_key for a season (e.g. 2027) by scanning
    the games this authorized account has access to."""
    data = api_get("users;use_login=1/games")
    try:
        games = data["fantasy_content"]["users"]["0"]["user"][1]["games"]
        for item in iter_collection(games):
            game = item["game"][0]
            if game.get("code") == game_code and str(game.get("season")) == str(season):
                return game["game_key"]
        return None
    except (KeyError, IndexError, TypeError):
        print("Unexpected response shape from Yahoo. Raw response:")
        print(json.dumps(data, indent=2))
        raise


def cmd_leagues(_args):
    """List every NFL season/league this account can see, so you can
    confirm the league_id and season you want to query."""
    data = api_get("users;use_login=1/games")
    try:
        games = data["fantasy_content"]["users"]["0"]["user"][1]["games"]
    except (KeyError, IndexError, TypeError):
        print("Unexpected response shape. Raw response:")
        print(json.dumps(data, indent=2))
        return

    for item in iter_collection(games):
        game = item["game"][0]
        if game.get("code") != "nfl":
            continue
        print(f"season {game['season']}  game_key={game['game_key']}")
        try:
            leagues_data = api_get(
                f"users;use_login=1/games;game_keys={game['game_key']}/leagues"
            )
            user_games = leagues_data["fantasy_content"]["users"]["0"]["user"][1]["games"]
            game_entry = next(iter_collection(user_games))
            leagues = game_entry["game"][1]["leagues"]
            for lg in iter_collection(leagues):
                league = lg["league"][0]
                print(f"    league_id={league['league_id']}  name={league['name']}")
        except (KeyError, IndexError, TypeError, StopIteration):
            print("    (couldn't list leagues for this season - "
                  "try `settings`/`draft` directly if you know the league_id)")


def cmd_settings(args):
    game_key = find_game_key(args.season)
    if not game_key:
        print(f"No NFL game found for season {args.season} on this account yet.")
        return
    league_key = f"{game_key}.l.{args.league_id}"
    data = api_get(f"league/{league_key}/settings")
    print(json.dumps(data, indent=2))


def cmd_draft(args):
    game_key = find_game_key(args.season)
    if not game_key:
        print(f"No NFL game found for season {args.season} on this account yet.")
        return
    league_key = f"{game_key}.l.{args.league_id}"
    data = api_get(f"league/{league_key}/draftresults")
    print(json.dumps(data, indent=2))


def cmd_roster(args):
    game_key = find_game_key(args.season)
    if not game_key:
        print(f"No NFL game found for season {args.season} on this account yet.")
        return
    team_key = f"{game_key}.l.{args.league_id}.t.{args.team_id}"
    data = api_get(f"team/{team_key}/roster")
    print(json.dumps(data, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Yahoo Fantasy Football tool")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("auth", help="Authorize this tool with your Yahoo account")
    sub.add_parser("leagues", help="List your visible NFL seasons/leagues")

    p = sub.add_parser("settings", help="Fetch league settings")
    p.add_argument("--league-id", required=True, type=int)
    p.add_argument("--season", required=True, type=int)
    p.set_defaults(func=cmd_settings)

    p = sub.add_parser("draft", help="Fetch draft results")
    p.add_argument("--league-id", required=True, type=int)
    p.add_argument("--season", required=True, type=int)
    p.set_defaults(func=cmd_draft)

    p = sub.add_parser("roster", help="Fetch a team's roster")
    p.add_argument("--league-id", required=True, type=int)
    p.add_argument("--season", required=True, type=int)
    p.add_argument("--team-id", required=True, type=int)
    p.set_defaults(func=cmd_roster)

    args = parser.parse_args()
    if args.command == "auth":
        do_auth()
    elif args.command == "leagues":
        cmd_leagues(args)
    else:
        args.func(args)


if __name__ == "__main__":
    main()

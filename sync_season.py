import os
import time
import sys
import requests

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
FOOTBALL_API_KEY = os.environ.get("FOOTBALL_API_KEY", "").strip()
SEASON = int(os.environ.get("SEASON", "2026"))

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY or not FOOTBALL_API_KEY:
    print("BŁĄD: Brak wymaganych zmiennych środowiskowych (SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, FOOTBALL_API_KEY).")
    sys.exit(1)

REST_BASE = SUPABASE_URL if SUPABASE_URL.endswith("/rest/v1") else f"{SUPABASE_URL}/rest/v1"
API_FOOTBALL_BASE = "https://v3.football.api-sports.io"

# ID lig w API-Football: Premier League (39), La Liga (140), Serie A (135), Bundesliga (78), Ligue 1 (61)
LEAGUES = {
    39: "Premier League",
    140: "La Liga",
    135: "Serie A",
    78: "Bundesliga",
    61: "Ligue 1",
}

FINISHED_STATUSES = {"FT", "AET", "PEN"}

sb_headers = {
    "apikey": SUPABASE_SERVICE_ROLE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates,return=minimal",
}

fb_headers = {
    "x-apisports-key": FOOTBALL_API_KEY,
}


def fb_get(endpoint: str, params: dict, retries: int = 3) -> dict:
    url = f"{API_FOOTBALL_BASE}/{endpoint.lstrip('/')}"
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=fb_headers, params=params, timeout=25)
            if resp.status_code == 429:
                wait = attempt * 10
                print(f"[RATE LIMIT 429] Czekam {wait}s przed ponowieniem ({endpoint})...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            errors = data.get("errors")
            if errors:
                print(f"[API ERRORS] {endpoint} {params}: {errors}")
                if isinstance(errors, dict) and errors.get("rateLimit"):
                    time.sleep(15)
                    continue
            return data
        except requests.RequestException as exc:
            print(f"[BŁĄD SIECI] Próba {attempt}/{retries} dla {endpoint}: {exc}")
            if attempt == retries:
                raise
            time.sleep(attempt * 3)
    return {"response": []}


def sb_upsert(table: str, rows: list[dict], on_conflict: str | None = None, chunk_size: int = 200) -> None:
    if not rows:
        return
    query = f"?on_conflict={on_conflict}" if on_conflict else ""
    url = f"{REST_BASE}/{table}{query}"
    for i in range(0, len(rows), chunk_size):
        batch = rows[i : i + chunk_size]
        resp = requests.post(url, headers=sb_headers, json=batch, timeout=30)
        if not resp.ok:
            raise RuntimeError(f"Supabase UPSERT {table} (HTTP {resp.status_code}): {resp.text}")


def parse_stat_value(statistics: list[dict], stat_type: str, is_percent: bool = False):
    for item in statistics or []:
        if item.get("type") == stat_type:
            val = item.get("value")
            if val is None:
                return 0
            if is_percent and isinstance(val, str):
                return float(val.replace("%", "").strip() or 0)
            try:
                return float(val) if "." in str(val) else int(val)
            except (ValueError, TypeError):
                return 0
    return 0


def map_status(short_status: str) -> str:
    if short_status in FINISHED_STATUSES:
        return "FINISHED"
    if short_status in {"NS", "TBD"}:
        return "SCHEDULED"
    return short_status or "SCHEDULED"


def sync_all() -> None:
    season_label = f"{SEASON}/{SEASON + 1}"
    all_matches = []
    all_team_stats = []
    all_player_stats = []
    all_odds = []

    for league_id, league_name in LEAGUES.items():
        print(f"=== Pobieranie terminarza i wyników: {league_name} ({season_label}) ===")
        fixtures_data = fb_get("fixtures", {"league": league_id, "season": SEASON})
        fixtures = fixtures_data.get("response", [])
        print(f"Znaleziono {len(fixtures)} meczów dla {league_name}.")

        finished_ids = []
        scheduled_ids = []

        for fx in fixtures:
            fixture_info = fx.get("fixture", {})
            teams_info = fx.get("teams", {})
            goals_info = fx.get("goals", {})
            match_id = fixture_info.get("id")
            if not match_id:
                continue

            status_short = (fixture_info.get("status") or {}).get("short", "NS")
            mapped_status = map_status(status_short)

            all_matches.append({
                "id": match_id,
                "league": league_name,
                "season": season_label,
                "match_date": fixture_info.get("date"),
                "home_team": (teams_info.get("home") or {}).get("name"),
                "away_team": (teams_info.get("away") or {}).get("name"),
                "status": mapped_status,
                "home_goals": goals_info.get("home"),
                "away_goals": goals_info.get("away"),
            })

            if mapped_status == "FINISHED":
                finished_ids.append(match_id)
            elif mapped_status == "SCHEDULED":
                scheduled_ids.append(match_id)

        # Statystyki drużyn i zawodników dla zakończonych meczów
        for idx, match_id in enumerate(finished_ids, start=1):
            if idx % 25 == 0:
                print(f"[{league_name}] Przetworzono {idx}/{len(finished_ids)} zakończonych meczów...")

            t_data = fb_get("fixtures/statistics", {"fixture": match_id})
            for team_block in t_data.get("response", []):
                team_obj = team_block.get("team") or {}
                team_name = team_obj.get("name")
                if not team_name:
                    continue
                stats_list = team_block.get("statistics") or []
                match_row = next((m for m in all_matches if m["id"] == match_id), None)
                is_home = bool(match_row and match_row["home_team"] == team_name)
                goals_val = (match_row["home_goals"] if is_home else match_row["away_goals"]) if match_row else 0

                all_team_stats.append({
                    "match_id": match_id,
                    "team_name": team_name,
                    "is_home": is_home,
                    "goals": int(goals_val or 0),
                    "corners": int(parse_stat_value(stats_list, "Corner Kicks") or 0),
                    "yellow_cards": int(parse_stat_value(stats_list, "Yellow Cards") or 0),
                    "red_cards": int(parse_stat_value(stats_list, "Red Cards") or 0),
                    "fouls": int(parse_stat_value(stats_list, "Fouls") or 0),
                    "shots": int(parse_stat_value(stats_list, "Total Shots") or 0),
                    "shots_on_target": int(parse_stat_value(stats_list, "Shots on Goal") or 0),
                    "possession": parse_stat_value(stats_list, "Ball Possession", is_percent=True),
                })

            p_data = fb_get("fixtures/players", {"fixture": match_id})
            for team_players_block in p_data.get("response", []):
                team_name = (team_players_block.get("team") or {}).get("name")
                for pl in team_players_block.get("players", []):
                    p_info = pl.get("player") or {}
                    p_name = p_info.get("name")
                    st_list = pl.get("statistics") or []
                    if not p_name or not st_list:
                        continue
                    s0 = st_list[0]
                    games = s0.get("games") or {}
                    goals = s0.get("goals") or {}
                    shots = s0.get("shots") or {}
                    cards = s0.get("cards") or {}
                    fouls = s0.get("fouls") or {}

                    minutes = int(games.get("minutes") or 0)
                    if minutes == 0 and (goals.get("total") or 0) == 0 and (shots.get("total") or 0) == 0:
                        continue

                    all_player_stats.append({
                        "match_id": match_id,
                        "player_name": p_name,
                        "team_name": team_name,
                        "minutes": minutes,
                        "goals": int(goals.get("total") or 0),
                        "assists": int(goals.get("assists") or 0),
                        "shots": int(shots.get("total") or 0),
                        "shots_on_target": int(shots.get("on") or 0),
                        "yellow_cards": int(cards.get("yellow") or 0),
                        "fouls_committed": int(fouls.get("committed") or 0),
                        "fouls_drawn": int(fouls.get("drawn") or 0),
                    })

            time.sleep(0.15)

        # Kursy dla najbliższych zaplanowanych meczów (np. pierwsze 10 na ligę, aby oszczędzać limit zapytań)
        for match_id in scheduled_ids[:10]:
            o_data = fb_get("odds", {"fixture": match_id})
            for item in o_data.get("response", []):
                for bk in item.get("bookmakers", []):
                    bk_name = bk.get("name") or "Bookmaker"
                    for bet in bk.get("bets", []):
                        bet_name = bet.get("name") or ""
                        for val in bet.get("values", []):
                            odd_raw = val.get("odd")
                            if odd_raw is None:
                                continue
                            try:
                                odd_val = float(odd_raw)
                            except ValueError:
                                continue
                            all_odds.append({
                                "match_id": match_id,
                                "bookmaker": bk_name,
                                "market": bet_name,
                                "selection": str(val.get("value") or ""),
                                "line": None,
                                "odd_value": odd_val,
                            })
            time.sleep(0.15)

    print(
        f"Podsumowanie pobranych rekordów: matches={len(all_matches)}, "
        f"team_stats={len(all_team_stats)}, player_stats={len(all_player_stats)}, odds={len(all_odds)}"
    )

    sb_upsert("matches", all_matches, on_conflict="id")
    sb_upsert("match_team_stats", all_team_stats, on_conflict="match_id,team_name")
    sb_upsert("match_player_stats", all_player_stats, on_conflict="match_id,player_name")
    sb_upsert("odds", all_odds)
    print("Synchronizacja z Supabase zakończona pomyślnie.")


if __name__ == "__main__":
    sync_all()

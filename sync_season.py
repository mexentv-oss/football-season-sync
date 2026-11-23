import os
import sys
import time
import requests

FOOTBALL_DATA_TOKEN = os.environ.get("FOOTBALL_DATA_TOKEN", "").strip()
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()

BASE_URL = "https://api.football-data.org/v4"
MATCHES_TABLE = "matches"

# Kody lig w football-data.org v4 (Top 5)
COMPETITIONS = {
    "PL": "Premier League",
    "PD": "La Liga",
    "SA": "Serie A",
    "BL1": "Bundesliga",
    "FL1": "Ligue 1",
}


def require_env(name: str, value: str) -> str:
    if not value:
        print(f"[BŁĄD KONFIGURACJI] Brak wymaganej zmiennej środowiskowej: {name}", file=sys.stderr)
        sys.exit(1)
    return value


def fd_get_matches(code: str, max_retries: int = 3) -> list[dict]:
    url = f"{BASE_URL}/competitions/{code}/matches"
    headers = {"X-Auth-Token": FOOTBALL_DATA_TOKEN}

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=20)
            if resp.status_code == 429:
                wait_s = 12 * attempt
                print(f"[LIMIT API 429] {code}: odczekuję {wait_s}s (próba {attempt}/{max_retries})")
                time.sleep(wait_s)
                continue
            resp.raise_for_status()
            payload = resp.json()
            matches = payload.get("matches")
            return matches if isinstance(matches, list) else []
        except requests.HTTPError as exc:
            print(f"[BŁĄD HTTP] Próba {attempt}/{max_retries} dla {code}: {exc}", file=sys.stderr)
            if attempt == max_retries:
                raise
            time.sleep(3 * attempt)
        except requests.RequestException as exc:
            print(f"[BŁĄD SIECI] Próba {attempt}/{max_retries} dla {code}: {exc}", file=sys.stderr)
            if attempt == max_retries:
                raise
            time.sleep(3 * attempt)

    return []


def map_match_row(code: str, league_name: str, m: dict) -> dict:
    home_team = m.get("homeTeam") or {}
    away_team = m.get("awayTeam") or {}
    score = m.get("score") or {}
    full_time = score.get("fullTime") or {}
    season = m.get("season") or {}

    return {
        "external_id": m.get("id"),
        "competition_code": code,
        "league_name": league_name,
        "season_start_date": season.get("startDate"),
        "utc_date": m.get("utcDate"),
        "status": m.get("status"),
        "matchday": m.get("matchday"),
        "home_team_id": home_team.get("id"),
        "home_team_name": home_team.get("name"),
        "away_team_id": away_team.get("id"),
        "away_team_name": away_team.get("name"),
        "home_goals": full_time.get("home"),
        "away_goals": full_time.get("away"),
    }


def supabase_upsert_matches(rows: list[dict]) -> None:
    if not rows:
        return

    url = f"{SUPABASE_URL}/rest/v1/{MATCHES_TABLE}?on_conflict=external_id"
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }

    batch_size = 200
    for i in range(0, len(rows), batch_size):
        chunk = rows[i : i + batch_size]
        resp = requests.post(url, headers=headers, json=chunk, timeout=30)
        resp.raise_for_status()


def sync_all() -> None:
    require_env("FOOTBALL_DATA_TOKEN", FOOTBALL_DATA_TOKEN)
    require_env("SUPABASE_URL", SUPABASE_URL)
    require_env("SUPABASE_SERVICE_ROLE_KEY", SUPABASE_SERVICE_ROLE_KEY)

    total_matches = 0
    items = list(COMPETITIONS.items())

    for idx, (code, league_name) in enumerate(items):
        print(f"=== Pobieranie meczów: {league_name} ({code}) ===")
        raw_matches = fd_get_matches(code)
        rows = [map_match_row(code, league_name, m) for m in raw_matches if m.get("id") is not None]
        print(f"Znaleziono {len(rows)} meczów dla {league_name}.")

        if rows:
            supabase_upsert_matches(rows)
            total_matches += len(rows)

        # Pauza między ligami, aby utrzymać się poniżej limitu 10 req/min
        if idx < len(items) - 1:
            time.sleep(7)

    print(f"Podsumowanie zapisanych rekordów: matches={total_matches}")
    print("Synchronizacja z Supabase zakończona pomyślnie.")


if __name__ == "__main__":
    sync_all()
    

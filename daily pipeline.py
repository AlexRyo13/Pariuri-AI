"""
Pipeline zilnic de analiza pariuri - 4 ligi (Anglia, Germania, Spania, Italia)
================================================================================
Versiune imbunatatita: adauga forma actuala, pozitia in clasament, goluri
acasa/deplasare separat, si accidentari/suspendari - pentru precizie mai buna.

Cum rulezi:
  1. pip install requests
  2. Seteaza variabilele: ODDS_API_KEY, API_FOOTBALL_KEY
  3. python daily_pipeline.py

Notite despre limita gratuita (100 cereri/zi la API-Football):
  - Cu 4 ligi x ~5 meciuri viitoare x 2 echipe x 2 cereri (stats+injuries)
    per echipa, plus fixtures, ne incadram confortabil sub 100/zi.
  - Cornerele NU sunt incluse (ar cere mult mai multe cereri per meci,
    din istoricul detaliat al fiecarui meci anterior).
"""

import os
import json
import math
from datetime import datetime, timezone
import requests

# ---------------------------------------------------------------
# Configurare
# ---------------------------------------------------------------
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY", "")

LEAGUES = {
    "PL": {"id": 39, "odds_key": "soccer_epl", "label": "Anglia", "full": "Premier League"},
    "BL": {"id": 78, "odds_key": "soccer_germany_bundesliga", "label": "Germania", "full": "Bundesliga"},
    "LL": {"id": 140, "odds_key": "soccer_spain_la_liga", "label": "Spania", "full": "La Liga"},
    "SA": {"id": 135, "odds_key": "soccer_italy_serie_a", "label": "Italia", "full": "Serie A"},
}

CURRENT_SEASON = 2025
MATCHES_PER_LEAGUE = 5

EDGE_VALUE_THRESHOLD = 0.08
EDGE_RISKY_THRESHOLD = 0.0

OUTPUT_PATH = "output.json"

FOOTBALL_API_BASE = "https://v3.football.api-sports.io"


def api_football_get(path, params):
    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    r = requests.get(f"{FOOTBALL_API_BASE}{path}", headers=headers, params=params, timeout=15)
    r.raise_for_status()
    return r.json().get("response", [])


# ---------------------------------------------------------------
# Meciurile viitoare dintr-o liga
# ---------------------------------------------------------------
def get_upcoming_fixtures(league_id):
    return api_football_get("/fixtures", {
        "league": league_id, "season": CURRENT_SEASON, "next": MATCHES_PER_LEAGUE,
    })


# ---------------------------------------------------------------
# Statistici echipa: goluri (total + acasa/deplasare), forma
# ---------------------------------------------------------------
def get_team_stats(league_id, team_id):
    data = api_football_get("/teams/statistics", {
        "league": league_id, "season": CURRENT_SEASON, "team": team_id,
    })
    if isinstance(data, list):
        data = {}

    goals_for = data.get("goals", {}).get("for", {}).get("average", {})
    goals_against = data.get("goals", {}).get("against", {}).get("average", {})

    return {
        "goals_for_total": float(goals_for.get("total", 1.3) or 1.3),
        "goals_for_home": float(goals_for.get("home", 1.4) or 1.4),
        "goals_for_away": float(goals_for.get("away", 1.1) or 1.1),
        "goals_against_total": float(goals_against.get("total", 1.3) or 1.3),
        "goals_against_home": float(goals_against.get("home", 1.1) or 1.1),
        "goals_against_away": float(goals_against.get("away", 1.4) or 1.4),
        "form": data.get("form", "") or "",
    }


# ---------------------------------------------------------------
# Pozitia in clasament
# ---------------------------------------------------------------
def get_standings(league_id):
    """Returneaza un dict {team_id: pozitie}."""
    data = api_football_get("/standings", {"league": league_id, "season": CURRENT_SEASON})
    positions = {}
    try:
        table = data[0]["league"]["standings"][0]
        for row in table:
            positions[row["team"]["id"]] = row["rank"]
    except (IndexError, KeyError, TypeError):
        pass
    return positions


# ---------------------------------------------------------------
# Accidentari / suspendari curente pentru o echipa
# ---------------------------------------------------------------
def get_injuries(league_id, team_id):
    data = api_football_get("/injuries", {
        "league": league_id, "season": CURRENT_SEASON, "team": team_id,
    })
    players = []
    for item in data[:8]:  # limitam la 8, ca sa nu incarcam prea mult
        player = item.get("player", {})
        players.append({
            "name": player.get("name", "Necunoscut"),
            "reason": player.get("reason", ""),
        })
    return players


# ---------------------------------------------------------------
# Cote curente
# ---------------------------------------------------------------
def get_odds(sport_key):
    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds"
    params = {"apiKey": ODDS_API_KEY, "regions": "eu", "markets": "h2h,totals", "oddsFormat": "decimal"}
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def best_odds_for_match(odds_data, home_team, away_team):
    for event in odds_data:
        if event.get("home_team") == home_team and event.get("away_team") == away_team:
            best = {"1": 0, "X": 0, "2": 0, "Peste 2.5": 0, "Sub 2.5": 0}
            for bookmaker in event.get("bookmakers", []):
                for market in bookmaker.get("markets", []):
                    if market["key"] == "h2h":
                        for outcome in market["outcomes"]:
                            if outcome["name"] == home_team:
                                best["1"] = max(best["1"], outcome["price"])
                            elif outcome["name"] == away_team:
                                best["2"] = max(best["2"], outcome["price"])
                            else:
                                best["X"] = max(best["X"], outcome["price"])
                    elif market["key"] == "totals":
                        for outcome in market["outcomes"]:
                            if outcome["name"] == "Over":
                                best["Peste 2.5"] = max(best["Peste 2.5"], outcome["price"])
                            elif outcome["name"] == "Under":
                                best["Sub 2.5"] = max(best["Sub 2.5"], outcome["price"])
            return best
    return None


# ---------------------------------------------------------------
# Model Poisson, acum foloseste goluri acasa/deplasare separat
# ---------------------------------------------------------------
def poisson_pmf(k, lam):
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def match_probabilities(home_stats, away_stats, league_avg_goals=1.4, max_goals=6):
    home_advantage = 1.1
    # gazdele: atacul lor acasa vs apararea oaspetilor in deplasare
    lambda_home = (home_stats["goals_for_home"] / league_avg_goals) * \
                  (away_stats["goals_against_away"] / league_avg_goals) * league_avg_goals * home_advantage
    # oaspetii: atacul lor in deplasare vs apararea gazdelor acasa
    lambda_away = (away_stats["goals_for_away"] / league_avg_goals) * \
                  (home_stats["goals_against_home"] / league_avg_goals) * league_avg_goals

    prob_home, prob_draw, prob_away, prob_over25 = 0.0, 0.0, 0.0, 0.0
    for hg in range(max_goals):
        for ag in range(max_goals):
            p = poisson_pmf(hg, lambda_home) * poisson_pmf(ag, lambda_away)
            if hg > ag:
                prob_home += p
            elif hg == ag:
                prob_draw += p
            else:
                prob_away += p
            if hg + ag > 2.5:
                prob_over25 += p

    return {
        "1": round(prob_home, 4), "X": round(prob_draw, 4), "2": round(prob_away, 4),
        "Peste 2.5": round(prob_over25, 4), "Sub 2.5": round(1 - prob_over25, 4),
    }


def compute_edge(model_prob, odds):
    return round(model_prob * odds - 1, 4) if odds else None


def verdict(edge):
    if edge is None:
        return "fara-cota"
    if edge >= EDGE_VALUE_THRESHOLD:
        return "valoare"
    if edge >= EDGE_RISKY_THRESHOLD:
        return "riscant"
    return "evita"


# ---------------------------------------------------------------
# Orchestrare
# ---------------------------------------------------------------
def run():
    output = {"generated_at": datetime.now(timezone.utc).isoformat()}

    for league_code, league_info in LEAGUES.items():
        print(f"Procesez {league_info['full']}...")
        league_id = league_info["id"]
        fixtures = get_upcoming_fixtures(league_id)
        odds_data = get_odds(league_info["odds_key"])
        standings = get_standings(league_id)

        league_results = []
        for fx in fixtures:
            home = fx["teams"]["home"]["name"]
            away = fx["teams"]["away"]["name"]
            home_id = fx["teams"]["home"]["id"]
            away_id = fx["teams"]["away"]["id"]
            kickoff = fx["fixture"]["date"]

            home_stats = get_team_stats(league_id, home_id)
            away_stats = get_team_stats(league_id, away_id)
            home_injuries = get_injuries(league_id, home_id)
            away_injuries = get_injuries(league_id, away_id)

            probs = match_probabilities(home_stats, away_stats)
            odds = best_odds_for_match(odds_data, home, away) or {}

            outcomes = {}
            for label in ["1", "X", "2", "Peste 2.5", "Sub 2.5"]:
                edge = compute_edge(probs[label], odds.get(label))
                outcomes[label] = {
                    "modelProb": probs[label], "odds": odds.get(label),
                    "edge": edge, "verdict": verdict(edge),
                }

            league_results.append({
                "id": f"{league_code}-{fx['fixture']['id']}",
                "home": home, "away": away, "kickoff": kickoff,
                "outcomes": outcomes,
                "context": {
                    "home": {
                        "form": home_stats["form"][-5:],
                        "position": standings.get(home_id),
                        "goals_home_avg": home_stats["goals_for_home"],
                        "goals_conceded_home_avg": home_stats["goals_against_home"],
                        "injuries": home_injuries,
                    },
                    "away": {
                        "form": away_stats["form"][-5:],
                        "position": standings.get(away_id),
                        "goals_away_avg": away_stats["goals_for_away"],
                        "goals_conceded_away_avg": away_stats["goals_against_away"],
                        "injuries": away_injuries,
                    },
                },
            })

        output[league_code] = league_results

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    total = sum(len(v) for k, v in output.items() if isinstance(v, list))
    print(f"Salvat {total} meciuri (4 ligi) in {OUTPUT_PATH}")


if __name__ == "__main__":
    if not ODDS_API_KEY or not API_FOOTBALL_KEY:
        print("Lipsesc cheile API. Seteaza ODDS_API_KEY si API_FOOTBALL_KEY.")
    else:
        run()

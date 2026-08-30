"""
Pipeline zilnic de analiza pariuri - Premier League
=====================================================
Ce face:
  1. Ia meciurile zilei/saptamanii din Premier League (API-Football)
  2. Ia cotele curente pentru aceste meciuri (The Odds API)
  3. Calculeaza probabilitati cu un model Poisson simplu, bazat pe
     golurile marcate/incasate de fiecare echipa in sezonul curent
  4. Calculeaza edge-ul (valoare) fata de cota bookmaker-ului
  5. Salveaza tot intr-un fisier JSON, gata de afisat in dashboard

Cum rulezi:
  1. pip install requests scipy
  2. Seteaza variabilele de mediu (sau .env):
       ODDS_API_KEY=...        (de pe the-odds-api.com)
       API_FOOTBALL_KEY=...    (de pe rapidapi.com -> API-Football)
  3. python daily_pipeline.py

Cum il programezi sa ruleze zilnic (gratis):
  - GitHub Actions: adauga un workflow cu "schedule: cron: '0 7 * * *'"
    (ruleaza zilnic la 07:00 UTC) care executa acest script si
    salveaza output.json intr-un repo/bucket citit de site.
  - Alternativ: Vercel Cron Jobs, daca site-ul e gazduit pe Vercel.
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

PREMIER_LEAGUE_ID = 39          # ID-ul Premier League in API-Football
CURRENT_SEASON = 2025           # sezonul curent (an de start)
SPORT_KEY = "soccer_epl"        # cheia ligii in The Odds API

EDGE_VALUE_THRESHOLD = 0.08     # peste asta => verde ("VALOARE")
EDGE_RISKY_THRESHOLD = 0.0      # intre 0 si prag_value => galben ("RISCANT")
# sub 0 => rosu ("EVITA")

OUTPUT_PATH = "output.json"


# ---------------------------------------------------------------
# Pas 1: ia meciurile urmatoarelor 7 zile din Premier League
# ---------------------------------------------------------------
def get_upcoming_fixtures():
    url = "https://v3.football.api-sports.io/fixtures"
    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    params = {
        "league": PREMIER_LEAGUE_ID,
        "season": CURRENT_SEASON,
        "next": 10,  # urmatoarele 10 meciuri programate
    }
    r = requests.get(url, headers=headers, params=params, timeout=15)
    r.raise_for_status()
    return r.json().get("response", [])


# ---------------------------------------------------------------
# Pas 2: statistici echipa (goluri marcate/incasate, medie pe meci)
# folosite ca input pentru modelul Poisson
# ---------------------------------------------------------------
def get_team_stats(team_id):
    url = "https://v3.football.api-sports.io/teams/statistics"
    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    params = {"league": PREMIER_LEAGUE_ID, "season": CURRENT_SEASON, "team": team_id}
    r = requests.get(url, headers=headers, params=params, timeout=15)
    r.raise_for_status()
    data = r.json().get("response", {})

    goals_for_avg = float(data.get("goals", {}).get("for", {}).get("average", {}).get("total", 1.3) or 1.3)
    goals_against_avg = float(data.get("goals", {}).get("against", {}).get("average", {}).get("total", 1.3) or 1.3)
    return goals_for_avg, goals_against_avg


# ---------------------------------------------------------------
# Pas 3: cote curente de la bookmakeri
# ---------------------------------------------------------------
def get_odds():
    url = f"https://api.the-odds-api.com/v4/sports/{SPORT_KEY}/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "eu",
        "markets": "h2h,totals",
        "oddsFormat": "decimal",
    }
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def best_odds_for_match(odds_data, home_team, away_team):
    """Gaseste cea mai buna cota disponibila pentru fiecare rezultat,
    comparand intre bookmakerii returnati."""
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
# Pas 4: model Poisson - probabilitati 1X2 si Peste/Sub 2.5
# ---------------------------------------------------------------
def poisson_pmf(k, lam):
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def match_probabilities(home_attack, home_defense, away_attack, away_defense,
                         league_avg_goals=1.4, max_goals=6):
    """
    Model Dixon-Coles simplificat:
    lambda_home = putere atac gazde * slabiciune aparare oaspeti * avantaj teren
    lambda_away = putere atac oaspeti * slabiciune aparare gazde
    """
    home_advantage = 1.15
    lambda_home = (home_attack / league_avg_goals) * (away_defense / league_avg_goals) * league_avg_goals * home_advantage
    lambda_away = (away_attack / league_avg_goals) * (home_defense / league_avg_goals) * league_avg_goals

    prob_home, prob_draw, prob_away = 0.0, 0.0, 0.0
    prob_over25 = 0.0

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
        "1": round(prob_home, 4),
        "X": round(prob_draw, 4),
        "2": round(prob_away, 4),
        "Peste 2.5": round(prob_over25, 4),
        "Sub 2.5": round(1 - prob_over25, 4),
    }


# ---------------------------------------------------------------
# Pas 5: edge si verdict
# ---------------------------------------------------------------
def compute_edge(model_prob, odds):
    if not odds:
        return None
    return round(model_prob * odds - 1, 4)


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
    fixtures = get_upcoming_fixtures()
    odds_data = get_odds()
    results = []

    for fx in fixtures:
        home = fx["teams"]["home"]["name"]
        away = fx["teams"]["away"]["name"]
        home_id = fx["teams"]["home"]["id"]
        away_id = fx["teams"]["away"]["id"]
        kickoff = fx["fixture"]["date"]

        home_gf, home_ga = get_team_stats(home_id)
        away_gf, away_ga = get_team_stats(away_id)

        probs = match_probabilities(home_gf, home_ga, away_gf, away_ga)
        odds = best_odds_for_match(odds_data, home, away) or {}

        outcomes = {}
        for label in ["1", "X", "2", "Peste 2.5", "Sub 2.5"]:
            edge = compute_edge(probs[label], odds.get(label))
            outcomes[label] = {
                "modelProb": probs[label],
                "odds": odds.get(label),
                "edge": edge,
                "verdict": verdict(edge),
            }

        results.append({
            "home": home,
            "away": away,
            "kickoff": kickoff,
            "outcomes": outcomes,
        })

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "league": "Premier League",
        "matches": results,
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"Salvat {len(results)} meciuri in {OUTPUT_PATH}")


if __name__ == "__main__":
    if not ODDS_API_KEY or not API_FOOTBALL_KEY:
        print("Lipsesc cheile API. Seteaza ODDS_API_KEY si API_FOOTBALL_KEY.")
    else:
        run()

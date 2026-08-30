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
    "PL": {"id": 39, "odds_key": "soccer_epl", "label": "Anglia", "full": "Premier League", "home_advantage": 1.12},
    "BL": {"id": 78, "odds_key": "soccer_germany_bundesliga", "label": "Germania", "full": "Bundesliga", "home_advantage": 1.08},
    "LL": {"id": 140, "odds_key": "soccer_spain_la_liga", "label": "Spania", "full": "La Liga", "home_advantage": 1.15},
    "SA": {"id": 135, "odds_key": "soccer_italy_serie_a", "label": "Italia", "full": "Serie A", "home_advantage": 1.10},
}

CURRENT_SEASON = 2025
MATCHES_PER_LEAGUE = 5

EDGE_VALUE_THRESHOLD = 0.08
EDGE_RISKY_THRESHOLD = 0.0

OUTPUT_PATH = "output.json"
TRACK_RECORD_PATH = "track_record.json"

FOOTBALL_API_BASE = "https://v3.football.api-sports.io"


def load_track_record():
    if os.path.exists(TRACK_RECORD_PATH):
        with open(TRACK_RECORD_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"history": [], "stats": {"total": 0, "castigate": 0, "rata_reusita": None}}


def get_fixture_result(fixture_id):
    """Intoarce (goluri_gazde, goluri_oaspeti, status) sau None daca nu e gata."""
    data = api_football_get("/fixtures", {"id": fixture_id})
    if not data:
        return None
    fx = data[0]
    status = fx["fixture"]["status"]["short"]
    if status != "FT":  # meciul nu s-a terminat inca
        return None
    goals = fx["goals"]
    return goals["home"], goals["away"]


def actual_outcome(label, home_goals, away_goals):
    total = home_goals + away_goals
    if label == "1":
        return home_goals > away_goals
    if label == "X":
        return home_goals == away_goals
    if label == "2":
        return away_goals > home_goals
    if label == "Peste 2.5":
        return total > 2.5
    if label == "Sub 2.5":
        return total < 2.5
    if label == "GG":
        return home_goals > 0 and away_goals > 0
    if label == "NG":
        return home_goals == 0 or away_goals == 0
    return None


def update_track_record(track_record):
    """Verifica predictiile vechi 'in asteptare' si le actualizeaza cu
    rezultatul real, daca meciul s-a terminat intre timp."""
    for entry in track_record["history"]:
        if entry["rezultat"] != "in asteptare":
            continue
        result = get_fixture_result(entry["fixture_id"])
        if result is None:
            continue  # meciul inca nu s-a jucat / nu s-a terminat
        home_goals, away_goals = result
        correct = actual_outcome(entry["pick"], home_goals, away_goals)
        entry["rezultat"] = "castigat" if correct else "pierdut"
        entry["scor_real"] = f"{home_goals}-{away_goals}"

    finished = [e for e in track_record["history"] if e["rezultat"] in ("castigat", "pierdut")]
    total = len(finished)
    wins = len([e for e in finished if e["rezultat"] == "castigat"])
    track_record["stats"] = {
        "total": total, "castigate": wins,
        "rata_reusita": round(wins / total, 4) if total > 0 else None,
    }
    return track_record


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
    params = {"apiKey": ODDS_API_KEY, "regions": "eu", "markets": "h2h,totals,btts", "oddsFormat": "decimal"}
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def best_odds_for_match(odds_data, home_team, away_team):
    for event in odds_data:
        if event.get("home_team") == home_team and event.get("away_team") == away_team:
            best = {"1": 0, "X": 0, "2": 0, "Peste 2.5": 0, "Sub 2.5": 0, "GG": 0, "NG": 0}
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
                    elif market["key"] == "btts":
                        for outcome in market["outcomes"]:
                            if outcome["name"] == "Yes":
                                best["GG"] = max(best["GG"], outcome["price"])
                            elif outcome["name"] == "No":
                                best["NG"] = max(best["NG"], outcome["price"])
            return best
    return None


# ---------------------------------------------------------------
# Model Poisson, acum foloseste goluri acasa/deplasare separat
# ---------------------------------------------------------------
def form_adjustment(form_string):
    """
    Calculeaza un mic ajustor (intre ~0.9 si ~1.1) pe baza formei recente
    (ultimele 5 meciuri: V/E/I). O forma foarte buna creste usor atacul
    echipei, o forma proasta il scade usor. Efect limitat la +/-10%,
    ca sa nu domine media de sezon (care ramane baza principala).
    """
    if not form_string:
        return 1.0
    recent = form_string[-5:]
    score = 0
    for c in recent:
        if c == "W":
            score += 1
        elif c == "D":
            score += 0.5
        # "L" adauga 0
    avg = score / len(recent)  # intre 0 si 1
    # 0.5 (forma neutra) => 1.0 ; 1.0 (5 victorii) => ~1.1 ; 0.0 (5 infrangeri) => ~0.9
    return 0.9 + (avg * 0.2)


def poisson_pmf(k, lam):
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


# Constanta Dixon-Coles: corecteaza usor probabilitatile la scoruri mici
# (0-0, 1-0, 0-1, 1-1), unde Poisson-ul "pur" nu e perfect de precis.
# Valoare tipica din literatura de specialitate (Dixon & Coles, 1997): -0.08.
DIXON_COLES_RHO = -0.08


def dixon_coles_tau(hg, ag, lambda_home, lambda_away, rho):
    if hg == 0 and ag == 0:
        return 1 - (lambda_home * lambda_away * rho)
    if hg == 0 and ag == 1:
        return 1 + (lambda_home * rho)
    if hg == 1 and ag == 0:
        return 1 + (lambda_away * rho)
    if hg == 1 and ag == 1:
        return 1 - rho
    return 1.0


def match_probabilities(home_stats, away_stats, home_advantage, league_avg_goals=1.4, max_goals=6):
    home_form_adj = form_adjustment(home_stats["form"])
    away_form_adj = form_adjustment(away_stats["form"])

    lambda_home = (home_stats["goals_for_home"] / league_avg_goals) * \
                  (away_stats["goals_against_away"] / league_avg_goals) * league_avg_goals * \
                  home_advantage * home_form_adj
    lambda_away = (away_stats["goals_for_away"] / league_avg_goals) * \
                  (home_stats["goals_against_home"] / league_avg_goals) * league_avg_goals * \
                  away_form_adj

    raw_probs = {}
    total_p = 0.0
    for hg in range(max_goals):
        for ag in range(max_goals):
            p = poisson_pmf(hg, lambda_home) * poisson_pmf(ag, lambda_away)
            p *= dixon_coles_tau(hg, ag, lambda_home, lambda_away, DIXON_COLES_RHO)
            p = max(p, 0.0)  # tau poate da valori usor negative in cazuri extreme
            raw_probs[(hg, ag)] = p
            total_p += p

    # renormalizam, ca suma probabilitatilor sa ramana exact 1
    prob_home, prob_draw, prob_away, prob_over25, prob_btts = 0.0, 0.0, 0.0, 0.0, 0.0
    for (hg, ag), p in raw_probs.items():
        p = p / total_p if total_p > 0 else 0.0
        if hg > ag:
            prob_home += p
        elif hg == ag:
            prob_draw += p
        else:
            prob_away += p
        if hg + ag > 2.5:
            prob_over25 += p
        if hg > 0 and ag > 0:
            prob_btts += p

    return {
        "1": round(prob_home, 4), "X": round(prob_draw, 4), "2": round(prob_away, 4),
        "Peste 2.5": round(prob_over25, 4), "Sub 2.5": round(1 - prob_over25, 4),
        "GG": round(prob_btts, 4), "NG": round(1 - prob_btts, 4),
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
    track_record = load_track_record()
    track_record = update_track_record(track_record)

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

            probs = match_probabilities(home_stats, away_stats, league_info["home_advantage"])
            odds = best_odds_for_match(odds_data, home, away) or {}

            outcomes = {}
            for label in ["1", "X", "2", "Peste 2.5", "Sub 2.5", "GG", "NG"]:
                edge = compute_edge(probs[label], odds.get(label))
                outcomes[label] = {
                    "modelProb": probs[label], "odds": odds.get(label),
                    "edge": edge, "verdict": verdict(edge),
                }

            league_results.append({
                "id": f"{league_code}-{fx['fixture']['id']}",
                "fixture_id": fx["fixture"]["id"],
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

    total = sum(len(v) for k, v in output.items() if isinstance(v, list))
    print(f"Salvat {total} meciuri (4 ligi) in {OUTPUT_PATH}")

    # -----------------------------------------------------------
    # Biletul zilei: cele mai bune selectii (edge mare) din toate ligile,
    # combinate. ATENTIE: un bilet cu mai multe selectii inmulteste
    # riscul, nu il aduna - vezi nota din output.
    # -----------------------------------------------------------
    all_picks = []
    all_outcomes_ranked = []
    for league_code, matches in output.items():
        if not isinstance(matches, list):
            continue
        for m in matches:
            for label, out in m["outcomes"].items():
                if out["edge"] is None:
                    continue
                entry = {
                    "league": league_code, "match": f"{m['home']} - {m['away']}",
                    "pick": label, "odds": out["odds"], "modelProb": out["modelProb"],
                    "edge": out["edge"], "kickoff": m["kickoff"], "verdict": out["verdict"],
                    "fixture_id": m["fixture_id"],
                }
                all_outcomes_ranked.append(entry)
                if out["verdict"] == "valoare":
                    all_picks.append(entry)

    all_picks.sort(key=lambda p: p["edge"], reverse=True)
    all_outcomes_ranked.sort(key=lambda p: p["edge"], reverse=True)

    # Vrem mereu exact 6 selectii. Daca nu sunt 6 cu verdict "valoare",
    # completam cu urmatoarele cele mai bune disponibile (marcate ca atare).
    top_picks = all_picks[:6]
    if len(top_picks) < 6:
        already = {(p["match"], p["pick"]) for p in top_picks}
        for p in all_outcomes_ranked:
            if len(top_picks) >= 6:
                break
            if (p["match"], p["pick"]) not in already:
                top_picks.append(p)
                already.add((p["match"], p["pick"]))

    combined_odds = 1.0
    combined_prob = 1.0
    for p in top_picks:
        combined_odds *= p["odds"]
        combined_prob *= p["modelProb"]

    output["biletul_zilei"] = {
        "selectii": top_picks,
        "cota_totala": round(combined_odds, 2),
        "sansa_estimata_model": round(combined_prob, 4),
        "avertisment": "Un bilet cu mai multe selectii inmulteste riscul, nu il aduna. "
                        "Sansa combinata scade mult sub sansa fiecarei selectii individuale.",
    }

    # -----------------------------------------------------------
    # Adaugam predictiile de tip "valoare" din aceasta zi in istoric,
    # ca maine sa le putem verifica fata de rezultatul real.
    # -----------------------------------------------------------
    already_tracked = {(e["match"], e["pick"], e["fixture_id"]) for e in track_record["history"]}
    today_str = datetime.now(timezone.utc).date().isoformat()
    for p in all_picks:
        key = (p["match"], p["pick"], p["fixture_id"])
        if key in already_tracked:
            continue
        track_record["history"].append({
            "data": today_str, "league": p["league"], "match": p["match"],
            "pick": p["pick"], "odds": p["odds"], "edge": p["edge"],
            "fixture_id": p["fixture_id"], "rezultat": "in asteptare", "scor_real": None,
        })

    # pastram doar ultimele 200 de intrari, ca fisierul sa nu creasca la nesfarsit
    track_record["history"] = track_record["history"][-200:]

    output["istoric_performanta"] = track_record["stats"]

    with open(TRACK_RECORD_PATH, "w", encoding="utf-8") as f:
        json.dump(track_record, f, ensure_ascii=False, indent=2)

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    if not ODDS_API_KEY or not API_FOOTBALL_KEY:
        print("Lipsesc cheile API. Seteaza ODDS_API_KEY si API_FOOTBALL_KEY.")
    else:
        run()

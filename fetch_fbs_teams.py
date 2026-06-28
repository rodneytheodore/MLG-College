"""
Fetches every FBS conference + team (logos, colors) from ESPN in a single
request, then writes a clean, flat JSON file keyed by conference name.

Run with:  python fetch_fbs_teams.py
Requires:  pip install requests
"""

import json
import requests

URL = (
    "https://site.web.api.espn.com/apis/site/v2/sports/football/"
    "college-football/teams"
)
PARAMS = {
    "groups": 80,
    "groupType": "conference",
    "enable": "groups",
    "limit": 200,
}


def fetch_all_fbs_teams() -> dict:
    resp = requests.get(URL, params=PARAMS, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    groups = data["sports"][0]["leagues"][0]["groups"]

    result = {}
    for group in groups:
        conf_name = group.get("midsizeName") or group.get("name")
        teams = []
        for t in group.get("teams", []):
            logos = {logo["rel"][1]: logo["href"] for logo in t.get("logos", []) if len(logo.get("rel", [])) > 1}
            teams.append({
                "id": t["id"],
                "abbr": t["abbreviation"],
                "name": t["displayName"],
                "school": t.get("location", t["displayName"]),
                "color": t.get("color", ""),
                "altColor": t.get("alternateColor", ""),
                "logo": logos.get("default", ""),
                "logoDark": logos.get("dark", ""),
            })
        result[conf_name] = teams

    return result


def main():
    teams_by_conf = fetch_all_fbs_teams()

    total = sum(len(v) for v in teams_by_conf.values())
    print(f"Fetched {total} teams across {len(teams_by_conf)} conferences:")
    for conf, teams in teams_by_conf.items():
        print(f"  {conf}: {len(teams)} teams")

    with open("fbs_teams_full.json", "w") as f:
        json.dump(teams_by_conf, f, indent=2)

    print("\nWrote fbs_teams_full.json")


if __name__ == "__main__":
    main()

import os
import json

DATA_DIR = os.environ.get("DATA_DIR", ".")
ROSTER_PATH = os.path.join(DATA_DIR, "roster.json")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
TEAMS_PATH = "fbs_teams_full.json"  # bundled in repo, not the volume


def load_teams_by_conference() -> dict:
    """Raw structure: conference name -> list of team dicts."""
    with open(TEAMS_PATH, "r") as f:
        return json.load(f)


def load_teams() -> dict:
    """Flat lookup: abbr -> team info, across all conferences."""
    by_conference = load_teams_by_conference()
    flat = {}
    for conf_teams in by_conference.values():
        for team in conf_teams:
            flat[team["abbr"].upper()] = team
    return flat


def load_settings() -> dict:
    if not os.path.exists(SETTINGS_PATH):
        return {}
    with open(SETTINGS_PATH, "r") as f:
        return json.load(f)


def save_settings(settings: dict):
    folder = os.path.dirname(SETTINGS_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(SETTINGS_PATH, "w") as f:
        json.dump(settings, f, indent=2)


def load_roster() -> dict:
    if not os.path.exists(ROSTER_PATH):
        return {}
    with open(ROSTER_PATH, "r") as f:
        return json.load(f)


def save_roster(roster: dict):
    folder = os.path.dirname(ROSTER_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(ROSTER_PATH, "w") as f:
        json.dump(roster, f, indent=2)

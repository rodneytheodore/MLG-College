import os
import json
import re
import discord

DATA_DIR = os.environ.get("DATA_DIR", ".")
ROSTER_PATH = os.path.join(DATA_DIR, "roster.json")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
SEASON_PATH = os.path.join(DATA_DIR, "season.json")
SCHEME_CARDS_PATH = os.path.join(DATA_DIR, "scheme_cards.json")
TEAMS_PATH = "fbs_teams_full.json"  # bundled in repo, not the volume

ADMIN_ROLE_NAME = "Admin"


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


def load_season() -> dict:
    if not os.path.exists(SEASON_PATH):
        return {"year": None, "current_stage": "preseason", "current_week": None, "weeks": {}}
    with open(SEASON_PATH, "r") as f:
        return json.load(f)


def save_season(season: dict):
    folder = os.path.dirname(SEASON_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(SEASON_PATH, "w") as f:
        json.dump(season, f, indent=2)


def archive_dynasty(season: dict, roster: dict, scheme_cards: dict = None):
    """Saves a snapshot of the current season + roster (+ scheme cards, if
    passed) before a reset, tagged with whatever year was stored on the
    season (or 'unknown'). Scheme cards are archived here but -- unlike
    season/roster -- the live scheme_cards.json is deliberately left alone
    afterward; owners keep seeing their old card until they overwrite it
    themselves for the new dynasty."""
    year_label = season.get("year") or "unknown"
    folder = os.path.join(DATA_DIR, "archive")
    os.makedirs(folder, exist_ok=True)

    with open(os.path.join(folder, f"season_{year_label}.json"), "w") as f:
        json.dump(season, f, indent=2)
    with open(os.path.join(folder, f"roster_{year_label}.json"), "w") as f:
        json.dump(roster, f, indent=2)
    if scheme_cards is not None:
        with open(os.path.join(folder, f"scheme_cards_{year_label}.json"), "w") as f:
            json.dump(scheme_cards, f, indent=2)


def load_archived_scheme_cards(year) -> dict:
    """Reads a past dynasty year's scheme_cards snapshot from archive/.
    Returns {} if that year was never archived (e.g. pre-dates this feature)."""
    path = os.path.join(DATA_DIR, "archive", f"scheme_cards_{year}.json")
    if not os.path.exists(path):
        return {}
    with open(path, "r") as f:
        return json.load(f)


def list_archived_scheme_card_years() -> list:
    """Years that actually have an archived scheme_cards snapshot on disk,
    newest first -- used to populate the /view_archived_scheme_card autocomplete
    without offering years that have nothing saved."""
    folder = os.path.join(DATA_DIR, "archive")
    if not os.path.isdir(folder):
        return []
    years = []
    for name in os.listdir(folder):
        m = re.match(r"^scheme_cards_(.+)\.json$", name)
        if m:
            years.append(m.group(1))
    def sort_key(y):
        try:
            return (0, -int(y))
        except ValueError:
            return (1, y)
    return sorted(years, key=sort_key)


def load_scheme_cards() -> dict:
    if not os.path.exists(SCHEME_CARDS_PATH):
        return {}
    with open(SCHEME_CARDS_PATH, "r") as f:
        return json.load(f)


def save_scheme_cards(cards: dict):
    folder = os.path.dirname(SCHEME_CARDS_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(SCHEME_CARDS_PATH, "w") as f:
        json.dump(cards, f, indent=2)


def true_display_name(user: discord.abc.User) -> str:
    """Returns the account-level display name (global_name), falling back to
    username, ignoring any server-specific nickname."""
    return getattr(user, "global_name", None) or user.name


def is_admin(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True
    return any(role.name == ADMIN_ROLE_NAME for role in interaction.user.roles)


def resolve_team(query: str, teams: dict):
    """Resolve user input to a team abbreviation.
    Tries exact abbreviation match first, then exact name match,
    then a unique partial name match. Returns (abbr, error_message)."""
    query = query.strip()
    upper = query.upper()

    if upper in teams:
        return upper, None

    lower = query.lower()
    exact_name_matches = [abbr for abbr, t in teams.items() if t["name"].lower() == lower]
    if len(exact_name_matches) == 1:
        return exact_name_matches[0], None

    partial_matches = [abbr for abbr, t in teams.items() if lower in t["name"].lower()]
    if len(partial_matches) == 1:
        return partial_matches[0], None
    if len(partial_matches) > 1:
        names = ", ".join(teams[a]["name"] for a in partial_matches[:8])
        return None, f"That matches multiple teams: {names}. Try being more specific or use the abbreviation."

    return None, f"Couldn't find a team matching `{query}`. Try the team name or abbreviation, e.g. `UGA`."

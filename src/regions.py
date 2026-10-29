"""Where an activity happened, for filtering routes: its platform — outdoors, or a virtual
one (Zwift, TrainingPeaks Virtual, an indoor video ride such as ROUVY) — and the world or
place within it, e.g. "Zwift · Watopia", "TPV · Island", "Outdoor · Bydgoszcz".

Virtual worlds put their GPS tracks on real coordinates (Zwift's Watopia sits by Vanikoro
in the Solomon Islands, its London on London), so a track alone can't tell virtual from
real: the platform comes from the activity's titles (its own and those of recordings
merged into it) and whether it was recorded indoors; the world from the title, the TPV
catalogue, or — for an untitled indoor session — the Zwift world its coordinates are in.
"""

import math
import re

import tpv

OUTDOOR, ZWIFT, TPV, INDOOR = "Outdoor", "Zwift", "TPV", "Indoor video"
INDOOR_SUB_SPORTS = {"indoor_cycling", "virtual_activity", "treadmill", "indoor_running", "indoor_rowing", "indoor_walking"}
# Zwift worlds' coordinates (where they've shown up in this athlete's activities, or Zwift's
# well-known placements), for indoor sessions whose title doesn't name the world.
ZWIFT_WORLDS = {
    "Watopia": (-11.64, 166.95), "Makuri Islands": (-10.79, 165.82), "Richmond": (37.54, -77.44),
    "London": (51.50, -0.13), "New York": (40.78, -73.97), "Innsbruck": (47.27, 11.40),
    "Yorkshire": (53.99, -1.54), "Paris": (48.87, 2.31),
}
ZWIFT_WORLD_RADIUS_KM = 30.0
_ZWIFT_TITLE = re.compile(r"^Zwift\b", re.I)
_ZWIFT_WORLD_IN_TITLE = re.compile(r"\bin ([A-Z][\w' ]+?)\s*$")


def _zwift_world_at(lat: float, lon: float) -> str | None:
    for world, (wlat, wlon) in ZWIFT_WORLDS.items():
        km = math.hypot(lat - wlat, (lon - wlon) * math.cos(math.radians(lat))) * 111.2
        if km <= ZWIFT_WORLD_RADIUS_KM:
            return world
    return None


def classify(titles: list[str], indoor: bool, start: tuple[float, float] | None, place: str | None,
             catalogue: list[dict]) -> tuple[str, str | None]:
    """(platform, world) of an activity: `titles` its own and its merged recordings'
    Garmin titles, `indoor` whether any recording of it was made indoors / virtually,
    `start` its route's first GPS point, `place` its route's geocoded place (the world of
    outdoor and indoor-video activities; None when not geocoded yet)."""
    for title in titles:
        if title and _ZWIFT_TITLE.match(title):
            world = _ZWIFT_WORLD_IN_TITLE.search(title)
            return ZWIFT, world.group(1).strip() if world else (_zwift_world_at(*start) if start else None) or "?"
        name = tpv.route_name(title)
        if name:
            return TPV, "Island" if tpv.find_route(name, catalogue) else name
    if indoor:
        world = _zwift_world_at(*start) if start else None
        if world:
            return ZWIFT, world
        return INDOOR, place
    return OUTDOOR, place


def label(platform: str, world: str | None) -> str:
    return f"{platform} · {world or 'unknown place'}"



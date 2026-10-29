"""TrainingPeaks Virtual (TPV): its route catalogue and laps of a route.

TPV's community hub lists its routes (tpvirtualhub.com/map — the Island world): each has a
lead-in and a lap (distance, elevation) or is point to point. A ride's route comes from
Garmin's title for the TPV recording, "TrainingPeaks Virtual - <route>". On a known route
a ride splits exactly by virtual distance — lead-in, then a loop every lap distance —
instead of by GPS loop detection; routes not in the catalogue (other worlds) still get the
latter. Pure logic plus one HTTP fetch (fetch_catalogue), no UI.
"""

import json
import re
import urllib.request
from datetime import timedelta

import fit_parser
import geocode
from models import Lap, RecordPoint

HUB_MAP_URL = "https://tpvirtualhub.com/map"
TITLE_PREFIX = "TrainingPeaks Virtual - "
CATALOGUE_MAX_AGE_DAYS = 30
# The final loop counts when ridden to at least this share of its distance (else it's the
# way out of the course, "out").
COMPLETE_LOOP_SHARE = 0.97


def route_name(title: str | None) -> str | None:
    """The TPV route of a Garmin activity title, or None for a title that isn't TPV's."""
    if title and title.startswith(TITLE_PREFIX):
        return title[len(TITLE_PREFIX):].strip() or None
    return None


def _resolve(values: list):
    """Rebuild the object graph of a Nuxt payload (devalue: a flat array where objects and
    arrays hold indices into it; ["Reactive", i]-style wrappers hold one)."""
    memo: dict[int, object] = {}

    def at(index):
        if not isinstance(index, int) or index < 0:
            return None
        if index in memo:
            return memo[index]
        raw = values[index]
        if isinstance(raw, dict):
            out: dict = {}
            memo[index] = out
            out.update({k: at(v) for k, v in raw.items()})
            return out
        if isinstance(raw, list):
            if raw and isinstance(raw[0], str) and raw[0] in ("ShallowReactive", "Reactive", "Ref", "ShallowRef"):
                return at(raw[1])
            out_list: list = []
            memo[index] = out_list
            out_list.extend(at(v) for v in raw)
            return out_list
        return raw

    return at(0)


def parse_catalogue(html: str) -> list[dict]:
    """The hub map page's routes: [{"id", "name", "description", "point_to_point",
    "leadin_m", "leadin_ascent_m", "lap_m", "lap_ascent_m"}]."""
    match = re.search(r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', html, re.S)
    if not match:
        return []
    found: list[dict] = []

    def walk(node, depth=0) -> None:
        if depth > 12:
            return
        if isinstance(node, dict):
            if {"id", "name", "lap", "leadin"} <= node.keys():
                found.append(node)
                return
            for value in node.values():
                walk(value, depth + 1)
        elif isinstance(node, list):
            for value in node:
                walk(value, depth + 1)

    walk(_resolve(json.loads(match.group(1))))
    routes = {}
    for r in found:
        lap, leadin = r.get("lap") or {}, r.get("leadin") or {}
        routes[r["id"]] = {
            "id": r["id"], "name": r["name"], "description": r.get("description") or "",
            "point_to_point": bool(r.get("isPointToPoint")),
            "leadin_m": leadin.get("distance") or 0.0, "leadin_ascent_m": leadin.get("elevation") or 0.0,
            "lap_m": lap.get("distance") or 0.0, "lap_ascent_m": lap.get("elevation") or 0.0,
        }
    return list(routes.values())


def fetch_catalogue(timeout: float = 15.0) -> list[dict]:
    request = urllib.request.Request(HUB_MAP_URL, headers={"User-Agent": geocode.USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return parse_catalogue(response.read().decode("utf-8"))


def find_route(name: str | None, catalogue: list[dict]) -> dict | None:
    if not name:
        return None
    key = name.casefold().strip()
    return next((r for r in catalogue if r["name"].casefold() == key), None)


# How close a recorded lap's distance must be to the route's lap distance to count as a loop
# (TPV laps at the lap line; the first lap may also hold the lead-in).
LOOP_TOLERANCE = 0.03


def _infer_loop_m(laps: list[Lap]) -> float | None:
    """The loop distance of a route not in the catalogue: the distance at least two of the
    recording's laps share (within LOOP_TOLERANCE)."""
    best, best_count = None, 1
    for lap in laps:
        if lap.distance_m <= 0:
            continue
        same = [o.distance_m for o in laps if abs(o.distance_m - lap.distance_m) <= lap.distance_m * LOOP_TOLERANCE]
        if len(same) > best_count:
            best, best_count = sorted(same)[len(same) // 2], len(same)
    return best


def route_loops(laps: list[Lap], route: dict | None) -> tuple[list[Lap], float] | None:
    """A TPV recording's own laps (TPV laps at the route's lap line) labelled as the route's
    loops: "1".."N" for laps of the loop distance (the catalogue's, else the one its laps
    repeat), "in" before the first, "out" after the last, "other" between. (laps, loop
    distance), or None when no loop distance is known."""
    loop_m = (route or {}).get("lap_m") or _infer_loop_m(laps)
    if not loop_m or (route or {}).get("point_to_point"):
        return None
    out = [Lap(**{**lap.__dict__}) for lap in laps]
    is_loop = [abs(lap.distance_m - loop_m) <= loop_m * LOOP_TOLERANCE for lap in out]
    if not any(is_loop):
        return None
    number = 0
    for k, lap in enumerate(out):
        lap.is_interval = False
        if is_loop[k]:
            number += 1
            lap.label = str(number)
        elif not any(is_loop[:k]):
            lap.label = "in"
        elif not any(is_loop[k + 1:]):
            lap.label = "out"
        else:
            lap.label = "other"
    return out, loop_m

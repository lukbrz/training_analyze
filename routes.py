import math

from models import RecordPoint, Route, RouteSignature

RESAMPLE_POINTS = 20
MATCH_DISTANCE_THRESHOLD_M = 75.0
DISTANCE_TOLERANCE = 0.2


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def resample_by_distance(records: list[RecordPoint], n_points: int = RESAMPLE_POINTS) -> list[list[float]]:
    """Resample a record stream's GPS track to `n_points` evenly spaced by cumulative
    distance, so tracks recorded at different paces/sampling rates become comparable."""
    fixed = [r for r in records if r.lat is not None and r.lon is not None and r.distance_m is not None]
    if len(fixed) < 2:
        return []

    start_distance = fixed[0].distance_m
    total = fixed[-1].distance_m - start_distance
    if total <= 0:
        return []

    points = []
    idx = 0
    for step in range(n_points):
        target = start_distance + total * step / (n_points - 1)
        while idx < len(fixed) - 2 and fixed[idx + 1].distance_m < target:
            idx += 1
        a, b = fixed[idx], fixed[idx + 1]
        span = b.distance_m - a.distance_m
        frac = (target - a.distance_m) / span if span > 0 else 0.0
        lat = a.lat + (b.lat - a.lat) * frac
        lon = a.lon + (b.lon - a.lon) * frac
        points.append([lat, lon])
    return points


def build_signature(records: list[RecordPoint]) -> RouteSignature | None:
    """Build a comparable route "shape fingerprint" from a record stream, or None if there
    isn't enough GPS coverage (e.g. an indoor/treadmill activity)."""
    points = resample_by_distance(records)
    if not points:
        return None
    fixed = [r for r in records if r.distance_m is not None]
    distance_m = fixed[-1].distance_m - fixed[0].distance_m if fixed else 0.0
    if distance_m < min(200.0, MATCH_DISTANCE_THRESHOLD_M * 2):
        return None
    return RouteSignature(points=points, distance_m=distance_m)


def _mean_point_distance(points_a: list[list[float]], points_b: list[list[float]]) -> float:
    return sum(
        haversine_m(a[0], a[1], b[0], b[1]) for a, b in zip(points_a, points_b)
    ) / len(points_a)


def similarity(a: RouteSignature, b: RouteSignature) -> float:
    """Mean per-point distance (meters) between two route signatures, trying both the
    same and reversed point order for `b` (so an out-and-back course run in the opposite
    direction still matches) and returning the better orientation. Returns infinity if
    the two signatures' total distances differ by more than DISTANCE_TOLERANCE."""
    longer = max(a.distance_m, b.distance_m)
    if longer and abs(a.distance_m - b.distance_m) / longer > DISTANCE_TOLERANCE:
        return math.inf
    forward = _mean_point_distance(a.points, b.points)
    reversed_ = _mean_point_distance(a.points, list(reversed(b.points)))
    return min(forward, reversed_)


def match_or_create(
    signature: RouteSignature,
    sport: str,
    existing_routes: list[Route],
    next_id_fn,
) -> tuple[Route, bool]:
    """Find the best-matching existing route with the same sport, or synthesize a new one.
    Returns (route, is_new). The winning route's representative signature is never updated
    after creation — the first activity assigned to a route permanently anchors its shape."""
    best_route = None
    best_score = MATCH_DISTANCE_THRESHOLD_M
    for route in existing_routes:
        if route.sport != sport:
            continue
        score = similarity(route.signature, signature)
        if score <= best_score:
            best_route = route
            best_score = score
    if best_route is not None:
        return best_route, False

    new_route = Route(route_id=next_id_fn(), name=f"Route {len(existing_routes) + 1}", sport=sport, signature=signature)
    return new_route, True

"""Straight-line glide over the toy city — pure arithmetic, no clock.

The same movement model the FE's click-to-move uses conceptually: meters
per second converted into degrees at this latitude, stepping toward a
target and stopping exactly on it. Kept dependency-free so both the sim's
tick loop and its tests are plain function calls.
"""

import math

METERS_PER_DEG_LAT = 111_320.0


def meters_between(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Equirectangular meters — plenty inside a 4 km box (haversine's
    error here is centimeters, and the sim is not a surveyor)."""
    lat_m = (b[0] - a[0]) * METERS_PER_DEG_LAT
    lon_m = (b[1] - a[1]) * METERS_PER_DEG_LAT * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot(lat_m, lon_m)


def step_toward(
    current: tuple[float, float], target: tuple[float, float], *, speed_mps: float, dt_s: float
) -> tuple[float, float]:
    """One tick of glide: at most speed*dt meters toward the target,
    landing EXACTLY on it when within reach (no orbiting the goal)."""
    distance = meters_between(current, target)
    step = speed_mps * dt_s
    if distance <= step or distance == 0.0:
        return target
    fraction = step / distance
    return (
        current[0] + (target[0] - current[0]) * fraction,
        current[1] + (target[1] - current[1]) * fraction,
    )

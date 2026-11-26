"""Pure geometry and grouping rules for dispatching new survey points."""

from __future__ import annotations

from math import sqrt
from typing import Any, Iterable

GRID_SPACING_CM = 20


def _value(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(name, default)
    return getattr(row, name, default)


def grid_slots(panel: dict[str, Any], spacing: int = GRID_SPACING_CM) -> list[tuple[int, int]]:
    """Return panel-anchored lattice points inside the half-open panel bounds."""
    spacing = int(spacing)
    if spacing <= 0:
        raise ValueError('grid spacing must be positive')
    x1, y1 = int(panel['x1']), int(panel['y1'])
    x2, y2 = int(panel['x2']), int(panel['y2'])
    return [
        (x, y)
        for x in range(x1, x2, spacing)
        for y in range(y1, y2, spacing)
    ]


def location_key(panel: str | dict[str, Any], survey: str | None = None,
                 x: int | None = None, y: int | None = None) -> str:
    if isinstance(panel, dict):
        p = panel
        panel_code = str(_value(p, 'code', _value(p, 'panel', '')))
        survey = str(_value(p, 'survey', survey or ''))
        x = int(_value(p, 'x_cm', x if x is not None else 0))
        y = int(_value(p, 'y_cm', y if y is not None else 0))
    else:
        panel_code = str(panel)
    return f'{survey}:{panel_code}:{x}:{y}'


def point_group(point: dict[str, Any], groups: dict[str, str] | None = None) -> str | None:
    code = _value(point, 'code')
    if groups and code in groups:
        return groups[code]
    group = _value(point, 'group', None)
    if group is None:
        group = _value(point, 'group_name', None)
    return str(group) if group not in (None, '') else None


def find_cross_group_pair(
    points: Iterable[dict[str, Any]],
    groups: dict[str, str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], float] | None:
    """Find the closest pair whose current group labels differ.

    Ties are resolved by the sorted pair of codes, then coordinate order.  This
    keeps dispatch deterministic when a midpoint is equidistant from slots.
    """
    eligible = []
    for point in points:
        group = point_group(point, groups)
        if group is None:
            continue
        eligible.append(point)
    eligible.sort(key=lambda p: (str(_value(p, 'code', '')), int(_value(p, 'x_cm', 0)),
                                 int(_value(p, 'y_cm', 0))))

    best: tuple[tuple[float, str, str, int, int], dict[str, Any], dict[str, Any]] | None = None
    for index, a in enumerate(eligible):
        ag = point_group(a, groups)
        ax, ay = int(_value(a, 'x_cm')), int(_value(a, 'y_cm'))
        for b in eligible[index + 1:]:
            bg = point_group(b, groups)
            if not bg or ag == bg:
                continue
            bx, by = int(_value(b, 'x_cm')), int(_value(b, 'y_cm'))
            dx, dy = ax - bx, ay - by
            distance2 = dx * dx + dy * dy
            ca, cb = sorted([str(_value(a, 'code', '')), str(_value(b, 'code', ''))])
            tie = (distance2, ca, cb, min(ax, bx), min(ay, by))
            if best is None or tie < best[0]:
                best = (tie, a, b)
    if best is None:
        return None
    _, a, b = best
    ax, ay = int(_value(a, 'x_cm')), int(_value(a, 'y_cm'))
    bx, by = int(_value(b, 'x_cm')), int(_value(b, 'y_cm'))
    return a, b, sqrt((ax - bx) ** 2 + (ay - by) ** 2)


def nearest_empty_slot(
    panel: dict[str, Any],
    midpoint_x: float,
    midpoint_y: float,
    occupied: Iterable[tuple[int, int]] = (),
    spacing: int = GRID_SPACING_CM,
    blocked: Iterable[tuple[int, int]] = (),
) -> tuple[int, int] | None:
    occupied_set = {(int(x), int(y)) for x, y in occupied}
    blocked_set = {(int(x), int(y)) for x, y in blocked}
    available = [
        slot for slot in grid_slots(panel, spacing)
        if slot not in occupied_set and slot not in blocked_set
    ]
    if not available:
        return None

    # Compare four times squared distance using integer midpoint numerators.
    mid_x2 = midpoint_x * 2
    mid_y2 = midpoint_y * 2

    def key(slot: tuple[int, int]):
        x, y = slot
        dx2 = 2 * x - mid_x2
        dy2 = 2 * y - mid_y2
        return (dx2 * dx2 + dy2 * dy2, x, y)

    return min(available, key=key)


def plan_claim(
    panel: dict[str, Any],
    grouped_points: Iterable[dict[str, Any]],
    groups: dict[str, str] | None = None,
    occupied: Iterable[tuple[int, int]] = (),
    spacing: int = GRID_SPACING_CM,
    blocked: Iterable[tuple[int, int]] = (),
) -> dict[str, Any]:
    points = list(grouped_points)
    present_groups = {point_group(p, groups) for p in points}
    present_groups.discard(None)
    if len(present_groups) < 2:
        raise ValueError('need_at_least_two_groups')

    pair = find_cross_group_pair(points, groups)
    if pair is None:
        raise ValueError('need_cross_group_pair')
    a, b, distance = pair
    ax, ay = int(_value(a, 'x_cm')), int(_value(a, 'y_cm'))
    bx, by = int(_value(b, 'x_cm')), int(_value(b, 'y_cm'))
    mx, my = (ax + bx) / 2, (ay + by) / 2
    slot = nearest_empty_slot(panel, mx, my, occupied, spacing, blocked)
    if slot is None:
        raise ValueError('no_empty_grid_slot')
    x, y = slot
    return {
        'x_cm': x,
        'y_cm': y,
        'spacing_cm': spacing,
        'midpoint_x': mx,
        'midpoint_y': my,
        'point_a': _value(a, 'code'),
        'point_b': _value(b, 'code'),
        'group_a': point_group(a, groups),
        'group_b': point_group(b, groups),
        'pair_distance_cm': distance,
    }


# Descriptive aliases for callers/tests using either domain vocabulary.
grid_positions = grid_slots
build_grid = grid_slots
grid_points = grid_slots
empty_grid_position_nearest_midpoint = nearest_empty_slot
nearest_free_slot = nearest_empty_slot
nearest_empty_grid_point = nearest_empty_slot
choose_cross_group_pair = find_cross_group_pair
closest_cross_group_pair = find_cross_group_pair
find_nearest_cross_group_pair = find_cross_group_pair
choose_next_point = plan_claim
dispatch_point = plan_claim
claim_next_grid_point = plan_claim

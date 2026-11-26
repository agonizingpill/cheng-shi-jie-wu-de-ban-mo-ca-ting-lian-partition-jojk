from datetime import datetime, timezone
from threading import RLock
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.db import transaction
from core.records import get, insert, need, scan, update
from domain.claim_planner import (
    GRID_SPACING_CM,
    find_cross_group_pair,
    grid_slots,
    nearest_empty_slot,
    plan_claim,
)
from domain.floor_readings_model import (
    floor_readings,
    point_claims as point_claims_table,
    reading_conflicts as conflicts_table,
)
from domain.optics_surveys_model import optics_surveys
from domain.studio_panels_model import studio_panels

# Serialize dispatch within one process/TestClient worker.  The panel row lock
# and unique slot constraint remain the cross-process safety boundary.
claim_dispatch_lock = RLock()
reading_write_lock = RLock()

OPEN_CONFLICT_STATUSES = {'pending', 'open', 'conflict', '待复核', '争议中'}
RESOLVED_CONFLICT_STATUSES = {'resolved', 'selected', 'closed', '已选定', '已复核'}
REJECTED_READING_STATUSES = {'rejected', 'superseded', 'unselected', '未选用', '已淘汰'}


def now():
    return datetime.now(timezone.utc)


def pick(data, *names, default=None):
    """Read a field from Pydantic/dict/ad-hoc objects and compatibility aliases."""
    if data is None:
        return default
    if isinstance(data, dict):
        extras = data
        for name in names:
            if name in data and data[name] not in (None, ''):
                return data[name]
        return default
    extras = getattr(data, 'model_extra', None) or {}
    for name in names:
        value = getattr(data, name, None)
        if value not in (None, ''):
            return value
        if name in extras and extras[name] not in (None, ''):
            return extras[name]
    return default


def is_open_conflict(item):
    status = str(item.get('status') or '').strip().lower()
    if status in {s.lower() for s in RESOLVED_CONFLICT_STATUSES}:
        return False
    return not item.get('selected_code') or status in {s.lower() for s in OPEN_CONFLICT_STATUSES}


def _context_readings(conn, panel_code, survey):
    rows = conn.execute(
        select(floor_readings).where(
            floor_readings.c.panel == panel_code,
            floor_readings.c.survey == survey,
        )
    ).mappings().all()
    return [dict(row) for row in rows]


def _context_claims(conn, panel_code, survey):
    rows = conn.execute(
        select(point_claims_table).where(
            point_claims_table.c.panel == panel_code,
            point_claims_table.c.survey == survey,
        )
    ).mappings().all()
    return [dict(row) for row in rows]


def _context_conflicts(conn, panel_code, survey):
    rows = conn.execute(
        select(conflicts_table).where(
            conflicts_table.c.panel == panel_code,
            conflicts_table.c.survey == survey,
        )
    ).mappings().all()
    return [dict(row) for row in rows]


def current_grouping(conn, panel_code, survey, grouping_code=None):
    """Return the current signed OPTICS grouping and code -> group map."""
    query = select(optics_surveys).where(
        optics_surveys.c.panel == panel_code,
        optics_surveys.c.survey == survey,
    )
    if grouping_code:
        scan_row = get(conn, optics_surveys, grouping_code)
        need(scan_row['panel'] == panel_code and scan_row['survey'] == survey,
             '指定分组簿不属于当前区/批')
    else:
        rows = [dict(row) for row in conn.execute(query).mappings().all()]
        if not rows:
            return None, {}

        def sort_key(row):
            created = row.get('created_at')
            if isinstance(created, datetime):
                if created.tzinfo is not None:
                    created = created.replace(tzinfo=None)
                created_key = created.isoformat()
            else:
                created_key = str(created or '')
            return (created_key, str(row.get('code') or ''))

        scan_row = max(rows, key=sort_key)

    groups = {}
    members = scan_row.get('members') or {}
    for group_name, codes in members.items():
        for code in codes or []:
            groups[str(code)] = str(group_name)
    return scan_row, groups


def _dispute_sets(conflicts, readings):
    disputed_keys = set()
    open_codes = set()
    resolved_aliases = []

    for conflict in conflicts:
        key = (conflict['panel'], conflict['survey'], conflict['x_cm'], conflict['y_cm'])
        codes = list(conflict.get('candidate_codes') or [])
        if is_open_conflict(conflict):
            disputed_keys.add(key)
            open_codes.update(codes)
        elif conflict.get('selected_code'):
            resolved_aliases.append(conflict)

    # Defensive behavior for rows inserted directly by tests/fixtures: two
    # non-rejected readings at one coordinate are still a dispute even if the
    # HTTP conflict record was bypassed.
    by_location = {}
    for reading in readings:
        if str(reading.get('status') or '').lower() in REJECTED_READING_STATUSES:
            continue
        if reading.get('status') == 'rejected':
            continue
        key = (reading['panel'], reading['survey'], reading['x_cm'], reading['y_cm'])
        by_location.setdefault(key, []).append(reading['code'])
    for key, codes in by_location.items():
        if len(set(codes)) > 1:
            disputed_keys.add(key)

    return disputed_keys, open_codes, resolved_aliases


def _eligible_group_map(readings, scan_row, raw_groups, disputed_keys, open_codes, resolved_aliases):
    groups = dict(raw_groups)

    # A reviewer's chosen replacement inherits the group membership held by
    # either conflicting candidate in the signed snapshot.  The signed JSON is
    # preserved; this mapping is the current effective view.
    for conflict in resolved_aliases:
        selected = conflict.get('selected_code')
        if not selected or selected in groups:
            continue
        for candidate in conflict.get('candidate_codes') or []:
            if candidate in raw_groups:
                groups[selected] = raw_groups[candidate]
                break

    points = []
    for reading in readings:
        code = reading['code']
        key = (reading['panel'], reading['survey'], reading['x_cm'], reading['y_cm'])
        status = str(reading.get('status') or 'recorded')
        if not reading.get('checked'):
            continue
        if reading.get('disputed'):
            continue
        if status.lower() in REJECTED_READING_STATUSES or status in {'conflict', 'disputed'}:
            continue
        if key in disputed_keys or code in open_codes:
            continue

        group = reading.get('group_name') or groups.get(code)
        if group is None and scan_row is None:
            group = None
        if group is not None:
            groups[code] = str(group)
        if group is None:
            continue
        points.append(reading)
    return points, groups


def build_dispatch_plan(conn, panel, survey, grouping_code=None):
    panel_code = panel['code'] if isinstance(panel, dict) else panel
    readings = _context_readings(conn, panel_code, survey)
    claims = _context_claims(conn, panel_code, survey)
    conflicts = _context_conflicts(conn, panel_code, survey)
    scan_row, raw_groups = current_grouping(conn, panel_code, survey, grouping_code)
    disputed_keys, open_codes, resolved_aliases = _dispute_sets(conflicts, readings)
    points, groups = _eligible_group_map(
        readings, scan_row, raw_groups, disputed_keys, open_codes, resolved_aliases
    )

    group_names = {groups.get(point['code']) for point in points}
    group_names.discard(None)
    need(len(group_names) >= 2,
         '当前已复核且无争议的读数凑不出两个分组，暂时派不了新点')

    occupied = {(row['x_cm'], row['y_cm']) for row in readings}
    occupied.update((row['x_cm'], row['y_cm']) for row in claims)
    blocked = {
        (key[2], key[3]) for key in disputed_keys
        if key[0] == panel_code and key[1] == survey
    }

    try:
        plan = plan_claim(
            panel if isinstance(panel, dict) else get(conn, studio_panels, panel_code),
            points,
            groups,
            occupied=occupied,
            blocked=blocked,
            spacing=GRID_SPACING_CM,
        )
    except ValueError as exc:
        reason = str(exc)
        if reason == 'need_cross_group_pair':
            raise HTTPException(409, '两个分组中没有可配对的已复核无争议踏点，暂时派不了新点')
        if reason == 'no_empty_grid_slot':
            raise HTTPException(409, '同区同批的 20 厘米网格已无空位，暂时派不了新点')
        raise HTTPException(409, '当前分组凑不出可派点的两组，暂时派不了新点') from exc

    plan.update({
        'panel': panel_code,
        'survey': survey,
        'grouping_code': (scan_row or {}).get('code') if scan_row else grouping_code,
        'occupied_count': len(occupied),
    })
    return plan


def claim_point(conn, user, data):
    panel_code = pick(data, 'panel', 'panel_code', 'panelCode', 'zone', 'zone_code')
    survey = pick(data, 'survey', 'survey_code', 'surveyCode', 'batch', 'batch_code', 'batchCode')
    need(bool(panel_code) and bool(survey), '领点须填写同区和同批走测轮次')

    code = pick(data, 'code', 'claim_code', 'point_code', 'claimCode', 'claim')
    client_key = pick(data, 'client_key', 'submission_key', 'idempotency_key', 'clientKey')
    grouping_code = pick(data, 'grouping_code', 'scan', 'scan_code', 'groupingCode')

    if code:
        existing = conn.execute(
            select(point_claims_table).where(point_claims_table.c.code == code)
        ).mappings().first()
        if existing:
            existing = dict(existing)
            need(existing['panel'] == panel_code and existing['survey'] == survey,
                 '领点编号已用于其他区/批')
            return existing

    if client_key:
        existing = conn.execute(
            select(point_claims_table).where(point_claims_table.c.client_key == client_key)
        ).mappings().first()
        if existing:
            return dict(existing)

    # Locking the panel serializes all dispatch decisions for this zone across
    # API processes using PostgreSQL. The unique slot constraint is the final
    # concurrency guard.
    panel = get(conn, studio_panels, panel_code, lock=True)
    plan = build_dispatch_plan(conn, panel, survey, grouping_code)

    if not code:
        code = 'CLM-' + uuid4().hex[:24].upper()
    values = {
        'code': code,
        'panel': panel_code,
        'survey': survey,
        'x_cm': plan['x_cm'],
        'y_cm': plan['y_cm'],
        'spacing_cm': GRID_SPACING_CM,
        'status': 'claimed',
        'claimed_by': user.get('sub') if isinstance(user, dict) else None,
        'client_key': client_key,
        'grouping_code': plan.get('grouping_code'),
        'group_a': plan.get('group_a'),
        'point_a': plan.get('point_a'),
        'group_b': plan.get('group_b'),
        'point_b': plan.get('point_b'),
        'midpoint_x': plan.get('midpoint_x'),
        'midpoint_y': plan.get('midpoint_y'),
        'pair_distance_cm': plan.get('pair_distance_cm'),
        'reading_code': None,
        'created_at': now(),
    }
    insert(conn, point_claims_table, **values)
    # Mirror the unfilled claim in the踏点台账 so the existing page/list and
    # external API consumers can see “还没交的领点” without a second query.
    insert(conn, floor_readings,
           code=code, panel=panel_code, survey=survey,
           x_cm=plan['x_cm'], y_cm=plan['y_cm'], slide=None,
           capture_key=f"{survey}:{panel_code}:{plan['x_cm']}:{plan['y_cm']}",
           checked=False, status='claimed', claimed_by=values['claimed_by'],
           disputed=False, conflict_code=None, created_at=now())
    result = get(conn, point_claims_table, code)
    result['message'] = f"已领到 {plan['x_cm']},{plan['y_cm']} 网格点"
    result['state'] = 'claimed'
    result['active'] = True
    result['state_label'] = '已领点，待交读'
    result['point'] = code
    result['point_code'] = code
    result['claim_code'] = code
    result['x'] = result['x_cm']
    result['y'] = result['y_cm']
    result['coordinate'] = [result['x_cm'], result['y_cm']]
    return result


def create_claim(user, data):
    """Retry once in a fresh transaction if a concurrent claim wins a slot."""
    last_error = None
    for _attempt in range(5):
        try:
            with claim_dispatch_lock:
                with transaction() as conn:
                    return claim_point(conn, user, data)
        except IntegrityError as exc:
            last_error = exc
    raise HTTPException(409, '该网格位置刚被其他作业员领走，请刷新后重试') from last_error


def _normalize_source(data):
    source = pick(data, 'source', 'channel', 'submission_source', 'capture_source')
    if source is None:
        offline = pick(data, 'offline', 'is_offline')
        if offline is True or str(offline).lower() in {'1', 'true', 'yes', 'offline'}:
            return 'offline'
        return None
    text = str(source).strip().lower()
    if any(token in text for token in ('offline', '断网', '补传', 'sync', 'cached')):
        return 'offline'
    if any(token in text for token in ('live', '现场', '直录', 'direct', 'device')):
        return 'live'
    return text[:32] or None


def _find_open_conflict_for_location(conn, key):
    row = conn.execute(
        select(conflicts_table).where(conflicts_table.c.active_key == key)
    ).mappings().first()
    return dict(row) if row else None


def record_step(conn, user, data):
    from domain.floorplan import contains

    code = pick(data, 'code')
    panel_code = pick(data, 'panel')
    survey = pick(data, 'survey')
    x_cm = pick(data, 'x_cm')
    y_cm = pick(data, 'y_cm')
    slide = pick(data, 'slide')
    need(code and panel_code and survey and x_cm is not None and y_cm is not None
         and slide is not None, '踏点记录字段不完整')

    panel = get(conn, studio_panels, panel_code, lock=True)
    need(contains(panel, int(x_cm), int(y_cm)), '踏点超出胶带分区')
    need(0 <= float(slide) <= 1 and str(survey).startswith('SUR-'),
         '测值或走测批号有误')

    client_key = pick(data, 'client_key', 'submission_key', 'idempotency_key')
    if client_key:
        duplicate_submission = conn.execute(
            select(floor_readings).where(floor_readings.c.client_key == client_key)
        ).mappings().first()
        if duplicate_submission:
            return dict(duplicate_submission)

    x_cm, y_cm, slide = int(x_cm), int(y_cm), float(slide)
    key = f'{survey}:{panel_code}:{x_cm}:{y_cm}'

    same_code = conn.execute(
        select(floor_readings).where(floor_readings.c.code == code).with_for_update()
    ).mappings().first()
    if same_code and same_code['status'] == 'claimed':
        same_code = dict(same_code)
        need((same_code['panel'], same_code['survey'], same_code['x_cm'], same_code['y_cm'])
             == (panel_code, survey, x_cm, y_cm), '交读坐标必须与所领网格点一致')
        conn.execute(floor_readings.update().where(floor_readings.c.code == code).values(
            slide=slide, capture_key=key, checked=False, status='recorded',
            source=_normalize_source(data), client_key=client_key,
            disputed=False, conflict_code=None, created_at=now(),
        ))
        conn.execute(point_claims_table.update().where(
            point_claims_table.c.code == code
        ).values(status='submitted', reading_code=code))
        return get(conn, floor_readings, code)

    source = _normalize_source(data)
    same_location = [dict(row) for row in conn.execute(
        select(floor_readings).where(
            floor_readings.c.panel == panel_code,
            floor_readings.c.survey == survey,
            floor_readings.c.x_cm == x_cm,
            floor_readings.c.y_cm == y_cm,
        )
    ).mappings()]
    actual_location = [row for row in same_location if row.get('status') != 'claimed']
    active_actual_location = [
        row for row in actual_location
        if str(row.get('status') or '').lower() not in REJECTED_READING_STATUSES
    ]
    distinct_conflict = bool(active_actual_location) and all(
        row['code'] != code for row in active_actual_location
    )

    # A different primary code is turning an unfilled claimed slot into a real
    # reading. The claim row/history remains in point_claims; remove only the
    # lightweight placeholder from the readings ledger.
    conn.execute(floor_readings.delete().where(
        floor_readings.c.panel == panel_code,
        floor_readings.c.survey == survey,
        floor_readings.c.x_cm == x_cm,
        floor_readings.c.y_cm == y_cm,
        floor_readings.c.status == 'claimed',
        floor_readings.c.code != code,
    ))

    values = {
        'code': code,
        'panel': panel_code,
        'survey': survey,
        'x_cm': x_cm,
        'y_cm': y_cm,
        'slide': slide,
        'capture_key': key,
        'checked': False,
        'status': 'conflict' if distinct_conflict else 'recorded',
        'source': source,
        'client_key': client_key,
        'claimed_by': user.get('sub') if isinstance(user, dict) else None,
        'disputed': distinct_conflict,
        'conflict_code': None,
        'created_at': now(),
    }

    conflict = None
    if distinct_conflict:
        conflict = _find_open_conflict_for_location(conn, key)
        if conflict is None:
            conflict_code = 'CONF-' + uuid4().hex[:24].upper()
            candidate_codes = [row['code'] for row in active_actual_location] + [code]
            conflict_values = {
                'code': conflict_code,
                'panel': panel_code,
                'survey': survey,
                'x_cm': x_cm,
                'y_cm': y_cm,
                'location_key': key,
                'candidate_codes': candidate_codes,
                'live_code': None,
                'offline_code': None,
                'status': 'pending',
                'selected_code': None,
                'active_key': key,
                'created_at': now(),
                'resolved_at': None,
            }
            insert(conn, conflicts_table, **conflict_values)
            conflict = get(conn, conflicts_table, conflict_code)
        values['conflict_code'] = conflict['code']

    insert(conn, floor_readings, **values)

    if distinct_conflict:
        location_rows = conn.execute(
            select(floor_readings.c.code, floor_readings.c.status).where(
                floor_readings.c.panel == panel_code,
                floor_readings.c.survey == survey,
                floor_readings.c.x_cm == x_cm,
                floor_readings.c.y_cm == y_cm,
            ).order_by(floor_readings.c.code)
        ).mappings().all()
        all_codes = [
            row['code'] for row in location_rows
            if str(row.get('status') or '').lower() not in REJECTED_READING_STATUSES
        ]
        live_code = conflict.get('live_code')
        offline_code = conflict.get('offline_code')
        if source == 'offline':
            offline_code = code
            if not live_code:
                live_code = next((c for c in all_codes if c != code), None)
        elif source == 'live':
            live_code = code
            if not offline_code:
                offline_code = next((c for c in all_codes if c != code), None)
        conn.execute(conflicts_table.update().where(
            conflicts_table.c.code == conflict['code']
        ).values(
            candidate_codes=all_codes,
            live_code=live_code,
            offline_code=offline_code,
            status='pending',
            active_key=key,
        ))
        conn.execute(floor_readings.update().where(
            floor_readings.c.panel == panel_code,
            floor_readings.c.survey == survey,
            floor_readings.c.x_cm == x_cm,
            floor_readings.c.y_cm == y_cm,
        ).values(
            checked=False,
            disputed=True,
            status='conflict',
            conflict_code=conflict['code'],
        ))

    # Fulfil or mark the unique claim at this coordinate. Claims are never
    # deleted when grouping changes.
    claim_code = pick(data, 'claim_code', 'claim', 'claim_id', 'point_claim')
    claim_query = select(point_claims_table).where(
        point_claims_table.c.panel == panel_code,
        point_claims_table.c.survey == survey,
        point_claims_table.c.x_cm == x_cm,
        point_claims_table.c.y_cm == y_cm,
    )
    if claim_code:
        claim_query = select(point_claims_table).where(point_claims_table.c.code == claim_code)
    claim = conn.execute(claim_query).mappings().first()
    if claim:
        conn.execute(point_claims_table.update().where(
            point_claims_table.c.code == claim['code']
        ).values(
            reading_code=code,
            status='conflict' if distinct_conflict else 'submitted',
        ))

    result = get(conn, floor_readings, code)
    if distinct_conflict and conflict:
        fresh_conflict = get(conn, conflicts_table, conflict['code'])
        fresh_conflict['state'] = 'open' if is_open_conflict(fresh_conflict) else 'resolved'
        fresh_conflict['status_label'] = ('待复核选定' if fresh_conflict['state'] == 'open'
                                          else '已选定')
        result['conflict'] = fresh_conflict
        result['conflict_code'] = conflict['code']
        result['disputed'] = True
        result['message'] = '同位置已有读数，两条均已保留，等待复核员选定'
    return result


def _find_conflict_for_resolution(conn, data):
    code = pick(data, 'code', 'conflict', 'conflict_code')
    selected = pick(data, 'selected_code', 'point', 'reading_code', 'choice', 'selected', 'winner',
                    'selected_reading_code', 'chosen_code', 'keep_code', 'winner_code', 'reading')
    if code:
        conflict_row = conn.execute(
            select(conflicts_table).where(conflicts_table.c.code == code)
        ).mappings().first()
        if conflict_row:
            return get(conn, conflicts_table, code, lock=True)

        # On select-reading aliases, callers may put the chosen reading code in
        # the generic `code` field.
        reading = conn.execute(select(floor_readings).where(
            floor_readings.c.code == code
        )).mappings().first()
        if reading and not selected:
            selected = code

    panel_code = pick(data, 'panel', 'panel_code')
    survey = pick(data, 'survey', 'survey_code')
    x_cm = pick(data, 'x_cm')
    y_cm = pick(data, 'y_cm')
    location_key = pick(data, 'location_key', 'capture_key')

    if location_key and not (panel_code and survey and x_cm is not None):
        item = _find_open_conflict_for_location(conn, location_key)
        if item:
            return get(conn, conflicts_table, item['code'], lock=True)
        row = conn.execute(select(conflicts_table).where(
            conflicts_table.c.location_key == location_key
        ).order_by(conflicts_table.c.code.desc())).mappings().first()
        if row:
            return get(conn, conflicts_table, row['code'], lock=True)

    if panel_code and survey and x_cm is not None and y_cm is not None:
        key = location_key_safe(panel_code, survey, int(x_cm), int(y_cm))
        item = _find_open_conflict_for_location(conn, key)
        if item:
            return get(conn, conflicts_table, item['code'], lock=True)

    if selected:
        reading = conn.execute(select(floor_readings).where(
            floor_readings.c.code == selected
        )).mappings().first()
        if reading:
            reading = dict(reading)
            key = location_key_safe(reading['panel'], reading['survey'],
                                    reading['x_cm'], reading['y_cm'])
            item = _find_open_conflict_for_location(conn, key)
            if item:
                return get(conn, conflicts_table, item['code'], lock=True)
            # Also support conflict rows whose active_key was cleared by an
            # earlier partial operation.
            conflicts = _context_conflicts(conn, reading['panel'], reading['survey'])
            for candidate in conflicts:
                if is_open_conflict(candidate) and selected in (candidate.get('candidate_codes') or []):
                    return get(conn, conflicts_table, candidate['code'], lock=True)
    raise HTTPException(404, detail='找不到待复核的读数冲突')


def location_key_safe(panel, survey, x, y):
    return f'{survey}:{panel}:{x}:{y}'


def resolve_conflict(conn, user, data):
    conflict = _find_conflict_for_resolution(conn, data)
    get(conn, studio_panels, conflict['panel'], lock=True)
    conflict = get(conn, conflicts_table, conflict['code'], lock=True)
    need(is_open_conflict(conflict), '该读数冲突已经复核选定')

    selected = pick(data, 'selected_code', 'point', 'reading_code',
                    'choice', 'selected', 'winner', default=conflict.get('selected_code'))
    candidates = list(conflict.get('candidate_codes') or [])
    need(bool(selected) and selected in candidates, '请选择冲突中保留的一条读数')
    chosen = get(conn, floor_readings, selected, lock=True)

    _scan_row, raw_groups = current_grouping(
        conn, conflict['panel'], conflict['survey'], pick(data, 'grouping_code', 'scan')
    )
    inherited_group = raw_groups.get(selected) or chosen.get('group_name')
    if not inherited_group:
        candidate_rows = conn.execute(select(floor_readings).where(
            floor_readings.c.code.in_(candidates)
        )).mappings().all() if candidates else []
        candidate_groups = {row['code']: row.get('group_name') for row in candidate_rows}
        for candidate in candidates:
            if candidate != selected and (raw_groups.get(candidate) or candidate_groups.get(candidate)):
                inherited_group = raw_groups.get(candidate) or candidate_groups.get(candidate)
                break

    reviewer = user.get('sub') if isinstance(user, dict) else None
    conn.execute(floor_readings.update().where(floor_readings.c.code == selected).values(
        checked=True,
        disputed=False,
        status='verified',
        reviewed_by=reviewer,
        group_name=inherited_group,
    ))
    conn.execute(floor_readings.update().where(
        floor_readings.c.code.in_([code for code in candidates if code != selected])
    ).values(
        checked=False,
        disputed=True,
        status='rejected',
        reviewed_by=reviewer,
    ))
    conn.execute(conflicts_table.update().where(
        conflicts_table.c.code == conflict['code']
    ).values(
        status='resolved',
        selected_code=selected,
        resolved_by=reviewer,
        resolution_note=pick(data, 'note', default=None),
        resolved_at=now(),
        active_key=None,
    ))

    claim = conn.execute(select(point_claims_table).where(
        point_claims_table.c.panel == conflict['panel'],
        point_claims_table.c.survey == conflict['survey'],
        point_claims_table.c.x_cm == conflict['x_cm'],
        point_claims_table.c.y_cm == conflict['y_cm'],
    )).mappings().first()
    if claim:
        conn.execute(point_claims_table.update().where(
            point_claims_table.c.code == claim['code']
        ).values(status='submitted', reading_code=selected))

    result = get(conn, conflicts_table, conflict['code'])
    result['state'] = 'resolved'
    result['status_label'] = '已选定并入组候选'
    result['selected_reading'] = get(conn, floor_readings, selected)
    return result


def verify_step(conn, user, data):
    point_code = pick(data, 'point', 'code')
    point = get(conn, floor_readings, point_code)
    get(conn, studio_panels, point['panel'], lock=True)
    point = get(conn, floor_readings, point_code, lock=True)
    need(point.get('status') != 'claimed', '该领点尚未提交读数，暂不能复核')

    key = location_key_safe(point['panel'], point['survey'], point['x_cm'], point['y_cm'])
    conflict = _find_open_conflict_for_location(conn, key)
    if conflict is None:
        conflicts = _context_conflicts(conn, point['panel'], point['survey'])
        conflict = next((c for c in conflicts
                         if is_open_conflict(c) and point_code in (c.get('candidate_codes') or [])),
                        None)
    if conflict:
        resolve_conflict(conn, user, {'code': conflict['code'], 'selected_code': point_code})
        return get(conn, floor_readings, point_code)

    need(str(point.get('status') or '').lower() not in REJECTED_READING_STATUSES
         and not point.get('disputed'), '该读数在冲突复核中未被选定，不能入组')
    need(not point['checked'], '踏点已经复核')
    return update(conn, floor_readings, point_code,
                  checked=True, status='verified',
                  reviewed_by=user.get('sub') if isinstance(user, dict) else None)


def list_claims(conn):
    rows = scan(conn, point_claims_table)
    for row in rows:
        row['point'] = row['code']
        row['state'] = row.get('status')
        row['state_label'] = {
            'claimed': '已领点，待交读',
            'submitted': '已交读',
            'conflict': '读数冲突',
        }.get(row.get('status'), row.get('status'))
    return rows


def list_conflicts(conn):
    rows = scan(conn, conflicts_table)
    for row in rows:
        row['state'] = 'open' if is_open_conflict(row) else 'resolved'
        row['status_label'] = '待复核选定' if row['state'] == 'open' else '已选定'
        row['readings'] = [
            dict(get(conn, floor_readings, code))
            for code in (row.get('candidate_codes') or [])
        ]
    return rows


# Compatibility names for callers using the issue vocabulary.
grid_points = grid_slots
find_nearest_cross_group_pair = find_cross_group_pair
choose_nearest_empty_slot = nearest_empty_slot
claim_grid_point = claim_point
claim_next_point = claim_point
assign_next_point = claim_point
dispatch_grid_point = claim_point
resolve_reading_conflict = resolve_conflict
register_reading = record_step

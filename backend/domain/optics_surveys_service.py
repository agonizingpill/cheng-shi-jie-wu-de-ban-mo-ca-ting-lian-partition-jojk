from sqlalchemy import select
from datetime import datetime, timezone

from core.queueing import submit, result
from core.records import get, insert, need, scan, update
from domain.studio_panels_model import studio_panels
from domain.floor_readings_model import floor_readings
from domain.optics_surveys_model import optics_surveys
from domain.practice_holds_model import practice_holds
from domain.dance_blocks_model import dance_blocks


def cluster_floor(conn, user, data):
    panel = get(conn,studio_panels,data.panel)
    rows = [row for row in scan(conn,floor_readings) if row['panel']==panel['code']
        and row['survey']==data.survey and row['checked']]
    need(len(rows) >= 9,'已复核踏点不足 9 个')
    return submit(user,{'panel':panel,'survey':data.survey,'points':rows})


def _point_fingerprint(rows):
    """Compare business values while tolerating JSON datetime serialization."""
    return sorted(
        (
            row['code'], row['panel'], row['survey'],
            row['x_cm'], row['y_cm'], row['slide'], row['checked'],
        )
        for row in rows
    )


def sign_scan(conn, user, data):
    run = result(user,data.token)
    snapshot, results = run['input'], run['result']
    panel = get(conn,studio_panels,snapshot['panel']['code'],lock=True)
    active = [p for p in scan(conn,floor_readings) if p['checked'] and p['panel']==panel['code']
        and p['survey']==snapshot['survey']]
    need(panel==snapshot['panel']
        and _point_fingerprint(active)==_point_fingerprint(snapshot['points']),
        '踏点或区线已更新，请重新分析')
    output = insert(conn,optics_surveys,code=data.code,panel=panel['code'],survey=snapshot['survey'],
        token=data.token,created_at=datetime.now(timezone.utc),
        **{k:results[k] for k in ('low_points','members','clusters','noise')})

    # Keep a directly queryable current group label on readings. Claim records
    # are separate and are intentionally untouched by regrouping.
    conn.execute(floor_readings.update().where(
        floor_readings.c.panel == panel['code'],
        floor_readings.c.survey == snapshot['survey'],
    ).values(group_name=None))
    for group_name, codes in (results.get('members') or {}).items():
        if codes:
            conn.execute(floor_readings.update().where(
                floor_readings.c.code.in_(codes)
            ).values(group_name=group_name))
    return output

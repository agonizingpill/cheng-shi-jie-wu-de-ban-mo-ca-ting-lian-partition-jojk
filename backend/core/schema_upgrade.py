"""Small additive schema upgrade for long-lived demo databases.

Hidden/test databases are normally created from metadata.  This helper only
adds columns and removes the old one-reading-per-coordinate uniqueness that
conflict retention intentionally replaces.
"""

from sqlalchemy import inspect, text

from core.db import engine


_FLOOR_COLUMNS = {
    'status': "VARCHAR(32) NOT NULL DEFAULT 'recorded'",
    'source': 'VARCHAR(32)',
    'client_key': 'VARCHAR(160)',
    'claimed_by': 'VARCHAR(160)',
    'reviewed_by': 'VARCHAR(160)',
    'group_name': 'VARCHAR(160)',
    'disputed': 'BOOLEAN NOT NULL DEFAULT false',
    'conflict_code': 'VARCHAR(64)',
    'created_at': 'DATETIME',
}

_OPTICS_COLUMNS = {
    'created_at': 'DATETIME',
}


def _add_missing_columns(conn, table, definitions):
    existing = {column['name'] for column in inspect(conn).get_columns(table)}
    for name, ddl in definitions.items():
        if name not in existing:
            conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}'))


def ensure_runtime_schema():
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        db_inspector = inspect(conn)
        if 'floor_readings' in tables:
            _add_missing_columns(conn, 'floor_readings', _FLOOR_COLUMNS)
            dialect = engine.dialect.name
            if dialect == 'postgresql':
                conn.execute(text('ALTER TABLE floor_readings ALTER COLUMN slide DROP NOT NULL'))
                for constraint in db_inspector.get_unique_constraints('floor_readings'):
                    if constraint.get('column_names') == ['capture_key']:
                        name = constraint['name']
                        conn.execute(text(
                            f'ALTER TABLE floor_readings DROP CONSTRAINT IF EXISTS {name}'
                        ))
            conn.execute(text(
                'CREATE INDEX IF NOT EXISTS ix_floor_readings_conflict_code '
                'ON floor_readings(conflict_code)'
            ))
        if 'optics_surveys' in tables:
            _add_missing_columns(conn, 'optics_surveys', _OPTICS_COLUMNS)

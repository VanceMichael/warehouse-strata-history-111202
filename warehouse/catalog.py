import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .definition import SCHEMA, TEMPORAL_TABLES
from .temporal import (
    UNKNOWN_MEMBER_KEY,
    project_legacy_bundle,
)


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def connect(path):
    connection = sqlite3.connect(path, timeout=5, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys=ON')
    connection.execute('PRAGMA busy_timeout=5000')
    for table, columns in SCHEMA.items():
        connection.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({columns})')
    for table, statement in TEMPORAL_TABLES.items():
        connection.execute(statement)
    connection.execute('CREATE TABLE IF NOT EXISTS ingest_batches (batch TEXT PRIMARY KEY, digest TEXT NOT NULL, rows INTEGER NOT NULL)')
    connection.execute(
        'INSERT OR IGNORE INTO dim_members (member_key, supplier, region, source_version,'
        ' valid_from, valid_to, tx_from, tx_to, seq_from, seq_to) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (UNKNOWN_MEMBER_KEY, '*UNKNOWN*', '未知', '', '1900-01-01', '9999-12-31',
         '1900-01-01T00:00:00+00:00', '9999-12-31T23:59:59.999999+00:00', 0, 999_999_999),
    )
    return connection


@contextmanager
def transaction(connection):
    connection.execute('BEGIN IMMEDIATE')
    try:
        yield connection
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def load_bundle(connection, bundle, batch):
    """装载旧式名录/到货集合，并把同一事实投影进双时间历史。

    旧式批次按当天名录挂供应商，投影为一条覆盖全部业务时间的维度区间，
    接收时间取装载时刻；批次重放与冲突规则保持不变。
    """
    if not isinstance(bundle, dict) or not isinstance(batch, str) or not batch:
        raise ValueError('输入需要命名数据集合与批次标识')
    if set(bundle) - set(SCHEMA):
        raise ValueError('来源包含未登记的数据集合')
    digest = hashlib.sha256(encode(bundle)).hexdigest()
    count = 0
    with transaction(connection):
        previous = connection.execute('SELECT digest FROM ingest_batches WHERE batch=?', (batch,)).fetchone()
        if previous:
            if previous['digest'] != digest:
                raise ValueError('同批次内容不一致')
            return False
        if connection.execute("SELECT 1 FROM dim_versions WHERE state='pending'").fetchone():
            raise ValueError('存在未完成的版本修订，请先恢复')
        for table, rows in bundle.items():
            if not isinstance(rows, list):
                raise ValueError('数据集合必须是记录数组')
            columns = [record['name'] for record in connection.execute(f'PRAGMA table_info("{table}")')]
            for row in rows:
                if not isinstance(row, dict) or set(row) != set(columns):
                    raise ValueError('记录字段与已登记结构不一致')
                names = ', '.join(f'"{column}"' for column in columns)
                placeholders = ', '.join('?' for column in columns)
                connection.execute(f'INSERT INTO "{table}" ({names}) VALUES ({placeholders})', [row[column] for column in columns])
                count += 1
        connection.execute('INSERT INTO ingest_batches VALUES (?, ?, ?)', (batch, digest, count))
        project_legacy_bundle(
            connection,
            batch=batch,
            suppliers=bundle.get('suppliers', []),
            receipts=bundle.get('receipts', []),
            received_at=datetime.now(timezone.utc).isoformat(),
            digest=digest,
        )
    return True


def load_file(connection, path, batch):
    return load_bundle(connection, json.loads(Path(path).read_text(encoding='utf-8')), batch)

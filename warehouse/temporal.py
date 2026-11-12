"""双时间维度修订。

业务时间轴（valid_from/valid_to，左闭右开）描述供应商归属在业务上的生效区间；
系统知悉时间轴（tx_from/tx_to + 版本序号）描述仓库在什么时刻认可该口径。
到货事实只追加事实链（fact_links），旧链与旧成员永不覆盖，因此上周导出的报表
不会因后续修订而悄悄变化。
"""

import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

EPOCH_DATE = '1900-01-01'
FUTURE_DATE = '9999-12-31'
EPOCH_TS = '1900-01-01T00:00:00+00:00'
FUTURE_TS = '9999-12-31T23:59:59.999999+00:00'
MAX_SEQ = 999_999_999
UNKNOWN_MEMBER_KEY = 1
LEGACY_PREFIX = 'batch:'

_DIMENSION_FIELDS = {'supplier', 'region', 'valid_from', 'valid_to'}
_RECEIPT_FIELDS = {'receipt', 'supplier', 'day', 'amount'}


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

def _parse_day(value):
    if not isinstance(value, str):
        raise ValueError('日期必须是 ISO 文本')
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'日期格式错误: {value}') from exc
    return parsed.isoformat()


def _parse_timestamp(value):
    if not isinstance(value, str) or not value:
        raise ValueError('接收时间必须是 ISO-8601 时间戳')
    text = value.replace('Z', '+00:00')
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f'接收时间格式错误: {value}') from exc
    if parsed.tzinfo is None:
        raise ValueError('接收时间必须带时区')
    return parsed.astimezone(timezone.utc).isoformat()


def _parse_amount(value):
    if not isinstance(value, str) or not value:
        raise ValueError('金额必须是十进制文本')
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f'金额格式错误: {value}') from exc
    if not amount.is_finite():
        raise ValueError('数量必须为有限十进制')
    return value


def validate_bundle(bundle):
    """校验版本包，返回规范化后的副本与内容摘要。"""
    if not isinstance(bundle, dict):
        raise ValueError('版本修订必须是对象')
    version = bundle.get('source_version')
    if not isinstance(version, str) or not version:
        raise ValueError('版本修订缺少 source_version')
    if version.startswith(LEGACY_PREFIX):
        raise ValueError('来源版本号不能使用保留前缀 batch:')
    received_at = _parse_timestamp(bundle.get('received_at'))

    dimensions = bundle.get('dimensions', [])
    receipts = bundle.get('receipts', [])
    retracts = bundle.get('retract_versions', [])
    if not isinstance(dimensions, list) or not isinstance(receipts, list) or not isinstance(retracts, list):
        raise ValueError('dimensions、receipts、retract_versions 必须是数组')

    clean_dimensions = []
    seen_intervals = {}
    for row in dimensions:
        if not isinstance(row, dict) or set(row) != _DIMENSION_FIELDS:
            raise ValueError('维度记录字段必须恰好为 supplier/region/valid_from/valid_to')
        if not isinstance(row['supplier'], str) or not row['supplier']:
            raise ValueError('供应商业务键不能为空')
        if not isinstance(row['region'], str) or not row['region']:
            raise ValueError('区域不能为空')
        valid_from = _parse_day(row['valid_from'])
        valid_to = _parse_day(row['valid_to'])
        if not (EPOCH_DATE <= valid_from < valid_to <= FUTURE_DATE):
            raise ValueError(f'生效区间越界或非左闭右开: {row}')
        intervals = seen_intervals.setdefault(row['supplier'], [])
        if any(not (valid_to <= left or right <= valid_from) for left, right in intervals):
            raise ValueError(f'同一版本内供应商 {row["supplier"]} 区间重叠')
        intervals.append((valid_from, valid_to))
        clean_dimensions.append({
            'supplier': row['supplier'],
            'region': row['region'],
            'valid_from': valid_from,
            'valid_to': valid_to,
        })

    clean_receipts = []
    seen_receipts = set()
    for row in receipts:
        if not isinstance(row, dict) or set(row) != _RECEIPT_FIELDS:
            raise ValueError('到货记录字段必须恰好为 receipt/supplier/day/amount')
        for field in ('receipt', 'supplier'):
            if not isinstance(row[field], str) or not row[field]:
                raise ValueError(f'{field} 不能为空')
        if row['receipt'] in seen_receipts:
            raise ValueError(f'版本内到货键重复: {row["receipt"]}')
        seen_receipts.add(row['receipt'])
        day = _parse_day(row['day'])
        if not (EPOCH_DATE <= day <= FUTURE_DATE):
            raise ValueError(f'到货日期越界: {row}')
        clean_receipts.append({
            'receipt': row['receipt'],
            'supplier': row['supplier'],
            'day': day,
            'amount': _parse_amount(row['amount']),
        })

    clean_retracts = []
    for target in retracts:
        if not isinstance(target, str) or not target:
            raise ValueError('撤销目标版本号不能为空')
        if target == version:
            raise ValueError('版本不能撤销自身')
        if target not in clean_retracts:
            clean_retracts.append(target)

    normalized = {
        'source_version': version,
        'received_at': received_at,
        'dimensions': clean_dimensions,
        'receipts': clean_receipts,
        'retract_versions': clean_retracts,
    }
    from .catalog import encode
    digest = hashlib.sha256(encode(normalized)).hexdigest()
    return normalized, digest


# ---------------------------------------------------------------------------
# 纯函数：按认可版本集合构建供应商当前时间线
# ---------------------------------------------------------------------------

def build_timeline(assertions):
    """把带优先级的区间断言折叠成互不相交的时间线段。

    assertions: [{'valid_from','valid_to','region','source_version',
                  'sequence','received_at'}]，仅包含仍认可（未撤销）的断言。
    同一生效日多个版本命中时，版本序号大（后知悉）者优先；序号相同再比接收时间。
    相邻且区域与来源版本都相同的线段合并；没有任何断言的日期留空，
    空窗内的到货归属未知成员。
    """
    events = []
    for index, assertion in enumerate(assertions):
        events.append((assertion['valid_from'], 1, index))
        events.append((assertion['valid_to'], 0, index))
    events.sort(key=lambda event: (event[0], event[1]))

    active = {}
    segments = []
    cursor = None
    winner = None
    for day, kind, index in events:
        if cursor is not None and day > cursor and winner is not None:
            segments.append({
                'valid_from': cursor,
                'valid_to': day,
                'region': winner['region'],
                'source_version': winner['source_version'],
            })
        if kind == 0:
            active.pop(index, None)
        else:
            active[index] = assertions[index]
        if active:
            winner = max(active.values(), key=lambda item: (item['sequence'], item['received_at']))
        else:
            winner = None
        cursor = day

    merged = []
    for segment in segments:
        if (merged and merged[-1]['valid_to'] == segment['valid_from']
                and merged[-1]['region'] == segment['region']
                and merged[-1]['source_version'] == segment['source_version']):
            merged[-1]['valid_to'] = segment['valid_to']
        else:
            merged.append(dict(segment))
    return merged


# ---------------------------------------------------------------------------
# 版本状态机：暂存（崩溃可留痕）→ 物化（一个事务完成重挂）
# ---------------------------------------------------------------------------

def _next_sequence(connection):
    row = connection.execute('SELECT COALESCE(MAX(applied_sequence), 0) + 1 AS next FROM dim_versions').fetchone()
    return row['next']


def _applied_assertions(connection, supplier):
    rows = connection.execute(
        "SELECT applied_sequence, received_at, payload FROM dim_versions"
        " WHERE state='applied' ORDER BY applied_sequence"
    ).fetchall()
    assertions = []
    for row in rows:
        payload = json.loads(row['payload'])
        if payload.get('kind') == 'legacy':
            for supplier_row in payload['suppliers']:
                if supplier_row['supplier'] != supplier:
                    continue
                assertions.append({
                    'valid_from': EPOCH_DATE,
                    'valid_to': FUTURE_DATE,
                    'region': supplier_row['region'],
                    'source_version': payload['source_version'],
                    'sequence': row['applied_sequence'],
                    'received_at': row['received_at'],
                })
        else:
            for dimension in payload['dimensions']:
                if dimension['supplier'] != supplier:
                    continue
                assertions.append({
                    'valid_from': dimension['valid_from'],
                    'valid_to': dimension['valid_to'],
                    'region': dimension['region'],
                    'source_version': payload['source_version'],
                    'sequence': row['applied_sequence'],
                    'received_at': row['received_at'],
                })
    return assertions


def _resolve_member(connection, supplier, day, sequence):
    row = connection.execute(
        'SELECT member_key FROM dim_members WHERE supplier=? AND valid_from <= ? AND ? < valid_to'
        ' AND seq_from <= ? AND ? < seq_to',
        (supplier, day, day, sequence, sequence),
    ).fetchone()
    return row['member_key'] if row else UNKNOWN_MEMBER_KEY


def _refresh_supplier(connection, supplier, sequence, received_at):
    """按当前认可版本集合重建某供应商的开放成员，返回时间线是否发生变化。"""
    timeline = build_timeline(_applied_assertions(connection, supplier))
    existing = [dict(row) for row in connection.execute(
        'SELECT valid_from, valid_to, region, source_version FROM dim_members'
        ' WHERE supplier=? AND seq_to=? ORDER BY valid_from', (supplier, MAX_SEQ),
    )]
    target = [{key: segment[key] for key in ('valid_from', 'valid_to', 'region', 'source_version')} for segment in timeline]
    if existing == target:
        return False
    connection.execute(
        'UPDATE dim_members SET seq_to=?, tx_to=? WHERE supplier=? AND seq_to=?',
        (sequence, received_at, supplier, MAX_SEQ),
    )
    for segment in timeline:
        connection.execute(
            'INSERT INTO dim_members (supplier, region, source_version, valid_from, valid_to,'
            ' tx_from, tx_to, seq_from, seq_to) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (supplier, segment['region'], segment['source_version'], segment['valid_from'],
             segment['valid_to'], received_at, FUTURE_TS, sequence, MAX_SEQ),
        )
    return True


def _materialize(connection, version, bundle, received_at, retracts, on_stage):
    from .catalog import transaction
    if on_stage is not None:
        on_stage(connection)
    with transaction(connection):
        sequence = _next_sequence(connection)
        # 先把本版本置为 applied，时间线重建才能读到本版本的断言。
        connection.execute(
            "UPDATE dim_versions SET state='applied', applied_sequence=? WHERE version=?",
            (sequence, version),
        )
        for target in retracts:
            connection.execute("UPDATE dim_versions SET state='retracted' WHERE version=?", (target,))

        affected = set()
        affected.update(dimension['supplier'] for dimension in bundle['dimensions'])
        affected.update(receipt['supplier'] for receipt in bundle['receipts'])
        if retracts:
            placeholders = ','.join('?' for _ in retracts)
            rows = connection.execute(
                f'SELECT DISTINCT supplier FROM dim_members WHERE source_version IN ({placeholders})',
                retracts,
            ).fetchall()
            affected.update(row['supplier'] for row in rows)
        # 迟到补档：当前挂在未知成员的到货也要纳入重挂范围。
        unknown_suppliers = set()
        rows = connection.execute(
            'SELECT r.supplier AS supplier FROM receipt_assignments a'
            ' JOIN receipts r ON r.receipt = a.receipt'
            ' WHERE a.member_key = ?', (UNKNOWN_MEMBER_KEY,),
        ).fetchall()
        unknown_suppliers.update(row['supplier'] for row in rows)
        affected.update(unknown_suppliers)

        changed_suppliers = set()
        for supplier in sorted(affected):
            if _refresh_supplier(connection, supplier, sequence, received_at):
                changed_suppliers.add(supplier)

        for receipt in bundle['receipts']:
            connection.execute(
                'INSERT OR IGNORE INTO receipt_origins (receipt, source_version, received_at) VALUES (?, ?, ?)',
                (receipt['receipt'], version, received_at),
            )
            member_key = _resolve_member(connection, receipt['supplier'], receipt['day'], sequence)
            connection.execute(
                'INSERT OR IGNORE INTO fact_links (receipt, member_key, source_version, received_at, sequence)'
                ' VALUES (?, ?, ?, ?, ?)',
                (receipt['receipt'], member_key, version, received_at, sequence),
            )
            connection.execute(
                'INSERT OR REPLACE INTO receipt_assignments (receipt, member_key, sequence) VALUES (?, ?, ?)',
                (receipt['receipt'], member_key, sequence),
            )

        relink_suppliers = changed_suppliers | unknown_suppliers
        for supplier in sorted(relink_suppliers):
            rows = connection.execute('SELECT receipt, day FROM receipts WHERE supplier=?', (supplier,)).fetchall()
            for row in rows:
                member_key = _resolve_member(connection, supplier, row['day'], sequence)
                current = connection.execute(
                    'SELECT member_key FROM receipt_assignments WHERE receipt=?', (row['receipt'],),
                ).fetchone()
                if current is None or current['member_key'] != member_key:
                    connection.execute(
                        'INSERT OR IGNORE INTO fact_links (receipt, member_key, source_version, received_at, sequence)'
                        ' VALUES (?, ?, ?, ?, ?)',
                        (row['receipt'], member_key, version, received_at, sequence),
                    )
                    connection.execute(
                        'INSERT OR REPLACE INTO receipt_assignments (receipt, member_key, sequence) VALUES (?, ?, ?)',
                        (row['receipt'], member_key, sequence),
                    )
    return sequence


def apply_version_bundle(connection, bundle, on_stage=None):
    """应用一个来源版本。

    返回 (changed, sequence)。相同版本号重复到达且内容一致时不产生新历史；
    同版本号内容不一致直接拒绝整批；存在其他版本的未完成暂存时拒绝写入。
    on_stage 在暂存提交后、物化前调用，抛异常即可模拟重挂中途失败。
    """
    normalized, digest = validate_bundle(bundle)
    version = normalized['source_version']
    received_at = normalized['received_at']

    row = connection.execute('SELECT * FROM dim_versions WHERE version=?', (version,)).fetchone()
    if row is not None:
        if row['digest'] != digest:
            raise ValueError('同来源版本内容不一致，拒绝整批')
        if row['state'] == 'pending':
            return False, recover_pending(connection, on_stage=on_stage)
        return False, row['applied_sequence']

    pending = connection.execute("SELECT version FROM dim_versions WHERE state='pending'").fetchone()
    if pending is not None:
        raise ValueError(f"存在未完成的版本修订 {pending['version']}，请先恢复")

    for target in normalized['retract_versions']:
        target_row = connection.execute("SELECT state FROM dim_versions WHERE version=?", (target,)).fetchone()
        if target_row is None:
            raise ValueError(f'撤销目标版本不存在: {target}')
        if target_row['state'] != 'applied':
            raise ValueError(f'撤销目标版本当前不可撤销: {target}')

    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    from .catalog import transaction
    with transaction(connection):
        connection.execute(
            'INSERT INTO dim_versions (version, digest, received_at, applied_sequence, retracts,'
            ' receipt_count, state, payload) VALUES (?, ?, ?, NULL, ?, ?, ?, ?)',
            (version, digest, received_at, ','.join(normalized['retract_versions']),
             len(normalized['receipts']), 'pending', payload),
        )
        for receipt in normalized['receipts']:
            connection.execute(
                'INSERT INTO receipts (receipt, supplier, day, amount) VALUES (?, ?, ?, ?)',
                (receipt['receipt'], receipt['supplier'], receipt['day'], receipt['amount']),
            )

    sequence = _materialize(connection, version, normalized, received_at,
                            normalized['retract_versions'], on_stage)
    return True, sequence


def recover_pending(connection, on_stage=None):
    """恢复中途失败的暂存版本；没有暂存时返回 None。

    物化是幂等的：事实链只追加且按键去重，恢复不会重复计数；
    暂存版本一直可读，恢复前旧版本快照不受影响。
    """
    row = connection.execute("SELECT * FROM dim_versions WHERE state='pending'").fetchone()
    if row is None:
        return None
    bundle = json.loads(row['payload'])
    retracts = [part for part in row['retracts'].split(',') if part]
    return _materialize(connection, row['version'], bundle, row['received_at'], retracts, on_stage)


def project_legacy_bundle(connection, batch, suppliers, receipts, received_at, digest):
    """把旧式名录批次投影为一条已应用版本（与 catalog 装载同一事务）。"""
    version = LEGACY_PREFIX + batch
    if connection.execute('SELECT 1 FROM dim_versions WHERE version=?', (version,)).fetchone():
        return
    if connection.execute("SELECT 1 FROM dim_versions WHERE state='pending'").fetchone():
        raise ValueError('存在未完成的版本修订，请先恢复')
    sequence = _next_sequence(connection)
    payload = {
        'kind': 'legacy',
        'source_version': version,
        'suppliers': suppliers,
        'receipts': receipts,
    }
    connection.execute(
        'INSERT INTO dim_versions (version, digest, received_at, applied_sequence, retracts,'
        ' receipt_count, state, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (version, digest, received_at, sequence, '', len(receipts), 'applied',
         json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)),
    )
    for supplier_row in suppliers:
        connection.execute(
            'INSERT INTO dim_members (supplier, region, source_version, valid_from, valid_to,'
            ' tx_from, tx_to, seq_from, seq_to) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (supplier_row['supplier'], supplier_row['region'], version, EPOCH_DATE, FUTURE_DATE,
             received_at, FUTURE_TS, sequence, MAX_SEQ),
        )
    for receipt in receipts:
        connection.execute(
            'INSERT OR IGNORE INTO receipt_origins (receipt, source_version, received_at) VALUES (?, ?, ?)',
            (receipt['receipt'], version, received_at),
        )
        member_key = _resolve_member(connection, receipt['supplier'], receipt['day'], sequence)
        connection.execute(
            'INSERT OR IGNORE INTO fact_links (receipt, member_key, source_version, received_at, sequence)'
            ' VALUES (?, ?, ?, ?, ?)',
            (receipt['receipt'], member_key, version, received_at, sequence),
        )
        connection.execute(
            'INSERT OR REPLACE INTO receipt_assignments (receipt, member_key, sequence) VALUES (?, ?, ?)',
            (receipt['receipt'], member_key, sequence),
        )


# ---------------------------------------------------------------------------
# 查询：版本快照、当时知悉视角、当前认可视角
# ---------------------------------------------------------------------------

def _sequence_of_version(connection, version):
    row = connection.execute(
        'SELECT applied_sequence, state FROM dim_versions WHERE version=?', (version,),
    ).fetchone()
    if row is None:
        raise ValueError(f'版本不存在: {version}')
    if row['state'] == 'pending' or row['applied_sequence'] is None:
        raise ValueError(f'版本尚未完成物化: {version}')
    return row['applied_sequence']


def _latest_sequence(connection):
    row = connection.execute(
        "SELECT COALESCE(MAX(applied_sequence), 0) AS sequence FROM dim_versions WHERE state IN ('applied', 'retracted')"
    ).fetchone()
    return row['sequence']


def _linked_receipts(connection, sequence):
    return connection.execute(
        'SELECT r.receipt, r.supplier, r.day, r.amount, fl.member_key, fl.sequence AS link_sequence,'
        ' m.region, m.source_version AS dimension_version'
        ' FROM receipts r'
        ' JOIN fact_links fl ON fl.receipt = r.receipt AND fl.sequence = ('
        '   SELECT MAX(sequence) FROM fact_links WHERE receipt = r.receipt AND sequence <= ?)'
        ' JOIN dim_members m ON m.member_key = fl.member_key'
        ' ORDER BY r.receipt',
        (sequence,),
    ).fetchall()


def _totals(rows):
    totals = {}
    for row in rows:
        amount = Decimal(row['amount'])
        if not amount.is_finite():
            raise ValueError('数量必须为有限十进制')
        totals[row['region']] = totals.get(row['region'], Decimal('0')) + amount
    return [{'region': region, 'amount': str(totals[region])} for region in sorted(totals)]


def snapshot_totals(connection, version=None):
    """用一个版本号同时拿到该版本下维度与事实一致的金额口径。"""
    sequence = _latest_sequence(connection) if version is None else _sequence_of_version(connection, version)
    if sequence == 0:
        return []
    return _totals(_linked_receipts(connection, sequence))


def snapshot_receipts(connection, version=None):
    sequence = _latest_sequence(connection) if version is None else _sequence_of_version(connection, version)
    if sequence == 0:
        return []
    return [dict(row) for row in _linked_receipts(connection, sequence)]


def as_known_totals(connection, known_at):
    """当时已知口径：只使用该知悉时刻之前已到达的版本。"""
    moment = _parse_timestamp(known_at)
    row = connection.execute(
        "SELECT COALESCE(MAX(applied_sequence), 0) AS sequence FROM dim_versions"
        " WHERE state IN ('applied', 'retracted') AND received_at <= ?",
        (moment,),
    ).fetchone()
    if row['sequence'] == 0:
        return []
    return _totals(_linked_receipts(connection, row['sequence']))


def as_known_receipts(connection, known_at):
    """当时已知口径的到货明细。"""
    moment = _parse_timestamp(known_at)
    row = connection.execute(
        "SELECT COALESCE(MAX(applied_sequence), 0) AS sequence FROM dim_versions"
        " WHERE state IN ('applied', 'retracted') AND received_at <= ?",
        (moment,),
    ).fetchone()
    if row['sequence'] == 0:
        return []
    return [dict(record) for record in _linked_receipts(connection, row['sequence'])]


def current_totals(connection):
    """现在认可的历史口径（最新版本快照）。"""
    return snapshot_totals(connection)


def list_versions(connection):
    rows = connection.execute(
        'SELECT version, applied_sequence, received_at, state, retracts, receipt_count, digest'
        ' FROM dim_versions ORDER BY CASE WHEN applied_sequence IS NULL THEN 1 ELSE 0 END,'
        ' applied_sequence'
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item['retract_versions'] = [part for part in item.pop('retracts').split(',') if part]
        result.append(item)
    return result

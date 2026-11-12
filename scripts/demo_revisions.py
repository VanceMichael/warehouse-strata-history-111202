"""双时间维度修订端到端演示。

在全新临时目录中依次应用 examples/revisions 下的版本包，展示：
1. 区间中部插入（v3 把 s2 的十月区间拆成两段）；
2. 迟到补档（v4 把原先落在未知成员的 r2 重新关联到华中）；
3. 撤销更正（v5 撤销 v2，s1 回到华东，旧报表仍可复现）；
4. 重挂中途失败与恢复（v6 模拟断电，旧版本仍可读，恢复不重复计数）。

同时用 CLI 演示“当时已知口径（--known-at）”与“版本一致快照（--version）”两个查询入口。
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from warehouse.catalog import connect
from warehouse.temporal import (
    apply_version_bundle,
    as_known_totals,
    current_totals,
    recover_pending,
    snapshot_receipts,
    snapshot_totals,
)

REVISION_DIR = ROOT / 'examples/revisions'
EXPECTED = json.loads((REVISION_DIR / 'expected.json').read_text(encoding='utf-8'))
ORDER = ['v1_initial', 'v2_october_correction', 'v3_split_insert',
         'v4_late_backfill', 'v5_retract_correction']


def heading(text):
    print(f'\n=== {text} ===', flush=True)


def show(totals):
    print(json.dumps(totals, ensure_ascii=False, sort_keys=True), flush=True)


def cli(*arguments):
    command = [sys.executable, '-m', 'warehouse', *arguments]
    print('$ ' + ' '.join(command), flush=True)
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=True)
    print(result.stdout.strip(), flush=True)
    return json.loads(result.stdout)


def main():
    with tempfile.TemporaryDirectory() as temporary:
        database = str(Path(temporary) / 'warehouse.sqlite3')
        connection = connect(database)
        try:
            heading('1. 区间拆分与同生效日优先（v1 -> v2 -> v3）')
            for name in ORDER[:3]:
                bundle = json.loads((REVISION_DIR / f'{name}.json').read_text(encoding='utf-8'))
                changed, sequence = apply_version_bundle(connection, bundle)
                print(f"{name}: changed={changed}, sequence={sequence}", flush=True)
            members = [
                {key: row[key] for key in ('valid_from', 'valid_to', 'region', 'source_version')}
                for row in connection.execute(
                    'SELECT valid_from, valid_to, region, source_version FROM dim_members'
                    " WHERE supplier='s2' AND seq_to=999999999 ORDER BY valid_from")
            ]
            assert members == EXPECTED['v3_s2_members']
            print('s2 时间线被左闭右开拆成两段:', json.dumps(members, ensure_ascii=False), flush=True)

            heading('2. 迟到补档：未知成员 -> 华中（v4）')
            before = {row['receipt']: row for row in snapshot_receipts(connection, 'v1')}
            print('v1 快照中 r2 成员键:', before['r2']['member_key'], before['r2']['region'], flush=True)
            apply_version_bundle(connection, json.loads(
                (REVISION_DIR / 'v4_late_backfill.json').read_text(encoding='utf-8')))
            after = {row['receipt']: row for row in snapshot_receipts(connection, 'v4')}
            print('v4 快照中 r2:', after['r2']['region'], after['r2']['dimension_version'], flush=True)
            old = {row['receipt']: row for row in snapshot_receipts(connection, 'v1')}
            assert old['r2']['region'] == '未知'
            print('v1 旧快照保持未知，上周导出不变', flush=True)

            heading('3. 撤销更正（v5 撤销 v2）')
            apply_version_bundle(connection, json.loads(
                (REVISION_DIR / 'v5_retract_correction.json').read_text(encoding='utf-8')))
            show(snapshot_totals(connection, 'v2'))
            show(current_totals(connection))
            assert current_totals(connection) == EXPECTED['current_after_v5']
            for version, totals in EXPECTED['snapshots'].items():
                assert snapshot_totals(connection, version) == totals
            print('五个版本快照全部与 expected.json 一致', flush=True)

            heading('4. 重挂中途失败与恢复（v6 模拟断电）')
            v6 = json.loads((REVISION_DIR / 'v6_crash_recovery.json').read_text(encoding='utf-8'))
            v5_snapshot = snapshot_totals(connection, 'v5')

            class PowerLoss(Exception):
                pass

            try:
                apply_version_bundle(
                    connection, v6,
                    on_stage=lambda _connection: (_ for _ in ()).throw(PowerLoss('模拟重挂中途断电')),
                )
            except PowerLoss as exc:
                print(f'捕获注入故障: {exc}', flush=True)
            pending = dict(connection.execute(
                "SELECT version, state, applied_sequence FROM dim_versions WHERE version='v6'").fetchone())
            print('故障后版本行:', pending, flush=True)
            assert pending['state'] == 'pending'
            assert snapshot_totals(connection, 'v5') == v5_snapshot
            print('旧版 v5 仍可读，结果未变', flush=True)

            sequence = recover_pending(connection)
            print('恢复后序号:', sequence, flush=True)
            assert recover_pending(connection) is None
            assert snapshot_totals(connection) == EXPECTED['v6_after_recovery']
            duplicates = connection.execute(
                'SELECT COUNT(*) FROM (SELECT receipt, sequence FROM fact_links GROUP BY receipt, sequence HAVING COUNT(*) > 1)'
            ).fetchone()[0]
            assert duplicates == 0
            print('恢复幂等、事实链无重复计数:', EXPECTED['v6_after_recovery'], flush=True)
        finally:
            connection.close()

        heading('5. CLI 两个历史视角入口')
        for name in ORDER:
            cli('revise', '--db', database, '--input', str(REVISION_DIR / f'{name}.json'), '--batch', name)
        cli('versions', '--db', database)
        cli('history', '--db', database, '--version', 'v4')
        cli('history', '--db', database, '--known-at', '2026-11-06T00:00:00+00:00')
        cli('history', '--db', database, '--known-at', '2026-11-12T00:00:00+00:00')
        known = as_known_totals(connect(database), '2026-11-06T00:00:00+00:00')
        assert known == EXPECTED['known_at']['2026-11-06T00:00:00+00:00']
        print('\n全部演示断言通过。', flush=True)


if __name__ == '__main__':
    main()

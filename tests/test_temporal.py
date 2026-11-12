import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from warehouse.catalog import connect, load_bundle
from warehouse.temporal import (
    LEGACY_PREFIX,
    UNKNOWN_MEMBER_KEY,
    apply_version_bundle,
    as_known_receipts,
    as_known_totals,
    build_timeline,
    current_totals,
    list_versions,
    recover_pending,
    snapshot_receipts,
    snapshot_totals,
    validate_bundle,
)

REVISION_DIR = Path(__file__).resolve().parents[1] / 'examples/revisions'
LEGACY_SOURCE = Path(__file__).resolve().parents[1] / 'examples/source.json'
EXPECTED = json.loads((REVISION_DIR / 'expected.json').read_text(encoding='utf-8'))
SCENARIO = {name: json.loads((REVISION_DIR / f'{name}.json').read_text(encoding='utf-8'))
            for name in ('v1_initial', 'v2_october_correction', 'v3_split_insert',
                         'v4_late_backfill', 'v5_retract_correction', 'v6_crash_recovery')}


def load_named(connection, name):
    return apply_version_bundle(connection, SCENARIO[name])


def load_through_v5(connection):
    for name in ('v1_initial', 'v2_october_correction', 'v3_split_insert',
                 'v4_late_backfill', 'v5_retract_correction'):
        load_named(connection, name)


class TimelineTests(unittest.TestCase):
    def test_same_effective_day_newer_version_wins(self):
        assertions = [
            {'valid_from': '2026-10-01', 'valid_to': '2026-11-01', 'region': '华东',
             'source_version': 'v1', 'sequence': 1, 'received_at': '2026-10-31T09:00:00+00:00'},
            {'valid_from': '2026-10-01', 'valid_to': '2026-11-01', 'region': '华北',
             'source_version': 'v2', 'sequence': 2, 'received_at': '2026-11-05T09:00:00+00:00'},
        ]
        self.assertEqual(build_timeline(assertions), [
            {'valid_from': '2026-10-01', 'valid_to': '2026-11-01', 'region': '华北', 'source_version': 'v2'},
        ])

    def test_split_keeps_half_open_segments_and_merges_adjacent(self):
        assertions = [
            {'valid_from': '2026-10-01', 'valid_to': '2026-11-01', 'region': '华南',
             'source_version': 'v1', 'sequence': 1, 'received_at': '2026-10-31T09:00:00+00:00'},
            {'valid_from': '2026-10-15', 'valid_to': '2026-11-01', 'region': '西南',
             'source_version': 'v3', 'sequence': 3, 'received_at': '2026-11-08T09:00:00+00:00'},
            {'valid_from': '2026-11-01', 'valid_to': '2026-12-01', 'region': '西南',
             'source_version': 'v3', 'sequence': 3, 'received_at': '2026-11-08T09:00:00+00:00'},
        ]
        self.assertEqual(build_timeline(assertions), [
            {'valid_from': '2026-10-01', 'valid_to': '2026-10-15', 'region': '华南', 'source_version': 'v1'},
            {'valid_from': '2026-10-15', 'valid_to': '2026-12-01', 'region': '西南', 'source_version': 'v3'},
        ])

    def test_retraction_reopens_older_assertion(self):
        assertions = [
            {'valid_from': '2026-10-01', 'valid_to': '2026-11-01', 'region': '华东',
             'source_version': 'v1', 'sequence': 1, 'received_at': '2026-10-31T09:00:00+00:00'},
        ]
        self.assertEqual(build_timeline(assertions)[0]['region'], '华东')


class ScenarioTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.connection = connect(Path(self.temporary.name) / 'warehouse.sqlite3')

    def tearDown(self):
        self.connection.close()
        self.temporary.cleanup()

    def test_version_snapshots_match_expected(self):
        load_through_v5(self.connection)
        for version, totals in EXPECTED['snapshots'].items():
            self.assertEqual(snapshot_totals(self.connection, version), totals, version)
        self.assertEqual(current_totals(self.connection), EXPECTED['current_after_v5'])

    def test_as_known_views(self):
        load_through_v5(self.connection)
        for moment, totals in EXPECTED['known_at'].items():
            self.assertEqual(as_known_totals(self.connection, moment), totals, moment)
        early = {row['receipt']: row for row in
                 as_known_receipts(self.connection, '2026-11-01T00:00:00+00:00')}
        self.assertEqual(early['r1']['dimension_version'], 'v1')
        later = {row['receipt']: row for row in
                 as_known_receipts(self.connection, '2026-11-06T00:00:00+00:00')}
        self.assertEqual(later['r1']['region'], '华北')
        self.assertEqual(later['r1']['dimension_version'], 'v2')
        self.assertEqual(as_known_receipts(self.connection, '2026-10-01T00:00:00+00:00'), [])

    def test_unknown_receipt_lands_on_unknown_member_then_backfill_relinks(self):
        load_named(self.connection, 'v1_initial')
        detail = {row['receipt']: row for row in snapshot_receipts(self.connection, 'v1')}
        self.assertEqual(detail['r2']['member_key'], UNKNOWN_MEMBER_KEY)
        self.assertEqual(detail['r2']['region'], '未知')
        load_named(self.connection, 'v4_late_backfill')
        detail = {row['receipt']: row for row in snapshot_receipts(self.connection, 'v4')}
        self.assertEqual(detail['r2']['region'], '华中')
        self.assertEqual(detail['r2']['dimension_version'], 'v4')
        # 旧版本快照仍保持未知，上周导出不会变
        old = {row['receipt']: row for row in snapshot_receipts(self.connection, 'v1')}
        self.assertEqual(old['r2']['member_key'], UNKNOWN_MEMBER_KEY)

    def test_interval_split_touching_only_named_supplier(self):
        for name in ('v1_initial', 'v2_october_correction', 'v3_split_insert'):
            load_named(self.connection, name)
        members = [
            {key: row[key] for key in ('valid_from', 'valid_to', 'region', 'source_version')}
            for row in self.connection.execute(
                'SELECT valid_from, valid_to, region, source_version FROM dim_members'
                ' WHERE supplier=? AND seq_to=? ORDER BY valid_from', ('s2', 999_999_999))
        ]
        self.assertEqual(members, EXPECTED['v3_s2_members'])
        # 不涉及的 s1 开放成员数量不因 s3 风格的拆分而增加
        s1_members = self.connection.execute(
            'SELECT COUNT(*) FROM dim_members WHERE supplier=? AND seq_to=?', ('s1', 999_999_999)
        ).fetchone()[0]
        self.assertEqual(s1_members, 1)

    def test_retraction_removes_correction_and_keeps_history(self):
        load_through_v5(self.connection)
        v2_members = self.connection.execute(
            "SELECT COUNT(*) FROM dim_members WHERE source_version='v2' AND seq_to<999999999"
        ).fetchone()[0]
        self.assertGreater(v2_members, 0)
        v5_state = self.connection.execute(
            "SELECT state FROM dim_versions WHERE version='v2'"
        ).fetchone()[0]
        self.assertEqual(v5_state, 'retracted')
        # v2 当时的报表仍可复现
        self.assertEqual(snapshot_totals(self.connection, 'v2'), EXPECTED['snapshots']['v2'])
        versions = [row['version'] for row in list_versions(self.connection)]
        self.assertEqual(versions, ['v1', 'v2', 'v3', 'v4', 'v5'])

    def test_transitive_retraction_does_not_resurrect_version(self):
        load_named(self.connection, 'v1_initial')
        load_named(self.connection, 'v2_october_correction')
        v7 = {'source_version': 'v7', 'received_at': '2026-11-12T10:00:00+00:00',
              'dimensions': [], 'receipts': [], 'retract_versions': ['v2']}
        v8 = {'source_version': 'v8', 'received_at': '2026-11-12T11:00:00+00:00',
              'dimensions': [], 'receipts': [], 'retract_versions': ['v7']}
        apply_version_bundle(self.connection, v7)
        self.assertEqual(snapshot_totals(self.connection)[0]['region'], '华东')
        apply_version_bundle(self.connection, v8)
        # 撤销 v7 不会让 v2 复活：v2 仍是 retracted，口径保持 v1 的华东
        self.assertEqual(snapshot_totals(self.connection)[0]['region'], '华东')
        states = dict(self.connection.execute('SELECT version, state FROM dim_versions').fetchall())
        self.assertEqual(states['v2'], 'retracted')
        self.assertEqual(states['v7'], 'retracted')
        self.assertEqual(states['v8'], 'applied')

    def test_duplicate_version_is_noop_but_inconsistent_rejected(self):
        load_through_v5(self.connection)
        bundle = copy.deepcopy(SCENARIO['v5_retract_correction'])
        changed, sequence = apply_version_bundle(self.connection, bundle)
        self.assertFalse(changed)
        self.assertEqual(sequence, 5)
        before = self.connection.execute('SELECT COUNT(*) FROM fact_links').fetchone()[0]
        bad = copy.deepcopy(SCENARIO['v5_retract_correction'])
        bad['retract_versions'] = []
        with self.assertRaises(ValueError):
            apply_version_bundle(self.connection, bad)
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM fact_links').fetchone()[0], before)

    def test_amount_totals_conserved_across_revisions(self):
        from decimal import Decimal
        load_through_v5(self.connection)
        for version in EXPECTED['version_order']:
            total = sum(Decimal(row['amount']) for row in snapshot_totals(self.connection, version))
            self.assertEqual(total, Decimal('19.30'), version)

    def test_crash_mid_materialization_keeps_old_version_and_recovery_counts_once(self):
        load_through_v5(self.connection)
        old_snapshot = snapshot_totals(self.connection, 'v5')
        before_links = self.connection.execute('SELECT COUNT(*) FROM fact_links').fetchone()[0]

        class PowerLoss(Exception):
            pass

        with self.assertRaises(PowerLoss):
            apply_version_bundle(
                self.connection,
                SCENARIO['v6_crash_recovery'],
                on_stage=lambda connection: (_ for _ in ()).throw(PowerLoss('模拟断电')),
            )
        pending = self.connection.execute(
            "SELECT state FROM dim_versions WHERE version='v6'"
        ).fetchone()
        self.assertEqual(pending['state'], 'pending')
        # 异常退出后旧版仍可读，且 v6 的事实尚未入链
        self.assertEqual(snapshot_totals(self.connection, 'v5'), old_snapshot)
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM fact_links').fetchone()[0], before_links)

        sequence = recover_pending(self.connection)
        self.assertEqual(sequence, 6)
        self.assertEqual(snapshot_totals(self.connection), EXPECTED['v6_after_recovery'])
        # 恢复幂等：再次恢复无操作，行数不翻倍
        self.assertIsNone(recover_pending(self.connection))
        link_rows = self.connection.execute('SELECT receipt, sequence, COUNT(*) c FROM fact_links GROUP BY receipt, sequence')
        self.assertTrue(all(row['c'] == 1 for row in link_rows))
        # 同版本重放不产生新历史
        changed, sequence_again = apply_version_bundle(self.connection, SCENARIO['v6_crash_recovery'])
        self.assertFalse(changed)
        self.assertEqual(sequence_again, 6)
        after_links = self.connection.execute('SELECT COUNT(*) FROM fact_links').fetchone()[0]
        self.assertEqual(after_links, self.connection.execute('SELECT COUNT(*) FROM fact_links').fetchone()[0])

    def test_pending_blocks_new_versions(self):
        class PowerLoss(Exception):
            pass
        with self.assertRaises(PowerLoss):
            apply_version_bundle(
                self.connection, SCENARIO['v1_initial'],
                on_stage=lambda connection: (_ for _ in ()).throw(PowerLoss()),
            )
        with self.assertRaises(ValueError):
            apply_version_bundle(self.connection, SCENARIO['v2_october_correction'])
        recover_pending(self.connection)
        changed, _ = apply_version_bundle(self.connection, SCENARIO['v2_october_correction'])
        self.assertTrue(changed)

    def test_uninvolved_supplier_facts_not_relinked(self):
        load_through_v5(self.connection)
        before = {row['receipt']: row['sequence'] for row in
                  self.connection.execute('SELECT receipt, sequence FROM receipt_assignments')}
        apply_version_bundle(self.connection, SCENARIO['v6_crash_recovery'])
        after = {row['receipt']: row['sequence'] for row in
                 self.connection.execute('SELECT receipt, sequence FROM receipt_assignments')}
        for receipt in ('r1', 'r2', 'r3'):
            self.assertEqual(before[receipt], after[receipt])
        self.assertEqual(after['r4'], 6)

    def test_unknown_retract_target_and_overlap_rejected(self):
        load_named(self.connection, 'v1_initial')
        bad = copy.deepcopy(SCENARIO['v5_retract_correction'])
        bad['source_version'] = 'v5b'
        with self.assertRaises(ValueError):
            apply_version_bundle(self.connection, bad)
        overlap = {
            'source_version': 'vx', 'received_at': '2026-11-11T09:00:00+00:00',
            'dimensions': [
                {'supplier': 's1', 'region': '华东', 'valid_from': '2026-10-01', 'valid_to': '2026-10-20'},
                {'supplier': 's1', 'region': '华北', 'valid_from': '2026-10-10', 'valid_to': '2026-11-01'},
            ],
            'receipts': [], 'retract_versions': [],
        }
        with self.assertRaises(ValueError):
            apply_version_bundle(self.connection, overlap)

    def test_validation_rejects_bad_intervals_and_naive_timestamp(self):
        base = {
            'source_version': 'vz', 'received_at': '2026-11-11T09:00:00+00:00',
            'dimensions': [], 'receipts': [], 'retract_versions': [],
        }
        bad_interval = copy.deepcopy(base)
        bad_interval['dimensions'] = [
            {'supplier': 's9', 'region': '华东', 'valid_from': '2026-11-01', 'valid_to': '2026-11-01'},
        ]
        with self.assertRaises(ValueError):
            validate_bundle(bad_interval)
        naive = copy.deepcopy(base)
        naive['received_at'] = '2026-11-11T09:00:00'
        with self.assertRaises(ValueError):
            validate_bundle(naive)
        with self.assertRaises(ValueError):
            validate_bundle({'source_version': '', 'received_at': '2026-11-11T09:00:00+00:00',
                             'dimensions': [], 'receipts': [], 'retract_versions': []})

    def test_duplicate_receipt_key_across_versions_rolls_back(self):
        load_named(self.connection, 'v1_initial')
        duplicate = {
            'source_version': 'vd', 'received_at': '2026-11-11T09:00:00+00:00',
            'dimensions': [],
            'receipts': [{'receipt': 'r1', 'supplier': 's1', 'day': '2026-10-10', 'amount': '9.99'}],
            'retract_versions': [],
        }
        with self.assertRaises(sqlite3.IntegrityError):
            apply_version_bundle(self.connection, duplicate)
        self.assertIsNone(self.connection.execute(
            "SELECT 1 FROM dim_versions WHERE version='vd'").fetchone())
        self.assertEqual(snapshot_totals(self.connection), EXPECTED['snapshots']['v1'])

    def test_timeline_gap_falls_back_to_unknown_member(self):
        bundle = {
            'source_version': 'gap', 'received_at': '2026-10-31T09:00:00+00:00',
            'dimensions': [
                {'supplier': 'sg', 'region': '华东',
                 'valid_from': '2026-10-01', 'valid_to': '2026-10-15'},
            ],
            'receipts': [
                {'receipt': 'g1', 'supplier': 'sg', 'day': '2026-10-10', 'amount': '1.00'},
                {'receipt': 'g2', 'supplier': 'sg', 'day': '2026-10-20', 'amount': '2.00'},
            ],
            'retract_versions': [],
        }
        apply_version_bundle(self.connection, bundle)
        detail = {row['receipt']: row for row in snapshot_receipts(self.connection)}
        self.assertEqual(detail['g1']['region'], '华东')
        self.assertEqual(detail['g2']['member_key'], UNKNOWN_MEMBER_KEY)
        self.assertEqual(detail['g2']['region'], '未知')

    def test_recover_then_replay_is_idempotent(self):
        class PowerLoss(Exception):
            pass
        with self.assertRaises(PowerLoss):
            apply_version_bundle(
                self.connection, SCENARIO['v1_initial'],
                on_stage=lambda connection: (_ for _ in ()).throw(PowerLoss()),
            )
        first = recover_pending(self.connection)
        second = recover_pending(self.connection)
        changed, third = apply_version_bundle(self.connection, SCENARIO['v1_initial'])
        self.assertEqual(first, 1)
        self.assertIsNone(second)
        self.assertFalse(changed)
        self.assertEqual(third, 1)
        self.assertEqual(
            self.connection.execute(
                'SELECT COUNT(*) FROM (SELECT receipt FROM fact_links GROUP BY receipt HAVING COUNT(*) = 1)'
            ).fetchone()[0],
            3,
        )
        self.assertEqual(
            self.connection.execute('SELECT COUNT(*) FROM fact_links').fetchone()[0], 3)


class LegacyInteropTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.connection = connect(Path(self.temporary.name) / 'warehouse.sqlite3')

    def tearDown(self):
        self.connection.close()
        self.temporary.cleanup()

    def test_legacy_bundle_is_projectable_and_keeps_overview_shape(self):
        legacy = json.loads(LEGACY_SOURCE.read_text(encoding='utf-8'))
        load_bundle(self.connection, legacy, 'daily')
        version = LEGACY_PREFIX + 'daily'
        self.assertEqual(snapshot_totals(self.connection, version),
                         [{'region': '华东', 'amount': '12.30'}, {'region': '未知', 'amount': '2.00'}])
        detail = {row['receipt']: row for row in snapshot_receipts(self.connection, version)}
        self.assertEqual(detail['r2']['member_key'], UNKNOWN_MEMBER_KEY)
        row = self.connection.execute(
            "SELECT state FROM dim_versions WHERE version=?", (version,)
        ).fetchone()
        self.assertEqual(row['state'], 'applied')
        # 后续正式版本可以在同一时间线上修订旧批次建立的归属
        revision = {
            'source_version': 'fix1', 'received_at': '2026-11-12T09:00:00+00:00',
            'dimensions': [
                {'supplier': 'missing', 'region': '华中',
                 'valid_from': '2026-10-01', 'valid_to': '2026-11-01'},
            ],
            'receipts': [], 'retract_versions': [],
        }
        apply_version_bundle(self.connection, revision)
        detail = {row['receipt']: row for row in snapshot_receipts(self.connection)}
        self.assertEqual(detail['r2']['region'], '华中')

    def test_legacy_replay_remains_idempotent(self):
        legacy = json.loads(LEGACY_SOURCE.read_text(encoding='utf-8'))
        self.assertTrue(load_bundle(self.connection, legacy, 'daily'))
        self.assertFalse(load_bundle(self.connection, legacy, 'daily'))
        self.assertEqual(
            self.connection.execute('SELECT COUNT(*) FROM dim_versions').fetchone()[0], 1)

    def test_legacy_load_blocked_while_pending_exists(self):
        class PowerLoss(Exception):
            pass
        with self.assertRaises(PowerLoss):
            apply_version_bundle(
                self.connection, SCENARIO['v1_initial'],
                on_stage=lambda connection: (_ for _ in ()).throw(PowerLoss()),
            )
        legacy = json.loads(LEGACY_SOURCE.read_text(encoding='utf-8'))
        with self.assertRaises(ValueError):
            load_bundle(self.connection, legacy, 'daily')
        recover_pending(self.connection)
        separate = {
            'suppliers': [{'supplier': 'legacy-s', 'region': '华东'}],
            'receipts': [{'receipt': 'legacy-r', 'supplier': 'legacy-s',
                          'day': '2026-10-10', 'amount': '3.00'}],
        }
        self.assertTrue(load_bundle(self.connection, separate, 'daily'))
        self.assertEqual(
            self.connection.execute("SELECT state FROM dim_versions WHERE version='batch:daily'").fetchone()[0],
            'applied')


if __name__ == '__main__':
    unittest.main()

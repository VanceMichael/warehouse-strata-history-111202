import argparse
import json
from pathlib import Path

from .catalog import connect, load_file
from .domain import overview
from .store import ObjectStore
from .temporal import (
    apply_version_bundle,
    as_known_receipts,
    as_known_totals,
    list_versions,
    recover_pending,
    snapshot_receipts,
    snapshot_totals,
)

def _load_version_input(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def main():
    parser = argparse.ArgumentParser(description='本地数据集合装载与双时间维度修订')
    parser.add_argument('command', choices=[
        'import', 'revise', 'recover', 'inspect', 'history', 'versions', 'export',
    ])
    parser.add_argument('--db', required=True)
    parser.add_argument('--input')
    parser.add_argument('--batch')
    parser.add_argument('--objects')
    parser.add_argument('--version', help='版本号：查询该版本下维度与事实的一致快照')
    parser.add_argument('--known-at', help='知悉时间：查询当时已知口径（ISO-8601）')
    parser.add_argument('--detail', action='store_true', help='history 输出到货明细而非区域汇总')
    args = parser.parse_args()
    if args.command in ('import', 'revise') and (not args.input or not args.batch):
        parser.error(f'{args.command} 需要 --input 和 --batch')
    if args.command == 'export' and not args.objects:
        parser.error('导出需要 --objects')
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    connection = connect(args.db)
    try:
        if args.command == 'import':
            result = {'changed': load_file(connection, args.input, args.batch)}
        elif args.command == 'revise':
            bundle = _load_version_input(args.input)
            changed, sequence = apply_version_bundle(connection, bundle)
            result = {'changed': changed, 'sequence': sequence,
                      'version': bundle['source_version']}
        elif args.command == 'recover':
            sequence = recover_pending(connection)
            result = {'recovered': sequence is not None, 'sequence': sequence}
        elif args.command == 'inspect':
            result = overview(connection)
        elif args.command == 'versions':
            result = list_versions(connection)
        elif args.command == 'history':
            if args.known_at and args.detail:
                result = as_known_receipts(connection, args.known_at)
            elif args.known_at:
                result = as_known_totals(connection, args.known_at)
            elif args.detail:
                result = snapshot_receipts(connection, args.version)
            else:
                result = snapshot_totals(connection, args.version)
        else:
            if args.known_at:
                records = as_known_totals(connection, args.known_at)
            else:
                records = snapshot_totals(connection, args.version)
            result = {'object': ObjectStore(args.objects).put(records), 'rows': len(records)}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
    finally:
        connection.close()


if __name__ == '__main__':
    main()

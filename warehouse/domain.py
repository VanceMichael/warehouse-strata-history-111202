from collections import defaultdict
from decimal import Decimal


def overview(connection):
    """现在认可的历史口径：按当前开放成员汇总到货金额。

    输出形状与金额文本口径保持与旧版一致；事实只追加不覆盖，
    历史版本请使用 warehouse.temporal.snapshot_totals 按版本号查询。
    """
    rows = [
        dict(record)
        for record in connection.execute(
            'SELECT m.region AS region, r.amount AS amount'
            ' FROM receipts r'
            ' JOIN receipt_assignments a ON a.receipt = r.receipt'
            ' JOIN dim_members m ON m.member_key = a.member_key'
            ' ORDER BY r.receipt'
        )
    ]
    totals = defaultdict(lambda: Decimal('0'))
    for record in rows:
        value = Decimal(record['amount'])
        if not value.is_finite():
            raise ValueError('数量必须为有限十进制')
        totals[record['region']] += value
    return [{'region': region, 'amount': str(totals[region])} for region in sorted(totals)]

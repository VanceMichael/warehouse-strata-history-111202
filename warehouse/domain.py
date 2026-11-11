from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal


def overview(connection):
    rows = [dict(record) for record in connection.execute("SELECT COALESCE(s.region, '未知') AS region, r.amount FROM receipts r LEFT JOIN suppliers s ON r.supplier=s.supplier ORDER BY r.receipt")]
    totals = defaultdict(lambda: Decimal("0"))
    for record in rows:
        key = tuple(record[field] for field in ['region'])
        value = Decimal(record['amount'])
        if not value.is_finite():
            raise ValueError("数量必须为有限十进制")
        totals[key] += value
    return [dict(zip(['region'], key), **{'amount': str(value)}) for key, value in sorted(totals.items())]

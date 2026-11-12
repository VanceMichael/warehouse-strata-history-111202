SCHEMA = {
    'suppliers': 'supplier TEXT PRIMARY KEY, region TEXT NOT NULL',
    'receipts': 'receipt TEXT PRIMARY KEY, supplier TEXT NOT NULL, day TEXT NOT NULL, amount TEXT NOT NULL',
}

# 双时间维度修订的关系结构。业务时间轴用 valid_from/valid_to（左闭右开），
# 系统知悉时间轴用 tx_from/tx_to，并以 seq_from/seq_to 保存版本序号便于快照查询。
TEMPORAL_TABLES = {
    'dim_versions': '''
        CREATE TABLE IF NOT EXISTS dim_versions (
            version TEXT PRIMARY KEY,
            digest TEXT NOT NULL,
            received_at TEXT NOT NULL,
            applied_sequence INTEGER UNIQUE,
            retracts TEXT NOT NULL DEFAULT '',
            receipt_count INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL CHECK (state IN ('pending', 'applied', 'retracted')),
            payload TEXT NOT NULL
        )
    ''',
    'dim_members': '''
        CREATE TABLE IF NOT EXISTS dim_members (
            member_key INTEGER PRIMARY KEY,
            supplier TEXT NOT NULL,
            region TEXT NOT NULL,
            source_version TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            valid_to TEXT NOT NULL,
            tx_from TEXT NOT NULL,
            tx_to TEXT NOT NULL,
            seq_from INTEGER NOT NULL,
            seq_to INTEGER NOT NULL
        )
    ''',
    'dim_member_index': '''
        CREATE INDEX IF NOT EXISTS idx_dim_members_supplier_open
        ON dim_members (supplier, seq_to, valid_from)
    ''',
    'receipt_origins': '''
        CREATE TABLE IF NOT EXISTS receipt_origins (
            receipt TEXT PRIMARY KEY,
            source_version TEXT NOT NULL,
            received_at TEXT NOT NULL
        )
    ''',
    'fact_links': '''
        CREATE TABLE IF NOT EXISTS fact_links (
            receipt TEXT NOT NULL,
            member_key INTEGER NOT NULL,
            source_version TEXT NOT NULL,
            received_at TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            PRIMARY KEY (receipt, sequence)
        )
    ''',
    'receipt_assignments': '''
        CREATE TABLE IF NOT EXISTS receipt_assignments (
            receipt TEXT PRIMARY KEY,
            member_key INTEGER NOT NULL,
            sequence INTEGER NOT NULL
        )
    ''',
}

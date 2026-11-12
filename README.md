# strata-history

供应商区域维度的双时间（业务时间 + 系统知悉时间）修订仓库：到货金额按**当时已知口径**与**现在认可的历史口径**都可追溯，维度修订只追加历史，不覆盖老记录，上周导出的报表不会悄悄变数。

## 数据模型

旧式当前态表保持不变：

- `suppliers`：`supplier TEXT PRIMARY KEY, region TEXT NOT NULL`
- `receipts`：`receipt TEXT PRIMARY KEY, supplier TEXT NOT NULL, day TEXT NOT NULL, amount TEXT NOT NULL`

双时间修订结构（定义在 `warehouse/definition.py`，结构说明在 `schema.json`）：

- `dim_versions`：来源版本台账。带供应商业务键的版本号、规范化内容摘要、接收时间（知悉时间）、应用序号、撤销目标与 `pending/applied/retracted` 状态；版本包原文存于 `payload`，供崩溃恢复重放。
- `dim_members`：维度成员（代理键 `member_key`）。每条带 `source_version`、业务区间 `valid_from/valid_to`（**左闭右开**）、知悉区间 `tx_from/tx_to` 与序号区间 `seq_from/seq_to`。旧成员只关闭不删除，形成 SCD2 历史。
- `fact_links`：到货事实链，按 `(receipt, sequence)` 只追加。事实通过成员代理键关联维度，重挂即新增一条链，历史链永久保留。
- `receipt_assignments`：每个到货当前认可的成员，供当前视角直接查询。
- `receipt_origins`：到货首次进入仓库的版本与接收时间。
- 固定的未知成员 `member_key=1`：未匹配名录的到货先落未知成员，迟到补档后再重挂。

## 修订与重挂规则

- 版本包字段：`source_version`、`received_at`（带时区的 ISO-8601）、`dimensions[]`（`supplier/region/valid_from/valid_to`，日期左闭右开）、`receipts[]`、`retract_versions[]`。
- **幂等与冲突**：同一来源版本号重复到达且内容一致，不产生新历史（无新成员、无新事实链）；同版本号内容不一致直接拒绝整批。
- **同一生效日优先规则**：多个版本在同一天生效时，版本应用序号大（后知悉）者优先，序号相同再比接收时间；纯函数 `build_timeline` 可单测。
- **区间拆分**：新断言可插入已有区间中部，时间线被折叠成互不相交的左闭右开线段；相邻且区域、来源版本相同的线段合并。
- **撤销**：`retract_versions` 把先前版本标记为 `retracted`，其断言退出认可集合，时间线回退到剩余版本（更早的口径重新生效）；被撤销版本的历史快照仍可复现。
- **影响范围**：只重建受影响供应商的开放成员、只重挂受影响供应商的事实，外加当前挂在未知成员上的到货（迟到补档场景）。未涉及供应商的事实不整表重挂，其 `receipt_assignments.sequence` 保持不变。
- **两个时间维度**：业务时间轴回答“区域在业务上哪天生效”，知悉时间轴回答“仓库哪天知道这件事”。
- **崩溃安全**：应用分两阶段——先在独立事务暂存版本与原始事实（`pending`），再在一个事务内完成维度重建与事实重挂（`applied`）。重挂中途异常退出后，`pending` 版本之外的旧版本快照仍可正常读取；`recover` 重放暂存原文，事实链按主键去重只追加，恢复不重复计数。存在未完成 `pending` 时拒绝写入新版本。

## 查询入口（两种历史视角）

- 版本一致快照：`history --version v3`，用一个版本号同时拿到该版本下维度与事实一致的结果；不带 `--version` 即最新认可口径。
- 当时已知视角：`history --known-at 2026-11-06T00:00:00+00:00`，只使用该知悉时刻之前已到达的版本。
- 当前认可视角：`inspect`（金额文本口径与旧版完全一致，区域汇总仍为十进制文本）。
- `history --detail` 输出到货明细及其代理键、区域、来源版本；`versions` 输出版本台账；`export` 把任一视角写入内容寻址对象存储。

## 输入样例

- `examples/source.json`：旧式名录/到货合成输入，经 `import` 装载时自动投影为一条覆盖全部业务时间的初始版本（`batch:<批次>`），旧流程与旧测试保持兼容。
- `examples/revisions/v1..v6*.json`：版本化样例，依次覆盖初次装载、十月更正、区间中部插入、迟到补档、撤销更正、失败恢复；`examples/revisions/expected.json` 是各版本快照与知悉视角的公开期望结果。

## 环境与验证

Python 3.11 以上，仅使用标准库，无第三方安装。数据库和对象目录均由入口参数指定；合成数据目录与仓库源码分离，读取样例的脚本通过路径参数传入。

```bash
python3 -m compileall -q warehouse tests scripts
python3 -m unittest discover -s tests -v
```

## 运行

旧式装载往返：

```bash
python3 scripts/demo.py
```

双时间修订全流程（区间拆分、迟到补档、撤销、失败恢复、两个查询视角）：

```bash
python3 -I scripts/demo_revisions.py
```

分步调用：

```bash
python3 -m warehouse import  --db /tmp/wh.sqlite3 --input examples/source.json --batch daily
python3 -m warehouse revise  --db /tmp/wh.sqlite3 --input examples/revisions/v2_october_correction.json --batch v2
python3 -m warehouse history --db /tmp/wh.sqlite3 --version v2
python3 -m warehouse history --db /tmp/wh.sqlite3 --known-at 2026-11-06T00:00:00+00:00
python3 -m warehouse versions --db /tmp/wh.sqlite3
python3 -m warehouse recover  --db /tmp/wh.sqlite3
python3 -m warehouse inspect  --db /tmp/wh.sqlite3
python3 -m warehouse export   --db /tmp/wh.sqlite3 --objects /tmp/objects --version v3
```

输入批次以内容 SHA-256 固定；相同批次重投不重复写入，相同批次内容不同则报错。装载以完整输入包为事务，主键冲突不留下半批记录。关系数据保存在 SQLite，精确数量用十进制文本；导出对象按内容哈希命名，读取会重新校验，来源不在运行时修改。手工重复使用数据库路径时应留意已存在的业务主键。

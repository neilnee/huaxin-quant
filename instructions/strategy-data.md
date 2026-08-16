# 策略数据存储

- **主库**：`cache/strategy/strategy_data.sqlite`。
- **职责**：保存 Quant 全量结构快照、Signal Plan、Bloom 日快照与事件、实际买点事件，以及买点后的逐交易日生命周期。
- **边界**：该层只持久化已有确定性结果，不重新判断 VCP、不修改买点、信号、交易或仓位规则。

## 存储契约

策略数据库是上述核心数据的权威来源。每日模块必须先完成数据库事务，再从已提交的数据生成兼容 JSON、CSV、Markdown 和 Dashboard 数据包；下游计算优先读数据库。文件继续保留，用于人工阅读、页面发布和外部兼容，不作为可变状态的唯一账本。

同一模块、交易日和内容哈希重复写入必须幂等；同日结果发生变化时保留文档修订，并由 `current_documents` 指向当前版本。结构化表保存查询字段，`payload_json` 保存完整原始契约，禁止迁移时重新计算历史策略结果。

## 初始化与历史迁移

```bash
python3 scripts/init_runtime.py
python3 scripts/migrate_strategy_data.py --dry-run
python3 scripts/migrate_strategy_data.py
```

迁移读取现有 Quant、Signal Plan、Bloom 状态输入与事件文件，并按既有 Plan 兑现规则重建实际买点事件。脚本可重复执行，记录数量不得因重复迁移而增长。

## 文件重建

兼容文件缺失或需要校验时，从数据库重建指定日期：

```bash
python3 scripts/strategy_publish.py all --date <YYMMDD>
```

可单独发布 `quant`、`signal-plan` 或 `bloom`。发布器不得重新运行筛选或信号规则。

## 生命周期观察

实际买点以兑现日收盘价为参考入场价，以 Plan 冻结的失效价定义初始风险 `1R`，从下一交易日起保存 OHLC、当前收益、MFE、MAE、1R/2R/3R 首次触达、失效触碰/确认和超时状态。该数据仅用于交易后研究，不反向改变 VCP、Plan 或买点判定。

# CLAUDE.md

## 项目概要

量化实验室（quant_lab），多模型流水线的股票筛选系统。每个筛选模型对应 `instructions/` 下的一个指令文件，由 LLM 读取后自动执行。

## 目录约定

```
instructions/       - 各模型的执行指令（markdown，LLM 可直接执行；历史由 Git 管理）
scripts/            - 辅助 Python 脚本（数据处理、格式转换等），统一放此处
pool/               - 模型一（海选初筛）输出
quant/              - 模型二（量价精筛）输出
reports/            - 模型三（深度估值）报告 + _index.csv + _ranking.csv
signals/            - 模型四（择时跟踪）工作区：每日信号报告 + core_pool.csv + positions.csv + batches.csv + trade_log.csv
cache/              - 所有缓存数据，脚本自动管理
  daily/            - 模型二+四共享日线缓存（<code>_<YYMMDD>.pkl），keep_days=5
  xuangu/           - 模型一段数据缓存（5天有效期，季度财报无需每日重拉）
  financial/        - 模型三财报缓存（_raw.json 保留，.xlsx/.txt 自动清理），90天超期警告
  briefing/         - 模型三简报册缓存（文件名含日期），--no-cache 清除旧简报
  calc_params/      - 模型三估值引擎输入参数 JSON（<code>_<YYMMDD>.json），按需保留
  calc_results/     - 模型三估值引擎输出结果 JSON（<code>_<YYMMDD>.json），按需保留
  research/         - mx-search 研报搜索结果缓存，保留供 LLM 引用
  zixuan/           - 自选股同步本地缓存（zixuan.csv），记录系统管理的自选股列表
refer/              - 参考资料（策略文档、研究 PDF、截图等），非执行文件
dev_logs/           - 开发复盘日志
SIGNALS.md          - 最新信号报告的根目录快捷副本，与 signals/ 中最新报告保持同步，方便快速打开
tmp_*/              - 其他临时目录，用完即删
```

- **脚本统一放 `scripts/`**，不散落在项目根目录
- **指令文件是源头**，脚本是指令的配套实现。改逻辑先改指令，再改脚本
- **缓存数据统一放 `cache/`**，由脚本自动创建和管理，无需手动维护
- **缓存刷新策略**：
  - `cache/xuangu/`（模型一）：`process_pool.py` 只加载 5 天内的文件，超期自动跳过。数据以财报指标为主（季度更新），无需每日重拉
  - `cache/daily/`（模型二+四共享）：文件名含日期（`<code>_<YYMMDD>.pkl`），跨天自动不命中。两个脚本共用同一缓存目录，每次运行均清理超过 5 天的旧文件
  - `cache/financial/`（模型三）：`valuate.py` 检测超过 90 天的财报缓存并警告，自动清理无用 .xlsx/.txt 文件（仅保留 _raw.json）。同一报告期内数据不变
  - `cache/briefing/`（模型三）：文件名含日期，`--no-cache` 清除旧简报后重新生成
- **脚本与指令同提交更新**，规则变更时同步修改指令卡和对应脚本；历史版本由 Git 追溯
- **数据口径统一**：模型一（process_pool.py）和模型三（valuate.py）使用相同报告期数据。模型一取 LATEST 并做周期感知阈值调整，模型三取最新可用数据不做年份限定。避免跨模型数据口径不一致导致"模型一通过→模型三发现亏损"的矛盾
- **指令卡保持精简**：指令文件只保留 LLM 执行所需的内容（流程、规则、约束）。公式速查、报告模板、字段定义等参考内容放入配对文件 `*-ref.md`，执行时不加载，按需查阅。版本历史由 Git 管理，踩坑记录、开发复盘写入 `dev_logs/`
- **固定文件名约定**：活跃指令卡只保留 `01-pool.md`、`02-quant.md`、`03-valuation.md`、`03-valuation-ref.md`、`04-tracker.md`、`04-tracker-ref.md`。不再新建 `-dev`、`-vX.Y` 或 `archive/` 指令副本
- **分支开发约定**：较大改动在 Git feature 分支上直接修改活跃文件；稳定后通过 commit/merge 保留历史，不靠复制文件发版
- **收尾更新**：每次完成一组规则变更后，更新 `TODO.md`，勾掉已完成项并补充新识别的后续待办
- **数据输出规范**：模型输出放对应目录（`pool/`、`quant/`、`reports/`），CSV 编码 UTF-8 BOM，股票代码公式文本 `="000001"`，数值保留 2 位小数
- **自选股监控池**：东方财富自选股的"全部"分组作为监控池权威列表。mx-zixuan 增删操作仅对"全部"分组生效。入池=add，出池=delete，手机APP可随时查看
- **Bash 授权避坑**：`Bash(python3 << *)` 匹配不了多行 heredoc，会反复弹授权。创建 JSON/数据文件用 `Write` 工具，再直接 `Bash(python3 scripts/xxx.py)` 跑脚本。单行 `python3 -c "..."` 也会弹授权且无法预授权，避免使用
- **Agent 执行后清理**：mx-search 等 skill 在 Agent 并行执行时可能在项目根目录遗留临时目录（如 `tmp_凯格精机/`），每次批量估值或搜索完成后需清理这些临时目录

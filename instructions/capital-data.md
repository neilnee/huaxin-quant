# 资金观测数据层（Capital Data）

- **核心目标**：统一获取并缓存财富通行业板块资金、个股订单流和两融数据，向上层提供申万行业与股票代码口径的稳定接口。
- **边界**：只负责数据获取、行业映射、字段标准化、缓存和质量校验；不判断资金生命周期，不修改 VCP、Bloom 或市场状态。
- **本地主键**：行业始终使用现有申万代码；财富通 `BK` 代码只在数据层内部出现。
- **主入口**：`scripts/data/capital_data_service.py`。
- **策略配置**：`strategies/capital-data.json`。

## 数据来源

- 申万行业及成分：共享市场数据库 `cache/market_data/market_data.sqlite`。
- 财富通行业目录及成分：东方财富板块行情接口；只获取行业板块，不混入概念、地域或风格板块。
- 板块资金、个股订单流与两融：妙想查询接口。

财富通目录和成分获取失败时，不得删除或覆盖最近一次完整快照。妙想返回实体、日期或必需字段不符合请求契约时，本次结果不得写入正式缓存。

## 映射规则

首次调用 `ensure_sector_mapping()` 且本地不存在完整映射时，按层级建立申万一级/二级到财富通一级/二级行业的映射。已有映射默认直接复用；只有显式传入 `force_rebuild=True` 才重建指定层级。

映射以同日成分股重合为准，名称只用于解释，不作为决定性证据。权重使用截至快照日最近 20 个交易日的个股平均成交额。默认同层映射：申万一级对应财富通一级，申万二级对应财富通二级。候选财富通行业的纯度必须不低于 35%，避免用覆盖范围过宽的板块资金代表申万行业；允许一个申万行业映射到最多三个合格的同层财富通行业。低质量映射必须返回明确状态，不得勉强生成板块资金结论，业务层应降级到个股资金验证。

映射版本必须保存申万快照日期、财富通快照日期、覆盖率、纯度、核心股覆盖率、权重、质量和创建时间。历史版本不得被覆盖。

## 查询接口

```python
service.ensure_sector_mapping(as_of=None, sw_level=1, force_rebuild=False)
service.fetch_sector_capital(sw_code, start_date, end_date, refresh=False)
service.fetch_stock_capital(codes, start_date, end_date, refresh=False)
```

`fetch_sector_capital()` 内部解析申万到财富通映射，调用妙想并返回映射明细和标准化日度数据。`fetch_stock_capital()` 将主力订单流和两融字段合并请求，每批最多五只；必须检查返回代码集合并补查被静默遗漏的证券。

默认只补本地缺失日期。`refresh=True` 允许重取目标区间，但不得改变映射版本；映射重建与资金数据刷新是两个独立动作。

## 批处理与额度

批量任务统一由 `scripts/capital_data.py` 执行。映射建立只访问东方财富板块接口，妙想请求数为零；板块资金和个股资金补数必须设置单次运行请求预算，达到预算后保存已有结果并正常停止，依靠缓存下次续跑。

```bash
python3 scripts/capital_data.py mapping-build --levels 1,2
python3 scripts/capital_data.py mapping-status --levels 1,2
python3 scripts/capital_data.py fetch --date 2026-08-07 --sw-codes X30,X3001 --stock-codes 300750 --dry-run
python3 scripts/capital_data.py fetch --date 2026-08-07 --sw-codes X30 --stock-codes 300750 --max-mx-requests 8
```

脚本默认缓存优先、可重复执行。不得通过多进程或并发请求绕过额度；单次运行预算只允许调低，超过策略配置硬上限时拒绝启动。

## 持久化

```text
cache/capital_flow/capital_data.sqlite
```

缓存保存财富通行业快照、成分股、映射版本、妙想字段契约、板块资金和个股资金。缺失值使用 `null`，不得把未覆盖、未披露或接口失败写成零。两融数据可能较主力订单流滞后一个交易日，必须保留各自行日期。

具体字段、映射公式和返回结构见 `instructions/capital-data-ref.md`。

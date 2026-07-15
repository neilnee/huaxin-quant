# 自选股同步指令卡

- **所属模块**: 模型四 Tracker
- **数据源**: Bloom 状态 + 东方财富妙想自选管理 Skill
- **核心目标**: 每日用 Bloom 的重点观察和已触发买点刷新系统管理的自选股，不触碰用户手工维护的其他自选股。

---

## 一、输入与输出

权威输入：

```text
bloom/state/bloom_state.csv
```

本地状态：

```text
cache/zixuan/target_watchlist.csv
cache/zixuan/managed_watchlist.csv（旧版迁移来源，仅首次使用）
```

- `target_watchlist.csv` 是系统受管账本：最近一次成功同步后，由工作流添加且未来有权删除的股票及其入选原因。
- 首次升级时，若 `target_watchlist.csv` 不存在，读取旧版 `managed_watchlist.csv` 作为一次性迁移来源。
- 两张表均为本地运行数据，不纳入 Git。

---

## 二、目标集合

Bloom 日报“重点观察”口径是目标集合的唯一标准，满足以下任一条件即入选：

```text
model2_stage=VCP_TIGHT 或 VCP_MATURE
model2_stage=VCP_FORMING 或 VCP_EARLY，且 structure_score >= 60
model2_setup_signal=PULLBACK_BUY / BREAKOUT_BUY / RETEST_BUY
bloom_status=TRIGGERED
```

三类买点和 `TRIGGERED` 不受结构分门槛限制，必须入选。`pool_decision=KEEP_FOCUS` 本身不是入选条件，避免低分 FORMING 标的进入自选列表。

---

## 三、同步规则

每次运行按“删除旧受管 → 添加当日目标 → 更新账本”执行。全程不查询东方财富“全部”自选列表：删除决策只读取本地受管账本，接口调用结果是本次执行的唯一反馈。

### 删除旧受管

- 对 `target_watchlist.csv` 中的上一日受管股票逐只调用删除接口。
- 不在本地受管账本中的股票不得删除；用户手工添加的其他自选股保持不变。
- 上一日受管且今日仍是目标的股票也先删除再重新添加，确保每日刷新后本地账本只对应当天工作流结果。

### 添加当日目标

- 对当日目标集合按重点排序的反向顺序逐只调用添加接口：低权重标的先添加，高权重标的后添加，使高权重标的最终更靠前。
- 重点排序从高到低为：已触发买点 > VCP_TIGHT > VCP_MATURE > FORMING / EARLY（按结构分降序）；同级按代码排序。
- 添加成功后写入受管账本。
- 如果添加接口提示该股票已存在或调用失败，不接管删除权，并由下一次工作流重新尝试添加。

### 账本更新

- 删除成功的旧受管股票从账本移除；删除失败的旧股票留在账本，确保下一次仍会重试删除。
- 添加成功的当日目标写入账本；添加失败的目标不写入账本，确保下一次仍会重试添加。
- 任何接口调用失败都返回非 0，让 daily 工作流明确记录失败；不再依赖不稳定的远端全量查询作校验。

---

## 四、安全规则

- 每日工作流是否执行同步仅由 `.env` 的 `ENABLE_ZIXUAN_SYNC` 决定；仅 `true` / `1` / `yes` / `on` 启用，未设置或其他值均跳过。
- 手动执行默认交互确认；每日工作流使用 `--yes` 非交互执行。
- `--dry-run` 只查询和展示差异，不调用增删接口，也不改写本地缓存。
- 目标集合为空时必须终止，绝不删除本地受管股票。
- Bloom 状态文件缺失、字段缺失时立即终止。
- 股票代码统一规范为 6 位数字字符串。
- API Key 只从 `MX_APIKEY` 环境变量读取，不写入日志或缓存。

---

## 五、命令

```bash
python3 scripts/sync_zixuan.py --date 260714 --dry-run
python3 scripts/sync_zixuan.py --date 260714
python3 scripts/sync_zixuan.py --date 260714 --yes
```

每日工作流启用配置：

```bash
# .env
ENABLE_ZIXUAN_SYNC=true
```

---

## 六、验收标准

- 脚本只操作本地账本中有删除权限的股票，不读取或清空远端“全部”列表。
- 低分 FORMING / EARLY 且无买点的标的不得进入自选列表。
- Bloom 目标为空时不得进行删除或添加。
- 每日工作流仅在 Tracker 成功后执行此步骤。
- dry-run 不改变远端或本地状态。

# 模型二：量价精筛模型（自执行指令）

- **版本管理**: 由 Git 分支与提交历史管理，文件名不再携带版本号
- **最近更新**: 2026-07-03
- **核心目标**: 在模型一基本面候选池中，寻找 VCP 蓄力结构及 P2/P3 买点状态，输出可复现、可回测、可供模型三/四复用的结构化量价结果。
- **核心哲学**: 基本面先过滤烂公司，模型二只判断资金行为和价格位置。脚本负责确定性计算，LLM 只做可选解释，不参与 P1/P2/P3 命中判定。
- **输入**: `pool/pool_<YYMMDD>.csv`，或命令行指定 `--code/--codes`
- **输出**: `quant/quant_<YYMMDD>.csv` + `cache/quant_runs/quant_<YYMMDD>.json`
- **配套脚本**: `scripts/quant_filter.py`

---

## 一、执行架构

模型二必须由脚本驱动，不能依赖对话上下文逐步执行。

```text
读取输入标的
→ 拉取/读取近 200 个交易日日线
→ 计算技术指标
→ 确定性识别 P1/P2/P3/风险状态
→ 计算 setup_score 与 risk_score
→ 输出 CSV + JSON
→ 可选调用 LLM 解释 Top N 或指定个股
```

### CLI

```bash
python3 scripts/quant_filter.py
python3 scripts/quant_filter.py --pool pool/pool_260703.csv
python3 scripts/quant_filter.py --code 300604 --name 长川科技
python3 scripts/quant_filter.py --codes 300604,300442
python3 scripts/quant_filter.py --code 300604 --with-llm
python3 scripts/quant_filter.py --code 300604 --json
```

### 职责边界

| 模块 | 职责 |
|------|------|
| 脚本 | 数据、指标、形态、评分、排序、输出 |
| LLM | 可选解释、异常复核、观察建议 |
| 模型三 | 估值锚点，判断贵便宜 |
| 模型四 | 结合估值和持仓执行交易动作 |

LLM 失败不能影响主流程。默认不调用 LLM。

---

## 二、核心模型注释

> 这一节是交易模型的注释性说明，便于人工查看。脚本实现必须以本节定义为准。

### P1：VCP 蓄力结构

P1 是观察对象识别，不是买点。

它寻找的是：股票已经有资金认可，随后进入波动收敛、成交量下降、抛压变轻的阶段。

```text
第一段上涨或修复：资金开始参与
第一次回撤：幅度较大但趋势未坏
第二次回撤：幅度变小，成交量下降
第三次整理：波动更窄，成交量更低
```

P1 的交易含义：

- 不是长期趴着不动。
- 不是短期情绪过热。
- 筹码正在稳定。
- 后续需要等待 P2 或 P3。

### P2：结构内缩量回踩低吸

P2 是 VCP 未突破前的低吸机会，适合轻仓试探。

```text
P1 已经成立
股价缩量回踩 MA20/MA60/收敛下沿
前低不破
均线不明显下行
价格没有过热
```

P2 买的是风险收益比。价格较低，失效位清楚，但突破尚未确认，确定性低于 P3。

仓位建议：

```text
20%-30%
```

### P3：突破后回踩确认

P3 是 VCP 突破后的确认买点。

```text
先放量突破箱体上沿/收敛上沿/近 60 日高点
随后 3-10 个交易日内缩量回踩
回踩不有效跌破突破位
重新站回突破位或 MA10/MA20
```

P3 买的是确定性。价格通常高于 P2，但突破已经发生并经过回踩验证。

仓位建议：

```text
加至 60%-80%
```

### P2/P3 仓位路径

```text
P2 买入 20%-30%
→ 若直接上涨但不给 P3：不追满，只持有已有仓位
→ 若突破后回踩确认：P3 加至 60%-80%
→ 若跌破失效位：减仓或退出
```

---

## 三、数据与缓存

每只股票拉取近 200 个交易日：

```text
date, open, high, low, close, volume, turnover
```

缓存目录：

```text
cache/daily/<code>_<YYMMDD>.pkl
```

缓存只保存原始日线，不保存指标列。指标每次实时计算，避免规则变更后旧缓存污染。

---

## 四、指标体系

### 均线与趋势

```text
MA5, MA10, MA20, MA60, MA120
MA20_slope, MA60_slope, MA120_slope
```

斜率用最近 N 日均线做线性回归，转成每日百分比。

### 波动收敛

```text
range_10 = (high_10 - low_10) / low_10 × 100
range_20
range_60
ATR14_pct
```

VCP 关注：

```text
range_10 < range_20 < range_60
ATR14_pct 处于下降或低位
最近回撤小于前一次回撤
```

### 量能

```text
vol_ma5, vol_ma20, vol_ma60
量比 = volume / vol_ma5.shift(1)
volume_dry_up = vol_ma5 / vol_ma20
volume_dry_up_60 = vol_ma10 / vol_ma60
```

### 价格位置

```text
distance_ma20
distance_ma60
distance_high_60
distance_low_60
chg_5
chg_20
chg_60
```

### 风险识别

```text
过热：短期涨幅过大、距离 MA20 过远
长上影：放量冲高回落
放量滞涨：量放大但价格不涨
趋势破坏：MA20 明显下行或跌破关键支撑
假突破：突破后跌回箱体
```

---

## 五、确定性规则

### P1：VCP 结构

基础条件：

```text
有效交易日 >= 60
close > MA60 或 MA20 >= MA60
MA60_slope >= -0.03%/日
近 60 日最大回撤不超过 35%
```

收敛条件，满足至少 3 项：

```text
range_10 < range_20
range_20 < range_60
volume_dry_up < 0.85
vol_ma20 < vol_ma60
近 20 日低点高于近 60 日低点
距离 60 日高点不低于 -15%
```

P1 状态：

| state | 说明 |
|-------|------|
| P1_FORMING | VCP 形成中 |
| P1_TIGHT | 收敛明显，接近突破区 |
| P1_HIGH | 强趋势偏高，等待回踩或收敛 |

### P2：结构内缩量回踩

必须先有 P1。

```text
volume_dry_up < 0.80
distance_ma20 在 [-4%, +3%]，或 distance_ma60 在 [-5%, +5%]
close > 近 20 日最低价 × 1.03
MA20_slope >= -0.03%/日
近 5 日涨幅 < 12%
无放量长阴
```

### P3：突破后回踩确认

突破识别：

```text
breakout_level = 最近 60 日箱体上沿/突破前高
突破日收盘价 > breakout_level × 1.01
突破日成交量 > vol_ma20 × 1.5
突破日无明显长上影
```

回踩确认：

```text
突破后 3-10 个交易日内
回踩低点 >= breakout_level × 0.97
回踩期缩量
最新收盘重新站回 breakout_level 或 MA10
```

---

## 六、评分与分层

```text
setup_score = structure_score
            + volume_score
            + trend_score
            + position_score
            + buy_point_score
            - risk_penalty
```

| 分数 | pool_type | 用途 |
|------|-----------|------|
| >= 80 | TRADE_CANDIDATE | P2/P3 高质量候选 |
| 65-79 | RESEARCH_WATCH | 值得估值或观察 |
| 50-64 | LOW_PRIORITY | 保留记录 |
| < 50 | REJECT | 不进入主列表 |

买点优先级：

```text
P3_RETEST > P2_PULLBACK > P1_TIGHT > P1_FORMING > P1_HIGH > REJECT
```

---

## 七、输出字段

CSV 和 JSON 至少包含：

```text
股票代码
股票名称
pattern
state
pool_type
setup_score
risk_score
action_hint
suggested_position
support_price
invalid_price
breakout_level
close
MA20 / MA60 / MA120
MA20_slope / MA60_slope
range_10 / range_20 / range_60
volume_dry_up
distance_ma20 / distance_ma60 / distance_high_60
reason
risk_flags
run_date
```

单股模式必须打印终端摘要，并同样写入 JSON。

---

## 八、DeepSeek LLM 可选解释

`--with-llm` 仅做解释，不做判定。

脚本从环境变量或本地 `.env` 读取配置：

```text
DEEPSEEK_API_KEY=...
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_BASE_URL=https://api.deepseek.com
```

本地 `.env` 不进入 Git。默认模型使用 `deepseek-v4-flash`。

DeepSeek 调用使用 OpenAI 兼容的 Chat Completions 接口，并启用 JSON Output：

```text
POST https://api.deepseek.com/chat/completions
response_format = {"type": "json_object"}
```

LLM 输入是脚本生成的结构化结果；输出必须为 JSON：

```json
{
  "code": "300604",
  "pattern_review": "强趋势偏高，暂未形成标准 VCP",
  "risk_notes": ["距离 MA20 偏远"],
  "watch_points": ["等待缩量回踩 MA20", "观察是否形成收敛区间"],
  "confidence": "medium"
}
```

若 LLM 失败：

```text
主流程继续
CSV/JSON 照常输出
llm_status = failed
```

---

## 九、验收标准

- 长川科技这类强基本面但技术偏高的标的，应识别为 `P1_HIGH` 或观察状态，而不是 P2/P3。
- P2 必须依赖 P1，不能变成下跌趋势抄底。
- P3 必须是突破后的回踩确认，不能变成突破当天追涨。
- 过热、放量滞涨、长上影等风险必须进入 `risk_flags`。
- 脚本可批量运行，也可 `--code` 单股运行。
- 所有核心判断可从 CSV/JSON 中复盘，不依赖对话上下文。

待优化项见 `TODO.md`。历史版本由 Git 追溯，复盘记录见 `dev_logs/`。

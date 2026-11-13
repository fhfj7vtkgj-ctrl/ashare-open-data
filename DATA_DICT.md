# 数据字典

## data/daily/

两种分片，列完全相同：

- `data/daily/YYYY.csv.gz`：**历史年份**（完整、不再重写）。
- `data/daily/YYYY/YYYY-MM-DD.csv.gz`：**当年**，一个交易日一个文件，每天只新增一个。

| 列 | 类型 | 说明 |
|---|---|---|
| symbol | str | 6 位代码（60/68 沪市，00/30 深市） |
| date | date | 交易日 |
| open/high/low/close | float | 未复权价，4 位小数 |
| volume | int | 成交量（股） |
| amount | float | 成交额（元）。原始字段大面积为空，此处为 `COALESCE(amount, volume*close)`，并对超过 5000 亿的脏值置空 |
| adj_factor | float | 复权因子，缺失较多，谨慎使用 |

> 注意：`close` 为未复权价，跨越除权日会出现价格跳变。做长周期收益计算前需自行复权。

## data/minute/5m/YYYY-MM-DD.csv.gz | data/minute/1m/YYYY-MM-DD.csv.gz

| 列 | 说明 |
|---|---|
| symbol | 6 位代码 |
| ts | 北京时间 `YYYY-MM-DD HH:MM:SS`，为该 bar 的**结束**时刻 |
| open/high/low/close | bar 内价格 |
| volume | 成交量（股） |
| amount | 成交额（元），5m 完整，1m 部分缺失 |

## data/paper/equity_daily.csv

模拟盘每日收盘净值快照。`equity = cash + margin_used + unrealized_pnl`。
`positions` 为当日持仓的 JSON 快照。

## data/paper/trades.csv

模拟盘成交明细。`quantity` 单位为**手**（1 手 = 100 股），`price` 为成交价，
`commission` 为佣金（元），`realized_pnl` 为平仓已实现盈亏（元）。
`trade_time_bj` 为北京时间。

## data/paper/orders.csv

全部委托，含被拒绝的。`status` 为 `FILLED` / `REJECTED` / `PENDING` 等，
`reject_reason` 记录风控拒绝原因（如 `STOP_LOSS`、`DRAWDOWN_LIMIT`）。

## data/shadow/gap_down.jsonl

每行一个交易日。规则：当日开盘相对前收跳空 ≤ -2% 的**全部**流动性标的
（近 60 日成交额中位数 ≥ 5000 万、股价 ≥ 3 元），
以 10:00 的 5 分钟收盘价买入、11:30 的 5 分钟收盘价卖出，每笔 2 万元。

| 字段 | 说明 |
|---|---|
| market_excess_pct | 同窗口全市场（流动性池）中位数收益，用于算超额 |
| legs[].gap_pct | 跳空幅度 |
| legs[].gross_pct | 未扣成本收益 |
| legs[].cost_pct | 往返成本（佣金 + 印花税 + 双边 1 tick） |
| legs[].net_pct | 扣成本后收益 |
| legs[].excess_pct | `net_pct - market_excess_pct` |

## data/reviews/YYYY-MM-DD.md

当日复盘：净值、当日盈亏、目标核对（月化 / 最大回撤 / 单笔最大亏损）、
当日成交、持仓、被拒绝的委托。

`index.jsonl` 为机器可读版本，每行一个交易日。

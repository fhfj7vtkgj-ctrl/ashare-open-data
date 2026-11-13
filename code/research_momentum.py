# -*- coding: utf-8 -*-
"""研究线 A：横截面动量/反转（日线，多年样本）——目标是月化 >10% 的候选。

为什么从这里开始：
  做 T 的三个方向已被证伪（5m 破位、VWAP/突破/尾盘、低开反转），共同瓶颈是
  0.23% 往返成本 vs 毫秒级边际。横截面日线策略换手低一个数量级（20 天一次），
  成本摊薄到可忽略，且样本量是 5m 数据的 100 倍，过拟合空间小得多。

数据口径修正（2026-11-13）：
  price_history.amount 对 A 股基本全 NULL（550 万行里只有 1,175 行有值），
  之前的流动性过滤 `liq >= 5e7` 因此把所有标的都筛掉，回测返回空。
  改用 volume * close 作成交额代理；volume 100% 有值。
  该代理有极端异常值（单日最高算出来 159 万亿，明显是脏数据），
  故用【滚动中位数】而非均值做流动性门槛，并给单日 5000 亿的硬上限。

规则（固定口径，先不调参）：
  每 hold 个交易日调仓一次；调仓日按过去 lookback 日收益（跳过最近 5 日）排序，
  取前 K 只等权持有一个周期。对比组：后 K 只（反转）、随机 K 只、全市场等权。

成本：每次调仓按双边 0.2%（佣金 + 印花税 + 滑点）计。

用法：python ai_quant/research_momentum.py [--start 2022-01-01] [--k 10]
      [--lookback 60] [--hold 20] [--liq 0.5]
"""
from __future__ import annotations

import argparse
import json
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, r"D:\AI_Quant_System\core")
import psycopg2
from config import DB_TRADE as DB

COST_ROUND_TRIP = 0.002
AMOUNT_CAP = 5e11  # 单只个股单日成交额 5000 亿，超过视为脏数据


def load(start: str, end: str) -> pd.DataFrame:
    conn = psycopg2.connect(**DB)
    q = """
        SELECT symbol, trade_time::date AS d, close, volume
          FROM price_history
         WHERE timeframe='daily'
           AND symbol ~ '^(60|68|00|30)[0-9]{4}$'
           AND trade_time::date BETWEEN %s AND %s
           AND close IS NOT NULL AND close > 0
    """
    df = pd.read_sql(q, conn, params=(start, end))
    conn.close()
    df["close"] = df["close"].astype(float)
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").astype(float)
    amt = df["volume"] * df["close"]
    df["amt"] = amt.where(amt <= AMOUNT_CAP)
    return df


def backtest(df: pd.DataFrame, k: int, lookback: int, hold: int, mode: str,
             liq_min: float = 5e7):
    px = df.pivot_table(index="d", columns="symbol", values="close").sort_index()
    amt = df.pivot_table(index="d", columns="symbol", values="amt").reindex(px.index)

    # 滚动中位数：对脏数据不敏感，不需要逐行清洗
    liq = amt.rolling(lookback, min_periods=lookback // 2).median()
    ok = (px >= 3.0) & (liq >= liq_min)

    mom = px.shift(5) / px.shift(lookback) - 1.0

    dates = list(px.index)
    periods = []
    idx = lookback
    while idx + hold < len(dates):
        d0, d1 = dates[idx], dates[idx + hold]
        score = mom.loc[d0].where(ok.loc[d0])
        s = score.dropna()
        if len(s) >= k * 3:
            if mode == "momentum":
                picks = s.nlargest(k).index
            elif mode == "reversal":
                picks = s.nsmallest(k).index
            elif mode == "random":
                picks = s.sample(k, random_state=idx).index
            else:  # universe：全市场等权基准
                picks = s.index
            fwd = (px.loc[d1, picks] / px.loc[d0, picks] - 1.0).dropna()
            if len(fwd):
                bench = (px.loc[d1, s.index] / px.loc[d0, s.index] - 1.0).dropna()
                periods.append({
                    "d0": str(d0), "d1": str(d1), "n": int(len(fwd)),
                    "ret": float(fwd.mean()),
                    "bench": float(bench.mean()) if len(bench) else None,
                })
        idx += hold

    if not periods:
        return None
    net = [p["ret"] - COST_ROUND_TRIP for p in periods]
    bench = [p["bench"] for p in periods if p["bench"] is not None]
    eq = np.cumprod([1 + r for r in net])
    peak, mdd = 1.0, 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak)
    n = len(net)
    sd = float(np.std(net, ddof=1)) if n > 1 else 0.0
    mean_r = float(np.mean(net))
    per_month = 21.0 / hold
    half = n // 2
    def _ann(vals):
        m = float(np.mean(vals))
        return round(((1 + m) ** per_month - 1) * 100, 2)
    return {
        "mode": mode, "k": k, "lookback": lookback, "hold_days": hold,
        "periods": n,
        "mean_period_ret_pct": round(mean_r * 100, 3),
        "monthly_pct": _ann(net),
        "monthly_is_pct": _ann(net[:half]) if half >= 2 else None,
        "monthly_oos_pct": _ann(net[half:]) if n - half >= 2 else None,
        "bench_monthly_pct": _ann(bench) if bench else None,
        "excess_monthly_pct": (_ann(net) - _ann(bench)) if bench else None,
        "win_rate": round(sum(1 for r in net if r > 0) / n, 3),
        "total_return_pct": round((eq[-1] - 1) * 100, 1),
        "max_drawdown_pct": round(mdd * 100, 2),
        "sharpe_period": round(mean_r / sd, 3) if sd else None,
        "t_stat": round(mean_r / (sd / n ** 0.5), 2) if sd else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2022-01-01")
    ap.add_argument("--end", default="2026-11-12")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--lookback", type=int, default=60)
    ap.add_argument("--hold", type=int, default=20)
    ap.add_argument("--liq", type=float, default=0.5, help="流动性门槛（亿元）")
    args = ap.parse_args()

    df = load(args.start, args.end)
    out = {"period": [args.start, args.end], "symbols": int(df["symbol"].nunique()),
           "rows": int(len(df)), "cost_round_trip_pct": COST_ROUND_TRIP * 100,
           "liq_min_yi": args.liq}
    res = []
    for mode in ("momentum", "reversal", "random", "universe"):
        r = backtest(df, args.k, args.lookback, args.hold, mode, args.liq * 1e8)
        if r:
            res.append(r)
    out["results"] = res
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

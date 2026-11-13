# -*- coding: utf-8 -*-
"""研究线 A-2：日线因子的横截面扫描（factor zoo）。

动机：动量扫描已经给出一个强结论——过去 20 日涨幅最大的 10 只在未来 5 日
平均每月亏 6.8%（t=-3.36），是全表最显著的信号。但它只能做空，A 股散户做不了。
所以需要把"能长仓做"的因子一次扫完，看有没有任何一个能达到月化 >10% 的量级。

因子（都只用日线 OHLCV，滞后一期，无未来函数）：
  ret5      过去 5 日收益（短期反转）
  ret20     过去 20 日收益
  ret60     过去 60 日收益
  vol20     过去 20 日收益波动率（低波动因子）
  maxret20  过去 20 日单日最大涨幅（彩票偏好 / MAX 效应）
  turn20    近 20 日成交额 / 前 60 日成交额（放量因子）
  illiq     近 60 日成交额中位数（流动性溢价 / 小市值代理）

评估：每 5 个交易日调仓，每期取因子最高 10% 与最低 10% 等权，
同时给出「相对全市场等权的超额」和「日级聚合 t 值」。
扣往返成本 0.2%/次。

用法：python ai_quant/research_factor_zoo.py [--start 2022-01-01] [--hold 5]
"""
from __future__ import annotations

import argparse
import json
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, r"D:\AI_Quant_System\core")
import psycopg2  # noqa: E402
from config import DB_TRADE as DB  # noqa: E402

COST_ROUND_TRIP = 0.002


def load(start: str, end: str) -> pd.DataFrame:
    conn = psycopg2.connect(**DB)
    q = """
        SELECT symbol, trade_time::date AS d, close, volume
          FROM price_history
         WHERE timeframe='daily' AND symbol ~ '^(60|68|00|30)[0-9]{4}$'
           AND trade_time::date BETWEEN %s AND %s
           AND close IS NOT NULL AND close > 0
    """
    df = pd.read_sql(q, conn, params=(start, end))
    conn.close()
    df["close"] = df["close"].astype(float)
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").astype(float)
    a = df["volume"] * df["close"]
    df["amt"] = a.where(a <= 5e11)
    return df


def build(df: pd.DataFrame) -> dict:
    px = df.pivot_table(index="d", columns="symbol", values="close").sort_index()
    amt = df.pivot_table(index="d", columns="symbol", values="amt").reindex(px.index)
    rets = px.pct_change(fill_method=None)
    f = {}
    f["ret5"] = px / px.shift(5) - 1
    f["ret20"] = px / px.shift(20) - 1
    f["ret60"] = px / px.shift(60) - 1
    f["vol20"] = rets.rolling(20, min_periods=15).std()
    f["maxret20"] = rets.rolling(20, min_periods=15).max()
    f["turn20"] = amt.rolling(20, min_periods=15).median() / amt.rolling(60, min_periods=30).median()
    f["illiq"] = amt.rolling(60, min_periods=30).median()
    liq = amt.rolling(60, min_periods=30).median()
    ok = (px >= 3.0) & (liq >= 5e7)
    return {"px": px, "f": f, "ok": ok}


def run(panel: dict, hold: int, pct: float = 0.10):
    px, f, ok = panel["px"], panel["f"], panel["ok"]
    dates = list(px.index)
    out = {}
    for name, F in f.items():
        per = []
        for i in range(60, len(dates) - hold, hold):
            d0, d1 = dates[i], dates[i + hold]
            s = F.loc[d0].where(ok.loc[d0]).dropna()
            if len(s) < 200:
                continue
            fwd = (px.loc[d1] / px.loc[d0] - 1).reindex(s.index).dropna()
            if len(fwd) < 200:
                continue
            s = s.reindex(fwd.index)
            n = max(int(len(s) * pct), 5)
            order = s.sort_values()
            lo, hi = order.index[:n], order.index[-n:]
            uni = fwd.mean()
            per.append({
                "d": d0,
                "hi": float(fwd.reindex(hi).mean()) - COST_ROUND_TRIP,
                "lo": float(fwd.reindex(lo).mean()) - COST_ROUND_TRIP,
                "uni": float(uni) - COST_ROUND_TRIP,
            })
        if len(per) < 8:
            continue
        hi = np.array([p["hi"] for p in per])
        lo = np.array([p["lo"] for p in per])
        uni = np.array([p["uni"] for p in per])
        lsp = hi - lo - 2 * COST_ROUND_TRIP
        def stat(x):
            m, sd = float(x.mean()), float(x.std(ddof=1))
            return (round(m * 100, 3),
                    round(((1 + m) ** (21 / hold) - 1) * 100, 2),
                    round(m / (sd / len(x) ** 0.5), 2) if sd else None)
        hm, hmon, ht = stat(hi)
        lm, lmon, lt = stat(lo)
        um, umon, ut = stat(uni)
        sm, smon, st = stat(lsp)
        out[name] = {
            "periods": len(per), "hold_days": hold,
            "top10_mean_pct": hm, "top10_monthly_pct": hmon, "top10_t": ht,
            "bot10_mean_pct": lm, "bot10_monthly_pct": lmon, "bot10_t": lt,
            "universe_monthly_pct": umon,
            "top10_excess_monthly_pct": round(hmon - umon, 2),
            "long_short_monthly_pct": smon, "long_short_t": st,
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2022-01-01")
    ap.add_argument("--end", default="2026-11-12")
    ap.add_argument("--hold", type=int, default=5)
    args = ap.parse_args()

    panel = build(load(args.start, args.end))
    res = run(panel, args.hold)
    oos = {}
    for hold in (20,):
        for name, r in run(panel, hold).items():
            oos[name] = r
    print(json.dumps({"period": [args.start, args.end], "cost_pct": COST_ROUND_TRIP * 100,
                      "hold5": res, "hold20": oos}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

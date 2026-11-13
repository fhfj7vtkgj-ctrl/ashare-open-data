# -*- coding: utf-8 -*-
"""研究线 A 的参数扫描 + 数据体检。

上一轮结论（2022-01-01 → 2026-11-12，55 个 20 日周期）：
  动量 60 日 -2.76%/月（t=-1.4）、反转 -0.64%、随机 +1.09%（t=0.96）、
  全市场等权 +0.02%。全市场等权的超额是 -0.21%/月，
  正好等于 0.2% 往返成本摊到 21/20 个月 —— 说明成本核算口径是对的。

本脚本回答两个问题：
  1. 是不是周期选错了？（lookback 20/60/120 × hold 5/20 × k 5/10/20）
  2. 数据本身有没有会造假的毛病？（复权、停牌重复价、退市/幸存者）

用法：python ai_quant/research_momentum_sweep.py [--start 2022-01-01]
"""
from __future__ import annotations

import argparse
import json
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, r"D:\AI_Quant_System\ai_quant")
sys.path.insert(0, r"D:\AI_Quant_System\core")
from research_momentum import load, backtest  # noqa: E402

import psycopg2  # noqa: E402
from config import DB_TRADE as DB  # noqa: E402


def data_quality(start: str, end: str) -> dict:
    conn = psycopg2.connect(**DB)
    cur = conn.cursor()
    cur.execute("""
        SELECT count(*) AS rows,
               count(adj_factor) AS adj_not_null,
               count(DISTINCT symbol) AS syms
          FROM price_history
         WHERE timeframe='daily' AND symbol ~ '^(60|68|00|30)[0-9]{4}$'
           AND trade_time::date BETWEEN %s AND %s
    """, (start, end))
    rows, adj, syms = cur.fetchone()
    # 停牌/重复价：与前一日收盘完全相同的比例
    cur.execute("""
        WITH x AS (
          SELECT symbol, trade_time::date d, close,
                 lag(close) OVER (PARTITION BY symbol ORDER BY trade_time) prev
            FROM price_history
           WHERE timeframe='daily' AND symbol ~ '^(60|68|00|30)[0-9]{4}$'
             AND trade_time::date BETWEEN %s AND %s
        )
        SELECT count(*) FILTER (WHERE prev IS NOT NULL) n,
               count(*) FILTER (WHERE prev IS NOT NULL AND close = prev) same
          FROM x
    """, (start, end))
    n, same = cur.fetchone()
    # 幸存者偏差：2022 年在库、2026 年仍有报价的标的有多少
    cur.execute("""
        SELECT count(DISTINCT symbol) FROM price_history
         WHERE timeframe='daily' AND trade_time::date BETWEEN '2022-01-01' AND '2022-12-31'
           AND symbol ~ '^(60|68|00|30)[0-9]{4}$'
    """)
    s2022 = cur.fetchone()[0]
    cur.execute("""
        SELECT count(DISTINCT symbol) FROM price_history
         WHERE timeframe='daily' AND trade_time::date >= '2026-06-01'
           AND symbol ~ '^(60|68|00|30)[0-9]{4}$'
    """)
    s2026 = cur.fetchone()[0]
    conn.close()
    return {"rows": rows, "adj_factor_nonnull": adj, "symbols": syms,
            "flat_close_rows": same, "flat_close_pct": round(same / max(n, 1) * 100, 2),
            "symbols_2022": s2022, "symbols_2026": s2026}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2022-01-01")
    ap.add_argument("--end", default="2026-11-12")
    args = ap.parse_args()

    df = load(args.start, args.end)
    out = {"period": [args.start, args.end],
           "data_quality": data_quality(args.start, args.end),
           "sweep": []}

    combos = [(20, 5, 10), (20, 20, 10), (60, 5, 10), (60, 20, 10),
              (60, 20, 20), (120, 20, 10), (120, 60, 10)]
    for lookback, hold, k in combos:
        for mode in ("momentum", "reversal", "universe"):
            r = backtest(df, k, lookback, hold, mode)
            if r:
                out["sweep"].append({
                    "mode": mode, "k": k, "lookback": lookback, "hold": hold,
                    "periods": r["periods"],
                    "monthly_pct": r["monthly_pct"],
                    "monthly_oos_pct": r["monthly_oos_pct"],
                    "bench_monthly_pct": r["bench_monthly_pct"],
                    "t_stat": r["t_stat"],
                    "mdd_pct": r["max_drawdown_pct"],
                })
    out["sweep"].sort(key=lambda x: (x["mode"], -abs(x["monthly_pct"] or 0)))
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

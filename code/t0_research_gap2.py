# -*- coding: utf-8 -*-
"""低开反转：入场时点 × 出场时点矩阵（市场中性 + 日级 t 值）。

动机：上一轮发现 10:00 出场超额 -0.235（t≈-2.0），即跳空低开的票在前 25 分钟
还在跌；而 11:30 出场超额 +0.37。若"先跌后修复"是真实现象，那么把入场推迟到
10:00 之后应该同时提高收益和 t 值。

注意：市场基准必须与入场/出场时点一致，否则 beta 剥不干净。

用法：python ai_quant/t0_research_gap2.py [--days 40] [--gap 0.02]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import timedelta
from statistics import median

sys.path.insert(0, r"D:\AI_Quant_System\core")
import psycopg2
from config import DB_TRADE as DB

COMMISSION_RATE = 0.00025
COMMISSION_MIN = 5.0
STAMP_DUTY = 0.0005
SLIPPAGE_TICKS = 1

ENTRIES = {"09:35": 575, "10:00": 600, "10:30": 630, "11:00": 660}
EXITS = {"10:30": 630, "11:30": 690, "13:30": 810, "15:00": 900}


def is_etf(sym: str) -> bool:
    return sym.startswith(("5", "1"))


def tick_of(sym: str) -> float:
    return 0.001 if is_etf(sym) else 0.01


def fees(sym: str, px: float, shares: int) -> float:
    amt = px * shares
    cost = max(COMMISSION_MIN, amt * COMMISSION_RATE) * 2
    if not is_etf(sym):
        cost += amt * STAMP_DUTY
    return cost + SLIPPAGE_TICKS * tick_of(sym) * shares * 2


def load(days: int):
    conn = psycopg2.connect(**DB)
    cur = conn.cursor()
    cur.execute("""SELECT DISTINCT ts::date FROM minute_bar WHERE tf='5m'
                    ORDER BY 1 DESC LIMIT %s""", (days,))
    dates = sorted(r[0] for r in cur.fetchall())
    cur.execute("""SELECT symbol, ts, open, high, low, close, amount FROM minute_bar
                    WHERE tf='5m' AND ts::date = ANY(%s) ORDER BY symbol, ts""", (dates,))
    bars = defaultdict(lambda: defaultdict(list))
    amt = defaultdict(list)
    for sym, ts, o, h, l, c, a in cur.fetchall():
        bars[sym][ts.date()].append((ts.hour * 60 + ts.minute, float(o), float(h),
                                     float(l), float(c)))
        if a:
            amt[sym].append(float(a))
    cur.execute("""SELECT symbol, trade_time::date, close FROM price_history
                    WHERE timeframe='daily' AND trade_time >= %s AND trade_time::date <= %s
                    ORDER BY symbol, trade_time""",
                (dates[0] - timedelta(days=90), dates[-1]))
    daily = defaultdict(list)
    for sym, d, c in cur.fetchall():
        if c is not None:
            daily[sym].append((d, float(c)))
    conn.close()
    return bars, amt, daily, dates


def prev_close(daily, sym, d):
    best = None
    for dd, c in daily.get(sym, []):
        if dd < d:
            best = c
        else:
            break
    return best


def close_at(day, minutes):
    for t, _o, _h, _l, c in day:
        if t >= minutes:
            return c
    return day[-1][4]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=40)
    ap.add_argument("--notional", type=float, default=20000)
    ap.add_argument("--gap", type=float, default=0.02)
    args = ap.parse_args()

    bars, amt, daily, dates = load(args.days)
    liquid = {s for s, a in amt.items() if median(a) * 48 >= 5e7}

    # 市场基准：任意 (entry, exit) 组合下的全市场中位数收益
    mkt = {}
    for en, et in ENTRIES.items():
        for xn, xt in EXITS.items():
            if xt <= et:
                continue
            per = {}
            for d in dates:
                rs = []
                for sym in liquid:
                    day = sorted(bars[sym].get(d, []))
                    if len(day) < 40:
                        continue
                    e, x = close_at(day, et), close_at(day, xt)
                    if e > 0:
                        rs.append(x / e - 1)
                if rs:
                    per[d] = median(rs)
            mkt[(en, xn)] = per

    out = []
    for en, et in ENTRIES.items():
        for xn, xt in EXITS.items():
            if xt <= et:
                continue
            rows = []
            for d in dates:
                for sym in liquid:
                    day = sorted(bars[sym].get(d, []))
                    if len(day) < 40:
                        continue
                    pc = prev_close(daily, sym, d)
                    if not pc:
                        continue
                    gap = day[0][1] / pc - 1
                    if gap > -args.gap:
                        continue
                    entry, exit_ = close_at(day, et), close_at(day, xt)
                    if entry <= 0 or exit_ <= 0:
                        continue
                    shares = int(args.notional / entry // 100) * 100
                    if shares < 100:
                        continue
                    gross = (exit_ - entry) * shares
                    cost = fees(sym, entry, shares)
                    rows.append({"d": d,
                                 "net": (gross - cost) / (entry * shares) * 100,
                                 "mkt": mkt[(en, xn)].get(d, 0.0) * 100})
            if not rows:
                continue
            by_day = defaultdict(list)
            for r in rows:
                by_day[r["d"]].append(r["net"] - r["mkt"])
            vals = [sum(v) / len(v) for v in by_day.values()]
            n = len(vals)
            m = sum(vals) / n
            sd = (sum((x - m) ** 2 for x in vals) / (n - 1)) ** 0.5 if n > 1 else 0.0
            out.append({
                "entry": en, "exit": xn, "trades": len(rows), "days": n,
                "avg_net_pct": round(sum(r["net"] for r in rows) / len(rows), 4),
                "avg_excess_pct": round(sum(r["net"] - r["mkt"] for r in rows) / len(rows), 4),
                "day_mean_excess_pct": round(m, 4),
                "t_stat": round(m / (sd / n ** 0.5), 2) if sd else None,
                "positive_days": sum(1 for x in vals if x > 0),
            })

    out.sort(key=lambda x: (x["t_stat"] or -9), reverse=True)
    print(json.dumps({"period": [str(dates[0]), str(dates[-1])], "days_total": len(dates),
                      "gap_threshold": -args.gap, "matrix": out}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

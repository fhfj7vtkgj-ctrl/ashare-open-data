# -*- coding: utf-8 -*-
"""做T 信号族筛子：先用"毛边际 vs 交易成本"淘汰，再谈组合模拟。

判据：avg_gross_pct 必须 > 2 * avg_cost_pct，否则不管怎么调仓都是给成本打工。

成本口径（修正 2026-11-13）：
  A 股【股票】最小变动单位统一 0.01 元，与股价无关；
  只有 ETF/LOF/基金是 0.001 元。之前按"股价<5 元"给 0.001，
  等于让 3-4 元的低价股少算 10 倍滑点 —— 这是最容易造假边际的地方。
  佣金 万 2.5（单笔最低 5 元）+ 卖出印花税 0.05%（ETF 免）+ 双边各 1 tick。

信号族（每个标的每天最多 1 笔，14:50 前平）：
  A VWAP 回归：跌破 VWAP-k 买，回到 VWAP 卖
  B 开盘区间突破：10:00 后突破前 30 分钟最高价买，回撤 1% 或收盘卖
  C 尾盘低吸：14:00 后跌破当日开盘 -1% 买，收盘卖
  D 低开反转：跳空低开 ≥2% 开盘买，收盘卖
  E 高开延续：跳空高开 ≥2% 开盘买，收盘卖
  F 破位买回（v3 基准）：跌破开盘 -0.8% 买，+2%/-1.5%/收盘卖

用法：python ai_quant/t0_research.py [--days 40] [--notional 20000]
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

ENTRY_AFTER = 9 * 60 + 45
ENTRY_UNTIL = 14 * 60 + 30
FORCE_FLAT = 14 * 60 + 50


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
    cur.execute("""SELECT symbol, ts, open, high, low, close, volume, amount FROM minute_bar
                    WHERE tf='5m' AND ts::date = ANY(%s) ORDER BY symbol, ts""", (dates,))
    bars = defaultdict(lambda: defaultdict(list))
    amt = defaultdict(list)
    for sym, ts, o, h, l, c, v, a in cur.fetchall():
        bars[sym][ts.date()].append((ts.hour * 60 + ts.minute, float(o), float(h),
                                     float(l), float(c), float(v or 0), float(a or 0)))
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


def vwap_series(day):
    out, cv, ca = [], 0.0, 0.0
    for (_t, o, h, l, c, v, a) in day:
        cv += v
        ca += a if a > 0 else (o + h + l + c) / 4 * v
        out.append(ca / cv if cv > 0 else c)
    return out


def sig_vwap_rev(sym, day, pc, k=0.004, sl=0.015):
    vw = vwap_series(day)
    if len(day) < 40:
        return None
    entry = None
    for i in range(12, len(day) - 1):
        t, o, h, l, c, v, a = day[i]
        if entry is None:
            if t >= ENTRY_UNTIL:
                break
            if c <= vw[i] * (1 - k):
                entry = c
                ei = i
            continue
        if c >= vw[i]:
            return entry, c
        if c <= entry * (1 - sl):
            return entry, entry * (1 - sl)
    if entry is None:
        return None
    return entry, day[-1][4]


def sig_orb(sym, day, pc, trail=0.01):
    if len(day) < 40:
        return None
    orb = [b for b in day if b[0] <= 10 * 60]
    if len(orb) < 5:
        return None
    hi = max(b[2] for b in orb)
    entry = None
    mx = 0.0
    for i, (t, o, h, l, c, v, a) in enumerate(day):
        if t <= 10 * 60 or t >= ENTRY_UNTIL:
            continue
        if entry is None:
            if c > hi:
                entry, mx = c, c
            continue
        mx = max(mx, c)
        if c <= mx * (1 - trail):
            return entry, c
    if entry is None:
        return None
    return entry, day[-1][4]


def sig_late_dip(sym, day, pc, d=0.01):
    if len(day) < 40:
        return None
    o0 = day[0][1]
    entry = None
    for (t, o, h, l, c, v, a) in day:
        if entry is None:
            if t >= 14 * 60 and c <= o0 * (1 - d):
                entry = c
            continue
    if entry is None:
        return None
    return entry, day[-1][4]


def sig_gap(sym, day, pc, th=0.02, direction=-1):
    if len(day) < 40 or not pc:
        return None
    gap = day[0][1] / pc - 1
    if direction < 0 and gap > -th:
        return None
    if direction > 0 and gap < th:
        return None
    return day[0][4], day[-1][4]


def sig_dip(sym, day, pc, entry_pct=0.008, tp=0.02, sl=0.015):
    if len(day) < 40:
        return None
    o0 = day[0][1]
    entry = None
    for (t, o, h, l, c, v, a) in day:
        if entry is None:
            if t < ENTRY_AFTER or t >= ENTRY_UNTIL:
                continue
            if c <= o0 * (1 - entry_pct):
                entry = c
            continue
        if c >= entry * (1 + tp):
            return entry, entry * (1 + tp)
        if c <= entry * (1 - sl):
            return entry, entry * (1 - sl)
    if entry is None:
        return None
    return entry, day[-1][4]


FAMILIES = {
    "A_VWAP回归": lambda s, d, p: sig_vwap_rev(s, d, p),
    "B_开盘区间突破": lambda s, d, p: sig_orb(s, d, p),
    "C_尾盘低吸": lambda s, d, p: sig_late_dip(s, d, p),
    "D_低开反转": lambda s, d, p: sig_gap(s, d, p, direction=-1),
    "E_高开延续": lambda s, d, p: sig_gap(s, d, p, direction=+1),
    "F_破位买回": lambda s, d, p: sig_dip(s, d, p),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=40)
    ap.add_argument("--notional", type=float, default=20000)
    args = ap.parse_args()

    bars, amt, daily, dates = load(args.days)
    liquid = {s for s, a in amt.items() if median(a) * 48 >= 5e7}

    res = {}
    for name, fn in FAMILIES.items():
        rows = []
        for sym in sorted(liquid):
            for d, day in bars[sym].items():
                day = sorted(day)
                if len(day) < 40:
                    continue
                pc = prev_close(daily, sym, d)
                r = fn(sym, day, pc)
                if not r:
                    continue
                entry, exit_ = r
                if entry <= 0 or exit_ <= 0:
                    continue
                shares = int(args.notional / entry // 100) * 100
                if shares < 100:
                    continue
                gross = (exit_ - entry) * shares
                cost = fees(sym, entry, shares)
                rows.append({
                    "gross_pct": gross / (entry * shares) * 100,
                    "cost_pct": cost / (entry * shares) * 100,
                    "net_pct": (gross - cost) / (entry * shares) * 100,
                })
        if not rows:
            continue
        n = len(rows)
        avg_g = sum(r["gross_pct"] for r in rows) / n
        avg_c = sum(r["cost_pct"] for r in rows) / n
        avg_n = sum(r["net_pct"] for r in rows) / n
        res[name] = {
            "trades": n,
            "trades_per_day": round(n / len(dates), 1),
            "win_rate": round(sum(1 for r in rows if r["net_pct"] > 0) / n, 4),
            "avg_gross_pct": round(avg_g, 4),
            "avg_cost_pct": round(avg_c, 4),
            "avg_net_pct": round(avg_n, 4),
            "gross_over_cost": round(avg_g / avg_c, 2) if avg_c else None,
            "total_net_pct": round(sum(r["net_pct"] for r in rows), 1),
            "verdict": "PASS 毛边际>2x成本" if avg_c and avg_g > 2 * avg_c else "FAIL 成本吃掉毛边际",
        }

    print(json.dumps({
        "period": [str(dates[0]), str(dates[-1])], "days": len(dates),
        "symbols_liquid": len(liquid), "notional_per_trade": args.notional,
        "families": res,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

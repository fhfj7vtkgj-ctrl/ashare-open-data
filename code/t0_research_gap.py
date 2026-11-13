# -*- coding: utf-8 -*-
"""D 组（低开反转）稳健性检验：把 beta 剥掉、把样本切开、把阈值和出场时间扫一遍。

上一轮筛子唯一过线的是"跳空低开 ≥2% 在 09:35 买、收盘卖"：
毛 0.558% / 成本 0.227% / 净 0.330%，2.45 倍成本。

三个必须回答的问题：
  1. 这是 alpha 还是 beta？当天大盘也从 09:35 涨到收盘的话，个股反弹不算本事。
     → 用同一时刻全市场（90 只流动性标的）中位数收益做基准，算 excess。
  2. 前后半段是否一致？→ 走前检验。
  3. 跳跃阈值、出场时间敏感吗？→ 网格。

成本：股票 tick 0.01、ETF tick 0.001（不按股价），佣金万 2.5（最低 5 元）、
卖出印花税 0.05%（ETF 免）、双边各 1 tick。

用法：python ai_quant/t0_research_gap.py [--days 40] [--notional 20000]
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
    """时间 >= minutes 的第一根 5m bar 的收盘价；没有则最后一根。"""
    for t, _o, _h, _l, c in day:
        if t >= minutes:
            return c
    return day[-1][4]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=40)
    ap.add_argument("--notional", type=float, default=20000)
    args = ap.parse_args()

    bars, amt, daily, dates = load(args.days)
    liquid = {s for s, a in amt.items() if median(a) * 48 >= 5e7}

    # 全市场基准：09:35 收盘 → 各出场时点的中位数收益
    exits = {"10:00": 600, "10:30": 630, "11:30": 690, "13:30": 810, "14:00": 840, "15:00": 900}
    market = {}
    for label, tm in exits.items():
        per_day = {}
        for d in dates:
            rs = []
            for sym in liquid:
                day = sorted(bars[sym].get(d, []))
                if len(day) < 40:
                    continue
                e, x = day[0][4], close_at(day, tm)
                if e > 0:
                    rs.append(x / e - 1)
            if rs:
                per_day[d] = median(rs)
        market[label] = per_day

    def collect(gap_th, exit_label, subset=None):
        tm = exits[exit_label]
        rows = []
        for d in (subset or dates):
            for sym in liquid:
                day = sorted(bars[sym].get(d, []))
                if len(day) < 40:
                    continue
                pc = prev_close(daily, sym, d)
                if not pc:
                    continue
                o0 = day[0][1]
                gap = o0 / pc - 1
                if gap > -gap_th:
                    continue
                entry, exit_ = day[0][4], close_at(day, tm)
                if entry <= 0 or exit_ <= 0:
                    continue
                shares = int(args.notional / entry // 100) * 100
                if shares < 100:
                    continue
                gross = (exit_ - entry) * shares
                cost = fees(sym, entry, shares)
                rows.append({
                    "d": d, "sym": sym, "gap": gap,
                    "gross_pct": gross / (entry * shares) * 100,
                    "net_pct": (gross - cost) / (entry * shares) * 100,
                    "mkt_pct": market[exit_label].get(d, 0.0) * 100,
                })
        return rows

    def evaluate(gap_th, exit_label, subset=None):
        rows = collect(gap_th, exit_label, subset)
        if not rows:
            return None
        n = len(rows)
        avg_net = sum(r["net_pct"] for r in rows) / n
        avg_mkt = sum(r["mkt_pct"] for r in rows) / n
        avg_ex = sum(r["net_pct"] - r["mkt_pct"] for r in rows) / n
        return {
            "gap_th": -gap_th, "exit": exit_label, "trades": n,
            "avg_gap_pct": round(sum(r["gap"] for r in rows) / n * 100, 2),
            "win_rate": round(sum(1 for r in rows if r["net_pct"] > 0) / n, 4),
            "avg_net_pct": round(avg_net, 4),
            "avg_market_pct": round(avg_mkt, 4),
            "avg_excess_pct": round(avg_ex, 4),
            "sharpe_like": round(avg_ex / (max(1e-9, (sum((r["net_pct"] - r["mkt_pct"] - avg_ex) ** 2 for r in rows) / n) ** 0.5)) * (n ** 0.5), 2),
        }

    # 1) 出场时间敏感度（固定 -2%）
    exit_sweep = [evaluate(0.02, lab) for lab in exits]
    # 2) 阈值敏感度（固定收盘）
    gap_sweep = [evaluate(th, "15:00") for th in (0.01, 0.015, 0.02, 0.03, 0.04)]
    # 3) 走前检验（-2% / 收盘）
    half = len(dates) // 2
    first, second = dates[:half], dates[half:]
    wf = {"in_sample": evaluate(0.02, "15:00", first),
          "out_of_sample": evaluate(0.02, "15:00", second)}

    # 4) 日级聚合：303 笔挤在 31 天里，笔级 t 值不可信，按天取均值再算 t
    def day_stats(th, lab):
        rows = collect(th, lab)
        by_day = defaultdict(list)
        for r in rows:
            by_day[r["d"]].append(r["net_pct"] - r["mkt_pct"])
        vals = [sum(v) / len(v) for v in by_day.values()]
        n = len(vals)
        m = sum(vals) / n
        sd = (sum((x - m) ** 2 for x in vals) / (n - 1)) ** 0.5 if n > 1 else 0.0
        return {"days": n, "mean_excess_pct": round(m, 4), "sd_pct": round(sd, 4),
                "t_stat": round(m / (sd / n ** 0.5), 2) if sd else None,
                "positive_days": sum(1 for x in vals if x > 0)}

    def weekly(th, lab):
        rows = collect(th, lab)
        by_w = defaultdict(list)
        for r in rows:
            by_w[r["d"].isocalendar()[1]].append(r["net_pct"] - r["mkt_pct"])
        return {str(w): round(sum(v) / len(v), 4) for w, v in sorted(by_w.items())}

    wf_1130 = {"in_sample": evaluate(0.02, "11:30", first),
               "out_of_sample": evaluate(0.02, "11:30", second)}

    print(json.dumps({
        "period": [str(dates[0]), str(dates[-1])], "days": len(dates),
        "symbols_liquid": len(liquid),
        "exit_sweep_gap2pct": [x for x in exit_sweep if x],
        "gap_sweep_exit1500": [x for x in gap_sweep if x],
        "walk_forward_1500": wf,
        "walk_forward_1130": wf_1130,
        "day_level": {"exit_11:30": day_stats(0.02, "11:30"),
                      "exit_15:00": day_stats(0.02, "15:00")},
        "weekly_excess_1130": weekly(0.02, "11:30"),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

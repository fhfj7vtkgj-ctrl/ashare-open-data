# -*- coding: utf-8 -*-
"""低开反转候选 —— 影子盘（只记录，不下单）。

为什么是影子盘而不是真跑：
  1. 该信号市场中性化后日级 t 只有 0.85~1.05，31 天样本不足以下结论；
  2. 成本下限与 8% 回撤在 9.3 万本金上互斥——要压住 0.23% 往返成本，
     单次必须 ≥2 万元，而 T+1 要求等额底仓，5 个标的就要 10 万底仓（满仓）。
  用本金去赌一个 t<1.1 的信号不划算，但用零成本方式攒前瞻样本很划算。

每交易日 11:35 跑一次，用 5m 分钟线（10:00 收盘价入场、11:30 收盘价出场），
把当天所有「跳空低开 ≤ -2%」的流动性标的全部记账，同时记录同窗口的
全市场中位数收益，这样第一天就有可比的超额，不用等几个月才能算。

输出 reports/t_gap_shadow.jsonl（每日追加），reports/t_gap_shadow_summary.json（累计）。

用法：python ai_quant/t_gap_shadow.py [--date 2026-11-13] [--gap 0.02]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from statistics import median

sys.path.insert(0, r"D:\AI_Quant_System\core")
import psycopg2  # noqa: E402
from config import DB_TRADE as DB  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(r"D:\AI_Quant_System")
OUT = ROOT / "reports" / "t_gap_shadow.jsonl"
SUMMARY = ROOT / "reports" / "t_gap_shadow_summary.json"

COMMISSION_RATE = 0.00025
COMMISSION_MIN = 5.0
STAMP_DUTY = 0.0005
SLIP_TICKS = 1
NOTIONAL = 20000.0
ENTRY_T = 10 * 60      # 10:00
EXIT_T = 11 * 60 + 30  # 11:30


def is_etf(sym: str) -> bool:
    return sym.startswith(("5", "1"))


def tick_of(sym: str) -> float:
    return 0.001 if is_etf(sym) else 0.01


def fees(sym: str, px: float, shares: int) -> float:
    amt = px * shares
    cost = max(COMMISSION_MIN, amt * COMMISSION_RATE) * 2
    if not is_etf(sym):
        cost += amt * STAMP_DUTY
    return cost + SLIP_TICKS * tick_of(sym) * shares * 2


def load(d: date):
    conn = psycopg2.connect(**DB)
    cur = conn.cursor()
    cur.execute("""
        SELECT symbol, ts::time, open, close, amount
          FROM minute_bar WHERE tf='5m' AND ts::date=%s
    """, (d,))
    bars, amt, first_open = {}, {}, {}
    for sym, t, o, c, a in cur.fetchall():
        mins = t.hour * 60 + t.minute
        bars.setdefault(sym, {})[mins] = float(c)
        if sym not in first_open and o:
            first_open[sym] = float(o)
        if a:
            amt.setdefault(sym, []).append(float(a))
    cur.execute("""
        SELECT DISTINCT ON (symbol) symbol, close
          FROM price_history
         WHERE timeframe='daily' AND trade_time::date < %s
           AND symbol = ANY(%s)
         ORDER BY symbol, trade_time DESC
    """, (d, list(bars.keys())))
    prev = {s: float(c) for s, c in cur.fetchall() if c}
    conn.close()
    return bars, amt, prev, first_open


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--gap", type=float, default=0.02)
    args = ap.parse_args()
    d = date.fromisoformat(args.date) if args.date else date.today()

    bars, amt, prev, first_open = load(d)
    if not bars:
        print(json.dumps({"date": str(d), "status": "NO_MINUTE_DATA"}, ensure_ascii=False))
        return 2

    liquid = {s for s, a in amt.items() if median(a) * 48 >= 5e7}

    # 市场基准：同样的 10:00 → 11:30 窗口，全市场中位数
    mkt = []
    for s in liquid:
        b = bars.get(s, {})
        e, x = b.get(ENTRY_T), b.get(EXIT_T)
        if e and x and e > 0:
            mkt.append(x / e - 1)
    mkt_ret = median(mkt) if mkt else 0.0

    legs, skipped = [], 0
    for s in sorted(liquid):
        b = bars.get(s, {})
        pc, e, x = prev.get(s), b.get(ENTRY_T), b.get(EXIT_T)
        if not pc or not e or not x:
            skipped += 1
            continue
        op = first_open.get(s)  # 当日第一根 5m 的开盘价
        gap = (op / pc - 1) if op else None
        if gap is None or gap > -args.gap:
            continue
        shares = int(NOTIONAL / e // 100) * 100
        if shares < 100:
            skipped += 1
            continue
        gross = (x - e) * shares
        cost = fees(s, e, shares)
        notional = e * shares
        legs.append({
            "date": str(d), "symbol": s, "gap_pct": round(gap * 100, 3),
            "entry": round(e, 3), "exit": round(x, 3), "shares": shares,
            "notional": round(notional, 2),
            "gross_pct": round(gross / notional * 100, 4),
            "cost_pct": round(cost / notional * 100, 4),
            "net_pct": round((gross - cost) / notional * 100, 4),
            "excess_pct": round((gross - cost) / notional * 100 - mkt_ret * 100, 4),
        })

    mean_leg_excess = (sum(x["excess_pct"] for x in legs) / len(legs)) if legs else None
    mean_gross = (sum(x["gross_pct"] for x in legs) / len(legs)) if legs else None
    mean_cost = (sum(x["cost_pct"] for x in legs) / len(legs)) if legs else None
    cost_to_gross = (mean_cost / mean_gross) if mean_gross and mean_gross > 0 else None
    rec = {"date": str(d),
           "market_benchmark_ret_pct": round(mkt_ret * 100, 4),
           "mean_excess_pct": round(mean_leg_excess, 4) if mean_leg_excess is not None else None,
           "mean_gross_pct": round(mean_gross, 4) if mean_gross is not None else None,
           "mean_cost_pct": round(mean_cost, 4) if mean_cost is not None else None,
           "cost_to_gross_ratio": round(cost_to_gross, 4) if cost_to_gross is not None else None,
           "liquid_symbols": len(liquid), "skipped": skipped,
           "signals": len(legs), "legs": legs}

    OUT.parent.mkdir(parents=True, exist_ok=True)
    # 幂等：同一天重复运行只保留最新一条（避免任务重试把样本翻倍）
    kept = []
    if OUT.exists():
        for line in OUT.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                if json.loads(line).get("date") != str(d):
                    kept.append(line)
            except Exception:
                pass
    kept.append(json.dumps(rec, ensure_ascii=False))
    OUT.write_text("\n".join(kept) + "\n", encoding="utf-8")

    # 累计统计（按日聚合，避免同一天多笔互相充数）
    rows = []
    for line in OUT.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    day_ex = [r["date"] for r in rows]
    per_day = []
    for r in rows:
        if r["legs"]:
            per_day.append(sum(l["excess_pct"] for l in r["legs"]) / len(r["legs"]))
    n = len(per_day)
    m = sum(per_day) / n if n else 0.0
    sd = (sum((x - m) ** 2 for x in per_day) / (n - 1)) ** 0.5 if n > 1 else 0.0
    all_legs = [l for r in rows for l in r["legs"]]
    summ = {
        "days_recorded": len(rows), "days_with_signals": n,
        "signals_total": len(all_legs),
        "avg_net_pct": round(sum(l["net_pct"] for l in all_legs) / len(all_legs), 4) if all_legs else None,
        "avg_excess_pct": round(sum(l["excess_pct"] for l in all_legs) / len(all_legs), 4) if all_legs else None,
        "avg_gross_pct": round(sum(l["gross_pct"] for l in all_legs) / len(all_legs), 4) if all_legs else None,
        "avg_cost_pct": round(sum(l["cost_pct"] for l in all_legs) / len(all_legs), 4) if all_legs else None,
        "cost_to_gross_ratio": (
            round((sum(l["cost_pct"] for l in all_legs) / len(all_legs)) /
                  (sum(l["gross_pct"] for l in all_legs) / len(all_legs)), 4)
            if all_legs and sum(l["gross_pct"] for l in all_legs) > 0 else None
        ),
        "day_mean_excess_pct": round(m, 4),
        "t_stat": round(m / (sd / n ** 0.5), 2) if sd and n > 1 else None,
        "positive_days": sum(1 for x in per_day if x > 0),
        "window": [day_ex[0], day_ex[-1]] if day_ex else None,
        "verdict": "样本不足" if n < 20 else ("正超额" if m > 0 else "无超额"),
    }
    SUMMARY.write_text(json.dumps(summ, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({"today": rec, "cumulative": summ}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

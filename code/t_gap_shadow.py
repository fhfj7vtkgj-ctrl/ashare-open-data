# -*- coding: utf-8 -*-
"""低开反转候选 —— 影子盘（只记录，不下单）。

为什么是影子盘而不是真跑：
  1. 该信号市场中性化后日级 t 只有 0.85~1.05，31 天样本不足以下结论；
  2. 基准成本约 0.16%（佣金+印花+一档总价差），压力口径约 0.24%；
     9.3 万本金同时承担底仓、5 个标的分散与 8% 回撤约束时空间很紧。
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
from datetime import date, datetime, timedelta
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
# A quoted spread of one tick costs one tick round-trip: half a tick when
# buying at ask and half when selling at bid. Keep a separate one-tick-per-side
# stress case; the old model charged the stress case as the base case, which
# doubled spread cost and made the 87% cost/gross snapshot misleading.
SLIP_TICKS_PER_SIDE = 0.5
STRESS_SLIP_TICKS_PER_SIDE = 1.0
NOTIONAL = 20000.0
ENTRY_T = 10 * 60      # 10:00
EXIT_T = 11 * 60 + 30  # 11:30
ROLLING_DAYS = 5
MAX_COST_TO_POSITIVE_GROSS = 0.35


def is_etf(sym: str) -> bool:
    return sym.startswith(("5", "1"))


def tick_of(sym: str) -> float:
    return 0.001 if is_etf(sym) else 0.01


def fees(sym: str, px: float, shares: int, slip_ticks_per_side: float = SLIP_TICKS_PER_SIDE) -> float:
    amt = px * shares
    cost = max(COMMISSION_MIN, amt * COMMISSION_RATE) * 2
    if not is_etf(sym):
        cost += amt * STAMP_DUTY
    return cost + slip_ticks_per_side * tick_of(sym) * shares * 2


def recost(rec: dict) -> dict:
    """Rebase legacy rows onto the documented spread/fee model."""
    benchmark = rec.get("market_benchmark_ret_pct")
    if benchmark is None:
        benchmark = rec.get("market_excess_pct", 0.0)
    rec.pop("market_excess_pct", None)
    rec["market_benchmark_ret_pct"] = benchmark
    for leg in rec.get("legs", []):
        entry = float(leg["entry"])
        shares = int(leg["shares"])
        notional = entry * shares
        cost = fees(str(leg["symbol"]), entry, shares)
        stress_cost = fees(str(leg["symbol"]), entry, shares, STRESS_SLIP_TICKS_PER_SIDE)
        gross = float(leg["gross_pct"])
        leg["cost_pct"] = round(cost / notional * 100, 4)
        leg["net_pct"] = round(gross - leg["cost_pct"], 4)
        leg["stress_cost_pct"] = round(stress_cost / notional * 100, 4)
        leg["stress_net_pct"] = round(gross - leg["stress_cost_pct"], 4)
        leg["excess_pct"] = round(leg["net_pct"] - float(benchmark), 4)
    legs = rec.get("legs", [])
    if legs:
        rec["mean_gross_pct"] = round(sum(float(x["gross_pct"]) for x in legs) / len(legs), 4)
        rec["mean_cost_pct"] = round(sum(float(x["cost_pct"]) for x in legs) / len(legs), 4)
        positive = [float(x["gross_pct"]) for x in legs if float(x["gross_pct"]) > 0]
        rec["mean_positive_gross_pct"] = round(sum(positive) / len(positive), 4) if positive else None
        rec["cost_to_gross_ratio"] = (
            round(rec["mean_cost_pct"] / rec["mean_positive_gross_pct"], 4)
            if rec.get("mean_positive_gross_pct") else None
        )
        rec["mean_excess_pct"] = round(sum(float(x["excess_pct"]) for x in legs) / len(legs), 4)
    rec["cost_model_version"] = "spread-one-tick-v2"
    return rec


def load(d: date):
    conn = psycopg2.connect(**DB)
    cur = conn.cursor()
    cur.execute("""
        SELECT symbol, ts::time, open, close, amount
          FROM minute_bar WHERE tf='5m' AND ts::date=%s
         ORDER BY ts
    """, (d,))
    bars, amt, first_open, first_open_min = {}, {}, {}, {}
    for sym, t, o, c, a in cur.fetchall():
        mins = t.hour * 60 + t.minute
        bars.setdefault(sym, {})[mins] = float(c)
        if o and (sym not in first_open_min or mins < first_open_min[sym]):
            first_open[sym] = float(o)
            first_open_min[sym] = mins
        if a and mins <= ENTRY_T:
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
        stress_cost = fees(s, e, shares, STRESS_SLIP_TICKS_PER_SIDE)
        notional = e * shares
        legs.append({
            "date": str(d), "symbol": s, "gap_pct": round(gap * 100, 3),
            "entry": round(e, 3), "exit": round(x, 3), "shares": shares,
            "notional": round(notional, 2),
            "gross_pct": round(gross / notional * 100, 4),
            "cost_pct": round(cost / notional * 100, 4),
            "net_pct": round((gross - cost) / notional * 100, 4),
            "stress_cost_pct": round(stress_cost / notional * 100, 4),
            "stress_net_pct": round((gross - stress_cost) / notional * 100, 4),
            "excess_pct": round((gross - cost) / notional * 100 - mkt_ret * 100, 4),
        })

    mean_leg_excess = (sum(x["excess_pct"] for x in legs) / len(legs)) if legs else None
    mean_gross = (sum(x["gross_pct"] for x in legs) / len(legs)) if legs else None
    mean_cost = (sum(x["cost_pct"] for x in legs) / len(legs)) if legs else None
    positive_gross = [x["gross_pct"] for x in legs if x["gross_pct"] > 0]
    mean_positive_gross = sum(positive_gross) / len(positive_gross) if positive_gross else None
    cost_to_gross = (mean_cost / mean_positive_gross) if mean_positive_gross else None
    rec = {"date": str(d),
           "cost_model_version": "spread-one-tick-v2",
           "market_benchmark_ret_pct": round(mkt_ret * 100, 4),
           "mean_excess_pct": round(mean_leg_excess, 4) if mean_leg_excess is not None else None,
           "mean_gross_pct": round(mean_gross, 4) if mean_gross is not None else None,
           "mean_cost_pct": round(mean_cost, 4) if mean_cost is not None else None,
           "cost_to_gross_ratio": round(cost_to_gross, 4) if cost_to_gross is not None else None,
           "mean_positive_gross_pct": round(mean_positive_gross, 4) if mean_positive_gross is not None else None,
           "liquid_symbols": len(liquid), "skipped": skipped,
           "signals": len(legs), "legs": legs}

    OUT.parent.mkdir(parents=True, exist_ok=True)
    # 幂等：同一天重复运行只保留最新一条（避免任务重试把样本翻倍）
    kept = []
    unparsable = []
    if OUT.exists():
        for line in OUT.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                if json.loads(line).get("date") != str(d):
                    kept.append(line)
            except Exception as exc:
                print(f"WARN: preserving unparsable shadow row: {exc}", file=sys.stderr)
                unparsable.append(line)
    kept.append(json.dumps(rec, ensure_ascii=False))
    OUT.write_text("\n".join(kept + unparsable) + "\n", encoding="utf-8")

    # 累计统计（按日聚合，避免同一天多笔互相充数）
    rows = []
    migrated = False
    preserved = []
    for line in OUT.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                row = json.loads(line)
                migrated = migrated or row.get("cost_model_version") != "spread-one-tick-v2"
                rows.append(recost(row))
            except Exception as exc:
                print(f"WARN: preserving row after cost rebase failure: {exc}", file=sys.stderr)
                preserved.append(line)
    if migrated:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = OUT.with_name(f"{OUT.stem}_before_cost_rebase_{stamp}.jsonl")
        backup.write_text(OUT.read_text(encoding="utf-8"), encoding="utf-8")
        OUT.write_text("\n".join(
            [json.dumps(r, ensure_ascii=False) for r in rows] + preserved
        ) + "\n",
                       encoding="utf-8")
    day_ex = [r["date"] for r in rows]
    per_day = []
    for r in rows:
        if r["legs"]:
            per_day.append(sum(l["excess_pct"] for l in r["legs"]) / len(r["legs"]))
    n = len(per_day)
    m = sum(per_day) / n if n else 0.0
    sd = (sum((x - m) ** 2 for x in per_day) / (n - 1)) ** 0.5 if n > 1 else 0.0
    t_net = m / (sd / n ** 0.5) if sd and n > 1 else None
    all_legs = [l for r in rows for l in r["legs"]]
    total_cost = sum(l["cost_pct"] for l in all_legs)
    total_stress_cost = sum(l.get("stress_cost_pct", l["cost_pct"]) for l in all_legs)
    positive_gross = [l["gross_pct"] for l in all_legs if l["gross_pct"] > 0]
    avg_positive_gross = sum(positive_gross) / len(positive_gross) if positive_gross else None
    rolling = rows[-ROLLING_DAYS:]
    rolling_legs = [l for r in rolling for l in r["legs"]]
    rolling_days = []
    for r in rolling:
        if r["legs"]:
            rolling_days.append(sum(l["net_pct"] for l in r["legs"]) / len(r["legs"]))
    rolling_mean = sum(rolling_days) / len(rolling_days) if rolling_days else None
    rolling_cost = (
        sum(l["cost_pct"] for l in rolling_legs) / len(rolling_legs)
        if rolling_legs else None
    )
    rolling_positive_gross = [l["gross_pct"] for l in rolling_legs if l["gross_pct"] > 0]
    rolling_positive_mean = (
        sum(rolling_positive_gross) / len(rolling_positive_gross)
        if rolling_positive_gross else None
    )
    rolling_cost_ratio = (
        rolling_cost / rolling_positive_mean
        if rolling_cost is not None and rolling_positive_mean else None
    )
    if len(rolling) >= ROLLING_DAYS and rolling_mean is not None and rolling_mean <= 0:
        strategy_status = "DISABLE_NEGATIVE_NET"
    elif (len(rolling) >= ROLLING_DAYS and rolling_cost_ratio is not None
          and rolling_cost_ratio > MAX_COST_TO_POSITIVE_GROSS):
        strategy_status = "DISABLE_COST_DRAG"
    elif n < 20:
        strategy_status = "RESEARCH_ONLY"
    elif m > 0 and t_net is not None and t_net >= 1.5:
        strategy_status = "PILOT"
    else:
        strategy_status = "DISABLE_NEGATIVE_NET"
    summ = {
        "days_recorded": len(rows), "days_with_signals": n,
        "signals_total": len(all_legs),
        "avg_net_pct": round(sum(l["net_pct"] for l in all_legs) / len(all_legs), 4) if all_legs else None,
        "avg_excess_pct": round(sum(l["excess_pct"] for l in all_legs) / len(all_legs), 4) if all_legs else None,
        "avg_gross_pct": round(sum(l["gross_pct"] for l in all_legs) / len(all_legs), 4) if all_legs else None,
        "avg_cost_pct": round(sum(l["cost_pct"] for l in all_legs) / len(all_legs), 4) if all_legs else None,
        "avg_stress_cost_pct": round(total_stress_cost / len(all_legs), 4) if all_legs else None,
        "avg_positive_gross_pct": round(avg_positive_gross, 4) if avg_positive_gross is not None else None,
        "cost_to_gross_ratio": (
            round((total_cost / len(all_legs)) / avg_positive_gross, 4)
            if all_legs and avg_positive_gross else None
        ),
        "rolling_5d": {
            "days_recorded": len(rolling),
            "signals": len(rolling_legs),
            "mean_daily_net_pct": round(rolling_mean, 4) if rolling_mean is not None else None,
            "cost_to_positive_gross_ratio": round(rolling_cost_ratio, 4) if rolling_cost_ratio is not None else None,
        },
        "strategy_status": strategy_status,
        "hard_gate": "NO_NEW_POSITION_UNLESS_PILOT",
        "day_mean_excess_pct": round(m, 4),
        "t_stat": round(m / (sd / n ** 0.5), 2) if sd and n > 1 else None,
        "positive_days": sum(1 for x in per_day if x > 0),
        "window": [day_ex[0], day_ex[-1]] if day_ex else None,
        "verdict": strategy_status,
    }
    SUMMARY.write_text(json.dumps(summ, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({"today": rec, "cumulative": summ}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

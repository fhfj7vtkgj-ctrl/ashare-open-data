# -*- coding: utf-8 -*-
"""做T 策略回测 v3：严格的资金约束 + 横截面动量选股 + 走前检验。

v2 的缺陷（已修正）：按 capital/slots 定仓却不限制同时持仓数，106 笔/10 天
意味着名义金额超过本金，收益被隐性杠杆放大。v3 用事件驱动模拟：

  * 同时持仓数 <= slots，总名义金额 <= 本金（不加杠杆）
  * 每笔仓位 = capital / slots，按 100 股取整
  * 同日先处理已有持仓的止盈/止损，再处理新开仓（容量满则跳过）
  * 止盈/止损用该 5m bar 的 high/low 判定；同一根 bar 同时触发按止损算（保守）
  * 14:50 后不再开仓，15:00 强制平（A 股 T+1，卖的是底仓）

选股（结构性改进，不是调参）：
  * 日线收盘 > MA20 才做（只做多头趋势）
  * 按前 5 日收益率排序，只做最强的 topK 只（topK=0 表示不筛）
  * 09:45 之前不进场（避开开盘 15 分钟噪声）

成本：佣金万 2.5（单笔最低 5 元）+ 卖出印花税 0.05%（ETF 免）+ 双边 1 tick。

用法：python ai_quant/t0_backtest_v3.py [--days 21] [--capital 100000]
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

ENTRY_AFTER = 9 * 60 + 45     # 09:45
ENTRY_UNTIL = 14 * 60 + 30    # 14:30
FORCE_FLAT = 14 * 60 + 50     # 14:50


def tick_of(sym: str, px: float) -> float:
    # A 股股票最小变动单位统一 0.01 元，与股价无关；ETF/LOF/基金才是 0.001。
    # 2026-11-13 修正：旧口径按 px<5 给 0.001，让 3-4 元低价股少算 10 倍滑点。
    return 0.001 if is_etf(sym) else 0.01


def is_etf(sym: str) -> bool:
    return sym.startswith(("5", "1"))


def fees(sym: str, px: float, shares: int) -> float:
    amt = px * shares
    cost = max(COMMISSION_MIN, amt * COMMISSION_RATE) * 2
    if not is_etf(sym):
        cost += amt * STAMP_DUTY
    return cost + SLIPPAGE_TICKS * tick_of(sym, px) * shares * 2


def hm(ts) -> int:
    return ts.hour * 60 + ts.minute


def load(days: int):
    conn = psycopg2.connect(**DB)
    cur = conn.cursor()
    cur.execute("""SELECT DISTINCT ts::date FROM minute_bar WHERE tf='5m'
                    ORDER BY 1 DESC LIMIT %s""", (days,))
    dates = sorted(r[0] for r in cur.fetchall())
    cur.execute("""SELECT symbol, ts, open, high, low, close, amount FROM minute_bar
                    WHERE tf='5m' AND ts::date = ANY(%s) ORDER BY symbol, ts""", (dates,))
    bars = defaultdict(lambda: defaultdict(dict))
    amt = defaultdict(list)
    for sym, ts, o, h, l, c, a in cur.fetchall():
        bars[sym][ts.date()][hm(ts)] = (float(o), float(h), float(l), float(c))
        if a:
            amt[sym].append(float(a))
    cur.execute("""SELECT symbol, trade_time::date, close, high, low FROM price_history
                    WHERE timeframe='daily' AND trade_time >= %s AND trade_time::date <= %s
                    ORDER BY symbol, trade_time""",
                (dates[0] - timedelta(days=90), dates[-1]))
    daily = defaultdict(list)
    for sym, d, c, h, l in cur.fetchall():
        if c is not None:
            daily[sym].append((d, float(c), float(h) if h else float(c), float(l) if l else float(c)))
    conn.close()
    return bars, amt, daily, dates


def features(daily, dates):
    """每 (symbol, date) 的 ma20 通过与否、前 5 日收益、前一日振幅。"""
    feats = {}
    for sym, seq in daily.items():
        closes = [c for _d, c, _h, _l in seq]
        prev_range = [None] * len(seq)
        for i in range(1, len(seq)):
            _d0, _c0, h0, l0 = seq[i - 1]
            prev_range[i] = (h0 - l0) / _c0 if _c0 else None
        for i, (d, _c, _h, _l) in enumerate(seq):
            if d not in dates or i < 20:
                continue
            ma20 = sum(closes[i - 20:i]) / 20
            ret5 = closes[i - 1] / closes[i - 6] - 1 if i >= 6 else 0.0
            feats[(sym, d)] = {"ma20_ok": closes[i] > ma20, "ret5": ret5,
                               "prev_range": prev_range[i] or 0.0}
    return feats


def run(bars, amt, daily, feats, dates, capital, slots, topk,
        entry_pct, tp_pct, sl_pct, cost_scale=1.0, min_range=0.0):
    liquid = {s for s, a in amt.items() if median(a) * 48 >= 5e7 and s in daily}
    slot_cash = capital / slots
    equity = capital
    peak = capital
    mdd = 0.0
    trades = []
    curve = []
    exp_sum = 0.0      # 时间加权占用（每个 5m 时点已持仓的名义金额之和）
    exp_n = 0

    for d in dates:
        # 当日候选：流动性 + 趋势 + 动量排序
        cands = []
        for sym in liquid:
            f = feats.get((sym, d))
            if not f or not f["ma20_ok"]:
                continue
            if d not in bars[sym] or len(bars[sym][d]) < 40:
                continue
            if min_range and f["prev_range"] < min_range:
                continue
            cands.append((f["ret5"], sym))
        cands.sort(reverse=True)
        if topk:
            cands = cands[:topk]
        cset = {s for _r, s in cands}
        # 按动量排序的优先队列：强势的先用仓位
        order = {s: i for i, (_r, s) in enumerate(cands)}

        times = sorted({t for s in cset for t in bars[s][d]})
        if not times:
            continue

        open_pos = {}     # sym -> dict(entry, shares, t_entry, entry_bar)
        pnl_day = 0.0
        done = set()

        for t in times:
            # 1) 先处理已有持仓的离场
            for sym in list(open_pos):
                bar = bars[sym][d].get(t)
                if bar is None:
                    continue
                _o, h, l, c = bar
                p = open_pos[sym]
                tp_px = p["entry"] * (1 + tp_pct)
                sl_px = p["entry"] * (1 - sl_pct)
                exit_px = None
                if t >= FORCE_FLAT:
                    exit_px = c
                elif l <= sl_px:                 # 同一根 bar 双触发按止损算
                    exit_px = sl_px
                elif h >= tp_px:
                    exit_px = tp_px
                if exit_px is None:
                    continue
                pnl = (exit_px - p["entry"]) * p["shares"] - fees(sym, exit_px, p["shares"]) * cost_scale
                pnl_day += pnl
                trades.append({
                    "sym": sym, "d": d, "pnl": pnl,
                    "risk_pct": sl_pct * p["entry"] * p["shares"] / equity * 100,
                    "ret_pct": (exit_px / p["entry"] - 1) * 100,
                })
                done.add(sym)
                del open_pos[sym]

            # 时间加权占用：此刻已持仓的名义金额
            exp_sum += sum(p["shares"] * p["entry"] for p in open_pos.values())
            exp_n += 1

            # 2) 再考虑开新仓
            if not (ENTRY_AFTER <= t < ENTRY_UNTIL):
                continue
            if len(open_pos) >= slots:
                continue
            for _r, sym in sorted(cands, key=lambda x: order[x[1]]):
                if len(open_pos) >= slots:
                    break
                if sym in open_pos or sym in done:
                    continue
                bar = bars[sym][d].get(t)
                if bar is None:
                    continue
                _o, _h, _l, c = bar
                o0 = bars[sym][d][min(bars[sym][d])][0]
                if o0 <= 0 or c > o0 * (1 - entry_pct):
                    continue
                shares = int(slot_cash / c // 100) * 100
                if shares < 100:
                    continue
                open_pos[sym] = {"entry": c, "shares": shares, "t": t}

        # 收盘强制平掉残余
        for sym, p in list(open_pos.items()):
            c = bars[sym][d][max(bars[sym][d])][3]
            pnl = (c - p["entry"]) * p["shares"] - fees(sym, c, p["shares"]) * cost_scale
            pnl_day += pnl
            trades.append({"sym": sym, "d": d, "pnl": pnl,
                           "risk_pct": sl_pct * p["entry"] * p["shares"] / equity * 100,
                           "ret_pct": (c / p["entry"] - 1) * 100})
            del open_pos[sym]

        equity += pnl_day
        peak = max(peak, equity)
        mdd = max(mdd, (peak - equity) / peak)
        curve.append((d, equity))

    if not trades:
        return None
    wins = sum(1 for t in trades if t["pnl"] > 0)
    return {
        "slots": slots, "topk": topk or "all",
        "entry_pct": entry_pct, "tp_pct": tp_pct, "sl_pct": sl_pct,
        "trades": len(trades),
        "trades_per_day": round(len(trades) / len(dates), 2),
        "win_rate": round(wins / len(trades), 4),
        "monthly_pct": round((equity / capital - 1) / len(dates) * 21 * 100, 2),
        "max_drawdown_pct": round(mdd * 100, 2),
        "max_trade_risk_pct": round(max(t["risk_pct"] for t in trades), 2),
        "avg_pnl_per_trade": round(sum(t["pnl"] for t in trades) / len(trades), 2),
        "final_equity": round(equity, 2),
        "avg_exposure_pct": round(exp_sum / exp_n / capital * 100, 1) if exp_n else 0.0,
    }


GRID = [(e, tp, sl) for e in (0.008, 0.012, 0.018)
        for tp in (0.008, 0.012, 0.02)
        for sl in (0.01, 0.015)]

VARIANTS = [
    {"name": "A 全市场 slots5", "slots": 5, "topk": 0},
    {"name": "B 动量top40 slots5", "slots": 5, "topk": 40},
    {"name": "C 动量top20 slots5", "slots": 5, "topk": 20},
    {"name": "D 动量top40 slots8", "slots": 8, "topk": 40},
    {"name": "E 动量top20 slots8", "slots": 8, "topk": 20},
    {"name": "F 动量top10 slots5", "slots": 5, "topk": 10},
    {"name": "G 波幅>3% top40 slots5", "slots": 5, "topk": 40, "min_range": 0.03},
    {"name": "H 波幅>4% top40 slots5", "slots": 5, "topk": 40, "min_range": 0.04},
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=21)
    ap.add_argument("--capital", type=float, default=100000)
    args = ap.parse_args()

    bars, amt, daily, dates = load(args.days)
    feats = features(daily, set(dates))
    half = len(dates) // 2
    first, second = dates[:half], dates[half:]

    out = []
    for v in VARIANTS:
        kw = dict(slots=v["slots"], topk=v["topk"], min_range=v.get("min_range", 0.0))
        # 全期最优（仅参考，属于样本内）
        full = []
        for e, tp, sl in GRID:
            r = run(bars, amt, daily, feats, dates, args.capital, **kw,
                    entry_pct=e, tp_pct=tp, sl_pct=sl)
            if r:
                full.append(r)
        full.sort(key=lambda x: -x["monthly_pct"])
        # 走前：前半段选参 → 后半段只跑一次
        best = None
        for e, tp, sl in GRID:
            r = run(bars, amt, daily, feats, first, args.capital, **kw,
                    entry_pct=e, tp_pct=tp, sl_pct=sl)
            if r and (best is None or r["monthly_pct"] > best["monthly_pct"]):
                best = r
        oos = None
        if best:
            oos = run(bars, amt, daily, feats, second, args.capital, **kw,
                      entry_pct=best["entry_pct"], tp_pct=best["tp_pct"], sl_pct=best["sl_pct"])
        out.append({
            "variant": v["name"],
            "in_sample_best": full[0] if full else None,
            "walk_forward": {
                "chosen": {k: best[k] for k in ("entry_pct", "tp_pct", "sl_pct")} if best else None,
                "is_monthly_pct": best["monthly_pct"] if best else None,
                "oos_monthly_pct": oos["monthly_pct"] if oos else None,
                "oos_drawdown_pct": oos["max_drawdown_pct"] if oos else None,
                "oos_trades": oos["trades"] if oos else None,
                "oos_win_rate": oos["win_rate"] if oos else None,
                "oos_exposure_pct": oos["avg_exposure_pct"] if oos else None,
            },
        })

    # 成本敏感度：用 A 组（全市场 slots5）全期最优参数，0.5x / 1x / 2x 成本各跑一次
    cost_sens = []
    if out and out[0]["in_sample_best"]:
        b = out[0]["in_sample_best"]
        for cs in (0.5, 1.0, 2.0):
            r = run(bars, amt, daily, feats, dates, args.capital,
                    slots=b["slots"], topk=0 if b["topk"] == "all" else b["topk"],
                    entry_pct=b["entry_pct"], tp_pct=b["tp_pct"], sl_pct=b["sl_pct"],
                    cost_scale=cs)
            if r:
                cost_sens.append({"cost_scale": cs, "monthly_pct": r["monthly_pct"],
                                  "win_rate": r["win_rate"],
                                  "avg_pnl_per_trade": r["avg_pnl_per_trade"]})

    print(json.dumps({
        "period": [str(dates[0]), str(dates[-1])], "days": len(dates),
        "capital": args.capital,
        "targets": {"monthly_pct": "6-8", "max_drawdown_pct": "<8", "trade_risk_pct": "<2"},
        "variants": out,
        "cost_sensitivity": cost_sens,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

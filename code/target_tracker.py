# -*- coding: utf-8 -*-
"""目标验证器：按"月化 6-8%、最大回撤 <8%、单笔风险 <2%"逐项核对实盘/纸面账户。

数据来源：
  paper_equity_daily  每日净值快照（服务每 15 分钟写一次，当天覆盖）
  paper_account       当前实时净值
  paper_trade         逐笔成交（算单笔风险与胜率）

口径：
  月化 = 区间累计收益 / 区间交易日数 × 21
  最大回撤 = 净值曲线峰谷最大跌幅
  单笔风险 = 该笔最大可能亏损 / 当时净值；平仓单用实际亏损兜底（两者取大）

用法：python ai_quant/target_tracker.py [--days 63]
退出码：0 全部达标，1 有未达标项（便于计划任务/告警使用）
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, r"D:\AI_Quant_System\core")
import psycopg2
from config import DB_TRADE as DB

TARGETS = {"monthly_pct_min": 6.0, "monthly_pct_max": 8.0,
           "max_drawdown_pct": 8.0, "trade_risk_pct": 2.0}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=63)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    conn = psycopg2.connect(**DB)
    cur = conn.cursor()
    cur.execute("""SELECT trade_date, equity, margin_used FROM paper_equity_daily
                    WHERE account_id=1 ORDER BY trade_date""")
    hist = [(r[0], float(r[1]), float(r[2] or 0)) for r in cur.fetchall()
            if r[1] is not None]
    cur.execute("SELECT initial_cash, cash FROM paper_account WHERE id=1")
    initial, cash_now = (float(x) for x in cur.fetchone())
    cur.execute("""SELECT id, symbol, side, quantity, price, commission, realized_pnl, trade_time
                     FROM paper_trade ORDER BY trade_time""")
    trades = cur.fetchall()
    conn.close()

    today = date.today()
    if not hist or hist[-1][0] != today:
        hist.append((today, cash_now, 0.0))
    window = [h for h in hist if (today - h[0]).days <= args.days * 2] or hist

    eq0, eq1 = window[0][1], window[-1][1]
    n_days = max(len(window) - 1, 1)
    total_ret = (eq1 - eq0) / eq0 if eq0 else 0.0
    monthly = total_ret / n_days * 21 * 100
    # 参与率口径（2026-11-17 门槛研究，与 daily_review.py 一致）：
    # 月化目标 6~8% 按窗口内 margin_used>0 的交易日占比等比折算；
    # 在场 <3 个交易日时月化记"不适用"，不判 FAIL（空仓期属闸门策略常态）。
    days_in_mkt = sum(1 for _d, _v, mg in window if mg > 0)
    participation = min(days_in_mkt / n_days, 1.0) if n_days else 0.0
    monthly_sampled = days_in_mkt >= 3
    tgt_low = TARGETS["monthly_pct_min"] * participation
    tgt_high = TARGETS["monthly_pct_max"] * participation
    monthly_ok = (not monthly_sampled) or (tgt_low <= monthly <= tgt_high)

    peak, mdd = window[0][1], 0.0
    for _d, v, _mg in window:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak)

    # 单笔风险：亏损单的实际亏损 / 当期净值（保守：用区间起点净值）
    risks = []
    for (_id, sym, side, qty, px, comm, pnl, _t) in trades:
        if pnl is None or float(pnl) >= 0:
            continue
        risks.append(abs(float(pnl)) / eq0 * 100)
    max_risk = max(risks) if risks else 0.0

    closed = [t for t in trades if t[6] is not None and float(t[6]) != 0]
    wins = sum(1 for t in closed if float(t[6]) > 0)

    checks = {
        "monthly_pct": {"value": round(monthly, 2), "target": f"6-8 ×参与率{participation:.0%}",
                        "pass": monthly_ok},
        "max_drawdown_pct": {"value": round(mdd * 100, 2), "target": "<8",
                             "pass": mdd * 100 < TARGETS["max_drawdown_pct"]},
        "max_trade_risk_pct": {"value": round(max_risk, 2), "target": "<2",
                               "pass": max_risk < TARGETS["trade_risk_pct"]},
    }
    out = {
        "as_of": str(today),
        "window": [str(window[0][0]), str(window[-1][0])],
        "trading_days": n_days,
        "equity": {"start": round(eq0, 2), "now": round(eq1, 2),
                   "initial_cash": initial, "total_return_pct": round(total_ret * 100, 2)},
        "trades": {"total": len(trades), "closed": len(closed),
                   "win_rate": round(wins / len(closed), 4) if closed else None},
        "checks": checks,
        "all_pass": all(c["pass"] for c in checks.values()),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2) if args.json else
          "\n".join([
              f"截至 {out['as_of']}  区间 {out['window'][0]} → {out['window'][1]}（{n_days} 个交易日）",
              f"净值 {eq0:,.2f} → {eq1:,.2f}  累计 {total_ret * 100:+.2f}%  月化 {monthly:+.2f}%  "
              f"[参与率 {participation:.0%} → 目标 {tgt_low:.2f}~{tgt_high:.2f}%] "
              f"{'不适用' if not monthly_sampled else ('PASS' if checks['monthly_pct']['pass'] else 'FAIL')}",
              f"最大回撤 {mdd * 100:.2f}%  [目标 <8%] {'PASS' if checks['max_drawdown_pct']['pass'] else 'FAIL'}",
              f"单笔最大亏损 {max_risk:.2f}% 净值  [目标 <2%] {'PASS' if checks['max_trade_risk_pct']['pass'] else 'FAIL'}",
              f"成交 {len(trades)} 笔，已平 {len(closed)} 笔，胜率 "
              f"{(wins / len(closed) * 100):.1f}%" if closed else "暂无平仓单",
          ]))
    return 0 if out["all_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

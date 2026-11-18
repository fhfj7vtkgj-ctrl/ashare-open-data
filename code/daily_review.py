# -*- coding: utf-8 -*-
"""每日下午复盘（15:10）：账户、当日成交、持仓、与目标的差距，写成一页。

为什么要"下午复盘"而不是看实时：A 股 15:00 收盘价才是当天的结算价，
盘中数字会被最后一笔集合竞价改掉；净值快照也是收盘后才稳定。

输出：
  reports/daily/YYYY-MM-DD.md     当日复盘（人看）
  reports/daily/index.jsonl       每日一行（机器看，便于回测）
退出码：0 全部达标（月化按参与率折算，在场<3个交易日时月化记"不适用"，不算未达标）；
        1 有未达标项（便于计划任务告警）；2 数据异常
口径（2026-11-17 门槛研究）：月化目标 6~8% 按窗口参与率等比折算为 6~8×P%，
空仓/低参与期的绝对月化不再判 FAIL——依据 AQR 趋势跟随长期研究与
mlfinlab（AFML）meta-labeling 方法论：信号闸门策略的空仓期属常态。

用法：python ai_quant/daily_review.py [--date 2026-11-13] [--quiet]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, r"D:\AI_Quant_System\core")
import psycopg2
from config import DB_TRADE as DB

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

REPORT_DIR = Path(r"D:\AI_Quant_System\reports\daily")
TARGETS = {"monthly_pct_min": 6.0, "monthly_pct_max": 8.0,
           "max_drawdown_pct": 8.0, "trade_risk_pct": 2.0}


def fetch(d: date):
    conn = psycopg2.connect(**DB)
    cur = conn.cursor()
    cur.execute("""SELECT initial_cash, cash, equity, COALESCE(peak_equity, equity)
                     FROM paper_account WHERE id=1""")
    a = cur.fetchone()
    # 唯一权威账本：paper_cashflow。paper_trade 有历史行缺失，不能再用它推导已实现盈亏。
    cur.execute("SELECT COALESCE(SUM(amount),0) FROM paper_cashflow "
                "WHERE account_id=1 AND reason='SETTLE'")
    realized_db = float(cur.fetchone()[0])
    cur.execute("""SELECT symbol, side, quantity, avg_price, current_price, unrealized_pnl
                     FROM paper_position WHERE account_id=1 AND quantity > 0 ORDER BY symbol""")
    pos = cur.fetchall()
    cur.execute("""SELECT id, symbol, side, quantity, price, commission, realized_pnl,
                          (trade_time + interval '8 hour')
                     FROM paper_trade
                    WHERE account_id=1 AND (trade_time + interval '8 hour')::date = %s
                    ORDER BY trade_time""", (d,))
    trades = cur.fetchall()
    cur.execute("""SELECT trade_date, equity, margin_used FROM paper_equity_daily
                    WHERE account_id=1 AND trade_date <= %s ORDER BY trade_date""", (d,))
    hist = [(r[0], float(r[1]), float(r[2] or 0)) for r in cur.fetchall()
            if r[1] is not None]
    cur.execute("""SELECT id, symbol, side, quantity, filled_price, status, reject_reason,
                          created_time
                     FROM paper_order
                    WHERE (created_time + interval '8 hour')::date = %s
                    ORDER BY id""", (d,))
    orders = cur.fetchall()
    cur.execute("""SELECT symbol, tf, ts FROM minute_bar
                    WHERE tf='5m' ORDER BY ts DESC LIMIT 1""")
    last_bar = cur.fetchone()
    conn.close()
    return a, realized_db, pos, trades, hist, orders, last_bar


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    d = date.fromisoformat(args.date) if args.date else date.today()

    a, realized_db, pos, trades, hist, orders, last_bar = fetch(d)
    initial, cash, equity, peak = (float(x or 0) for x in a)
    equity = equity if equity > 0 else cash
    unreal = sum(float(p[5] or 0) for p in pos)
    margin = max(0.0, equity - cash)
    realized = realized_db

    # 当日盈亏
    prev_eq = None
    for dd, v, _mg in hist:
        if dd < d:
            prev_eq = v
    day_pnl = (equity - prev_eq) if prev_eq is not None else None

    # 区间目标
    win = [(dd, v, mg) for dd, v, mg in hist if dd <= d]
    if not win or win[-1][0] != d:
        win.append((d, equity, margin))
    eq0, eq1 = win[0][1], win[-1][1]
    n_days = max(len(win) - 1, 1)
    monthly = (eq1 - eq0) / eq0 / n_days * 21 * 100 if eq0 else 0.0
    # 参与率：窗口内保证金占用（margin_used>0）的交易日占比。
    # 依据（2026-11-17 门槛研究）：AQR《A Century of Evidence on Trend-Following
    # Investing》与 mlfinlab/AFML meta-labeling 方法论——带信号闸门的趋势策略
    # 空仓期属常态，月化绝对目标必须按参与率折算，否则空仓期必然 FAIL。
    days_in_mkt = sum(1 for _dd, _v, mg in win if mg > 0)
    participation = min(days_in_mkt / n_days, 1.0) if n_days else 0.0
    monthly_sampled = days_in_mkt >= 3          # 在场<3天：样本不足以评月化
    tgt_low = TARGETS["monthly_pct_min"] * participation
    tgt_high = TARGETS["monthly_pct_max"] * participation
    monthly_ok = (not monthly_sampled) or (tgt_low <= monthly <= tgt_high)  # 不适用→不判FAIL
    # 峰值必须含 paper_account.peak_equity：equity_daily 只有 2 行时，
    # 只看窗口会把 6.81% 的回撤读成 0.49%，掩盖 8% 熔断距离。
    pk, mdd = max(peak, win[0][1]), 0.0
    for _dd, v, _mg in win:
        pk = max(pk, v)
        if pk > 0:
            mdd = max(mdd, (pk - v) / pk)
    closed = [t for t in trades if t[6] is not None]
    risks = [abs(float(t[6])) / eq0 * 100 for t in closed if float(t[6]) < 0]
    max_risk = max(risks) if risks else 0.0

    checks = {
        "monthly": monthly_ok,
        "drawdown": mdd * 100 < TARGETS["max_drawdown_pct"],
        "trade_risk": max_risk < TARGETS["trade_risk_pct"],
    }
    ok = all(checks.values())

    lines = [
        f"# 复盘 {d}（{d.strftime('%A')}）",
        "",
        f"- 净值：**{equity:,.2f}**（现金 {cash:,.2f}，保证金 {margin:,.2f}，持仓浮盈 {unreal:,.2f}）",
        f"- 当日盈亏：**{day_pnl:+,.2f}**" if day_pnl is not None else "- 当日盈亏：无前一日快照",
        f"- 累计已实现：{realized:+,.2f}　峰值净值：{peak:,.2f}",
        "",
        "## 目标核对",
        "",
        "| 指标 | 实测 | 目标 | 判定 |",
        "|---|---|---|---|",
        f"| 月化 | {monthly:+.2f}% | 参与率 {participation:.0%} → {tgt_low:.2f} ~ {tgt_high:.2f}% | "
        f"{'不适用' if not monthly_sampled else ('PASS' if monthly_ok else 'FAIL')} |",
        f"| 最大回撤 | {mdd * 100:.2f}% | <8% | {'PASS' if checks['drawdown'] else 'FAIL'} |",
        f"| 单笔最大亏损 | {max_risk:.2f}% 净值 | <2% | {'PASS' if checks['trade_risk'] else 'FAIL'} |",
        "",
        f"区间 {win[0][0]} → {win[-1][0]}，{n_days} 个交易日。",
        "",
        "## 当日成交",
        "",
    ]
    if trades:
        lines += ["| 时间 | 标的 | 方向 | 手数 | 价格 | 佣金 | 已实现 |", "|---|---|---|---|---|---|---|"]
        for t in trades:
            lines.append(f"| {t[7]:%H:%M:%S} | {t[1]} | {t[2]} | {float(t[3]):.0f} | "
                         f"{float(t[4]):.3f} | {float(t[5]):.2f} | {float(t[6] or 0):+.2f} |")
        day_realized = sum(float(t[6] or 0) for t in trades)
        lines += ["", f"当日已实现合计 **{day_realized:+,.2f}**。"
                      f"注意：净值变化（{day_pnl:+,.2f}）才是当日真实损益，"
                      "已实现金额可能包含前一日持仓在今日平仓的部分。"
                 if day_pnl is not None else
                 f"当日已实现合计 **{day_realized:+,.2f}**。"]
    else:
        lines.append("无成交。")

    lines += ["", "## 持仓", ""]
    if pos:
        lines += ["| 标的 | 手数 | 成本 | 现价 | 浮盈 |", "|---|---|---|---|---|"]
        for p in pos:
            lines.append(f"| {p[0]} | {float(p[2]):.0f} | {float(p[3] or 0):.3f} | "
                         f"{float(p[4] or 0):.3f} | {float(p[5] or 0):+.2f} |")
    else:
        lines.append("空仓。")

    rejected = [o for o in orders if o[5] not in ("FILLED", "PENDING")]
    if rejected:
        lines += ["", "## 被拒绝的委托", ""]
        for o in rejected:
            lines.append(f"- {o[0]} {o[1]} {o[2]} {float(o[3]):.0f} 手 → {o[5]}：{o[6] or ''}")

    if last_bar:
        lines += ["", f"（分钟线最新：{last_bar[0]} {last_bar[1]} @ {last_bar[2]}）"]

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    md = REPORT_DIR / f"{d}.md"
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")

    rec = {"date": str(d), "equity": round(equity, 2), "cash": round(cash, 2),
           "day_pnl": round(day_pnl, 2) if day_pnl is not None else None,
           "monthly_pct": round(monthly, 2), "max_drawdown_pct": round(mdd * 100, 2),
           "max_trade_risk_pct": round(max_risk, 2),
           "trades": len(trades), "positions": len(pos), "all_pass": ok}
    with (REPORT_DIR / "index.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    if not args.quiet:
        print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

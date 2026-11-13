# -*- coding: utf-8 -*-
"""把本机的 A 股数据与复盘样本导出成一个可公开的仓库目录。

设计原则：
  1. **可复现**：同样的数据导出同样的字节（固定排序 + 固定小数位），
     这样每天的 git 提交只会包含真正变化的部分，历史年份文件不会天天重写。
  2. **不含密钥**：只导出行情与模拟盘账本，不碰 secrets.json、不导 paper-api token。
  3. **自描述**：manifest.json 记录每个文件的行数、字节、sha256、时间范围，
     README.md 每次重新生成，统计数字不会过期。

输出目录默认 D:\\AI_Quant_System\\public\\ashare-open-data

用法：
  python ai_quant/open_data_export.py                # 重建全部
  python ai_quant/open_data_export.py --skip-minute  # 跳过分钟线（较慢）
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import shutil
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

warnings.filterwarnings("ignore", message="pandas only supports SQLAlchemy")

sys.path.insert(0, r"D:\AI_Quant_System\core")
import psycopg2  # noqa: E402
from config import DB_TRADE as DB  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(r"D:\AI_Quant_System")
OUT = Path(os.environ.get("OPEN_DATA_OUT") or (ROOT / "public" / "ashare-open-data"))
AMOUNT_CAP = 5e11
PREV: dict[str, dict] = {}
CODE_FILES = [
    "research_momentum.py", "research_momentum_sweep.py", "research_factor_zoo.py",
    "t0_research.py", "t0_research_gap.py", "t0_research_gap2.py",
    "t0_backtest_v3.py", "t_gap_shadow.py", "daily_review.py", "target_tracker.py",
    "open_data_export.py", "open_data_github.py",
]


def conn():
    return psycopg2.connect(**DB)


def write_csv(df: pd.DataFrame, path: Path) -> None:
    """固定字节输出：gzip 的 mtime 置 0，排序与小数位由调用方保证。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".gz":
        with path.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz, \
                io.TextIOWrapper(gz, encoding="utf-8", newline="") as txt:
            df.to_csv(txt, index=False, float_format="%.4f")
    else:
        df.to_csv(path, index=False, float_format="%.4f", encoding="utf-8")


def clean_daily(df: pd.DataFrame) -> pd.DataFrame:
    """挡掉两类已知脏数据，宁可少发一天也不发假数据：

    1. 2026-11 出现的整段错误价（000004 等 7 只票的 close 被写成 3000+）；
    2. 盘中同步中断造成的残缺交易日（2026-11-12 只有 20 行，正常 ~5,966 行）。
    """
    from datetime import date as _date
    df = df[~((df["close"] > 3000) & (df["date"] >= _date(2000, 1, 1)))]
    cnt = df.groupby("date").size()
    if len(cnt) > 1:
        med = cnt.expanding(min_periods=1).median().shift(1)
        refer = med.fillna(cnt)
        bad = set(cnt[(refer > 100) & (cnt < 0.3 * refer)].index)
        if bad:
            df = df[~df["date"].isin(bad)]
    return df


def export_daily(c) -> list[dict]:
    out = []
    c.execute("SELECT min(trade_time)::date, max(trade_time)::date FROM price_history "
              "WHERE timeframe='daily' AND symbol ~ '^(60|68|00|30)[0-9]{4}$'")
    d0, d1 = c.fetchone()
    current_year = datetime.now().year
    for year in range(d0.year, d1.year + 1):
        rel_year = f"data/daily/{year}.csv.gz"
        if year < current_year and (OUT / rel_year).exists() and rel_year in PREV:
            # 历史年份已封版：连数据库都不用再查
            out.append({"file": rel_year, "rows": PREV[rel_year]["rows"],
                        "range": PREV[rel_year]["range"]})
            continue
        df = pd.read_sql("""
            SELECT symbol,
                   trade_time::date AS date,
                   open, high, low, close,
                   volume,
                   COALESCE(amount, volume * close) AS amount,
                   adj_factor
              FROM price_history
             WHERE timeframe='daily'
               AND symbol ~ '^(60|68|00|30)[0-9]{4}$'
               AND trade_time::date BETWEEN %s AND %s
             ORDER BY symbol, trade_time
        """, c.connection, params=(f"{year}-01-01", f"{year}-12-31"))
        if df.empty:
            continue
        df["close"] = df["close"].astype(float)
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype("int64")
        a = pd.to_numeric(df["amount"], errors="coerce")
        df["amount"] = a.where(a <= AMOUNT_CAP).round(2)
        df = clean_daily(df)
        if df.empty:
            continue

        if year < current_year:
            write_csv(df, OUT / rel_year)
            out.append({"file": rel_year, "rows": len(df),
                        "range": [str(df["date"].min()), str(df["date"].max())]})
            continue

        d = OUT / "data" / "daily" / str(year)
        keep = set()
        for date, g in df.groupby("date", sort=True):
            name = f"{date}.csv.gz"
            write_csv(g, d / name)
            keep.add(name)
            out.append({"file": f"data/daily/{year}/{name}", "rows": len(g),
                        "range": [str(date), str(date)]})
        if d.exists():
            for f in d.glob("*.csv.gz"):
                if f.name not in keep:
                    f.unlink()
        legacy = OUT / "data" / "daily" / f"{year}.csv.gz"
        if legacy.exists():
            legacy.unlink()
    return out


def export_minute(c) -> list[dict]:
    out = []
    for tf in ("5m", "1m"):
        df = pd.read_sql(f"""
            SELECT symbol, ts, open, high, low, close, volume, amount
              FROM minute_bar WHERE tf='{tf}' ORDER BY symbol, ts
        """, c.connection)
        if df.empty:
            continue
        df["ts"] = pd.to_datetime(df["ts"])
        df["date"] = df["ts"].dt.strftime("%Y-%m-%d")
        df["ts"] = df["ts"].dt.strftime("%Y-%m-%d %H:%M:%S")
        d = OUT / "data" / "minute" / tf
        keep = set()
        for date, g in df.groupby("date", sort=True):
            name = f"{date}.csv.gz"
            write_csv(g.drop(columns=["date"]), d / name)
            keep.add(name)
            out.append({"file": f"data/minute/{tf}/{name}", "rows": len(g),
                        "range": [g["ts"].min(), g["ts"].max()]})
        if d.exists():
            for f in d.glob("*.csv.gz"):
                if f.name not in keep:
                    f.unlink()
        legacy = OUT / "data" / "minute" / f"{tf}.csv.gz"
        if legacy.exists():
            legacy.unlink()
    return out


def export_paper(c) -> list[dict]:
    out = []
    eq = pd.read_sql("""
        SELECT trade_date, cash, margin_used, equity, unrealized_pnl, positions
          FROM paper_equity_daily WHERE account_id=1 ORDER BY trade_date
    """, c.connection)
    eq["positions"] = eq["positions"].apply(lambda x: json.dumps(x, ensure_ascii=False) if x else "")
    write_csv(eq, OUT / "data" / "paper" / "equity_daily.csv")
    out.append({"file": "data/paper/equity_daily.csv", "rows": len(eq),
                "range": [str(eq["trade_date"].min()), str(eq["trade_date"].max())]})

    tr = pd.read_sql("""
        SELECT id, symbol, side, quantity, price, commission, realized_pnl,
               (trade_time + interval '8 hour') AS trade_time_bj
          FROM paper_trade WHERE account_id=1 ORDER BY id
    """, c.connection)
    tr["trade_time_bj"] = pd.to_datetime(tr["trade_time_bj"]).dt.strftime("%Y-%m-%d %H:%M:%S")
    write_csv(tr, OUT / "data" / "paper" / "trades.csv")
    out.append({"file": "data/paper/trades.csv", "rows": len(tr),
                "range": [tr["trade_time_bj"].min(), tr["trade_time_bj"].max()]})

    od = pd.read_sql("""
        SELECT id, symbol, side, quantity, filled_price, status, reject_reason,
               (created_time + interval '8 hour') AS created_time_bj
          FROM paper_order ORDER BY id
    """, c.connection)
    od["created_time_bj"] = pd.to_datetime(od["created_time_bj"]).dt.strftime("%Y-%m-%d %H:%M:%S")
    write_csv(od, OUT / "data" / "paper" / "orders.csv")
    out.append({"file": "data/paper/orders.csv", "rows": len(od),
                "range": [od["created_time_bj"].min(), od["created_time_bj"].max()]})
    return out


def export_symbols() -> list[dict]:
    src = ROOT / "data" / "name_cache.json"
    if not src.exists():
        return []
    try:
        d = json.loads(src.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(d, dict):
        rows = [{"symbol": k, "name": v} for k, v in d.items()
                if str(k).isdigit() and isinstance(v, str)]
    else:
        return []
    if not rows:
        return []
    df = pd.DataFrame(rows).sort_values("symbol")
    p = OUT / "data" / "symbols.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(p, index=False, encoding="utf-8")
    return [{"file": "data/symbols.csv", "rows": len(df)}]


def copy_misc() -> list[dict]:
    out = []
    # 每日复盘
    rd = ROOT / "reports" / "daily"
    if rd.exists():
        dst = OUT / "data" / "reviews"
        dst.mkdir(parents=True, exist_ok=True)
        n = 0
        for f in sorted(rd.glob("*.md")):
            shutil.copy2(f, dst / f.name)
            n += 1
        idx = rd / "index.jsonl"
        if idx.exists():
            shutil.copy2(idx, dst / "index.jsonl")
        if n:
            out.append({"file": "data/reviews/", "rows": n})
    # 影子盘样本
    sh = ROOT / "reports" / "t_gap_shadow.jsonl"
    if sh.exists():
        d = OUT / "data" / "shadow"
        d.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sh, d / "gap_down.jsonl")
        n = sum(1 for line in sh.read_text(encoding="utf-8").splitlines() if line.strip())
        out.append({"file": "data/shadow/gap_down.jsonl", "rows": n})
        sm = ROOT / "reports" / "t_gap_shadow_summary.json"
        if sm.exists():
            shutil.copy2(sm, d / "gap_down_summary.json")
    # 研究结果
    rs = ROOT / "reports" / "research"
    if rs.exists():
        d = OUT / "data" / "research"
        d.mkdir(parents=True, exist_ok=True)
        for f in sorted(rs.glob("*.json")):
            shutil.copy2(f, d / f.name)
            out.append({"file": f"data/research/{f.name}", "rows": None})
    return out


def copy_code() -> list[dict]:
    out = []
    d = OUT / "code"
    d.mkdir(parents=True, exist_ok=True)
    for name in CODE_FILES:
        src = ROOT / "ai_quant" / name
        if src.exists():
            shutil.copy2(src, d / name)
            out.append({"file": f"code/{name}", "rows": None})
    return out


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_readme(files: list[dict], generated: str) -> None:
    def stat(name):
        for f in files:
            if f["file"] == name:
                return f
        return None

    daily = [f for f in files if f["file"].startswith("data/daily/")]
    rows_daily = sum(f["rows"] or 0 for f in daily)
    minute = [f for f in files if f["file"].startswith("data/minute/")]
    paper = stat("data/paper/equity_daily.csv")
    trades = stat("data/paper/trades.csv")
    shadow = stat("data/shadow/gap_down.jsonl")

    txt = f"""# A 股公开数据集 · 实盘（模拟）复盘样本

> 由一套跑在本机的量化系统每日自动导出。**所有交易都是模拟盘**，不涉及真实资金。

## 里面有什么

| 目录 | 内容 | 规模 |
|---|---|---|
| `data/daily/` | A 股日线 OHLCV：历史年份按年打包（不再重写），当年按交易日分文件（每天只增一个） | {len(daily)} 个文件、**{rows_daily:,}** 行 |
| `data/minute/` | 5 分钟与 1 分钟线，按交易日分文件 | {len(minute)} 个文件 |
| `data/paper/` | 模拟盘账本：每日净值 {paper['rows'] if paper else 0} 行、成交 {trades['rows'] if trades else 0} 笔、委托全量 |
| `data/reviews/` | 每个交易日的收盘复盘（Markdown）+ 机器可读索引 |
| `data/shadow/` | 影子盘前瞻样本：跳空低开候选的逐日记录（**只记录不下单**） |
| `data/research/` | 因子扫描与动量回测的原始输出（JSON） |
| `code/` | 生成上述结论的脚本（研究与复盘） |

## 为什么值得看

- **日线是完整面板**，不是抽样：沪深主板 / 创业板 / 科创板全部代码、逐日。
- **复盘是带结论的**：每天的 Markdown 里有当日成交、持仓、目标核对（月化 / 回撤 / 单笔风险）与实际 PASS/FAIL 判定，亏了也照写。
- **影子盘是真正的前瞻样本**：信号在记录前就固定了规则，不存在事后拟合。每条样本都同时记录了同窗口全市场中位数收益，可以直接算超额。
- **研究结论是否定的也公开**：`data/research/` 里包含被证伪的信号（动量、反转、VWAP 回归、开盘突破、低开反转），以及证伪用的统计量。

## 已知边界（先说清楚）

- 价格是**未复权**价，跨除权日会跳变；`adj_factor` 列缺失较多，长周期收益请自行用除权信息复权。
- 覆盖沪深两市（`60/68/00/30` 开头），**不含北交所**（`8/4` 开头）；退市股覆盖不完整，做历史股票池时要注意幸存者偏差。
- `amount` 原始字段大面积为空，本数据集用 `volume × close` 估算，超过 5000 亿的脏值已置空。
- 两类脏数据会被挡在门外：单日覆盖不足近 20 日中枢 30%（盘中同步中断）整天不发；2000 年后 `close > 3000` 的异常行剔除。
- 模拟盘账本来自本机策略，**不是投资建议**，也不代表任何真实账户。

## 相关项目（开源 A 股数据）

| 项目 | 许可 | 定位 |
|---|---|---|
| [akfamily/akshare](https://github.com/akfamily/akshare) | MIT | 覆盖面最广的取数接口库，不带历史落盘 |
| [rootSunc/CNEquity](https://github.com/rootSunc/CNEquity) | Apache-2.0 | 本地数据湖：86 个数据集、复权与 PIT、保留退市股 |
| [1nchaos/adata](https://github.com/1nchaos/adata) | Apache-2.0 | 面向量化的免费 A 股数据库 |
| [mpquant/Ashare](https://github.com/mpquant/Ashare) | 未声明 | 新浪/腾讯双源实时行情，最简 API |

本仓库与它们的区别：**直接给可下载的版本化快照**，不用自己跑采集；代价是只覆盖行情与模拟盘复盘，没有财务、PIT、退市股补全。

## 数据字典

见 [DATA_DICT.md](DATA_DICT.md)。`manifest.json` 记录每个文件的行数、字节数、sha256 与时间范围。

## 使用声明

- 行情数据来源于公开渠道，仅供研究与学习使用；如需商用请自行取得交易所授权。
- 复盘与账本数据来自本机模拟盘，**不是投资建议**，也不代表任何真实账户。
- 代码以 MIT 许可发布；数据部分请遵守上述声明。

## 更新节奏

每个交易日收盘后自动重建并提交：日线、净值、复盘、影子盘样本。
"""
    (OUT / "README.md").write_text(txt, encoding="utf-8")

    dd = """# 数据字典

## data/daily/

两种分片，列完全相同：

- `data/daily/YYYY.csv.gz`：**历史年份**（完整、不再重写）。
- `data/daily/YYYY/YYYY-MM-DD.csv.gz`：**当年**，一个交易日一个文件，每天只新增一个。

| 列 | 类型 | 说明 |
|---|---|---|
| symbol | str | 6 位代码（60/68 沪市，00/30 深市） |
| date | date | 交易日 |
| open/high/low/close | float | 未复权价，4 位小数 |
| volume | int | 成交量（股） |
| amount | float | 成交额（元）。原始字段大面积为空，此处为 `COALESCE(amount, volume*close)`，并对超过 5000 亿的脏值置空 |
| adj_factor | float | 复权因子，缺失较多，谨慎使用 |

> 注意：`close` 为未复权价，跨越除权日会出现价格跳变。做长周期收益计算前需自行复权。

## data/minute/5m/YYYY-MM-DD.csv.gz | data/minute/1m/YYYY-MM-DD.csv.gz

| 列 | 说明 |
|---|---|
| symbol | 6 位代码 |
| ts | 北京时间 `YYYY-MM-DD HH:MM:SS`，为该 bar 的**结束**时刻 |
| open/high/low/close | bar 内价格 |
| volume | 成交量（股） |
| amount | 成交额（元），5m 完整，1m 部分缺失 |

## data/paper/equity_daily.csv

模拟盘每日收盘净值快照。`equity = cash + margin_used + unrealized_pnl`。
`positions` 为当日持仓的 JSON 快照。

## data/paper/trades.csv

模拟盘成交明细。`quantity` 单位为**手**（1 手 = 100 股），`price` 为成交价，
`commission` 为佣金（元），`realized_pnl` 为平仓已实现盈亏（元）。
`trade_time_bj` 为北京时间。

## data/paper/orders.csv

全部委托，含被拒绝的。`status` 为 `FILLED` / `REJECTED` / `PENDING` 等，
`reject_reason` 记录风控拒绝原因（如 `STOP_LOSS`、`DRAWDOWN_LIMIT`）。

## data/shadow/gap_down.jsonl

每行一个交易日。规则：当日开盘相对前收跳空 ≤ -2% 的**全部**流动性标的
（近 60 日成交额中位数 ≥ 5000 万、股价 ≥ 3 元），
以 10:00 的 5 分钟收盘价买入、11:30 的 5 分钟收盘价卖出，每笔 2 万元。

| 字段 | 说明 |
|---|---|
| market_excess_pct | 同窗口全市场（流动性池）中位数收益，用于算超额 |
| legs[].gap_pct | 跳空幅度 |
| legs[].gross_pct | 未扣成本收益 |
| legs[].cost_pct | 往返成本（佣金 + 印花税 + 双边 1 tick） |
| legs[].net_pct | 扣成本后收益 |
| legs[].excess_pct | `net_pct - market_excess_pct` |

## data/reviews/YYYY-MM-DD.md

当日复盘：净值、当日盈亏、目标核对（月化 / 最大回撤 / 单笔最大亏损）、
当日成交、持仓、被拒绝的委托。

`index.jsonl` 为机器可读版本，每行一个交易日。
"""
    (OUT / "DATA_DICT.md").write_text(dd, encoding="utf-8")

    lic = """MIT License

Copyright (c) 2026 fhfj7vtkgj-ctrl

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

--- 

行情数据部分：来源于公开渠道，仅供研究与学习使用；商用需自行取得交易所授权。
"""
    (OUT / "LICENSE").write_text(lic, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-minute", action="store_true")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    generated = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")

    global PREV
    try:
        old = json.loads((OUT / "manifest.json").read_text(encoding="utf-8"))
        PREV = {f["file"]: f for f in old.get("files", []) if f.get("file")}
    except Exception:
        PREV = {}

    c = conn().cursor()
    files: list[dict] = []
    files += export_daily(c)
    if not args.skip_minute:
        files += export_minute(c)
    files += export_paper(c)
    c.connection.close()

    files += export_symbols()
    files += copy_misc()
    files += copy_code()

    # 补齐 sha256 / 字节数
    for f in files:
        p = OUT / f["file"]
        if p.is_file():
            f["bytes"] = p.stat().st_size
            f["sha256"] = sha256(p)[:16]
        else:
            f["bytes"] = None
            f["sha256"] = None

    write_readme(files, generated)
    manifest = {
        "generated_at": generated,
        "files": files,
        "total_bytes": sum(f["bytes"] or 0 for f in files),
        "total_rows": sum(f["rows"] or 0 for f in files),
    }
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "out": str(OUT), "generated_at": generated,
        "files": len(files), "total_rows": manifest["total_rows"],
        "total_mb": round(manifest["total_bytes"] / 1e6, 1),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

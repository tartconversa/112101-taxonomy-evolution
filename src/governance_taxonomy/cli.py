"""命令行入口：synth / build / report / demo。

用法（仓库根目录）：
    PYTHONPATH=src python -m governance_taxonomy demo
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from . import etl, queries, synth


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="governance_taxonomy",
        description="治理分类演进数仓（离线运行，仅标准库）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_synth = sub.add_parser("synth", help="生成合成源数据 CSV")
    p_synth.add_argument("--out", default="data/synthetic", help="输出目录")
    p_synth.add_argument("--seed", type=int, default=20260701, help="随机种子")

    p_build = sub.add_parser("build", help="加载源数据并构建数仓")
    p_build.add_argument("--sources", default="sources", help="源 CSV 目录")
    p_build.add_argument("--db", default="data/warehouse.db", help="SQLite 输出路径")

    p_report = sub.add_parser("report", help="打印治理报表")
    p_report.add_argument("--db", default="data/warehouse.db", help="SQLite 数仓路径")

    p_demo = sub.add_parser("demo", help="一键演示：合成 + 构建 + 报表")
    p_demo.add_argument("--dir", default="data", help="演示产物目录")
    p_demo.add_argument("--seed", type=int, default=20260701, help="随机种子")

    args = parser.parse_args(argv)

    if args.command == "synth":
        counts = synth.generate(args.out, seed=args.seed)
        print(f"合成数据已写入 {args.out}/")
        for name, n in counts.items():
            print(f"  {name}: {n} 行")
        return 0

    if args.command == "build":
        stats = etl.build_file(args.db, args.sources)
        print(f"数仓已构建：{args.db}（源目录 {args.sources}）")
        for table, n in stats.items():
            print(f"  {table}: {n} 行")
        return 0

    if args.command == "report":
        conn = etl.connect(args.db)
        try:
            print_report(conn)
        finally:
            conn.close()
        return 0

    if args.command == "demo":
        base = Path(args.dir)
        sources = base / "synthetic"
        db = base / "warehouse.db"
        counts = synth.generate(sources, seed=args.seed)
        print(f"[1/3] 合成数据已写入 {sources}/（{sum(counts.values())} 行）")
        stats = etl.build_file(db, sources)
        print(f"[2/3] 数仓已构建：{db}（事件 {stats['fact_event']} 条，"
              f"复核 {stats['fact_review']} 条）")
        print("[3/3] 治理报表")
        conn = etl.connect(db)
        try:
            print_report(conn)
        finally:
            conn.close()
        return 0

    return 1


def print_report(conn: sqlite3.Connection) -> None:
    print("\n== 月度总览（两种口径共用同一事件总量）==")
    _print_table(
        ["月份", "事件数", "已复核", "滞后提交", "待审", "无法转换"],
        [(r["event_month"], r["events"], r["reviewed"], r["lag_or_unknown"],
          r["pending_review"], r["unmappable"]) for r in queries.monthly_overview(conn)],
    )

    print("\n== 趋势 · 事件发生时口径（as-was，按提交类别）==")
    _print_pivot(queries.trend_aswas(conn), "aswas_code")

    print("\n== 趋势 · 当前可比口径（as-is，含待审/无法转换桶）==")
    _print_pivot(queries.trend_asis(conn), "asis_bucket")

    print("\n== 对账（每月两口径合计必须等于事件数）==")
    recon = queries.reconciliation(conn)
    _print_table(
        ["月份", "事件数", "as-was 合计", "as-is 合计", "一致"],
        [(r["event_month"], r["events"], r["aswas_total"], r["asis_total"],
          "OK" if r["ok"] else "不一致!") for r in recon],
    )

    lag = queries.lag_events(conn)
    print(f"\n== 切换期滞后提交（{len(lag)} 条，各计一次，不重复计入新旧类别）==")
    _print_table(
        ["事件", "月份", "平台", "接收时间", "提交代码", "提交版本", "as-is 落点"],
        [(r["event_id"], r["event_month"], r["source_platform"], r["received_at"],
          r["aswas_code"], r["source_taxonomy_version"], r["asis_bucket"])
         for r in lag[:10]],
        suffix="..." if len(lag) > 10 else "",
    )

    pending = queries.pending_events(conn)
    print(f"\n== 待审事件（{len(pending)} 条，定义歧义或无对应项，未落入默认类）==")
    _print_table(
        ["事件", "月份", "原类别", "待审原因", "复核状态"],
        [(r["event_id"], r["event_month"], r["aswas_code"], r["conversion_reason"],
          r["latest_review_state"]) for r in pending[:10]],
        suffix="..." if len(pending) > 10 else "",
    )

    unmappable = queries.unmappable_events(conn)
    print(f"\n== 无法可靠转换的历史记录（{len(unmappable)} 条）==")
    _print_table(
        ["事件", "月份", "原类别", "原因"],
        [(r["event_id"], r["event_month"], r["aswas_code"], r["conversion_reason"])
         for r in unmappable[:10]],
        suffix="..." if len(unmappable) > 10 else "",
    )

    print("\n== 世系钻取示例（汇总单元格 -> 事件明细）==")
    _print_lineage_sample(conn)

    issues = queries.dq_issues(conn)
    print(f"\n== 数据质量问题（{len(issues)} 条）==")
    if issues:
        _print_table(
            ["级别", "对象", "标识", "说明"],
            [(r["severity"], r["entity"], r["entity_id"], r["message"]) for r in issues],
        )
    else:
        print("  无")


def _print_lineage_sample(conn: sqlite3.Connection) -> None:
    pending = queries.pending_events(conn)
    if pending:
        month = pending[0]["event_month"]
        rows = queries.lineage_asis(conn, month, etl.PENDING_BUCKET)
        print(f"-- 单元格 [{month} × {etl.PENDING_BUCKET}] 共 {len(rows)} 条，前 5 条：")
        _print_table(
            ["事件", "原类别", "定义版本", "版本匹配", "转换状态", "复核状态", "复核人"],
            [(r["event_id"], r["aswas_code"], r["aswas_def_version"],
              r["def_version_match"], r["conversion_status"],
              r["latest_review_state"], r["latest_reviewer_ref"] or "-")
             for r in rows[:5]],
        )
    reviewed = conn.execute(
        "SELECT event_month, asis_bucket FROM mart_trend_asis "
        "WHERE asis_bucket NOT IN (?, ?) ORDER BY event_month DESC, asis_bucket LIMIT 1",
        (etl.PENDING_BUCKET, etl.UNMAPPABLE_BUCKET),
    ).fetchone()
    if reviewed:
        rows = queries.lineage_asis(conn, reviewed["event_month"], reviewed["asis_bucket"])
        print(f"-- 单元格 [{reviewed['event_month']} × {reviewed['asis_bucket']}] "
              f"共 {len(rows)} 条，前 5 条：")
        _print_table(
            ["事件", "原类别", "定义版本", "转换状态", "映射依据", "复核状态", "复核改派"],
            [(r["event_id"], r["aswas_code"], r["aswas_def_version"],
              r["conversion_status"], r["map_record_id"] or "-",
              r["latest_review_state"], r["review_assigned_code"] or "-")
             for r in rows[:5]],
        )


def _print_pivot(rows: list[sqlite3.Row], key_column: str) -> None:
    months = sorted({r["event_month"] for r in rows})
    keys = sorted({r[key_column] for r in rows},
                  key=lambda k: (k in (etl.PENDING_BUCKET, etl.UNMAPPABLE_BUCKET), k))
    cell = {(r["event_month"], r[key_column]): r["events"] for r in rows}
    _print_table(
        ["月份", *keys, "合计"],
        [([m, *[cell.get((m, k), 0) for k in keys],
           sum(cell.get((m, k), 0) for k in keys)]) for m in months],
    )


def _print_table(headers: list[str], rows: list[tuple], suffix: str = "") -> None:
    if not rows:
        print("  （无记录）")
        return
    str_rows = [["" if v is None else str(v) for v in row] for row in rows]
    widths = [
        max(len(str(h)), *(len(r[i]) for r in str_rows))
        for i, h in enumerate(headers)
    ]
    line = "  " + "  ".join(str(h).ljust(widths[i]) for i, h in enumerate(headers))
    print(line)
    print("  " + "  ".join("-" * widths[i] for i in range(len(headers))))
    for r in str_rows:
        print("  " + "  ".join(r[i].ljust(widths[i]) for i in range(len(headers))))
    if suffix:
        print(f"  {suffix}")

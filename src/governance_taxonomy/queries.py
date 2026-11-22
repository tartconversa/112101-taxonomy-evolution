"""面向治理人员的查询函数。

每个函数返回 sqlite3.Row 列表，调用方可自行格式化。
所有汇总均可经 lineage_* 函数下钻到事件级明细，
明细中包含原始类别、事件时定义版本与人工复核状态。
"""

from __future__ import annotations

import sqlite3

from .etl import PENDING_BUCKET, UNMAPPABLE_BUCKET


def monthly_overview(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """每月事件总量、复核量、滞后提交量与两个特殊桶的规模。"""
    return conn.execute(
        """
        SELECT event_month,
               COUNT(*) AS events,
               SUM(latest_review_state <> 'none') AS reviewed,
               SUM(def_version_match <> 'exact') AS lag_or_unknown,
               SUM(asis_bucket = ?) AS pending_review,
               SUM(asis_bucket = ?) AS unmappable
        FROM bridge_event_classification
        GROUP BY event_month ORDER BY event_month
        """,
        (PENDING_BUCKET, UNMAPPABLE_BUCKET),
    ).fetchall()


def trend_aswas(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """事件发生时口径：按提交时的类别统计。"""
    return conn.execute(
        "SELECT * FROM mart_trend_aswas ORDER BY event_month, aswas_code"
    ).fetchall()


def trend_asis(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """当前可比口径：按当前分类统计，含 PENDING_REVIEW / UNMAPPABLE 桶。"""
    return conn.execute(
        "SELECT * FROM mart_trend_asis ORDER BY event_month, asis_bucket"
    ).fetchall()


def reconciliation(conn: sqlite3.Connection) -> list[dict]:
    """对账：每月两种口径的合计都必须等于当月事件数（每事件只计一次）。"""
    months = [r["event_month"] for r in conn.execute(
        "SELECT DISTINCT event_month FROM bridge_event_classification ORDER BY 1"
    )]
    rows = []
    for month in months:
        events = conn.execute(
            "SELECT COUNT(*) AS n FROM bridge_event_classification WHERE event_month = ?",
            (month,),
        ).fetchone()["n"]
        aswas = conn.execute(
            "SELECT COALESCE(SUM(events), 0) AS n FROM mart_trend_aswas WHERE event_month = ?",
            (month,),
        ).fetchone()["n"]
        asis = conn.execute(
            "SELECT COALESCE(SUM(events), 0) AS n FROM mart_trend_asis WHERE event_month = ?",
            (month,),
        ).fetchone()["n"]
        rows.append({
            "event_month": month, "events": events,
            "aswas_total": aswas, "asis_total": asis,
            "ok": events == aswas == asis,
        })
    return rows


def pending_events(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """待审事件：定义有歧义或无对应项，需要人工复核，未落入任何默认类。"""
    return conn.execute(
        """
        SELECT event_id, event_month, aswas_code, conversion_reason,
               latest_review_state, latest_reviewer_ref
        FROM bridge_event_classification
        WHERE conversion_status = 'pending_review'
        ORDER BY event_month, event_id
        """
    ).fetchall()


def unmappable_events(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """无法可靠转换的历史记录（维护方声明无后继类别）。"""
    return conn.execute(
        """
        SELECT event_id, event_month, aswas_code, conversion_reason,
               latest_review_state
        FROM bridge_event_classification
        WHERE conversion_status = 'unmappable'
        ORDER BY event_month, event_id
        """
    ).fetchall()


def lag_events(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """切换期滞后提交：接收时提交的类别版本已不在有效期内。"""
    return conn.execute(
        """
        SELECT b.event_id, b.event_month, f.source_platform, f.received_at,
               b.aswas_code, f.source_taxonomy_version, b.def_version_match,
               b.asis_bucket, b.conversion_status
        FROM bridge_event_classification b
        JOIN fact_event f USING (event_sk)
        WHERE b.def_version_match <> 'exact'
        ORDER BY f.received_at
        """
    ).fetchall()


def lineage_aswas(
    conn: sqlite3.Connection, event_month: str, aswas_code: str
) -> list[sqlite3.Row]:
    """下钻 as-was 汇总单元格：返回构成该单元格的全部事件及世系字段。"""
    return _lineage(conn, "b.aswas_code", event_month, aswas_code)


def lineage_asis(
    conn: sqlite3.Connection, event_month: str, asis_bucket: str
) -> list[sqlite3.Row]:
    """下钻 as-is 汇总单元格（含 PENDING_REVIEW / UNMAPPABLE 桶）。"""
    return _lineage(conn, "b.asis_bucket", event_month, asis_bucket)


def _lineage(
    conn: sqlite3.Connection, key_column: str, event_month: str, key: str
) -> list[sqlite3.Row]:
    assert key_column in ("b.aswas_code", "b.asis_bucket")
    return conn.execute(
        f"""
        SELECT b.event_id, f.content_ref, f.received_at, f.source_platform,
               b.aswas_code, av.version AS aswas_def_version,
               b.def_version_match,
               b.asis_code, b.asis_bucket, b.conversion_status, b.conversion_reason,
               b.map_record_id,
               b.latest_review_state, b.latest_reviewed_at,
               b.latest_reviewer_ref, b.review_assigned_code
        FROM bridge_event_classification b
        JOIN fact_event f USING (event_sk)
        LEFT JOIN dim_category_version av
               ON av.category_version_sk = b.aswas_category_version_sk
        WHERE b.event_month = ? AND {key_column} = ?
        ORDER BY b.event_id
        """,
        (event_month, key),
    ).fetchall()


def dq_issues(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT severity, entity, entity_id, message FROM dq_issue "
        "ORDER BY severity, entity, entity_id"
    ).fetchall()

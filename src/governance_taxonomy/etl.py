"""ETL：源 CSV -> staging -> 维度/事实 -> 双口径桥表 -> 月度汇总。

口径定义：
- as-was（事件发生时口径）：平台提交的原始类别 + 事件接收时刻有效的定义版本。
  切换期仍提交旧代码的事件保留原样并标记 def_version_match = 'lag'。
- as-is（当前可比口径）：最新人工复核优先；无复核时按维护方映射做确定性转换。
  拆分 / 部分适用 / 无映射的情形一律进入 PENDING_REVIEW 桶，
  维护方声明无后继的退役类别进入 UNMAPPABLE 桶，绝不静默落入默认类。
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from .schema import DDL

PENDING_BUCKET = "PENDING_REVIEW"
UNMAPPABLE_BUCKET = "UNMAPPABLE"
NO_REVIEW = "none"

SOURCE_FILES = {
    "stg_taxonomy": "taxonomy.csv",
    "stg_category_map": "category_mappings.csv",
    "stg_content_events": "content_events.csv",
    "stg_classifications": "classifications.csv",
}


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def build_file(db_path: str | Path, sources_dir: str | Path) -> dict:
    """在磁盘上构建数仓；已存在的同名文件会被替换。"""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    conn = connect(db_path)
    try:
        stats = build(conn, sources_dir)
        conn.commit()
        return stats
    finally:
        conn.close()


def build(conn: sqlite3.Connection, sources_dir: str | Path) -> dict:
    """在已打开的连接上完成全部构建步骤，返回各表行数统计。"""
    sources_dir = Path(sources_dir)
    conn.executescript(DDL)
    load_sources(conn, sources_dir)
    _build_dimensions(conn)
    _build_facts(conn)
    _build_bridge(conn)
    _build_marts(conn)
    _validate(conn)
    tables = [
        "stg_taxonomy", "stg_category_map", "stg_content_events", "stg_classifications",
        "dim_category_version", "dim_category_map", "fact_event", "fact_review",
        "bridge_event_classification", "mart_trend_aswas", "mart_trend_asis", "dq_issue",
    ]
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


def load_sources(conn: sqlite3.Connection, sources_dir: Path) -> None:
    for table, filename in SOURCE_FILES.items():
        path = sources_dir / filename
        with path.open(encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            cols = reader.fieldnames or []
            rows = [
                tuple((row[c].strip() or None) if row[c] is not None else None for c in cols)
                for row in reader
            ]
        placeholders = ", ".join("?" for _ in cols)
        conn.executemany(
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})", rows
        )


def _build_dimensions(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO dim_category_version
            (taxonomy_id, code, version, label, valid_from, valid_to,
             parent_code, is_current, source_record_id)
        SELECT taxonomy_id, code, version, label, valid_from, valid_to,
               parent_code, CASE WHEN valid_to IS NULL THEN 1 ELSE 0 END, record_id
        FROM stg_taxonomy
        ORDER BY code, valid_from
        """
    )
    conn.execute(
        """
        INSERT INTO dim_category_map
            (taxonomy_id, from_code, from_version, to_code, to_version,
             map_type, coverage, effective_from, note, source_record_id)
        SELECT taxonomy_id, from_code, from_version, to_code, to_version,
               map_type, coverage, effective_from, note, record_id
        FROM stg_category_map
        """
    )


def _resolve_def_version(versions: list[sqlite3.Row], day: str) -> tuple[sqlite3.Row, str]:
    """返回 (定义版本, 匹配方式)。valid_to 含当日；无覆盖版本时取最近版本并记 'lag'。"""
    for v in versions:
        if v["valid_from"] <= day and (v["valid_to"] is None or day <= v["valid_to"]):
            return v, "exact"
    return versions[-1], "lag"


def _build_facts(conn: sqlite3.Connection) -> None:
    versions_by_code: dict[str, list[sqlite3.Row]] = {}
    for row in conn.execute(
        "SELECT * FROM dim_category_version ORDER BY code, valid_from"
    ):
        versions_by_code.setdefault(row["code"], []).append(row)

    for ev in conn.execute("SELECT * FROM stg_content_events ORDER BY record_id"):
        day = ev["received_at"][:10]
        versions = versions_by_code.get(ev["source_code"])
        if versions is None:
            def_sk, match = None, "unknown_code"
        else:
            version_row, match = _resolve_def_version(versions, day)
            def_sk = version_row["category_version_sk"]
        conn.execute(
            """
            INSERT INTO fact_event
                (event_id, content_ref, received_at, event_month, source_platform,
                 source_code, source_taxonomy_version, aswas_category_version_sk,
                 def_version_match, source_record_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ev["event_id"], ev["content_ref"], ev["received_at"], day[:7],
                ev["source_platform"], ev["source_code"], ev["source_taxonomy_version"],
                def_sk, match, ev["record_id"],
            ),
        )

    event_sk_by_id = {
        r["event_id"]: r["event_sk"] for r in conn.execute("SELECT event_id, event_sk FROM fact_event")
    }
    inserted: list[dict] = []
    for rv in conn.execute("SELECT * FROM stg_classifications ORDER BY record_id"):
        event_sk = event_sk_by_id.get(rv["event_id"])
        if event_sk is None:
            conn.execute(
                "INSERT INTO dq_issue (severity, entity, entity_id, message) VALUES (?, ?, ?, ?)",
                ("error", "classification", rv["classification_id"],
                 f"复核记录引用了未知事件 {rv['event_id']}，已跳过"),
            )
            continue
        inserted.append(
            {
                "classification_id": rv["classification_id"],
                "event_sk": event_sk,
                "reviewed_at": rv["reviewed_at"],
                "assigned_code": rv["assigned_code"],
                "reviewer_ref": rv["reviewer_ref"],
                "review_state": rv["review_state"],
                "source_record_id": rv["record_id"],
            }
        )
    latest_by_event: dict[int, str] = {}
    for row in inserted:
        key = (row["reviewed_at"], row["classification_id"])
        cur = latest_by_event.get(row["event_sk"])
        if cur is None or key > cur:
            latest_by_event[row["event_sk"]] = key
    for row in inserted:
        is_latest = int(
            latest_by_event[row["event_sk"]]
            == (row["reviewed_at"], row["classification_id"])
        )
        conn.execute(
            """
            INSERT INTO fact_review
                (classification_id, event_sk, reviewed_at, assigned_code,
                 reviewer_ref, review_state, is_latest, source_record_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["classification_id"], row["event_sk"], row["reviewed_at"],
                row["assigned_code"], row["reviewer_ref"], row["review_state"],
                is_latest, row["source_record_id"],
            ),
        )


def _resolve_through_mapping(
    code: str,
    mappings: dict[str, list[sqlite3.Row]],
    current_codes: set[str],
) -> tuple[str | None, str, str, str | None]:
    """无人工复核（或复核仅确认原类别）时的确定性转换。

    返回 (asis_code, status, reason, map_record_id)。
    """
    if code in current_codes:
        return code, "identity", "current_code", None
    rows = mappings.get(code, [])
    if not rows:
        return None, "pending_review", "no_mapping_defined", None
    for r in rows:
        if r["map_type"] == "none":
            return None, "unmappable", "retired_without_successor", r["source_record_id"]
    if len(rows) == 1:
        r = rows[0]
        if r["to_code"] and r["coverage"] == "all" and r["map_type"] in ("rename", "merge"):
            return r["to_code"], "auto_mapped", f"{r['map_type']}_mapping", r["source_record_id"]
    return None, "pending_review", "ambiguous_split_or_partial", None


def _build_bridge(conn: sqlite3.Connection) -> None:
    versions_by_code: dict[str, list[sqlite3.Row]] = {}
    current_codes: set[str] = set()
    current_sk: dict[str, int] = {}
    for row in conn.execute(
        "SELECT * FROM dim_category_version ORDER BY code, valid_from"
    ):
        versions_by_code.setdefault(row["code"], []).append(row)
        if row["is_current"]:
            current_codes.add(row["code"])
            current_sk[row["code"]] = row["category_version_sk"]
    latest_sk = {code: vs[-1]["category_version_sk"] for code, vs in versions_by_code.items()}

    mappings: dict[str, list[sqlite3.Row]] = {}
    for row in conn.execute("SELECT * FROM dim_category_map"):
        mappings.setdefault(row["from_code"], []).append(row)

    latest_reviews = {
        r["event_sk"]: r
        for r in conn.execute("SELECT * FROM fact_review WHERE is_latest = 1")
    }

    known_codes = set(versions_by_code)
    for ev in conn.execute("SELECT * FROM fact_event ORDER BY event_sk"):
        source_code = ev["source_code"]
        review = latest_reviews.get(ev["event_sk"])
        state = review["review_state"] if review else NO_REVIEW
        assigned = review["assigned_code"] if review else None
        map_record_id = None

        if state == "changed" and assigned:
            if assigned in known_codes:
                asis, status, reason = assigned, "reviewed", "review_changed"
            else:
                asis, status, reason = None, "pending_review", "review_assigned_unknown_code"
        elif state == "confirmed":
            base = assigned or source_code
            asis, st2, reason2, map_record_id = _resolve_through_mapping(
                base, mappings, current_codes
            )
            if st2 in ("identity", "auto_mapped"):
                status, reason = "reviewed", f"review_confirmed_{reason2}"
            else:
                status, reason = st2, reason2
        elif state == "needs_review":
            asis, status, reason = None, "pending_review", "review_requested"
        else:
            asis, status, reason, map_record_id = _resolve_through_mapping(
                source_code, mappings, current_codes
            )

        if asis is not None:
            asis_sk = current_sk.get(asis, latest_sk.get(asis))
            bucket = asis
        else:
            asis_sk = None
            bucket = PENDING_BUCKET if status == "pending_review" else UNMAPPABLE_BUCKET

        conn.execute(
            """
            INSERT INTO bridge_event_classification
                (event_sk, event_id, event_month, aswas_code,
                 aswas_category_version_sk, def_version_match,
                 asis_code, asis_bucket, asis_category_version_sk,
                 conversion_status, conversion_reason, map_record_id,
                 latest_review_state, latest_reviewed_at,
                 latest_reviewer_ref, review_assigned_code)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ev["event_sk"], ev["event_id"], ev["event_month"], source_code,
                ev["aswas_category_version_sk"], ev["def_version_match"],
                asis, bucket, asis_sk, status, reason, map_record_id,
                state,
                review["reviewed_at"] if review else None,
                review["reviewer_ref"] if review else None,
                assigned,
            ),
        )


def _build_marts(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO mart_trend_aswas
            (event_month, aswas_code, events, reviewed_events, source_lag_events)
        SELECT event_month, aswas_code, COUNT(*),
               SUM(latest_review_state <> 'none'),
               SUM(def_version_match <> 'exact')
        FROM bridge_event_classification
        GROUP BY event_month, aswas_code
        """
    )
    conn.execute(
        """
        INSERT INTO mart_trend_asis
            (event_month, asis_bucket, events, reviewed_events, auto_mapped_events)
        SELECT event_month, asis_bucket, COUNT(*),
               SUM(latest_review_state <> 'none'),
               SUM(conversion_status = 'auto_mapped')
        FROM bridge_event_classification
        GROUP BY event_month, asis_bucket
        """
    )


def _validate(conn: sqlite3.Connection) -> None:
    """目录级与引用级数据质量检查，结果写入 dq_issue。"""
    issues: list[tuple[str, str, str, str]] = []

    rows = conn.execute(
        """
        SELECT code, GROUP_CONCAT(source_record_id) AS refs, COUNT(*) AS n
        FROM dim_category_version GROUP BY code
        HAVING SUM(is_current) = 0
        """
    ).fetchall()
    mapped_from = {
        r["from_code"] for r in conn.execute("SELECT DISTINCT from_code FROM dim_category_map")
    }
    for r in rows:
        if r["code"] not in mapped_from:
            issues.append(("error", "category", r["code"],
                           "类别已退役但维护方未提供任何映射；相关事件将标记为待审"))

    known_codes = {
        r["code"] for r in conn.execute("SELECT DISTINCT code FROM dim_category_version")
    }
    for r in conn.execute(
        "SELECT source_record_id, to_code FROM dim_category_map WHERE to_code IS NOT NULL"
    ):
        if r["to_code"] not in known_codes:
            issues.append(("error", "category_map", r["source_record_id"],
                           f"映射目标类别 {r['to_code']} 不在分类目录中"))

    by_code: dict[str, list[sqlite3.Row]] = {}
    for r in conn.execute(
        "SELECT code, valid_from, valid_to, source_record_id FROM dim_category_version "
        "ORDER BY code, valid_from"
    ):
        by_code.setdefault(r["code"], []).append(r)
    for code, vs in by_code.items():
        for prev, cur in zip(vs, vs[1:]):
            if prev["valid_to"] is None or cur["valid_from"] <= prev["valid_to"]:
                issues.append(("error", "category", code,
                               f"版本有效期重叠：{prev['source_record_id']} 与 {cur['source_record_id']}"))

    for r in conn.execute(
        "SELECT event_id FROM fact_event WHERE def_version_match = 'unknown_code'"
    ):
        issues.append(("warn", "event", r["event_id"],
                       "平台提交了分类目录中不存在的类别代码"))

    for r in conn.execute(
        "SELECT classification_id, assigned_code FROM fact_review "
        "WHERE review_state = 'changed' AND assigned_code IS NOT NULL"
    ):
        if r["assigned_code"] not in known_codes:
            issues.append(("warn", "classification", r["classification_id"],
                           f"复核指定的类别 {r['assigned_code']} 不在分类目录中"))

    conn.executemany(
        "INSERT INTO dq_issue (severity, entity, entity_id, message) VALUES (?, ?, ?, ?)",
        issues,
    )

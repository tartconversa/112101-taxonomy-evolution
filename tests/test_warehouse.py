"""数仓核心不变量测试。

FixtureWarehouseTests  使用仓库自带的基线 sources/（含拆分、滞后提交、待审样例），
                       对具体事件做精确断言。
SyntheticInvariantTests 使用确定性合成数据（约数百事件），验证全局不变量：
                       每事件只计一次、两口径对账一致、歧义不落默认类等。
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from governance_taxonomy import etl, queries, synth  # noqa: E402


def build_memory(sources_dir: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    etl.build(conn, sources_dir)
    return conn


class FixtureWarehouseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.conn = build_memory(ROOT / "sources")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()

    def bridge_row(self, event_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM bridge_event_classification WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        self.assertIsNotNone(row, event_id)
        return row

    def test_one_bridge_row_per_event(self) -> None:
        events = self.conn.execute("SELECT COUNT(*) AS n FROM fact_event").fetchone()["n"]
        bridge = self.conn.execute(
            "SELECT COUNT(*) AS n FROM bridge_event_classification"
        ).fetchone()["n"]
        self.assertEqual(events, bridge)

    def test_confirmed_split_event_stays_pending(self) -> None:
        # EV-71：C-4 在 v1 下被人工确认，但确认旧类不能替代在 C-41/C-42 之间的裁决
        row = self.bridge_row("EV-71")
        self.assertEqual(row["aswas_code"], "C-4")
        self.assertEqual(row["def_version_match"], "exact")
        self.assertIsNone(row["asis_code"])
        self.assertEqual(row["asis_bucket"], etl.PENDING_BUCKET)
        self.assertEqual(row["conversion_status"], "pending_review")
        self.assertEqual(row["latest_review_state"], "confirmed")

    def test_changed_review_overrides_to_current_code(self) -> None:
        # EV-72：复核将 C-41 改派为 C-42，as-is 以复核结果为准
        row = self.bridge_row("EV-72")
        self.assertEqual(row["aswas_code"], "C-41")
        self.assertEqual(row["asis_code"], "C-42")
        self.assertEqual(row["asis_bucket"], "C-42")
        self.assertEqual(row["conversion_status"], "reviewed")
        self.assertEqual(row["latest_review_state"], "changed")

    def test_lag_submission_flagged_and_counted_once(self) -> None:
        # EV-73：切换后仍提交 v1 的 C-4，且复核状态为 needs_review
        row = self.bridge_row("EV-73")
        self.assertEqual(row["aswas_code"], "C-4")
        self.assertEqual(row["def_version_match"], "lag")
        self.assertEqual(row["asis_bucket"], etl.PENDING_BUCKET)
        self.assertEqual(row["latest_review_state"], "needs_review")

    def test_no_double_count_in_transition_month(self) -> None:
        # 2026-07 共 2 个事件；两种口径各自的合计都必须等于 2
        recon = {r["event_month"]: r for r in queries.reconciliation(self.conn)}
        july = recon["2026-07"]
        self.assertEqual(july["events"], 2)
        self.assertTrue(july["ok"])
        # as-is 口径下退役代码不得作为落点出现
        buckets = {
            r["asis_bucket"]
            for r in self.conn.execute(
                "SELECT asis_bucket FROM mart_trend_asis WHERE event_month = '2026-07'"
            )
        }
        self.assertNotIn("C-4", buckets)

    def test_reconciliation_all_months(self) -> None:
        for row in queries.reconciliation(self.conn):
            self.assertTrue(row["ok"], row)

    def test_lineage_drilldown_matches_mart_cell(self) -> None:
        cell = self.conn.execute(
            "SELECT * FROM mart_trend_asis WHERE asis_bucket = ?",
            (etl.PENDING_BUCKET,),
        ).fetchone()
        rows = queries.lineage_asis(self.conn, cell["event_month"], cell["asis_bucket"])
        self.assertEqual(len(rows), cell["events"])
        for r in rows:
            self.assertIsNotNone(r["aswas_code"])
            self.assertIsNotNone(r["aswas_def_version"])
            self.assertIn(
                r["latest_review_state"], ("confirmed", "changed", "needs_review", "none")
            )


class SyntheticInvariantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        sources = Path(cls.tmp.name) / "synthetic"
        synth.generate(sources)
        cls.conn = build_memory(sources)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()
        cls.tmp.cleanup()

    def test_one_bridge_row_per_event(self) -> None:
        events = self.conn.execute("SELECT COUNT(*) AS n FROM fact_event").fetchone()["n"]
        bridge = self.conn.execute(
            "SELECT COUNT(*) AS n FROM bridge_event_classification"
        ).fetchone()["n"]
        distinct_events = self.conn.execute(
            "SELECT COUNT(DISTINCT event_sk) AS n FROM bridge_event_classification"
        ).fetchone()["n"]
        self.assertEqual(events, bridge)
        self.assertEqual(events, distinct_events)

    def test_reconciliation_all_months(self) -> None:
        rows = queries.reconciliation(self.conn)
        self.assertTrue(rows)
        for row in rows:
            self.assertTrue(row["ok"], row)

    def test_merge_maps_automatically(self) -> None:
        # C-2/C-3 无复核事件应自动并入 C-23，无需人工介入
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS n FROM bridge_event_classification
            WHERE aswas_code IN ('C-2', 'C-3')
              AND latest_review_state = 'none'
              AND asis_code = 'C-23'
              AND conversion_status = 'auto_mapped'
            """
        ).fetchone()
        self.assertGreater(row["n"], 0)

    def test_retired_without_successor_is_unmappable(self) -> None:
        # C-6 无后继：已确认旧类或未复核的事件必须进入 UNMAPPABLE 而非默认类；
        # 被显式标记 needs_review 的保持待审（人工仍可能改派到当前类别）。
        # 无论哪种情形，C-6 事件都不得静默获得当前类别。
        rows = self.conn.execute(
            """
            SELECT latest_review_state, conversion_status, COUNT(*) AS n
            FROM bridge_event_classification
            WHERE aswas_code = 'C-6'
            GROUP BY latest_review_state, conversion_status
            """
        ).fetchall()
        self.assertTrue(rows)
        for r in rows:
            if r["latest_review_state"] == "needs_review":
                self.assertEqual(r["conversion_status"], "pending_review")
            else:
                self.assertEqual(r["conversion_status"], "unmappable")
        converted = self.conn.execute(
            "SELECT COUNT(*) AS n FROM bridge_event_classification "
            "WHERE aswas_code = 'C-6' AND asis_code IS NOT NULL"
        ).fetchone()["n"]
        self.assertEqual(converted, 0)

    def test_no_silent_default_bucket(self) -> None:
        # 每个事件必须落在：当前类别、PENDING_REVIEW 或 UNMAPPABLE 之一
        current = {
            r["code"] for r in self.conn.execute(
                "SELECT code FROM dim_category_version WHERE is_current = 1"
            )
        }
        allowed = current | {etl.PENDING_BUCKET, etl.UNMAPPABLE_BUCKET}
        bad = self.conn.execute(
            "SELECT COUNT(*) AS n FROM bridge_event_classification "
            f"WHERE asis_bucket NOT IN ({', '.join('?' for _ in allowed)})",
            tuple(allowed),
        ).fetchone()["n"]
        self.assertEqual(bad, 0)
        # 且确实存在待审与无法转换的记录（合成数据应覆盖这些情形）
        pending = self.conn.execute(
            "SELECT COUNT(*) AS n FROM bridge_event_classification "
            "WHERE conversion_status = 'pending_review'"
        ).fetchone()["n"]
        unmappable = self.conn.execute(
            "SELECT COUNT(*) AS n FROM bridge_event_classification "
            "WHERE conversion_status = 'unmappable'"
        ).fetchone()["n"]
        self.assertGreater(pending, 0)
        self.assertGreater(unmappable, 0)

    def test_review_change_overrides_mapping(self) -> None:
        # 所有有效的 changed 复核：as-is 必须等于复核指定类别
        bad = self.conn.execute(
            """
            SELECT COUNT(*) AS n FROM bridge_event_classification
            WHERE latest_review_state = 'changed'
              AND review_assigned_code IS NOT NULL
              AND asis_code IS NOT NULL
              AND asis_code <> review_assigned_code
            """
        ).fetchone()["n"]
        self.assertEqual(bad, 0)

    def test_lag_events_exist_and_keep_original_code(self) -> None:
        lag = queries.lag_events(self.conn)
        self.assertTrue(lag)
        for r in lag:
            self.assertIn(r["aswas_code"], ("C-1", "C-2", "C-3", "C-4", "C-5", "C-6"))
            self.assertEqual(r["source_taxonomy_version"], 1)

    def test_lineage_counts_match_mart_cells(self) -> None:
        for cell in self.conn.execute("SELECT * FROM mart_trend_asis"):
            rows = queries.lineage_asis(
                self.conn, cell["event_month"], cell["asis_bucket"]
            )
            self.assertEqual(len(rows), cell["events"], dict(cell))
        for cell in self.conn.execute("SELECT * FROM mart_trend_aswas"):
            rows = queries.lineage_aswas(
                self.conn, cell["event_month"], cell["aswas_code"]
            )
            self.assertEqual(len(rows), cell["events"], dict(cell))

    def test_synth_is_deterministic(self) -> None:
        other = Path(self.tmp.name) / "again"
        synth.generate(other)
        for name in ("taxonomy.csv", "category_mappings.csv",
                     "content_events.csv", "classifications.csv"):
            a = (Path(self.tmp.name) / "synthetic" / name).read_text(encoding="utf-8")
            b = (other / name).read_text(encoding="utf-8")
            self.assertEqual(a, b, name)

    def test_no_dq_errors_on_synthetic_catalog(self) -> None:
        errors = [
            r for r in queries.dq_issues(self.conn) if r["severity"] == "error"
        ]
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()

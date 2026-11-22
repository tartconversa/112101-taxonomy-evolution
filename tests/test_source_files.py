from __future__ import annotations

import csv
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_SOURCES = [
    'category_mappings.csv',
    'classifications.csv',
    'content_events.csv',
    'taxonomy.csv',
]


class SourceFileTests(unittest.TestCase):
    def test_source_files_have_headers_and_records(self) -> None:
        for filename in EXPECTED_SOURCES:
            with self.subTest(filename=filename):
                with (ROOT / "sources" / filename).open(encoding="utf-8", newline="") as stream:
                    rows = list(csv.DictReader(stream))
                self.assertTrue(rows)
                self.assertTrue(rows[0])

    def test_record_identifiers_are_unique_within_each_source(self) -> None:
        for filename in EXPECTED_SOURCES:
            with self.subTest(filename=filename):
                with (ROOT / "sources" / filename).open(encoding="utf-8", newline="") as stream:
                    reader = csv.DictReader(stream)
                    rows = list(reader)
                values = [row["record_id"] for row in rows]
                self.assertTrue(all(values))
                self.assertEqual(len(values), len(set(values)))


if __name__ == "__main__":
    unittest.main()

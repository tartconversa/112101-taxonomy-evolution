"""确定性合成数据生成器。

生成的目录演进覆盖全部治理情形：
- rename  C-1        -> C-11（定义澄清，一对一，可自动转换）
- merge   C-2 + C-3  -> C-23（多对一，可自动转换）
- split   C-4        -> C-41 / C-42（一对多，必须人工复核才能转换）
- partial C-5        -> C-51（仅部分事件适用新定义，其余待审）
- none    C-6        -> 退役且无后继（历史数据无法可靠转换）

平台行为：PLAT-A / PLAT-C 于 2026-07-01 切换 v2；PLAT-B 在切换月（2026-07）
仍提交 v1 旧代码，2026-08 起切换。复核记录覆盖 confirmed / changed / needs_review。
"""

from __future__ import annotations

import csv
import random
from pathlib import Path

MONTHS = ["2026-03", "2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09"]
PLATFORMS = ["PLAT-A", "PLAT-B", "PLAT-C"]
V2_CODES = ["C-11", "C-23", "C-41", "C-42", "C-51"]
REVIEWERS = ["REV-A", "REV-B", "REV-C"]
REVIEW_PROBABILITY = 0.38

TAXONOMY_ROWS = [
    # record_id, version, code, label, valid_from, valid_to, parent_code
    ("taxonomy-01", 1, "C-1", "spam", "2026-01-01", "2026-06-30", None),
    ("taxonomy-02", 1, "C-2", "hate_speech", "2026-01-01", "2026-06-30", None),
    ("taxonomy-03", 1, "C-3", "harassment", "2026-01-01", "2026-06-30", None),
    ("taxonomy-04", 1, "C-4", "misleading", "2026-01-01", "2026-06-30", None),
    ("taxonomy-05", 1, "C-5", "adult_content", "2026-01-01", "2026-06-30", None),
    ("taxonomy-06", 1, "C-6", "self_harm", "2026-01-01", "2026-06-30", None),
    ("taxonomy-07", 2, "C-11", "unsolicited_promo", "2026-07-01", None, "C-1"),
    ("taxonomy-08", 2, "C-23", "harmful_conduct", "2026-07-01", None, None),
    ("taxonomy-09", 2, "C-41", "source_claim", "2026-07-01", None, "C-4"),
    ("taxonomy-10", 2, "C-42", "synthetic_context", "2026-07-01", None, "C-4"),
    ("taxonomy-11", 2, "C-51", "adult_nudity", "2026-07-01", None, "C-5"),
]

MAPPING_ROWS = [
    # record_id, from_code, from_version, to_code, to_version, map_type, coverage, effective_from, note
    ("mapping-01", "C-1", 1, "C-11", 2, "rename", "all", "2026-07-01",
     "垃圾推广定义澄清为未请求推广，适用范围一致"),
    ("mapping-02", "C-2", 1, "C-23", 2, "merge", "all", "2026-07-01",
     "仇恨言论整体并入有害行为"),
    ("mapping-03", "C-3", 1, "C-23", 2, "merge", "all", "2026-07-01",
     "骚扰整体并入有害行为"),
    ("mapping-04", "C-4", 1, "C-41", 2, "split", "some", "2026-07-01",
     "误导内容中涉及来源声明的部分"),
    ("mapping-05", "C-4", 1, "C-42", 2, "split", "some", "2026-07-01",
     "误导内容中涉及合成上下文的部分"),
    ("mapping-06", "C-5", 1, "C-51", 2, "partial", "some", "2026-07-01",
     "成人内容中仅裸露子类有对应新类，其余需人工复核"),
    ("mapping-07", "C-6", 1, None, None, "none", "none", "2026-07-01",
     "自残类目退役且无后继，历史数据无法可靠转换"),
]


def _uses_v2(platform: str, month: str) -> bool:
    if month < "2026-07":
        return False
    if month >= "2026-08":
        return True
    return platform != "PLAT-B"  # PLAT-B 在切换月仍提交旧代码


def _weights_v1(month_index: int) -> dict[str, int]:
    # 误导类（C-4）逐月上升，制造可观察的趋势
    return {"C-1": 22, "C-2": 13, "C-3": 11, "C-4": 22 + 4 * month_index,
            "C-5": 13, "C-6": 6}


def _weights_v2(month_index: int) -> dict[str, int]:
    rise = 4 * month_index
    return {"C-11": 22, "C-23": 24, "C-41": 12 + rise,
            "C-42": 9 + rise // 2, "C-51": 13}


def _pick(rng: random.Random, weights: dict[str, int]) -> str:
    codes = list(weights)
    return rng.choices(codes, [weights[c] for c in codes])[0]


def _make_review(
    rng: random.Random, seq: int, event_id: str, received_at: str, code: str
) -> tuple:
    reviewer = rng.choice(REVIEWERS)
    day = min(28, int(received_at[8:10]) + rng.randint(0, 4))
    reviewed_at = f"{received_at[:8]}{day:02d}T{rng.randint(8, 20):02d}:{rng.randint(0, 59):02d}:00Z"

    if code in ("C-4", "C-5"):
        # 拆分 / 部分映射：部分经人工改派到新类，部分明确待审，部分仅确认旧类
        roll = rng.random()
        if roll < 0.45:
            state = "changed"
            assigned = rng.choices(["C-41", "C-42"], [6, 4])[0] if code == "C-4" else "C-51"
        elif roll < 0.75:
            state, assigned = "needs_review", None
        else:
            state, assigned = "confirmed", code
    elif code == "C-6":
        # 无后继类别：人工也无法改派，只能确认或标记待审
        state = "confirmed" if rng.random() < 0.7 else "needs_review"
        assigned = code if state == "confirmed" else None
    elif code in ("C-1", "C-2", "C-3"):
        roll = rng.random()
        if roll < 0.80:
            state, assigned = "confirmed", code
        elif roll < 0.90:
            state, assigned = "needs_review", None
        else:
            state = "changed"
            auto_target = {"C-1": "C-11", "C-2": "C-23", "C-3": "C-23"}[code]
            assigned = rng.choice([c for c in V2_CODES if c != auto_target])
    else:
        # 当前类别：绝大多数确认，少量改派或待审
        roll = rng.random()
        if roll < 0.85:
            state, assigned = "confirmed", code
        elif roll < 0.95:
            state, assigned = "needs_review", None
        else:
            state = "changed"
            assigned = rng.choice([c for c in V2_CODES if c != code])

    return (f"classifications-{seq:04d}", f"CL-{seq:04d}", event_id,
            reviewed_at, assigned, reviewer, state)


def generate(out_dir: str | Path, seed: int = 20260701) -> dict[str, int]:
    """生成四个源 CSV，返回各文件行数。同一种子输出完全一致。"""
    rng = random.Random(seed)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    events: list[tuple] = []
    reviews: list[tuple] = []
    ev_seq = 0
    for month_index, month in enumerate(MONTHS):
        for platform in PLATFORMS:
            uses_v2 = _uses_v2(platform, month)
            for _ in range(rng.randint(16, 26)):
                ev_seq += 1
                received_at = (
                    f"{month}-{rng.randint(1, 28):02d}T"
                    f"{rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}:00Z"
                )
                if uses_v2:
                    code, version = _pick(rng, _weights_v2(month_index)), 2
                else:
                    code, version = _pick(rng, _weights_v1(month_index)), 1
                event_id = f"EV-{1000 + ev_seq}"
                events.append((f"content_events-{ev_seq:04d}", event_id,
                               f"OBJ-{ev_seq:04d}", received_at, code, version, platform))
                if rng.random() < REVIEW_PROBABILITY:
                    reviews.append(_make_review(rng, len(reviews) + 1, event_id,
                                                received_at, code))

    _write_csv(out / "taxonomy.csv",
               ["record_id", "taxonomy_id", "version", "code", "label",
                "valid_from", "valid_to", "parent_code"],
               [(rid, "TX-1", ver, code, label, vf, vt, parent)
                for rid, ver, code, label, vf, vt, parent in TAXONOMY_ROWS])
    _write_csv(out / "category_mappings.csv",
               ["record_id", "taxonomy_id", "from_code", "from_version", "to_code",
                "to_version", "map_type", "coverage", "effective_from", "note"],
               [(rid, "TX-1", fc, fv, tc, tv, mt, cov, ef, note)
                for rid, fc, fv, tc, tv, mt, cov, ef, note in MAPPING_ROWS])
    _write_csv(out / "content_events.csv",
               ["record_id", "event_id", "content_ref", "received_at",
                "source_code", "source_taxonomy_version", "source_platform"],
               events)
    _write_csv(out / "classifications.csv",
               ["record_id", "classification_id", "event_id", "reviewed_at",
                "assigned_code", "reviewer_ref", "review_state"],
               reviews)
    return {
        "taxonomy.csv": len(TAXONOMY_ROWS),
        "category_mappings.csv": len(MAPPING_ROWS),
        "content_events.csv": len(events),
        "classifications.csv": len(reviews),
    }


def _write_csv(path: Path, header: list[str], rows: list[tuple]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        for row in rows:
            writer.writerow(["" if v is None else v for v in row])

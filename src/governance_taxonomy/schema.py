"""数仓表结构定义（SQLite DDL，仅使用标准库）。

分层：
- stg_*    源 CSV 原样加载，保留源 record_id 以便回溯到原始文件。
- dim_*    分类版本维度（按有效期划分的 SCD 式定义）与分类维护方提供的类别映射。
- fact_*   内容审核事件与人工复核记录（保留全部复核历史）。
- bridge_event_classification
           每个事件恰好一行的双口径解析结果，是全部汇总的世系核心：
           同时携带原始类别、事件时有效的定义版本、当前可比类别、
           转换状态与人工复核状态。
- mart_*   两种口径的月度汇总，均由桥表聚合得到（每事件只计一次）。
- dq_issue 数据质量与治理问题清单（如退役类别缺少映射、复核引用了未知事件）。
"""

DDL = """
CREATE TABLE stg_taxonomy (
    record_id    TEXT PRIMARY KEY,
    taxonomy_id  TEXT NOT NULL,
    version      INTEGER NOT NULL,
    code         TEXT NOT NULL,
    label        TEXT NOT NULL,
    valid_from   TEXT NOT NULL,   -- ISO 日期，含当日
    valid_to     TEXT,            -- ISO 日期，含当日；NULL 表示当前有效
    parent_code  TEXT
);

CREATE TABLE stg_category_map (
    record_id       TEXT PRIMARY KEY,
    taxonomy_id     TEXT NOT NULL,
    from_code       TEXT NOT NULL,
    from_version    INTEGER NOT NULL,
    to_code         TEXT,          -- map_type = 'none' 时为 NULL
    to_version      INTEGER,
    map_type        TEXT NOT NULL, -- rename | split | merge | partial | none
    coverage        TEXT NOT NULL, -- all | some | none
    effective_from  TEXT NOT NULL,
    note            TEXT
);

CREATE TABLE stg_content_events (
    record_id                TEXT PRIMARY KEY,
    event_id                 TEXT NOT NULL,
    content_ref              TEXT NOT NULL,
    received_at              TEXT NOT NULL,  -- 带时区 ISO 8601
    source_code              TEXT NOT NULL,  -- 平台提交的原始类别
    source_taxonomy_version  INTEGER NOT NULL,
    source_platform          TEXT NOT NULL
);

CREATE TABLE stg_classifications (
    record_id         TEXT PRIMARY KEY,
    classification_id TEXT NOT NULL,
    event_id          TEXT NOT NULL,
    reviewed_at       TEXT NOT NULL,
    assigned_code     TEXT,        -- needs_review 时可为空
    reviewer_ref      TEXT NOT NULL,
    review_state      TEXT NOT NULL -- confirmed | changed | needs_review
);

CREATE TABLE dim_category_version (
    category_version_sk INTEGER PRIMARY KEY,
    taxonomy_id         TEXT NOT NULL,
    code                TEXT NOT NULL,
    version             INTEGER NOT NULL,
    label               TEXT NOT NULL,
    valid_from          TEXT NOT NULL,
    valid_to            TEXT,
    parent_code         TEXT,
    is_current          INTEGER NOT NULL,  -- 1 = 当前有效定义
    source_record_id    TEXT NOT NULL,
    UNIQUE (taxonomy_id, code, version)
);

CREATE TABLE dim_category_map (
    map_sk          INTEGER PRIMARY KEY,
    taxonomy_id     TEXT NOT NULL,
    from_code       TEXT NOT NULL,
    from_version    INTEGER NOT NULL,
    to_code         TEXT,
    to_version      INTEGER,
    map_type        TEXT NOT NULL,
    coverage        TEXT NOT NULL,
    effective_from  TEXT NOT NULL,
    note            TEXT,
    source_record_id TEXT NOT NULL
);

CREATE TABLE fact_event (
    event_sk                  INTEGER PRIMARY KEY,
    event_id                  TEXT NOT NULL UNIQUE,
    content_ref               TEXT NOT NULL,
    received_at               TEXT NOT NULL,
    event_month               TEXT NOT NULL,
    source_platform           TEXT NOT NULL,
    source_code               TEXT NOT NULL,
    source_taxonomy_version   INTEGER NOT NULL,
    aswas_category_version_sk INTEGER REFERENCES dim_category_version,
    def_version_match         TEXT NOT NULL,  -- exact | lag | unknown_code
    source_record_id          TEXT NOT NULL
);

CREATE TABLE fact_review (
    review_sk         INTEGER PRIMARY KEY,
    classification_id TEXT NOT NULL UNIQUE,
    event_sk          INTEGER NOT NULL REFERENCES fact_event,
    reviewed_at       TEXT NOT NULL,
    assigned_code     TEXT,
    reviewer_ref      TEXT NOT NULL,
    review_state      TEXT NOT NULL,
    is_latest         INTEGER NOT NULL,  -- 每事件最新一次复核为 1
    source_record_id  TEXT NOT NULL
);

CREATE TABLE bridge_event_classification (
    event_sk                  INTEGER PRIMARY KEY REFERENCES fact_event,
    event_id                  TEXT NOT NULL,
    event_month               TEXT NOT NULL,
    -- 事件发生时口径（as-was）
    aswas_code                TEXT NOT NULL,
    aswas_category_version_sk INTEGER,
    def_version_match         TEXT NOT NULL,
    -- 当前可比口径（as-is）
    asis_code                 TEXT,          -- 待审或无法转换时为 NULL
    asis_bucket               TEXT NOT NULL, -- asis_code 或 PENDING_REVIEW / UNMAPPABLE
    asis_category_version_sk  INTEGER,
    conversion_status         TEXT NOT NULL, -- identity | auto_mapped | reviewed | pending_review | unmappable
    conversion_reason         TEXT NOT NULL,
    map_record_id             TEXT,          -- 实际使用的映射（如有）
    -- 人工复核状态
    latest_review_state       TEXT NOT NULL, -- confirmed | changed | needs_review | none
    latest_reviewed_at        TEXT,
    latest_reviewer_ref       TEXT,
    review_assigned_code      TEXT
);

CREATE TABLE mart_trend_aswas (
    event_month       TEXT NOT NULL,
    aswas_code        TEXT NOT NULL,
    events            INTEGER NOT NULL,
    reviewed_events   INTEGER NOT NULL,
    source_lag_events INTEGER NOT NULL,
    PRIMARY KEY (event_month, aswas_code)
);

CREATE TABLE mart_trend_asis (
    event_month         TEXT NOT NULL,
    asis_bucket         TEXT NOT NULL,
    events              INTEGER NOT NULL,
    reviewed_events     INTEGER NOT NULL,
    auto_mapped_events  INTEGER NOT NULL,
    PRIMARY KEY (event_month, asis_bucket)
);

CREATE TABLE dq_issue (
    issue_sk  INTEGER PRIMARY KEY,
    severity  TEXT NOT NULL,  -- warn | error
    entity    TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    message   TEXT NOT NULL
);
"""

CREATE TABLE IF NOT EXISTS quality_issues
(
    run_id UUID,
    issue_id String,
    code LowCardinality(String),
    severity LowCardinality(String),
    source LowCardinality(String),
    row_number UInt32,
    business_key String,
    property_id Nullable(String),
    related_rows Array(UInt32),
    actual_values String,
    action LowCardinality(String),
    description String,
    recommendation String,
    impact String,
    inclusion LowCardinality(String)
)
ENGINE = MergeTree
ORDER BY (run_id, source, row_number, issue_id)

CREATE TABLE IF NOT EXISTS raw_rows
(
    run_id UUID,
    source LowCardinality(String),
    row_number UInt32,
    business_key String,
    payload_hash String,
    payload String,
    original_text String
)
ENGINE = MergeTree
ORDER BY (run_id, source, row_number)

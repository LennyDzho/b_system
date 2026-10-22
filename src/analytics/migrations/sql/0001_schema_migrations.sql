CREATE TABLE IF NOT EXISTS schema_migrations
(
    version String,
    checksum FixedString(64),
    applied_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = MergeTree
ORDER BY version

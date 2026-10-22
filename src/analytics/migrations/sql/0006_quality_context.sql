ALTER TABLE quality_issues
    ADD COLUMN IF NOT EXISTS related_entities String DEFAULT '[]',
    ADD COLUMN IF NOT EXISTS period_start Nullable(Date32),
    ADD COLUMN IF NOT EXISTS period_end Nullable(Date32),
    ADD COLUMN IF NOT EXISTS effects String DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS is_global UInt8 DEFAULT 0

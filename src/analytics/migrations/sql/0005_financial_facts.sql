CREATE TABLE IF NOT EXISTS financial_facts
(
    run_id UUID,
    source String,
    business_key String,
    property_id Nullable(String),
    fact_date Nullable(Date32),
    currency String,
    included UInt8,
    source_rows Array(UInt32),
    gross Nullable(Decimal(38, 2)),
    refund Nullable(Decimal(38, 2)),
    ota Nullable(Decimal(38, 2)),
    management Nullable(Decimal(38, 2)),
    amount Nullable(Decimal(38, 2)),
    fee_reason Nullable(String),
    reasons Array(String),
    reconciliation String
)
ENGINE = MergeTree
ORDER BY (run_id, source, business_key)

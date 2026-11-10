-- One row per award record in the raw USAspending pages.
-- batch_id is parsed from the object path; only use batches whose manifest has
-- status = 'succeeded' and window_settled = true (see pipeline_manifests/ in the artifacts bucket).
SELECT
    ingestion_date,
    regexp_extract("$path", 'batch_id=([^/]+)/', 1)                       AS batch_id,
    regexp_extract("$path", '(page-[0-9]+)\.json$', 1)                     AS page,
    json_extract_scalar(r, '$.generated_internal_id')                      AS generated_internal_id,
    json_extract_scalar(r, '$["Award ID"]')                                AS award_id,
    json_extract_scalar(r, '$["Recipient Name"]')                          AS recipient_name,
    json_extract_scalar(r, '$["Recipient UEI"]')                           AS recipient_uei,
    CAST(json_extract_scalar(r, '$["Award Amount"]') AS DECIMAL(20, 2))   AS award_amount,
    json_extract_scalar(r, '$["Awarding Agency"]')                         AS awarding_agency,
    json_extract_scalar(r, '$["Awarding Sub Agency"]')                     AS awarding_sub_agency,
    json_extract_scalar(r, '$["Contract Award Type"]')                     AS contract_award_type,
    json_extract_scalar(r, '$.NAICS.code')                                 AS naics_code,
    json_extract_scalar(r, '$.PSC.code')                                   AS psc_code,
    json_extract_scalar(r, '$["Last Modified Date"]')                      AS last_modified_date
FROM usaspending_spending_by_award_raw
CROSS JOIN UNNEST(CAST(json_extract(raw_json, '$.results') AS ARRAY(JSON))) AS t(r)

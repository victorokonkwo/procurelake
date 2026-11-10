# Glue Data Catalog: Bronze database and raw tables (ADR 0004, ADR 0021), and the Silver
# database whose Apache Iceberg tables the Silver Glue job creates and maintains (ADR 0025).
#
# Bronze tables expose source files exactly as landed: one row per line, in a single
# `raw_json` string column. USAspending pages are single-line JSON documents (verified across
# all landed pages), and their keys contain spaces ("Award ID"), so parsing happens in queries
# with JSON functions rather than in a SerDe. Partitions use projection on `ingestion_date`,
# so there is nothing to crawl or repair.

resource "aws_glue_catalog_database" "bronze" {
  name        = "${var.name_prefix}_bronze"
  description = "ProcureLake ${var.environment} Bronze: raw source data exactly as landed (immutable)."
}

resource "aws_glue_catalog_table" "usaspending_spending_by_award_raw" {
  name          = "usaspending_spending_by_award_raw"
  database_name = aws_glue_catalog_database.bronze.name
  description   = "Raw USAspending /api/v2/search/spending_by_award/ response pages (one JSON document per file)."
  table_type    = "EXTERNAL_TABLE"

  parameters = {
    EXTERNAL                                  = "TRUE"
    classification                            = "json"
    "projection.enabled"                      = "true"
    "projection.ingestion_date.type"          = "date"
    "projection.ingestion_date.format"        = "yyyy-MM-dd"
    "projection.ingestion_date.range"         = "${var.projection_start_date},NOW"
    "projection.ingestion_date.interval"      = "1"
    "projection.ingestion_date.interval.unit" = "DAYS"
    "storage.location.template"               = "s3://${var.raw_bucket_name}/bronze/usaspending/spending_by_award/ingestion_date=$${ingestion_date}/"
    source_system                             = "usaspending"
    manifest_location                         = "s3://${var.artifacts_bucket_name}/pipeline_manifests/usaspending/spending_by_award/"
    data_owner                                = var.data_owner
  }

  partition_keys {
    name    = "ingestion_date"
    type    = "string"
    comment = "UTC date the batch was collected (not the source window; see the manifest)."
  }

  storage_descriptor {
    location      = "s3://${var.raw_bucket_name}/bronze/usaspending/spending_by_award/"
    input_format  = "org.apache.hadoop.mapred.TextInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat"

    columns {
      name    = "raw_json"
      type    = "string"
      comment = "Entire response page as returned by the API."
    }

    ser_de_info {
      serialization_library = "org.apache.hadoop.hive.serde2.lazy.LazySimpleSerDe"
      parameters = {
        # Control character absent from the landed files, so each line stays one column.
        "field.delim"          = "\u0001"
        "serialization.format" = "\u0001"
      }
    }
  }
}

# Silver tables are created by the Silver job through the Iceberg Glue catalog, never by
# Terraform: their schema comes from the source contracts and their layout from Iceberg
# metadata (AGENTS.md section 2.5).
resource "aws_glue_catalog_database" "silver" {
  name         = "${var.name_prefix}_silver"
  description  = "ProcureLake ${var.environment} Silver: validated, deduplicated Apache Iceberg tables."
  location_uri = "s3://${var.lake_bucket_name}/silver/"
}

# Gold: dbt on Athena (ADR 0005, ADR 0026). `gold` holds the governed dimensions, facts and marts;
# `gold_staging` holds dbt's staging and intermediate views, kept apart so consumers are only
# ever granted `gold`. dbt creates the tables; Terraform only owns the databases.
resource "aws_glue_catalog_database" "gold" {
  name         = "${var.name_prefix}_gold"
  description  = "ProcureLake ${var.environment} Gold: governed dimensions, facts and marts (dbt)."
  location_uri = "s3://${var.lake_bucket_name}/gold/"
}

resource "aws_glue_catalog_database" "gold_staging" {
  name         = "${var.name_prefix}_gold_staging"
  description  = "ProcureLake ${var.environment} dbt staging and intermediate views (not for consumers)."
  location_uri = "s3://${var.lake_bucket_name}/gold_staging/"
}

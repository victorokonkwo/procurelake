"""Bronze JSON Lines -> validated, typed, deduplicated Silver rows (pure DataFrame logic).

``validate`` returns two DataFrames:

* **valid**: typed business columns plus provenance (``_batch_id``, ``_source_file``,
  ``_source_line_hash``, ``_row_hash``), at most one row per primary key.
* **rejected**: the original raw line, its file, and a non-empty ``reasons`` array.

Reasons: ``unparseable_json``, ``type_mismatch:<col>`` (value present but not castable),
``not_null:<col>``, ``too_long:<col>`` (char(n)), ``not_allowed:<col>``, ``rule:<name>``,
and ``duplicate_key_superseded`` (an older row for the same key in the same batch).
"""

from __future__ import annotations

from pyspark.sql import Column, DataFrame, SparkSession, Window
from pyspark.sql import functions as F

from procurelake.lakehouse.silver.spec import SilverTableSpec

RAW = "_raw_line"
FILE = "_source_file"


def read_bronze_lines(spark: SparkSession, paths: list[str]) -> DataFrame:
    """One row per line of the given Bronze files, with the file each line came from."""
    if not paths:
        return spark.createDataFrame([], f"{RAW} string, {FILE} string")
    return spark.read.text(paths).select(F.col("value").alias(RAW), F.input_file_name().alias(FILE))


def _present(column: str) -> Column:
    """True when the raw JSON line has a non-null value for ``column``."""
    return F.get_json_object(F.col(RAW), f"$.{column}").isNotNull()


def validate(lines: DataFrame, spec: SilverTableSpec, batch_id: str) -> tuple[DataFrame, DataFrame]:
    schema = spec.ddl_schema()  # from_json accepts a DDL schema string
    parsed = lines.withColumn("_parsed", F.from_json(F.col(RAW), schema, {"mode": "PERMISSIVE"}))
    parsed = parsed.withColumn(
        "_is_object", F.col(RAW).rlike(r"^\s*\{.*\}\s*$") & F.col("_parsed").isNotNull()
    )
    for name, _ in spec.columns:
        parsed = parsed.withColumn(name, F.col(f"_parsed.`{name}`"))

    checks: list[Column] = [F.when(~F.col("_is_object"), F.lit("unparseable_json"))]
    for name, _ in spec.columns:
        checks.append(
            F.when(
                F.col("_is_object") & _present(name) & F.col(name).isNull(), F.lit(f"type_mismatch:{name}")
            )
        )
    for name in spec.not_null:
        checks.append(F.when(F.col("_is_object") & ~_present(name), F.lit(f"not_null:{name}")))
    for name, length in spec.char_lengths.items():
        checks.append(F.when(F.length(F.col(name)) > length, F.lit(f"too_long:{name}")))
    for name, values in spec.allowed_values.items():
        checks.append(
            F.when(F.col(name).isNotNull() & ~F.col(name).isin(values), F.lit(f"not_allowed:{name}"))
        )
    # Business rules run only on structurally valid rows (parsed, typed, required fields present),
    # so a type error is reported once, as its root cause, not again as every rule it breaks.
    # On a structurally valid row, a rule that evaluates to NULL counts as a failure.
    structural = parsed.withColumn("_structural", F.filter(F.array(*checks), lambda r: r.isNotNull()))
    rule_checks = [
        F.when(
            F.size("_structural") == 0,
            F.when(~F.coalesce(F.expr(rule.sql), F.lit(False)), F.lit(f"rule:{rule.name}")),
        )
        for rule in spec.config.rules
    ]
    with_reasons = structural.withColumn(
        "reasons",
        F.concat(
            F.col("_structural"),
            F.filter(F.array(*rule_checks, F.lit(None).cast("string")), lambda r: r.isNotNull()),
        ),
    )
    clean = with_reasons.where(F.size("reasons") == 0)
    bad = with_reasons.where(F.size("reasons") > 0)

    # Deduplicate on the primary key: keep the latest change; older copies are superseded.
    key_window = Window.partitionBy(*spec.primary_key).orderBy(
        F.col(spec.change_column).desc_nulls_last(), F.col(RAW).desc()
    )
    ranked = clean.withColumn("_rank", F.row_number().over(key_window))
    superseded = ranked.where(F.col("_rank") > 1).withColumn(
        "reasons", F.array(F.lit("duplicate_key_superseded"))
    )

    business = [name for name, _ in spec.columns]
    valid = ranked.where(F.col("_rank") == 1).select(
        *business,
        F.lit(batch_id).alias("_batch_id"),
        F.col(FILE).alias("_source_file"),
        F.sha2(F.col(RAW), 256).alias("_source_line_hash"),
        F.sha2(F.to_json(F.struct(*[F.col(c) for c in business])), 256).alias("_row_hash"),
    )
    rejected = bad.unionByName(superseded.drop("_rank"), allowMissingColumns=True).select(
        F.lit(spec.source_table).alias("source_table"),
        F.lit(batch_id).alias("batch_id"),
        F.col(FILE).alias("source_file"),
        F.col(RAW).alias("raw_line"),
        F.col("reasons"),
        *[F.col(k).cast("string").alias(f"key_{k}") for k in spec.primary_key],
    )
    return valid, rejected

"""Local Spark session with Apache Iceberg, matching AWS Glue 5.1 (Spark 3.5, Iceberg 1.10.0).

Spark is 3.5.8 locally and 3.5.6 on Glue 5.1: the same 3.5 line (ADR 0024 amendment).

Uses a Hadoop catalog in a temporary warehouse directory, so no AWS access is needed. Requires
Java 17: JAVA_HOME, or the Homebrew openjdk@17 location on macOS.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

ICEBERG_RUNTIME = "org.apache.iceberg:iceberg-spark-runtime-3.5_2.12:1.10.0"  # Glue 5.1 bundles 1.10.0
HOMEBREW_JDK17 = Path("/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home")


@pytest.fixture(scope="session")
def spark(tmp_path_factory):
    if not os.environ.get("JAVA_HOME"):
        if not HOMEBREW_JDK17.exists():
            pytest.fail("Java 17 is required for Spark tests: set JAVA_HOME")
        os.environ["JAVA_HOME"] = str(HOMEBREW_JDK17)
    from pyspark.sql import SparkSession

    warehouse = tmp_path_factory.mktemp("iceberg-warehouse")
    session = (
        SparkSession.builder.master("local[2]")
        .appName("procurelake-tests")
        .config("spark.driver.bindAddress", "127.0.0.1")
        .config("spark.driver.host", "127.0.0.1")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.shuffle.partitions", "4")
        .config("spark.sql.session.timeZone", "UTC")
        .config("spark.jars.packages", ICEBERG_RUNTIME)
        .config("spark.sql.extensions", "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions")
        .config("spark.sql.catalog.lake", "org.apache.iceberg.spark.SparkCatalog")
        .config("spark.sql.catalog.lake.type", "hadoop")
        .config("spark.sql.catalog.lake.warehouse", str(warehouse))
        .getOrCreate()
    )
    session.sparkContext.setLogLevel("ERROR")
    yield session
    session.stop()

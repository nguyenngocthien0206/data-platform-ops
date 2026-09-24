"""Labelled discrepancies added to the migrated Iceberg tables.

The migration job's own defects are systematic. These are the one-off kind a
reconciliation also has to catch: a handful of rows deleted, invented, edited
or reformatted after the copy. Each fault picks its rows by a stable hash of the
seed, among rows no defect or earlier fault has touched, so every discrepancy
in the ground truth has exactly one cause and one expected class.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import duckdb
import pyarrow as pa
from pyiceberg.table import Table

from platform_ops.common.config import DiscrepancyClass
from platform_ops.reconcile.migrate import TruthCell
from platform_ops.reconcile.schema import TABLES_BY_NAME


@dataclass(frozen=True)
class TargetFault:
    fault_id: str
    table: str
    klass: DiscrepancyClass
    column: str
    rows: int
    # SQL expression for the new value; `{c}` is the column. Unused for row faults.
    change: str = ""


CATALOGUE: tuple[TargetFault, ...] = (
    TargetFault("M1", "orders", "extra_in_target", "", 5),
    TargetFault("M2", "customers", "missing_in_target", "", 6),
    TargetFault("M3", "payments", "value_mismatch", "method", 8, "'crypto'"),
    TargetFault("M4", "customers", "case_only", "email", 8, "upper({c})"),
    TargetFault("M5", "orders", "whitespace", "status", 6, "{c} || ' '"),
    TargetFault("M6", "payments", "rounding", "amount", 6, "{c} + 0.01"),
    TargetFault("M7", "customers", "timezone_shift", "created_local", 5, "{c} + INTERVAL 2 HOUR"),
)


def inject(tables: dict[str, Table], truth: Iterable[TruthCell], seed: int) -> list[TruthCell]:
    """Apply every fault to the Iceberg tables and return their ground truth."""
    touched: dict[str, set[int]] = {}
    for cell in truth:
        touched.setdefault(cell.table, set()).add(cell.key)
    added: list[TruthCell] = []
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    try:
        for table_name in sorted({f.table for f in CATALOGUE}):
            spec = TABLES_BY_NAME[table_name]
            key = spec.key
            data: pa.Table = tables[table_name].scan().to_arrow()
            con.register("current", data)
            con.execute("CREATE OR REPLACE TABLE work AS SELECT * FROM current")
            con.unregister("current")
            for fault in (f for f in CATALOGUE if f.table == table_name):
                excluded = sorted(touched.get(table_name, set()))
                if fault.klass == "extra_in_target":
                    top = con.execute(f"SELECT max({key}) FROM work").fetchone()
                    start = int(top[0]) if top and top[0] is not None else 0
                    keys = list(range(start + 1, start + 1 + fault.rows))
                    # Copies of real rows under new keys: plausible, and invented.
                    con.execute(
                        f"""INSERT INTO work
                            SELECT * REPLACE ({start} + row_number() OVER (ORDER BY {key})
                                              AS {key})
                            FROM (SELECT * FROM work ORDER BY {key} LIMIT {fault.rows})"""
                    )
                else:
                    keys = [
                        int(row[0])
                        for row in con.execute(
                            f"""SELECT {key} FROM work
                                WHERE {key} NOT IN (SELECT UNNEST(?::BIGINT[]))
                                  AND {fault.column or key} IS NOT NULL
                                ORDER BY hash({seed}, '{fault.fault_id}', {key}), {key}
                                LIMIT {fault.rows}""",
                            [excluded],
                        ).fetchall()
                    ]
                    selected = f"{key} IN (SELECT UNNEST(?::BIGINT[]))"
                    if fault.klass == "missing_in_target":
                        con.execute(f"DELETE FROM work WHERE {selected}", [keys])
                    else:
                        new_value = fault.change.format(c=fault.column)
                        con.execute(
                            f"UPDATE work SET {fault.column} = {new_value} WHERE {selected}",
                            [keys],
                        )
                touched.setdefault(table_name, set()).update(keys)
                added += [
                    TruthCell(table_name, k, fault.column, fault.klass, "injected", fault.fault_id)
                    for k in sorted(keys)
                ]
            changed = con.execute(f"SELECT * FROM work ORDER BY {key}").to_arrow_table()
            tables[table_name].overwrite(changed.cast(data.schema))
            con.execute("DROP TABLE work")
    finally:
        con.close()
    return added

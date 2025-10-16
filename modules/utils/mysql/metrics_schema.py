from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class ColumnDef:
    name: str
    definition: str

    @property
    def ddl(self) -> str:
        return f"`{self.name}` {self.definition}"


@dataclass(frozen=True)
class IndexDef:
    name: str
    sql: str

    @property
    def ddl(self) -> str:
        return self.sql


@dataclass(frozen=True)
class TableSchema:
    name: str
    columns: Sequence[ColumnDef]
    primary_key: str
    indexes: Sequence[IndexDef] = ()
    engine: str = "InnoDB"
    charset: str = "utf8mb4"

    async def ensure(self, cursor) -> None:
        await cursor.execute(self._create_table_sql())
        existing_columns = await self._fetch_existing_columns(cursor)
        await self._add_missing_columns(cursor, existing_columns)
        await self._align_column_definitions(cursor)
        await self._drop_extra_columns(cursor, existing_columns)
        await self._ensure_indexes(cursor)

    async def _fetch_existing_columns(self, cursor) -> dict[str, int]:
        await cursor.execute(f"SHOW COLUMNS FROM `{self.name}`")
        rows = await cursor.fetchall()
        return {row[0]: idx for idx, row in enumerate(rows or [])}

    async def _add_missing_columns(self, cursor, existing_columns: dict[str, int]) -> None:
        for idx, column in enumerate(self.columns):
            if column.name in existing_columns:
                continue
            position = self._position_clause(idx)
            await cursor.execute(
                f"ALTER TABLE `{self.name}` ADD COLUMN {column.ddl}{position}"
            )

    async def _align_column_definitions(self, cursor) -> None:
        for idx, column in enumerate(self.columns):
            position = self._position_clause(idx)
            await cursor.execute(
                f"ALTER TABLE `{self.name}` MODIFY COLUMN {column.ddl}{position}"
            )

    async def _drop_extra_columns(self, cursor, existing_columns: dict[str, int]) -> None:
        desired = {column.name for column in self.columns}
        for column_name in existing_columns:
            if column_name not in desired:
                await cursor.execute(
                    f"ALTER TABLE `{self.name}` DROP COLUMN `{column_name}`"
                )

    async def _ensure_indexes(self, cursor) -> None:
        if not self.indexes:
            return
        await cursor.execute(f"SHOW INDEX FROM `{self.name}`")
        rows = await cursor.fetchall()
        existing = {row[2] for row in rows or [] if row[2]}
        for index in self.indexes:
            if index.name in existing:
                continue
            await cursor.execute(f"ALTER TABLE `{self.name}` ADD {index.ddl}")

    def _create_table_sql(self) -> str:
        column_sql = ",\n                    ".join(column.ddl for column in self.columns)
        extras = [self.primary_key, *[index.ddl for index in self.indexes]]
        extras_sql = ""
        if extras:
            extras_sql = ",\n                    " + ",\n                    ".join(extras)
        return (
            f"""
                CREATE TABLE IF NOT EXISTS `{self.name}` (
                    {column_sql}{extras_sql}
                ) ENGINE={self.engine} DEFAULT CHARSET={self.charset};
            """
        )

    def _position_clause(self, index: int) -> str:
        if index == 0:
            return " FIRST"
        previous = self.columns[index - 1].name
        return f" AFTER `{previous}`"


METRIC_AGGREGATE_COLUMNS: tuple[ColumnDef, ...] = (
    ColumnDef("scans_count", "BIGINT NOT NULL DEFAULT 0"),
    ColumnDef("flagged_count", "BIGINT NOT NULL DEFAULT 0"),
    ColumnDef("flags_sum", "BIGINT NOT NULL DEFAULT 0"),
    ColumnDef("total_bytes", "BIGINT NOT NULL DEFAULT 0"),
    ColumnDef("total_duration_ms", "BIGINT NOT NULL DEFAULT 0"),
    ColumnDef("last_duration_ms", "BIGINT NOT NULL DEFAULT 0"),
)

ROLLUP_SCHEMA = TableSchema(
    name="moderation_metric_rollups",
    columns=(
        ColumnDef("metric_date", "DATE NOT NULL"),
        ColumnDef("guild_id", "BIGINT NOT NULL DEFAULT 0"),
        ColumnDef("content_type", "VARCHAR(32) NOT NULL"),
        *METRIC_AGGREGATE_COLUMNS,
        ColumnDef("last_flagged_at", "DATETIME NULL"),
        ColumnDef("last_reference", "VARCHAR(255) NULL"),
        ColumnDef("last_status", "VARCHAR(32) NULL"),
        ColumnDef("status_counts", "JSON NULL"),
        ColumnDef("last_details", "JSON NULL"),
    ),
    primary_key="PRIMARY KEY (`metric_date`, `guild_id`, `content_type`)",
    indexes=(IndexDef("idx_rollups_guild_date", "INDEX `idx_rollups_guild_date` (`guild_id`, `metric_date`)"),),
)

TOTALS_SCHEMA = TableSchema(
    name="moderation_metric_totals",
    columns=(
        ColumnDef("singleton_id", "TINYINT UNSIGNED NOT NULL DEFAULT 1"),
        *METRIC_AGGREGATE_COLUMNS,
        ColumnDef("last_flagged_at", "DATETIME NULL"),
        ColumnDef("last_reference", "VARCHAR(255) NULL"),
        ColumnDef("last_status", "VARCHAR(32) NULL"),
        ColumnDef("status_counts", "JSON NULL"),
        ColumnDef("last_details", "JSON NULL"),
        ColumnDef(
            "updated_at",
            "DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP",
        ),
    ),
    primary_key="PRIMARY KEY (`singleton_id`)",
)


async def ensure_metrics_schema(cursor) -> None:
    for schema in (ROLLUP_SCHEMA, TOTALS_SCHEMA):
        await schema.ensure(cursor)

"""Schema catalog: typed metadata for tables, columns, keys, and FK edges.

The Catalog is the single source of truth for schema information used by
binding, verification, and witness synthesis.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from ..ir.types import SemType


class ColumnInfo(BaseModel):
    """Metadata for a single column."""
    name: str
    sem_type: SemType
    nullable: bool = True
    is_primary_key: bool = False
    distinct_values: Optional[list[str | int | float]] = None
    description: Optional[str] = None


class ForeignKey(BaseModel):
    """A foreign key relationship: src_table.src_col → dst_table.dst_col."""
    src_table: str
    src_column: str
    dst_table: str
    dst_column: str


class TableInfo(BaseModel):
    """Metadata for a single table."""
    name: str
    columns: list[ColumnInfo] = Field(default_factory=list)
    primary_keys: list[str] = Field(default_factory=list)
    primary_key_groups: list[list[str]] = Field(default_factory=list)
    unique_columns: list[str] = Field(default_factory=list)
    row_count: Optional[int] = None
    description: Optional[str] = None

    def get_column(self, name: str) -> Optional[ColumnInfo]:
        name_lower = name.lower()
        for col in self.columns:
            if col.name.lower() == name_lower:
                return col
        return None

    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]


class Catalog(BaseModel):
    """Complete schema catalog for a database."""
    tables: dict[str, TableInfo] = Field(default_factory=dict)
    foreign_keys: list[ForeignKey] = Field(default_factory=list)
    value_constraints: list[dict] = Field(default_factory=list)

    def list_tables(self) -> list[str]:
        return list(self.tables.keys())

    def get_table(self, name: str) -> Optional[TableInfo]:
        name_lower = name.lower()
        for tname, tinfo in self.tables.items():
            if tname.lower() == name_lower:
                return tinfo
        return None

    def get_column(self, table: str, column: str) -> Optional[ColumnInfo]:
        tbl = self.get_table(table)
        if tbl is None:
            return None
        return tbl.get_column(column)

    def get_foreign_keys_from(self, table: str) -> list[ForeignKey]:
        """Get all FKs originating from a table."""
        t = table.lower()
        return [fk for fk in self.foreign_keys if fk.src_table.lower() == t]

    def get_foreign_keys_to(self, table: str) -> list[ForeignKey]:
        """Get all FKs pointing to a table."""
        t = table.lower()
        return [fk for fk in self.foreign_keys if fk.dst_table.lower() == t]

    def get_all_foreign_keys_for(self, table: str) -> list[ForeignKey]:
        """Get all FKs involving a table (as source or destination)."""
        t = table.lower()
        return [
            fk for fk in self.foreign_keys
            if fk.src_table.lower() == t or fk.dst_table.lower() == t
        ]

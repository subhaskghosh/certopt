"""Shared fixtures for tests."""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from optim.ir.types import (
    AggCall,
    AggFunc,
    BinOp,
    BinOpKind,
    ColumnRef,
    JoinClause,
    JoinType,
    Literal,
    QueryIR,
    RelRef,
    SemType,
    SortDir,
    SortSpec,
)
from optim.schema.catalog import Catalog, ColumnInfo, ForeignKey, TableInfo


@pytest.fixture
def sample_catalog() -> Catalog:
    """A small e-commerce catalog: customers, orders, order_items, products."""
    return Catalog(
        tables={
            "customers": TableInfo(
                name="customers",
                columns=[
                    ColumnInfo(name="id", sem_type=SemType.INT, nullable=False, is_primary_key=True),
                    ColumnInfo(name="name", sem_type=SemType.STRING, nullable=False),
                    ColumnInfo(name="email", sem_type=SemType.STRING, nullable=True),
                    ColumnInfo(name="status", sem_type=SemType.STRING, nullable=True),
                ],
                primary_keys=["id"],
            ),
            "orders": TableInfo(
                name="orders",
                columns=[
                    ColumnInfo(name="id", sem_type=SemType.INT, nullable=False, is_primary_key=True),
                    ColumnInfo(name="customer_id", sem_type=SemType.INT, nullable=False),
                    ColumnInfo(name="order_date", sem_type=SemType.DATE, nullable=False),
                    ColumnInfo(name="total", sem_type=SemType.DECIMAL, nullable=False),
                ],
                primary_keys=["id"],
            ),
            "order_items": TableInfo(
                name="order_items",
                columns=[
                    ColumnInfo(name="id", sem_type=SemType.INT, nullable=False, is_primary_key=True),
                    ColumnInfo(name="order_id", sem_type=SemType.INT, nullable=False),
                    ColumnInfo(name="product_id", sem_type=SemType.INT, nullable=False),
                    ColumnInfo(name="quantity", sem_type=SemType.INT, nullable=False),
                    ColumnInfo(name="price", sem_type=SemType.DECIMAL, nullable=False),
                ],
                primary_keys=["id"],
            ),
            "products": TableInfo(
                name="products",
                columns=[
                    ColumnInfo(name="id", sem_type=SemType.INT, nullable=False, is_primary_key=True),
                    ColumnInfo(name="name", sem_type=SemType.STRING, nullable=False),
                    ColumnInfo(name="category", sem_type=SemType.STRING, nullable=True),
                    ColumnInfo(name="price", sem_type=SemType.DECIMAL, nullable=False),
                ],
                primary_keys=["id"],
            ),
        },
        foreign_keys=[
            ForeignKey(src_table="orders", src_column="customer_id", dst_table="customers", dst_column="id"),
            ForeignKey(src_table="order_items", src_column="order_id", dst_table="orders", dst_column="id"),
            ForeignKey(src_table="order_items", src_column="product_id", dst_table="products", dst_column="id"),
        ],
    )


@pytest.fixture
def simple_select_ir() -> QueryIR:
    """SELECT name, email FROM customers WHERE status = 'active' LIMIT 100."""
    return QueryIR(
        select=[
            ColumnRef(table="customers", column="name", sem_type=SemType.STRING),
            ColumnRef(table="customers", column="email", sem_type=SemType.STRING),
        ],
        from_table=RelRef(table="customers"),
        where=BinOp(
            op=BinOpKind.EQ,
            left=ColumnRef(table="customers", column="status", sem_type=SemType.STRING),
            right=Literal(value="active"),
        ),
        limit=100,
    )


@pytest.fixture
def agg_join_ir() -> QueryIR:
    """SELECT c.name, SUM(o.total) FROM customers c JOIN orders o ON c.id = o.customer_id GROUP BY c.name."""
    return QueryIR(
        select=[
            ColumnRef(table="c", column="name", sem_type=SemType.STRING),
            AggCall(
                func=AggFunc.SUM,
                arg=ColumnRef(table="o", column="total", sem_type=SemType.DECIMAL),
                alias="total_spent",
            ),
        ],
        from_table=RelRef(table="customers", alias="c"),
        joins=[
            JoinClause(
                join_type=JoinType.INNER,
                right=RelRef(table="orders", alias="o"),
                on=BinOp(
                    op=BinOpKind.EQ,
                    left=ColumnRef(table="c", column="id", sem_type=SemType.INT),
                    right=ColumnRef(table="o", column="customer_id", sem_type=SemType.INT),
                ),
            ),
        ],
        group_by=[
            ColumnRef(table="c", column="name", sem_type=SemType.STRING),
        ],
    )


@pytest.fixture
def sqlite_db_path(sample_catalog: Catalog, tmp_path) -> str:
    """A sqlite3 DB with the sample e-commerce schema. Returns the file path."""
    path = str(tmp_path / "test.sqlite")
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY,
            name VARCHAR NOT NULL,
            email VARCHAR,
            status VARCHAR
        );
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL REFERENCES customers(id),
            order_date DATE NOT NULL,
            total REAL NOT NULL
        );
        CREATE TABLE products (
            id INTEGER PRIMARY KEY,
            name VARCHAR NOT NULL,
            category VARCHAR,
            price REAL NOT NULL
        );
        CREATE TABLE order_items (
            id INTEGER PRIMARY KEY,
            order_id INTEGER NOT NULL REFERENCES orders(id),
            product_id INTEGER NOT NULL REFERENCES products(id),
            quantity INTEGER NOT NULL,
            price REAL NOT NULL
        );
        INSERT INTO customers VALUES (1, 'Alice', 'alice@ex.com', 'active');
        INSERT INTO customers VALUES (2, 'Bob', 'bob@ex.com', 'inactive');
        INSERT INTO customers VALUES (3, 'Carol', NULL, 'active');
        INSERT INTO orders VALUES (1, 1, '2024-01-15', 100.00);
        INSERT INTO orders VALUES (2, 1, '2024-02-20', 200.00);
        INSERT INTO orders VALUES (3, 2, '2024-03-10', 50.00);
        INSERT INTO products VALUES (1, 'Widget', 'gadgets', 25.00);
        INSERT INTO products VALUES (2, 'Gizmo', 'gadgets', 50.00);
        INSERT INTO order_items VALUES (1, 1, 1, 2, 25.00);
        INSERT INTO order_items VALUES (2, 1, 2, 1, 50.00);
        INSERT INTO order_items VALUES (3, 2, 1, 4, 25.00);
        INSERT INTO order_items VALUES (4, 3, 2, 1, 50.00);
    """)
    conn.close()
    return path


@pytest.fixture
def sqlite_conn(sqlite_db_path) -> sqlite3.Connection:
    """A sqlite3 connection to the sample database."""
    conn = sqlite3.connect(sqlite_db_path)
    yield conn
    conn.close()

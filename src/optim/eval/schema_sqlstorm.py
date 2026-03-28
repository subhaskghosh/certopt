"""Schema ingestion for SQLStorm datasets.

Provides hard-coded Catalog builders for the SQLStorm benchmark suites:
- StackOverflow (16-table PostgreSQL schema)
- TPC-DS (core tables stub)
- TPC-H (standard 8-table schema)
- JOB (delegates to ``schema_imdb``)

``get_sqlstorm_catalog`` is the recommended entry-point — it routes by
dataset name.
"""

from __future__ import annotations

import logging

from ..ir.types import SemType
from ..schema.catalog import Catalog, ColumnInfo, ForeignKey, TableInfo

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Column helpers
# ---------------------------------------------------------------------------

def _col(
    name: str,
    sem_type: SemType = SemType.STRING,
    nullable: bool = True,
    is_pk: bool = False,
) -> ColumnInfo:
    """Shorthand for building a ColumnInfo."""
    return ColumnInfo(
        name=name,
        sem_type=sem_type,
        nullable=nullable,
        is_primary_key=is_pk,
    )


def _pk(name: str = "Id") -> ColumnInfo:
    return _col(name, SemType.INT, nullable=False, is_pk=True)


def _int_nn(name: str) -> ColumnInfo:
    return _col(name, SemType.INT, nullable=False)


def _int(name: str) -> ColumnInfo:
    return _col(name, SemType.INT, nullable=True)


def _str(name: str) -> ColumnInfo:
    return _col(name, SemType.STRING, nullable=True)


def _str_nn(name: str) -> ColumnInfo:
    return _col(name, SemType.STRING, nullable=False)


def _ts(name: str, nullable: bool = True) -> ColumnInfo:
    return _col(name, SemType.TIMESTAMP, nullable=nullable)


def _bool(name: str, nullable: bool = True) -> ColumnInfo:
    return _col(name, SemType.BOOL, nullable=nullable)


def _smallint(name: str, nullable: bool = False) -> ColumnInfo:
    return _col(name, SemType.INT, nullable=nullable)


def _date(name: str, nullable: bool = True) -> ColumnInfo:
    return _col(name, SemType.DATE, nullable=nullable)


def _decimal(name: str, nullable: bool = True) -> ColumnInfo:
    return _col(name, SemType.DECIMAL, nullable=nullable)


def _add(
    tables: dict[str, TableInfo], name: str, cols: list[ColumnInfo],
) -> None:
    pks = [c.name for c in cols if c.is_primary_key]
    tables[name] = TableInfo(name=name, columns=cols, primary_keys=pks)


# =========================================================================
# StackOverflow
# =========================================================================

def build_stackoverflow_catalog() -> Catalog:
    """Build the StackOverflow catalog from the SQLStorm v1.0 schema DDL.

    Covers all 13 tables defined in
    ``data/SQLStorm/v1.0/stackoverflow/schema.sql``.
    """
    tables: dict[str, TableInfo] = {}

    # --- PostHistoryTypes ---
    _add(tables, "PostHistoryTypes", [
        _col("Id", SemType.INT, nullable=False, is_pk=True),
        _str_nn("Name"),
    ])

    # --- LinkTypes ---
    _add(tables, "LinkTypes", [
        _col("Id", SemType.INT, nullable=False, is_pk=True),
        _str_nn("Name"),
    ])

    # --- PostTypes ---
    _add(tables, "PostTypes", [
        _col("Id", SemType.INT, nullable=False, is_pk=True),
        _str_nn("Name"),
    ])

    # --- CloseReasonTypes ---
    _add(tables, "CloseReasonTypes", [
        _col("Id", SemType.INT, nullable=False, is_pk=True),
        _str_nn("Name"),
    ])

    # --- VoteTypes ---
    _add(tables, "VoteTypes", [
        _col("Id", SemType.INT, nullable=False, is_pk=True),
        _str_nn("Name"),
    ])

    # --- Users ---
    _add(tables, "Users", [
        _pk(),
        _int_nn("Reputation"),
        _ts("CreationDate", nullable=False),
        _str("DisplayName"),
        _ts("LastAccessDate", nullable=False),
        _str("WebsiteUrl"),
        _str("Location"),
        _str("AboutMe"),
        _int("Views"),
        _int("UpVotes"),
        _int("DownVotes"),
        _str("ProfileImageUrl"),
        _int("AccountId"),
    ])

    # --- Badges ---
    _add(tables, "Badges", [
        _pk(),
        _int_nn("UserId"),
        _str_nn("Name"),
        _ts("Date", nullable=False),
        _smallint("Class", nullable=False),
        _bool("TagBased", nullable=False),
    ])

    # --- Posts ---
    _add(tables, "Posts", [
        _pk(),
        _smallint("PostTypeId", nullable=True),
        _int("AcceptedAnswerId"),
        _int("ParentId"),
        _ts("CreationDate"),
        _int("Score"),
        _int("ViewCount"),
        _str("Body"),
        _int("OwnerUserId"),
        _str("OwnerDisplayName"),
        _int("LastEditorUserId"),
        _str("LastEditorDisplayName"),
        _ts("LastEditDate"),
        _ts("LastActivityDate"),
        _str("Title"),
        _str("Tags"),
        _int("AnswerCount"),
        _int("CommentCount"),
        _int("FavoriteCount"),
        _ts("ClosedDate"),
        _ts("CommunityOwnedDate"),
        _str("ContentLicense"),
    ])

    # --- Comments ---
    _add(tables, "Comments", [
        _pk(),
        _int_nn("PostId"),
        _int("Score"),
        _str_nn("Text"),
        _ts("CreationDate", nullable=False),
        _str("UserDisplayName"),
        _int("UserId"),
        _str("ContentLicense"),
    ])

    # --- PostHistory ---
    _add(tables, "PostHistory", [
        _pk(),
        _smallint("PostHistoryTypeId", nullable=True),
        _int("PostId"),
        _str("RevisionGUID"),
        _ts("CreationDate"),
        _int("UserId"),
        _str("UserDisplayName"),
        _str("Comment"),
        _str("Text"),
        _str("ContentLicense"),
    ])

    # --- PostLinks ---
    _add(tables, "PostLinks", [
        _col("Id", SemType.INT, nullable=False, is_pk=True),
        _ts("CreationDate", nullable=False),
        _int_nn("PostId"),
        _int_nn("RelatedPostId"),
        _smallint("LinkTypeId", nullable=False),
    ])

    # --- Tags ---
    _add(tables, "Tags", [
        _pk(),
        _str("TagName"),
        _int_nn("Count"),
        _int("ExcerptPostId"),
        _int("WikiPostId"),
        _bool("IsModeratorOnly"),
        _bool("IsRequired"),
    ])

    # --- Votes ---
    _add(tables, "Votes", [
        _pk(),
        _int_nn("PostId"),
        _smallint("VoteTypeId", nullable=False),
        _int("UserId"),
        _ts("CreationDate"),
        _int("BountyAmount"),
    ])

    foreign_keys = _build_stackoverflow_foreign_keys()
    return Catalog(tables=tables, foreign_keys=foreign_keys)


def _build_stackoverflow_foreign_keys() -> list[ForeignKey]:
    """Return all foreign keys from the StackOverflow schema DDL."""
    fk = ForeignKey
    return [
        # Badges.UserId → Users.Id
        fk(src_table="Badges", src_column="UserId", dst_table="Users", dst_column="Id"),
        # Posts.PostTypeId → PostTypes.Id
        fk(src_table="Posts", src_column="PostTypeId", dst_table="PostTypes", dst_column="Id"),
        # Posts.OwnerUserId → Users.Id
        fk(src_table="Posts", src_column="OwnerUserId", dst_table="Users", dst_column="Id"),
        # Posts.LastEditorUserId → Users.Id
        fk(src_table="Posts", src_column="LastEditorUserId", dst_table="Users", dst_column="Id"),
        # Posts.AcceptedAnswerId → Posts.Id (self-referential)
        fk(src_table="Posts", src_column="AcceptedAnswerId", dst_table="Posts", dst_column="Id"),
        # Posts.ParentId → Posts.Id (self-referential)
        fk(src_table="Posts", src_column="ParentId", dst_table="Posts", dst_column="Id"),
        # Comments.PostId → Posts.Id
        fk(src_table="Comments", src_column="PostId", dst_table="Posts", dst_column="Id"),
        # Comments.UserId → Users.Id
        fk(src_table="Comments", src_column="UserId", dst_table="Users", dst_column="Id"),
        # PostHistory.PostHistoryTypeId → PostHistoryTypes.Id
        fk(src_table="PostHistory", src_column="PostHistoryTypeId", dst_table="PostHistoryTypes", dst_column="Id"),
        # PostHistory.PostId → Posts.Id
        fk(src_table="PostHistory", src_column="PostId", dst_table="Posts", dst_column="Id"),
        # PostHistory.UserId → Users.Id
        fk(src_table="PostHistory", src_column="UserId", dst_table="Users", dst_column="Id"),
        # PostLinks.PostId → Posts.Id
        fk(src_table="PostLinks", src_column="PostId", dst_table="Posts", dst_column="Id"),
        # PostLinks.RelatedPostId → Posts.Id
        fk(src_table="PostLinks", src_column="RelatedPostId", dst_table="Posts", dst_column="Id"),
        # PostLinks.LinkTypeId → LinkTypes.Id
        fk(src_table="PostLinks", src_column="LinkTypeId", dst_table="LinkTypes", dst_column="Id"),
        # Tags.ExcerptPostId → Posts.Id
        fk(src_table="Tags", src_column="ExcerptPostId", dst_table="Posts", dst_column="Id"),
        # Tags.WikiPostId → Posts.Id
        fk(src_table="Tags", src_column="WikiPostId", dst_table="Posts", dst_column="Id"),
        # Votes.PostId → Posts.Id
        fk(src_table="Votes", src_column="PostId", dst_table="Posts", dst_column="Id"),
        # Votes.VoteTypeId → VoteTypes.Id
        fk(src_table="Votes", src_column="VoteTypeId", dst_table="VoteTypes", dst_column="Id"),
        # Votes.UserId → Users.Id
        fk(src_table="Votes", src_column="UserId", dst_table="Users", dst_column="Id"),
    ]


# =========================================================================
# TPC-H
# =========================================================================

def build_tpch_catalog() -> Catalog:
    """Build the standard TPC-H 8-table schema catalog."""
    tables: dict[str, TableInfo] = {}

    # --- region ---
    _add(tables, "region", [
        _col("r_regionkey", SemType.INT, nullable=False, is_pk=True),
        _str_nn("r_name"),
        _str("r_comment"),
    ])

    # --- nation ---
    _add(tables, "nation", [
        _col("n_nationkey", SemType.INT, nullable=False, is_pk=True),
        _str_nn("n_name"),
        _int_nn("n_regionkey"),
        _str("n_comment"),
    ])

    # --- supplier ---
    _add(tables, "supplier", [
        _col("s_suppkey", SemType.INT, nullable=False, is_pk=True),
        _str_nn("s_name"),
        _str_nn("s_address"),
        _int_nn("s_nationkey"),
        _str_nn("s_phone"),
        _decimal("s_acctbal", nullable=False),
        _str("s_comment"),
    ])

    # --- customer ---
    _add(tables, "customer", [
        _col("c_custkey", SemType.INT, nullable=False, is_pk=True),
        _str_nn("c_name"),
        _str_nn("c_address"),
        _int_nn("c_nationkey"),
        _str_nn("c_phone"),
        _decimal("c_acctbal", nullable=False),
        _str_nn("c_mktsegment"),
        _str("c_comment"),
    ])

    # --- part ---
    _add(tables, "part", [
        _col("p_partkey", SemType.INT, nullable=False, is_pk=True),
        _str_nn("p_name"),
        _str_nn("p_mfgr"),
        _str_nn("p_brand"),
        _str_nn("p_type"),
        _int_nn("p_size"),
        _str_nn("p_container"),
        _decimal("p_retailprice", nullable=False),
        _str("p_comment"),
    ])

    # --- partsupp ---
    _add(tables, "partsupp", [
        _col("ps_partkey", SemType.INT, nullable=False, is_pk=True),
        _col("ps_suppkey", SemType.INT, nullable=False, is_pk=True),
        _int_nn("ps_availqty"),
        _decimal("ps_supplycost", nullable=False),
        _str("ps_comment"),
    ])

    # --- orders ---
    _add(tables, "orders", [
        _col("o_orderkey", SemType.INT, nullable=False, is_pk=True),
        _int_nn("o_custkey"),
        _str_nn("o_orderstatus"),
        _decimal("o_totalprice", nullable=False),
        _date("o_orderdate", nullable=False),
        _str_nn("o_orderpriority"),
        _str_nn("o_clerk"),
        _int_nn("o_shippriority"),
        _str("o_comment"),
    ])

    # --- lineitem ---
    _add(tables, "lineitem", [
        _col("l_orderkey", SemType.INT, nullable=False, is_pk=True),
        _int_nn("l_partkey"),
        _int_nn("l_suppkey"),
        _col("l_linenumber", SemType.INT, nullable=False, is_pk=True),
        _decimal("l_quantity", nullable=False),
        _decimal("l_extendedprice", nullable=False),
        _decimal("l_discount", nullable=False),
        _decimal("l_tax", nullable=False),
        _str_nn("l_returnflag"),
        _str_nn("l_linestatus"),
        _date("l_shipdate", nullable=False),
        _date("l_commitdate", nullable=False),
        _date("l_receiptdate", nullable=False),
        _str_nn("l_shipinstruct"),
        _str_nn("l_shipmode"),
        _str("l_comment"),
    ])

    foreign_keys = [
        ForeignKey(src_table="nation", src_column="n_regionkey", dst_table="region", dst_column="r_regionkey"),
        ForeignKey(src_table="supplier", src_column="s_nationkey", dst_table="nation", dst_column="n_nationkey"),
        ForeignKey(src_table="customer", src_column="c_nationkey", dst_table="nation", dst_column="n_nationkey"),
        ForeignKey(src_table="partsupp", src_column="ps_partkey", dst_table="part", dst_column="p_partkey"),
        ForeignKey(src_table="partsupp", src_column="ps_suppkey", dst_table="supplier", dst_column="s_suppkey"),
        ForeignKey(src_table="orders", src_column="o_custkey", dst_table="customer", dst_column="c_custkey"),
        ForeignKey(src_table="lineitem", src_column="l_orderkey", dst_table="orders", dst_column="o_orderkey"),
        ForeignKey(src_table="lineitem", src_column="l_partkey", dst_table="part", dst_column="p_partkey"),
        ForeignKey(src_table="lineitem", src_column="l_suppkey", dst_table="supplier", dst_column="s_suppkey"),
    ]

    return Catalog(tables=tables, foreign_keys=foreign_keys)


# =========================================================================
# TPC-DS (minimal stub)
# =========================================================================

def build_tpcds_catalog() -> Catalog:
    """Build a minimal TPC-DS catalog with core tables.

    This is a stub covering enough tables to parse and verify basic TPC-DS
    queries.  Extend as needed.
    """
    tables: dict[str, TableInfo] = {}

    # --- date_dim ---
    _add(tables, "date_dim", [
        _col("d_date_sk", SemType.INT, nullable=False, is_pk=True),
        _str_nn("d_date_id"),
        _date("d_date"),
        _int("d_month_seq"),
        _int("d_week_seq"),
        _int("d_quarter_seq"),
        _int("d_year"),
        _int("d_dow"),
        _int("d_moy"),
        _int("d_dom"),
        _int("d_qoy"),
        _int("d_fy_year"),
        _int("d_fy_quarter_seq"),
        _int("d_fy_week_seq"),
        _str("d_day_name"),
        _str("d_quarter_name"),
        _str("d_holiday"),
        _str("d_weekend"),
        _str("d_following_holiday"),
        _int("d_first_dom"),
        _int("d_last_dom"),
        _int("d_same_day_ly"),
        _int("d_same_day_lq"),
        _str("d_current_day"),
        _str("d_current_week"),
        _str("d_current_month"),
        _str("d_current_quarter"),
        _str("d_current_year"),
    ])

    # --- item ---
    _add(tables, "item", [
        _col("i_item_sk", SemType.INT, nullable=False, is_pk=True),
        _str_nn("i_item_id"),
        _date("i_rec_start_date"),
        _date("i_rec_end_date"),
        _str("i_item_desc"),
        _decimal("i_current_price"),
        _decimal("i_wholesale_cost"),
        _int("i_brand_id"),
        _str("i_brand"),
        _int("i_class_id"),
        _str("i_class"),
        _int("i_category_id"),
        _str("i_category"),
        _int("i_manufact_id"),
        _str("i_manufact"),
        _str("i_size"),
        _str("i_formulation"),
        _str("i_color"),
        _str("i_units"),
        _str("i_container"),
        _int("i_manager_id"),
        _str("i_product_name"),
    ])

    # --- customer ---
    _add(tables, "customer", [
        _col("c_customer_sk", SemType.INT, nullable=False, is_pk=True),
        _str_nn("c_customer_id"),
        _int("c_current_cdemo_sk"),
        _int("c_current_hdemo_sk"),
        _int("c_current_addr_sk"),
        _int("c_first_shipto_date_sk"),
        _int("c_first_sales_date_sk"),
        _str("c_salutation"),
        _str("c_first_name"),
        _str("c_last_name"),
        _str("c_preferred_cust_flag"),
        _int("c_birth_day"),
        _int("c_birth_month"),
        _int("c_birth_year"),
        _str("c_birth_country"),
        _str("c_login"),
        _str("c_email_address"),
        _int("c_last_review_date_sk"),
    ])

    # --- store ---
    _add(tables, "store", [
        _col("s_store_sk", SemType.INT, nullable=False, is_pk=True),
        _str_nn("s_store_id"),
        _date("s_rec_start_date"),
        _date("s_rec_end_date"),
        _int("s_closed_date_sk"),
        _str("s_store_name"),
        _int("s_number_employees"),
        _int("s_floor_space"),
        _str("s_hours"),
        _str("s_manager"),
        _int("s_market_id"),
        _str("s_geography_class"),
        _str("s_market_desc"),
        _str("s_market_manager"),
        _int("s_division_id"),
        _str("s_division_name"),
        _int("s_company_id"),
        _str("s_company_name"),
        _str("s_street_number"),
        _str("s_street_name"),
        _str("s_street_type"),
        _str("s_suite_number"),
        _str("s_city"),
        _str("s_county"),
        _str("s_state"),
        _str("s_zip"),
        _str("s_country"),
        _decimal("s_gmt_offset"),
        _decimal("s_tax_precentage"),
    ])

    # --- store_sales ---
    _add(tables, "store_sales", [
        _col("ss_sold_date_sk", SemType.INT, nullable=True),
        _col("ss_sold_time_sk", SemType.INT, nullable=True),
        _col("ss_item_sk", SemType.INT, nullable=False, is_pk=True),
        _int_nn("ss_customer_sk"),
        _int("ss_cdemo_sk"),
        _int("ss_hdemo_sk"),
        _int("ss_addr_sk"),
        _int("ss_store_sk"),
        _int("ss_promo_sk"),
        _col("ss_ticket_number", SemType.INT, nullable=False, is_pk=True),
        _int("ss_quantity"),
        _decimal("ss_wholesale_cost"),
        _decimal("ss_list_price"),
        _decimal("ss_sales_price"),
        _decimal("ss_ext_discount_amt"),
        _decimal("ss_ext_sales_price"),
        _decimal("ss_ext_wholesale_cost"),
        _decimal("ss_ext_list_price"),
        _decimal("ss_ext_tax"),
        _decimal("ss_coupon_amt"),
        _decimal("ss_net_paid"),
        _decimal("ss_net_paid_inc_tax"),
        _decimal("ss_net_profit"),
    ])

    # --- store_returns ---
    _add(tables, "store_returns", [
        _int("sr_returned_date_sk"),
        _int("sr_return_time_sk"),
        _col("sr_item_sk", SemType.INT, nullable=False, is_pk=True),
        _int_nn("sr_customer_sk"),
        _int("sr_cdemo_sk"),
        _int("sr_hdemo_sk"),
        _int("sr_addr_sk"),
        _int("sr_store_sk"),
        _int("sr_reason_sk"),
        _col("sr_ticket_number", SemType.INT, nullable=False, is_pk=True),
        _int("sr_return_quantity"),
        _decimal("sr_return_amt"),
        _decimal("sr_return_tax"),
        _decimal("sr_return_amt_inc_tax"),
        _decimal("sr_fee"),
        _decimal("sr_return_ship_cost"),
        _decimal("sr_refunded_cash"),
        _decimal("sr_reversed_charge"),
        _decimal("sr_store_credit"),
        _decimal("sr_net_loss"),
    ])

    # --- catalog_sales ---
    _add(tables, "catalog_sales", [
        _int("cs_sold_date_sk"),
        _int("cs_sold_time_sk"),
        _int("cs_ship_date_sk"),
        _int("cs_bill_customer_sk"),
        _int("cs_bill_cdemo_sk"),
        _int("cs_bill_hdemo_sk"),
        _int("cs_bill_addr_sk"),
        _int("cs_ship_customer_sk"),
        _int("cs_ship_cdemo_sk"),
        _int("cs_ship_hdemo_sk"),
        _int("cs_ship_addr_sk"),
        _int("cs_call_center_sk"),
        _int("cs_catalog_page_sk"),
        _int("cs_ship_mode_sk"),
        _int("cs_warehouse_sk"),
        _col("cs_item_sk", SemType.INT, nullable=False, is_pk=True),
        _int("cs_promo_sk"),
        _col("cs_order_number", SemType.INT, nullable=False, is_pk=True),
        _int("cs_quantity"),
        _decimal("cs_wholesale_cost"),
        _decimal("cs_list_price"),
        _decimal("cs_sales_price"),
        _decimal("cs_ext_discount_amt"),
        _decimal("cs_ext_sales_price"),
        _decimal("cs_ext_wholesale_cost"),
        _decimal("cs_ext_list_price"),
        _decimal("cs_ext_tax"),
        _decimal("cs_coupon_amt"),
        _decimal("cs_ext_ship_cost"),
        _decimal("cs_net_paid"),
        _decimal("cs_net_paid_inc_tax"),
        _decimal("cs_net_paid_inc_ship"),
        _decimal("cs_net_paid_inc_ship_tax"),
        _decimal("cs_net_profit"),
    ])

    foreign_keys = [
        # store_sales FKs
        ForeignKey(src_table="store_sales", src_column="ss_sold_date_sk", dst_table="date_dim", dst_column="d_date_sk"),
        ForeignKey(src_table="store_sales", src_column="ss_item_sk", dst_table="item", dst_column="i_item_sk"),
        ForeignKey(src_table="store_sales", src_column="ss_customer_sk", dst_table="customer", dst_column="c_customer_sk"),
        ForeignKey(src_table="store_sales", src_column="ss_store_sk", dst_table="store", dst_column="s_store_sk"),
        # store_returns FKs
        ForeignKey(src_table="store_returns", src_column="sr_returned_date_sk", dst_table="date_dim", dst_column="d_date_sk"),
        ForeignKey(src_table="store_returns", src_column="sr_item_sk", dst_table="item", dst_column="i_item_sk"),
        ForeignKey(src_table="store_returns", src_column="sr_customer_sk", dst_table="customer", dst_column="c_customer_sk"),
        ForeignKey(src_table="store_returns", src_column="sr_store_sk", dst_table="store", dst_column="s_store_sk"),
        # catalog_sales FKs
        ForeignKey(src_table="catalog_sales", src_column="cs_sold_date_sk", dst_table="date_dim", dst_column="d_date_sk"),
        ForeignKey(src_table="catalog_sales", src_column="cs_item_sk", dst_table="item", dst_column="i_item_sk"),
        ForeignKey(src_table="catalog_sales", src_column="cs_bill_customer_sk", dst_table="customer", dst_column="c_customer_sk"),
    ]

    return Catalog(tables=tables, foreign_keys=foreign_keys)


# =========================================================================
# Router
# =========================================================================

def get_sqlstorm_catalog(dataset: str) -> Catalog:
    """Return a Catalog for the given SQLStorm dataset.

    Parameters
    ----------
    dataset:
        One of ``"stackoverflow"``, ``"tpcds"``, ``"tpch"``, ``"job"``.

    Returns
    -------
    Catalog

    Raises
    ------
    ValueError
        If *dataset* is not recognised.
    """
    key = dataset.lower().replace("-", "").replace("_", "")

    if key == "stackoverflow":
        catalog = build_stackoverflow_catalog()
        logger.info("SQLStorm catalog built for StackOverflow")
        return catalog

    if key == "tpcds":
        catalog = build_tpcds_catalog()
        logger.info("SQLStorm catalog built for TPC-DS (stub)")
        return catalog

    if key == "tpch":
        catalog = build_tpch_catalog()
        logger.info("SQLStorm catalog built for TPC-H")
        return catalog

    if key == "job":
        from .schema_imdb import get_imdb_catalog
        catalog = get_imdb_catalog()
        logger.info("SQLStorm catalog delegated to IMDB/JOB")
        return catalog

    raise ValueError(
        f"Unknown SQLStorm dataset: {dataset!r}. "
        f"Expected one of: stackoverflow, tpcds, tpch, job"
    )

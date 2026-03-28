"""Abstract LLM candidate provider and shared utilities.

Defines the base class for LLM-based rewrite candidate generation
and shared helpers (prompt building, SQL extraction, schema formatting)
used by all concrete providers.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from ..cegis.equivalence import Candidate
from ..parser.sql_to_ir import sql_to_ir
from ..schema.catalog import Catalog

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class LLMConfig:
    """Provider-agnostic configuration for LLM rewrite generation."""

    provider: str = "openai"  # "openai", "anthropic", or "amp"
    model: str = "gpt-4o"
    n_candidates: int = 5
    timeout_seconds: float = 120.0
    max_retries: int = 3
    retry_base_delay: float = 5.0

    # Provider-specific
    api_key: Optional[str] = None  # falls back to env var
    base_url: Optional[str] = None  # for OpenAI-compatible endpoints
    amp_mode: str = "smart"  # Amp-specific: "smart" or "deep"


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are a SQL optimization expert. Given a SQL query and its schema, "
    "generate semantically equivalent rewrites that may be more efficient. "
    "Each rewrite must produce identical results. "
    "Output each SQL query in a ```sql code block. "
    "Focus on: predicate pushdown, join reordering, subquery elimination, "
    "redundant join removal, DISTINCT elimination, aggregation pushdown."
)

_USER_PROMPT_TEMPLATE = """\
### Database Schema
{schema}

### Original SQL Query
```sql
{sql}
```

### Instructions
Generate exactly {n} semantically equivalent rewrites of the query above \
that may be more efficient. Each rewrite MUST produce identical results on \
any valid instance of the schema.

Each rewrite should explore a different optimization strategy. \
Output each in a ```sql code block.

Target dialect: {dialect}.
"""


# ---------------------------------------------------------------------------
# Schema formatting
# ---------------------------------------------------------------------------

_TYPE_MAP = {
    "INT": "INTEGER", "FLOAT": "REAL", "DECIMAL": "DECIMAL",
    "BOOL": "BOOLEAN", "STRING": "TEXT", "DATE": "DATE",
    "TIMESTAMP": "TIMESTAMP", "UNKNOWN": "TEXT",
}


def format_schema(catalog: Catalog) -> str:
    """Format catalog as CREATE TABLE DDL for prompt context."""
    parts: list[str] = []
    for tname in catalog.list_tables():
        table = catalog.tables[tname]
        cols = []
        for c in table.columns:
            sql_type = _TYPE_MAP.get(c.sem_type.value, "TEXT")
            tokens = [f"  {c.name} {sql_type}"]
            if c.is_primary_key:
                tokens.append("PRIMARY KEY")
            if not c.nullable:
                tokens.append("NOT NULL")
            line = " ".join(tokens)
            if c.description:
                line += f"  -- {c.description[:80]}"
            cols.append(line)
        ddl = f"CREATE TABLE {tname} (\n" + ",\n".join(cols) + "\n);"
        for fk in catalog.get_foreign_keys_from(tname):
            ddl += f"\n-- FK: {fk.src_table}.{fk.src_column} -> {fk.dst_table}.{fk.dst_column}"
        parts.append(ddl)
    return "\n\n".join(parts)


def build_prompt(
    sql: str,
    catalog: Catalog,
    *,
    dialect: str = "sqlite",
    n: int = 5,
) -> str:
    """Build the user prompt for rewrite generation."""
    schema = format_schema(catalog)
    return _USER_PROMPT_TEMPLATE.format(
        schema=schema, sql=sql, n=n, dialect=dialect,
    )


# ---------------------------------------------------------------------------
# SQL extraction
# ---------------------------------------------------------------------------

_SQL_BLOCK_RE = re.compile(r"```sql\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_BARE_SELECT_RE = re.compile(r"(SELECT\s.+?;)", re.DOTALL | re.IGNORECASE)


def extract_sql_blocks(content: str) -> list[str]:
    """Extract SQL from markdown code blocks or bare SELECT statements."""
    if not content:
        return []
    blocks = _SQL_BLOCK_RE.findall(content)
    if blocks:
        return [b.strip() for b in blocks if b.strip()]
    bare = _BARE_SELECT_RE.findall(content)
    if bare:
        return [s.strip() for s in bare if s.strip()]
    return []


def sql_blocks_to_candidates(
    sql_blocks: list[str],
    *,
    dialect: str = "sqlite",
    source: str = "llm",
    max_candidates: int = 5,
) -> list[Candidate]:
    """Parse SQL blocks into Candidate objects."""
    candidates = []
    for idx, rewrite_sql in enumerate(sql_blocks):
        ir, err = sql_to_ir(rewrite_sql, dialect=dialect)
        if ir is not None:
            confidence = max(0.9 - idx * 0.1, 0.3)
            candidates.append(Candidate(
                id=f"{source}_{idx}",
                ir=ir,
                confidence=confidence,
                source=source,
                metadata={"sql": rewrite_sql},
            ))
    return candidates[:max_candidates]


# ---------------------------------------------------------------------------
# Abstract provider
# ---------------------------------------------------------------------------


class LLMCandidateProvider(ABC):
    """Base class for LLM-based rewrite candidate providers."""

    def __init__(self, config: Optional[LLMConfig] = None):
        self.config = config or LLMConfig()

    @abstractmethod
    def generate(
        self,
        sql: str,
        catalog: Catalog,
        *,
        dialect: str = "sqlite",
    ) -> list[Candidate]:
        """Generate rewrite candidates for the given SQL query."""
        ...


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_provider(config: Optional[LLMConfig] = None) -> LLMCandidateProvider:
    """Create an LLM provider based on config.provider."""
    cfg = config or LLMConfig()

    if cfg.provider == "openai":
        from .openai_provider import OpenAICandidateProvider
        return OpenAICandidateProvider(cfg)
    elif cfg.provider == "anthropic":
        from .anthropic_provider import AnthropicCandidateProvider
        return AnthropicCandidateProvider(cfg)
    elif cfg.provider == "amp":
        from .amp_provider import AmpCandidateProvider
        return AmpCandidateProvider(cfg)
    else:
        raise ValueError(f"Unknown LLM provider: {cfg.provider!r}. "
                         f"Supported: 'openai', 'anthropic', 'amp'")

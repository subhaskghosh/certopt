"""Anthropic Claude provider for rewrite candidate generation.

Uses the Anthropic Python SDK to call Claude models (e.g., Claude Opus 4.6).

Requires: pip install anthropic
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

from ..cegis.equivalence import Candidate
from ..schema.catalog import Catalog
from .provider import (
    SYSTEM_PROMPT,
    LLMCandidateProvider,
    LLMConfig,
    build_prompt,
    extract_sql_blocks,
    sql_blocks_to_candidates,
)

logger = logging.getLogger(__name__)

# Default to Claude Opus 4.6 (Amp Smart mode equivalent)
_DEFAULT_MODEL = "claude-opus-4-6-20250624"


class AnthropicCandidateProvider(LLMCandidateProvider):
    """Generate rewrite candidates using the Anthropic Python SDK."""

    def __init__(self, config: Optional[LLMConfig] = None):
        super().__init__(config)
        if self.config.model == "gpt-4o":
            self.config.model = _DEFAULT_MODEL

    def generate(
        self,
        sql: str,
        catalog: Catalog,
        *,
        dialect: str = "sqlite",
    ) -> list[Candidate]:
        try:
            import anthropic
        except ImportError:
            logger.warning("anthropic package not installed; returning empty candidates. "
                           "Install with: pip install anthropic")
            return []

        api_key = self.config.api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            logger.warning("No Anthropic API key found (set ANTHROPIC_API_KEY or config.api_key)")
            return []

        client = anthropic.Anthropic(api_key=api_key)
        prompt = build_prompt(sql, catalog, dialect=dialect, n=self.config.n_candidates)

        last_error: Optional[str] = None
        content = ""
        for attempt in range(self.config.max_retries + 1):
            if attempt > 0:
                delay = self.config.retry_base_delay * (3 ** (attempt - 1))
                logger.warning("Anthropic retry %d/%d after %.0fs",
                               attempt, self.config.max_retries, delay)
                time.sleep(delay)
            try:
                response = client.messages.create(
                    model=self.config.model,
                    max_tokens=4096,
                    system=SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": prompt}],
                )
                content = "".join(
                    block.text for block in response.content
                    if hasattr(block, "text")
                )
                last_error = None
                break
            except Exception as e:
                last_error = str(e)
                logger.warning("Anthropic attempt %d failed: %s", attempt + 1, e)

        if last_error:
            logger.error("Anthropic failed after %d attempts: %s",
                         self.config.max_retries + 1, last_error)
            return []

        sql_blocks = extract_sql_blocks(content)
        return sql_blocks_to_candidates(
            sql_blocks, dialect=dialect, source="anthropic",
            max_candidates=self.config.n_candidates,
        )

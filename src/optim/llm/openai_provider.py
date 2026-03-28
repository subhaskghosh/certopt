"""OpenAI-compatible LLM provider for rewrite candidate generation.

Works with any OpenAI-compatible API endpoint:
  - OpenAI (api_key via OPENAI_API_KEY)
  - Azure OpenAI (base_url + api_key)
  - Ollama (base_url=http://localhost:11434/v1)
  - vLLM, LiteLLM, etc.

Requires: pip install openai
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


class OpenAICandidateProvider(LLMCandidateProvider):
    """Generate rewrite candidates using the OpenAI Python SDK."""

    def __init__(self, config: Optional[LLMConfig] = None):
        super().__init__(config)

    def generate(
        self,
        sql: str,
        catalog: Catalog,
        *,
        dialect: str = "sqlite",
    ) -> list[Candidate]:
        try:
            import openai
        except ImportError:
            logger.warning("openai package not installed; returning empty candidates. "
                           "Install with: pip install openai")
            return []

        api_key = self.config.api_key or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            logger.warning("No OpenAI API key found (set OPENAI_API_KEY or config.api_key)")
            return []

        client_kwargs: dict = {"api_key": api_key}
        if self.config.base_url:
            client_kwargs["base_url"] = self.config.base_url

        client = openai.OpenAI(**client_kwargs)
        prompt = build_prompt(sql, catalog, dialect=dialect, n=self.config.n_candidates)

        last_error: Optional[str] = None
        content = ""
        for attempt in range(self.config.max_retries + 1):
            if attempt > 0:
                delay = self.config.retry_base_delay * (3 ** (attempt - 1))
                logger.warning("OpenAI retry %d/%d after %.0fs",
                               attempt, self.config.max_retries, delay)
                time.sleep(delay)
            try:
                response = client.chat.completions.create(
                    model=self.config.model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    timeout=self.config.timeout_seconds,
                )
                content = response.choices[0].message.content or ""
                last_error = None
                break
            except Exception as e:
                last_error = str(e)
                logger.warning("OpenAI attempt %d failed: %s", attempt + 1, e)

        if last_error:
            logger.error("OpenAI failed after %d attempts: %s",
                         self.config.max_retries + 1, last_error)
            return []

        sql_blocks = extract_sql_blocks(content)
        return sql_blocks_to_candidates(
            sql_blocks, dialect=dialect, source="openai",
            max_candidates=self.config.n_candidates,
        )

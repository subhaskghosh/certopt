"""Amp SDK-based rewrite candidate generation for query optimization.

Uses Amp as a text generator — sends schema + original SQL query,
parses semantically equivalent SQL rewrites from the response.

Requires amp-sdk (pip install amp-sdk) and an AMP_API_KEY with paid credits.
"""
# pyright: reportCallIssue=false, reportArgumentType=false, reportAttributeAccessIssue=false

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

from ..cegis.equivalence import Candidate
from ..schema.catalog import Catalog
from .provider import (
    LLMCandidateProvider,
    LLMConfig,
    build_prompt,
    extract_sql_blocks,
    sql_blocks_to_candidates,
)

logger = logging.getLogger(__name__)


# Legacy config alias for backward compatibility
AmpConfig = LLMConfig


class AmpCandidateProvider(LLMCandidateProvider):
    """Generate rewrite candidate IRs using Amp SDK."""

    def __init__(self, config: Optional[LLMConfig] = None):
        super().__init__(config)

    def generate(
        self,
        sql: str,
        catalog: Catalog,
        *,
        dialect: str = "sqlite",
    ) -> list[Candidate]:
        """Generate rewrite candidates synchronously (wraps async internally)."""
        coro = self._generate_async(sql, catalog, dialect=dialect)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result(timeout=self.config.timeout_seconds)
        else:
            return asyncio.run(coro)

    async def _generate_async(
        self,
        sql: str,
        catalog: Catalog,
        *,
        dialect: str = "sqlite",
    ) -> list[Candidate]:
        """Async implementation of rewrite candidate generation."""
        try:
            from amp_sdk import AmpOptions  # noqa: F401
        except ImportError:
            logger.warning("amp-sdk not installed; returning empty candidates")
            return []

        prompt = build_prompt(
            sql, catalog, dialect=dialect, n=self.config.n_candidates,
        )

        options = AmpOptions(
            mode=self.config.amp_mode,
            labels=["query-optim", "rewrite-gen"],
            visibility="private",
        )

        content = await self._call_amp_with_retry(prompt, options)
        if content is None:
            return []

        sql_blocks = extract_sql_blocks(content)
        return sql_blocks_to_candidates(
            sql_blocks, dialect=dialect, source="amp",
            max_candidates=self.config.n_candidates,
        )

    async def _call_amp_with_retry(
        self,
        prompt: str,
        options: object,
    ) -> Optional[str]:
        """Call Amp SDK with exponential backoff retry. Returns content or None."""
        from amp_sdk import execute

        content = ""
        last_error: Optional[str] = None
        for attempt in range(self.config.max_retries + 1):
            if attempt > 0:
                delay = self.config.retry_base_delay * (3 ** (attempt - 1))
                logger.warning("Amp SDK retry %d/%d after %.0fs delay", attempt, self.config.max_retries, delay)
                await asyncio.sleep(delay)

            content = ""
            try:
                async def _collect():
                    nonlocal content
                    async for msg in execute(prompt, options):
                        if msg.type == "assistant":
                            for c in msg.message.content:
                                if hasattr(c, "text"):
                                    content += c.text
                        elif msg.type == "result":
                            if msg.is_error:
                                logger.error("Amp SDK error: %s", msg.error)
                                return False
                            content += msg.result or ""
                            return True
                    return bool(content.strip())

                ok = await asyncio.wait_for(
                    _collect(), timeout=self.config.timeout_seconds,
                )
                if ok:
                    last_error = None
                    break
                last_error = "Amp SDK returned error"
            except asyncio.TimeoutError:
                last_error = f"Timeout after {self.config.timeout_seconds}s"
                logger.warning("Amp SDK attempt %d timed out", attempt + 1)
            except Exception as e:
                last_error = str(e)
                logger.warning("Amp SDK attempt %d failed: %s", attempt + 1, e)

        if last_error:
            logger.error("Amp SDK failed after %d attempts: %s", self.config.max_retries + 1, last_error)
            return None

        return content

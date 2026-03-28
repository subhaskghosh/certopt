# CertOpt: Proof-Carrying SQL Query Optimization via CEGIS

CertOpt is a formal verification framework for SQL query rewrites.
Given two SQL queries, it determines whether they are semantically
equivalent under bag semantics by encoding the problem as a
quantifier-free SMT formula and solving it with Z3. Equivalent
rewrites receive machine-checkable certificates; non-equivalent
pairs receive concrete distinguishing witness databases.

The system supports three modes of operation:

- **Equivalence checking**: verify whether two SQL queries produce
  identical results on all valid database instances (bounded to k rows).
- **Query optimization**: generate rewrite candidates (via algebraic
  rules and/or LLM), verify each against the original, and select the
  cheapest proven-equivalent rewrite.
- **LLM guardrail**: vet LLM-generated SQL rewrites before deployment,
  rejecting those that silently change query semantics.

## Table of Contents

- [Installation](#installation)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [LLM Providers](#llm-providers)
- [Running Benchmarks](#running-benchmarks)
- [Reproducing Paper Results](#reproducing-paper-results)
- [Project Structure](#project-structure)
- [Testing](#testing)
- [Extending](#extending)
- [License](#license)

## Installation

Requires Python 3.11 or later.

```bash
# Clone the repository
git clone <repository-url>
cd Query_Optimization_LLM

# Install core dependencies
pip install -e .

# Optional: LLM provider support
pip install -e ".[llm]"              # OpenAI / OpenAI-compatible
pip install -e ".[llm-anthropic]"    # Anthropic Claude
pip install -e ".[llm-amp]"          # Amp SDK

# Optional: witness validation via DuckDB
pip install -e ".[validate]"
```

Core dependencies (installed automatically):

| Package        | Purpose                          |
|----------------|----------------------------------|
| z3-solver      | SMT solver for equivalence proofs |
| sqlglot        | SQL parsing and dialect support  |
| pydantic       | Typed IR and schema models       |
| networkx       | Join graph analysis              |
| python-dotenv  | Environment variable loading     |

## Quick Start

### Optimize a single query

```bash
python3 -m scripts.run_optimizer \
  "SELECT e.name FROM emp e WHERE e.dept_id IN (SELECT d.id FROM dept d)" \
  --dialect postgres --k-rows 2
```

### Verify two queries are equivalent

```python
from src.optim.parser.sql_to_ir import sql_to_ir
from src.optim.cegis.witness_synthesis import synthesize_witness
from src.optim.schema.catalog import Catalog, TableInfo, ColumnInfo
from src.optim.ir.types import SemType
from src.optim.verify.encode_z3 import BoundedScope

# Build schema
catalog = Catalog(tables={
    "emp": TableInfo(name="emp", columns=[
        ColumnInfo(name="id", sem_type=SemType.INT, is_primary_key=True, nullable=False),
        ColumnInfo(name="name", sem_type=SemType.STRING),
        ColumnInfo(name="dept_id", sem_type=SemType.INT),
    ]),
})

# Parse both queries
q1, _ = sql_to_ir("SELECT name FROM emp WHERE dept_id = 1", dialect="sqlite")
q2, _ = sql_to_ir("SELECT name FROM emp WHERE dept_id = 1 AND id > 0", dialect="sqlite")

# Check equivalence (k=2 rows per table)
result = synthesize_witness(q1, q2, catalog, scope=BoundedScope(k_rows=2))
print(result.status)  # "sat" (different) or "unsat" (equivalent)
if result.witness_db:
    print(result.witness_db)  # concrete database proving non-equivalence
```

### Run a benchmark evaluation

```bash
# VeriEQL equivalence benchmark (Calcite suite)
python3 -m scripts.run_eval --benchmark verieql --suite calcite

# VeriEQL with config file
python3 -m scripts.run_eval --config configs/verieql_calcite.json

# SQLStorm LLM guardrail benchmark
python3 -m scripts.run_eval --config configs/sqlstorm_tpch.json

# JOB-Complex query optimization
python3 -m scripts.run_eval --benchmark job-complex --data-dir data/JOB-Complex
```

## Configuration

The optimizer is configured via `OptimizerConfig` (defined in
`src/optim/config.py`). Key parameters:

| Parameter                | Default    | Description                                    |
|--------------------------|------------|------------------------------------------------|
| `dialect`                | `postgres` | SQL dialect for parsing                        |
| `enable_rule_rewrites`   | `True`     | Enable algebraic rewrite rules (R1-R6)         |
| `enable_llm_rewrites`    | `False`    | Enable LLM-based rewrite generation            |
| `llm_provider`           | `openai`   | LLM provider: `openai`, `anthropic`, or `amp`  |
| `llm_model`              | `gpt-4o`   | Model name (provider-specific)                 |
| `llm_n_candidates`       | `5`        | Number of rewrite candidates to request        |
| `enable_family_pruning`  | `True`     | Prune rewrite families on first counterexample  |
| `enable_preprocessing`   | `True`     | Enable predicate promotion and table elimination|
| `validate_witnesses`     | `True`     | Validate SAT witnesses via SQLite execution     |

Bounded verification scope (`BoundedScope`):

| Parameter          | Default | Description                          |
|--------------------|---------|--------------------------------------|
| `k_rows`           | `3`     | Maximum rows per table               |
| `solver_timeout_ms`| `30000` | Z3 solver timeout in milliseconds    |

Named ablation presets are available via `OptimizerConfig.ablation(name)`:

```python
from src.optim.config import OptimizerConfig

config = OptimizerConfig.ablation("no_family_pruning")
config = OptimizerConfig.ablation("llm_only")
config = OptimizerConfig.ablation("no_preprocessing")
```

## LLM Providers

CertOpt supports three LLM providers for generating rewrite candidates.
All providers use the same prompt template and SQL extraction logic;
only the API call differs.

### OpenAI (default)

Works with OpenAI, Azure OpenAI, Ollama, vLLM, or any
OpenAI-compatible endpoint.

```bash
export OPENAI_API_KEY="sk-..."
```

```python
from src.optim.llm.provider import LLMConfig, create_provider

provider = create_provider(LLMConfig(
    provider="openai",
    model="gpt-4o",
))
candidates = provider.generate(sql, catalog, dialect="postgres")
```

For Azure OpenAI or local endpoints, set `base_url`:

```python
provider = create_provider(LLMConfig(
    provider="openai",
    model="my-deployment",
    base_url="https://my-resource.openai.azure.com/v1",
    api_key="...",
))
```

### Anthropic Claude

Uses the Anthropic Python SDK. Defaults to Claude Opus 4.6 when
no model is specified.

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
pip install anthropic
```

```python
provider = create_provider(LLMConfig(
    provider="anthropic",
    model="claude-opus-4-6-20250624",
))
```

### Amp

Uses the Amp SDK (async). Requires `AMP_API_KEY` with paid credits.

```bash
export AMP_API_KEY="..."
pip install amp-sdk
```

```python
provider = create_provider(LLMConfig(
    provider="amp",
    amp_mode="smart",  # "smart" (Claude Opus 4.6) or "deep"
))
```

### Using LLM in the optimizer loop

```python
from src.optim.config import OptimizerConfig
from src.optim.optimizer.loop import optimize

config = OptimizerConfig()
config.enable_llm_rewrites = True
config.llm_provider = "anthropic"
config.llm_model = "claude-opus-4-6-20250624"
config.llm_n_candidates = 5

result = optimize(sql, catalog, config=config)
```

## Data Setup

Benchmark datasets are not included in this repository. Clone them
into the `data/` directory before running evaluations.

### External datasets

```bash
mkdir -p data

# VeriEQL benchmarks (Calcite, Literature, LeetCode)
git clone https://github.com/VeriEQL/VeriEQL.git data/VeriEQL

# SQLStorm benchmarks (TPC-H, TPC-DS, StackOverflow, JOB schemas)
git clone https://github.com/SQL-Storm/SQLStorm.git data/SQLStorm

# JOB-Complex queries (IMDB schema, 30 multi-join queries)
git clone https://github.com/DataManagementLab/JOB-Complex.git data/JOB-Complex
```

### Bundled data

The following datasets are included in the repository under `scripts/`:

| Path | Description |
|------|-------------|
| `scripts/sqlstorm_sample/` | SQLStorm evaluation pairs (400/dataset, 8 files) with schema metadata |
| `scripts/sqlstorm_full/` | SQLStorm full evaluation pairs (all available pairs, 8 files) |
| `scripts/bird_llm_candidates/` | BIRD benchmark LLM candidates (500 queries, 3,571 candidates) for family pruning ablation |

The SQLStorm evaluation merges both sample and full data
(deduplicated) to produce the reported numbers. The BIRD dataset
is used exclusively by `scripts/run_bird_family_ablation.py`.

## Running Benchmarks

### VeriEQL (equivalence checking)

Three suites: Calcite (397 pairs), Literature (64 pairs),
LeetCode (23,994 pairs).

```bash
# Individual suite
python3 -m scripts.run_eval --benchmark verieql --suite calcite

# All suites
python3 -m scripts.run_eval --benchmark verieql --suite all --validate

# With options
python3 -m scripts.run_eval --benchmark verieql --suite calcite \
  --k-rows 3 --max-pairs 50 --verbose
```

### SQLStorm (LLM guardrail)

Four datasets: TPC-H, TPC-DS, StackOverflow, JOB.

```bash
python3 -m scripts.run_eval --config configs/sqlstorm_tpch.json
python3 -m scripts.run_eval --config configs/sqlstorm_tpcds.json
python3 -m scripts.run_eval --config configs/sqlstorm_stackoverflow.json
python3 -m scripts.run_eval --config configs/sqlstorm_job.json
```

### JOB-Complex (query optimization)

Requires the JOB-Complex dataset in `data/JOB-Complex/`.

```bash
python3 -m scripts.run_eval --benchmark job-complex --data-dir data/JOB-Complex
```

## Reproducing Paper Results

### Full evaluation pipeline

```bash
# 1. VeriEQL benchmarks (24,455 pairs)
python3 -m scripts.run_eval --config configs/verieql_calcite.json
python3 -m scripts.run_eval --config configs/verieql_literature.json
python3 -m scripts.run_eval --config configs/verieql_leetcode.json

# 2. SQLStorm benchmarks (24,495 pairs)
for ds in tpch tpcds stackoverflow job; do
  python3 -m scripts.run_eval --config configs/sqlstorm_${ds}.json
done

# 3. JOB-Complex (30 queries)
python3 -m scripts.run_eval --benchmark job-complex --data-dir data/JOB-Complex

# 4. Family pruning ablation (BIRD, 500 queries)
python3 -m scripts.run_bird_family_ablation
```

### Generating plots and tables

```bash
# VeriEQL plots
python3 scripts/gen_verieql_plots.py paper/figures/

# SQLStorm plots (uses merged sample + full-run data)
python3 scripts/gen_sqlstorm_plots.py --both paper/figures/

# Ablation plots
python3 scripts/gen_ablation_plots.py paper/figures/

# JOB-Complex tables
python3 scripts/gen_job_complex_tables.py
```

### Auditing paper numbers

Verify that all numbers cited in the paper match the result files:

```bash
python3 scripts/audit_paper_numbers.py
```

### Witness validation

Re-validate NEQ witnesses by re-executing both queries on each
witness database in DuckDB (primary) and SQLite (fallback):

```bash
# Validate all 241 "Our NEQ vs VeriEQL EQU" witnesses on LeetCode
python3 -m scripts.validate_witnesses --filter our-neq-vq-equ --suite leetcode

# Show detailed reasoning (witness DB, query results, verdict) for first 5
python3 -m scripts.validate_witnesses --filter our-neq-vq-equ --show 5

# Validate all NEQ witnesses for a suite
python3 -m scripts.validate_witnesses --suite calcite

# Validate from a specific results directory
python3 -m scripts.validate_witnesses --results-dir results/verieql_leetcode
```

### Certificate replay

Equivalence certificates are machine-checkable proofs that a rewrite
is semantically equivalent to the original query. The replay test
generates certificates from evaluation traces and independently
re-verifies each one:

```bash
# Replay certificates from LeetCode traces (default)
python3 -m scripts.test_certificate_replay

# Replay from a specific suite with a pair limit
python3 -m scripts.test_certificate_replay --suite calcite --max-pairs 50
```

Results are saved to `results/certificate_replay_test/`.

### Checking progress

Monitor a running or completed evaluation:

```bash
# Auto-detect the latest log or results directory
python3 -m scripts.check_progress

# Check a specific log file
python3 -m scripts.check_progress logs/eval_*.log

# Check a completed results directory
python3 -m scripts.check_progress results/verieql_calcite/

# Check from a summary file
python3 -m scripts.check_progress results/verieql_calcite/summary.json
```

Supports VeriEQL, SQLStorm, and JOB-Complex benchmarks with
auto-detection.

## Project Structure

```
src/optim/
  parser/          SQL string -> QueryIR (via sqlglot)
  ir/              Typed intermediate representation and normalization
  schema/          Catalog: tables, columns, foreign keys, constraints
  rewrite/         Algebraic rewrite rules (R1-R6), family classification
  verify/          Structural verification, Z3 encoding, certificates
  cegis/           Witness synthesis, preprocessing, equivalence types
  llm/             LLM providers (OpenAI, Anthropic, Amp)
  optimizer/       CEGIS loop: generate -> verify -> rank -> select
  cost/            Syntactic and EXPLAIN-based cost estimation
  eval/            Benchmark loaders (VeriEQL, SQLStorm, IMDB)
  config.py        OptimizerConfig with ablation presets

scripts/
  run_eval.py          Unified benchmark runner
  run_optimizer.py     Single-query optimizer CLI
  run_bird_family_ablation.py  Family pruning ablation
  check_progress.py    Monitor running/completed evaluations
  validate_witnesses.py  Re-validate NEQ witnesses via DuckDB/SQLite
  test_certificate_replay.py   Certificate replay validation
  audit_paper_numbers.py  Verify paper claims against data

configs/               Benchmark configuration files (JSON)
data/                  Benchmark datasets (VeriEQL, SQLStorm, JOB-Complex)
tests/                 508 unit and integration tests
results/               Evaluation output (per-pair results, summaries)
```

## Testing

```bash
# Run all tests
python3 -m pytest tests/

# Run fast tests only
python3 -m pytest tests/ -m "not slow"

# Run a specific test file
python3 -m pytest tests/test_equivalence.py -v

# Run with verbose output
python3 -m pytest tests/ -v --tb=short
```

## Extending

### Adding a new LLM provider

1. Create `src/optim/llm/my_provider.py`:

```python
from .provider import LLMCandidateProvider, LLMConfig, build_prompt, \
    extract_sql_blocks, sql_blocks_to_candidates

class MyCandidateProvider(LLMCandidateProvider):
    def generate(self, sql, catalog, *, dialect="sqlite"):
        prompt = build_prompt(sql, catalog, dialect=dialect,
                              n=self.config.n_candidates)
        content = call_my_api(prompt)  # your API call
        blocks = extract_sql_blocks(content)
        return sql_blocks_to_candidates(
            blocks, dialect=dialect, source="my_provider",
            max_candidates=self.config.n_candidates,
        )
```

2. Register in `src/optim/llm/provider.py` `create_provider()`.

### Adding a new rewrite rule

Rewrite rules live in `src/optim/rewrite/generator.py`. Each rule
takes a `QueryIR` and returns a list of candidate `QueryIR`s. Add
your rule function and register it in `RewriteGenerator.generate()`.

### Adding a new benchmark

1. Create a loader in `src/optim/eval/` that returns pairs of
   `(sql1, sql2, metadata)`.
2. Add a config file in `configs/`.
3. Add a handler in `scripts/run_eval.py`.

## License

See LICENSE file for details.

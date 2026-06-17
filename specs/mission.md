# Mission — Dometria Bulk Insert Pipeline

## Purpose

Provide an **asynchronous, resilient extraction service** that transforms raw documents received from *Dometria Connect* into fully populated, schema-compliant JSON assets — ready for downstream consumption by the Dometria platform.

## Problem Statement

Dometria Connect ingests documents (text-heavy PDFs, reports, contracts) at scale. Extracting structured data from these documents is:

1. **Computationally expensive** — NLP preprocessing, graph-based schema matching, and high-capacity LLM calls introduce significant latency.
2. **Non-deterministic** — LLM outputs must be constrained to deterministic schemas to guarantee data quality.
3. **Fragile without resilience** — network failures, API rate limits, and transient LLM errors must be absorbed gracefully, preserving intermediate results.

A synchronous API would block under load. This service decouples ingestion from extraction via an async task pipeline.

## Target Audience

**Internal only** — Dometria Connect calls this service programmatically. There are no external-facing APIs or end-user interfaces.

## Core Capabilities

| Capability | Description |
|---|---|
| **Chunking & Scoring** | Split raw text into semantically meaningful, overlapping chunks via spaCy; resolve coreferences and score each chunk's relevance in a single lightweight LLM call (GPT-4o-mini). |
| **Contract Determination** | Derive the target contract category path (`lv1/lv2/lv3`) using a two-stage hybrid approach: (1) a constrained LLM call classifies the document into one of 3 level-1 macro-categories, then (2) cosine similarity between chunk embeddings and candidate category labels — weighted by each chunk's relevance score — selects the winning `(lv2, lv3)` pair via majority vote. No knowledge graph is used; the category hierarchy is a simple 3-level tree traversed via dictionary lookup. |
| **Asset Extraction** | Load the JSON contract schema matching the category path determined by Block 2. Dynamically build a Pydantic model from the contract fields, then pass scored chunks + the dynamic schema to a high-capacity LLM (GPT-4o) via Instructor to populate the asset. |
| **Async Orchestration** | Receive requests via FastAPI, dispatch tasks to Celery workers via Redis, chain blocks automatically, and respond immediately with `202 Accepted`. |
| **Resilience** | Automatic retries per block; on irreversible failure, freeze state and flag for manual intervention — no intermediate data is lost. |

## Guiding Principles

1. **Schema-first** — Every LLM interaction is constrained by a Pydantic model via Instructor. No free-form JSON parsing.
2. **Modularity** — Each processing block is an independent Python module, independently testable and deployable as a Celery task.
3. **Fail-safe over fail-fast** — Intermediate results are persisted. A block failure does not invalidate upstream work.
4. **Observability from day one** — Structured logging at every pipeline stage, even before production-grade monitoring is introduced.
5. **Quality gates** — `pytest` + `ruff` run as pre-commit hooks. Every module has dedicated quality tests.

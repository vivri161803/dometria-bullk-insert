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
| **Chunking & Scoring** | Split raw text into semantically meaningful, overlapping chunks via spaCy; resolve coreferences; score each chunk's relevance using a lightweight LLM. |
| **Contract Determination** | Derive the target JSON schema ("contract") using a hybrid Rule-Based + RAG approach: LLM extracts level-1 fields, graph matching deduces levels 2–3, cosine similarity + weighted majority voting selects the final schema. |
| **Asset Extraction** | Populate the contract with extracted data by passing scored chunks + empty contract to a high-capacity LLM (GPT-4o), enforcing Pydantic schemas via Instructor. |
| **Async Orchestration** | Receive requests via FastAPI, dispatch tasks to Celery workers via Redis, chain blocks automatically, and respond immediately with `202 Accepted`. |
| **Resilience** | Automatic retries per block; on irreversible failure, freeze state and flag for manual intervention — no intermediate data is lost. |

## Guiding Principles

1. **Schema-first** — Every LLM interaction is constrained by a Pydantic model via Instructor. No free-form JSON parsing.
2. **Modularity** — Each processing block is an independent Python module, independently testable and deployable as a Celery task.
3. **Fail-safe over fail-fast** — Intermediate results are persisted. A block failure does not invalidate upstream work.
4. **Observability from day one** — Structured logging at every pipeline stage, even before production-grade monitoring is introduced.
5. **Quality gates** — `pytest` + `ruff` run as pre-commit hooks. Every module has dedicated quality tests.

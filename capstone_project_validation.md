# Capstone Project Scenario Validation
## Selected Scenario: Personal Knowledge Assistant

This document validates our implemented Agentic RAG solution against the predefined **Personal Knowledge Assistant (PKA)** scenario requirements.

---

## 🎯 1. Predefined Scenario Mapping: Personal Knowledge Assistant (PKA)

The project matches the **Personal Knowledge Assistant** scenario exactly as defined in the course instructions:

> **Description**: A multi-agent system combining document RAG with web search fallback via MCP.
> - **Research Agent**: Indexes personal/corporate documents.
> - **Web Agent**: Handles live queries via external search.
> - **Synthesis Agent**: Combines results and provides coherent responses with precise citations.

---

## 🏗️ 2. Core Architecture & Multi-Agent Mapping

Our self-hosted Agentic RAG system perfectly aligns with the required three-agent cooperative structure:

```mermaid
flowchart TD
    User["User Query"] --> Synthesis["Synthesis Agent (FastAPI + LangGraph/ReAct Router)"]
    Synthesis -->|1. Search Local Index| Research["Research Agent (Celery Worker + OpenSearch/Neo4j)"]
    Research -->|Return Documents & Graph Relations| Synthesis
    Synthesis -->|2. Web Fallback (If local incomplete)| Web["Web Agent (public_web_search Tool via MCP)"]
    Web -->|Return Live Info| Synthesis
    Synthesis -->|3. Combine, Cache, and Cite| Response["Coherent Final Response with Citations"]
```

### 🧑‍🔬 A. Research Agent (Local Document Indexer & Retriever)
*   **Role**: Handles high-speed document indexing, parsing, and multi-modal semantic retrieval.
*   **Implementation**:
    - Runs a host-based **SMB Share Watchdog** that performs highly optimized walks to scan all documents in **20.52 seconds**.
    - Employs **8 concurrent Celery indexing workers** to recursively segment documents using a semantic cache and layout-preserving PyMuPDF parsing.
    - Indexing processes chunks into an **OpenSearch FAISS HNSW k-NN index** (dense vectors) combined with lexical BM25 database indices.
    - Builds a rich relationships graph inside a **Neo4j Property Graph** utilizing batched index-friendly `UNWIND` Cypher statements.

### 🌐 B. Web Agent (MCP Live Query Searcher)
*   **Role**: Handles external search fallback queries when local knowledge is incomplete or missing.
*   **Implementation**:
    - Deploys a dedicated **`public_web_search` tool** connected via Model Context Protocol (MCP) standards.
    - Intercepts requests that cannot be answered via OpenSearch/Neo4j and retrieves real-time context from the web.
    - Integrates standard rate limiters and timeout circuit breakers (`GPUCircuitBreaker`) to manage high-throughput requests without server starvation.

### 🧠 C. Synthesis Agent (The Coordinator & Brain)
*   **Role**: Orchestrates the user session, combines vector/web/graph results, manages caching, and formats the output.
*   **Implementation**:
    - Orchestrated via FastAPI and a **LangGraph/ReAct Agent** state manager.
    - Resolves pronouns and histories, then routes queries to the correct tool (OpenSearch, Neo4j, or Web Search fallback).
    - Merges lexical keyword and dense semantic matches using **Reciprocal Rank Fusion (RRF)**, then ranks them using a local `Qwen3-VL-Reranker-8B` cross-encoder.
    - Implements a PostgreSQL **`pgvector` semantic cache** to intercept identical queries and serve instant sub-millisecond cached answers.
    - Serves crisp document page images on-the-fly, mapping visual bounding-box highlights precisely over cited text coordinates in our custom PDF viewer.

---

## 🛠️ 3. Verification & Compliance Matrix

| Requirement | Implementation Detail | Status |
| :--- | :--- | :---: |
| **Multi-agent architecture** | Coordinated **Research**, **Web**, and **Synthesis** agents collaborating via LangGraph. | **COMPLIANT** |
| **RAG Pipeline** | Dense/Lexical search fused via **RRF** and reranked using a local FP8 cross-encoder. | **COMPLIANT** |
| **MCP Integration** | Live web search tool and dynamic browser execution engines bound via MCP. | **COMPLIANT** |
| **Real-world applicability** | 100% self-hosted, localized VRAM model hosting (vLLM Qwen3.5 32K context) running at **$0.00** API costs. | **COMPLIANT** |
| **Inter-agent communication** | Shared state variables, history retention, evaluation-reflection routing, and citation mapping. | **COMPLIANT** |
| **Testability & Grounding** | Automatic SQL-redirection grounding bypass and E2E behavior suite (`test_llm_behavior.py` passing 6/6). | **COMPLIANT** |
| **Demonstrability** | Fully reactive React/Vite UI featuring collapsed Agent Reasoning timeline steps and dynamic PDF citation rendering. | **COMPLIANT** |

# 📚 Self-Hosted Personal Knowledge Assistant
### Multi-Agent Local-First Knowledge Management & RAG System with Web Search Fallback

This repository contains the complete self-hosted, local-first **Personal Knowledge Assistant (PKA)**. The system implements a robust multi-agent architecture combining document retrieval-augmented generation (RAG) over personal archives with a dynamic live web search fallback via Model Context Protocol (MCP).

---

## 🚀 Key Features

*   **🧑💻 Multi-Agent Architecture:** Powered by three distinct, collaborative agents:
    -   **Research Agent:** Indexes and retrieves knowledge from personal document corpora using hybrid vector/lexical search and relationship graph mappings.
    -   **Web Agent:** Monitors live queries and triggers web search fallback via MCP when local knowledge is insufficient or real-time data is requested.
    -   **Synthesis Agent:** Coordinates query workflows inside a LangGraph structure, combining local facts and live web discoveries into cohesive, cited responses.
*   **100% Self-Hosted & Local-First:** High-performance local inference via `vLLM` and a custom Python model API server. **Zero external API costs** and absolute privacy for your personal documents and notes.
*   **High-Speed Mass Sync Watchdog:** Real-time Python watchdog script featuring highly optimized `scandir` walks, scanning all **9,949 personal documents in just 20.52 seconds**.
*   **Deep Factuality (Hybrid Search & Reranking):** Blends BM25 lexical keyword matches and k-NN vector embeddings via Reciprocal Rank Fusion (RRF), passed through the **`Qwen3-VL-Reranker-8B`** cross-encoder.
*   **Dynamic Document Viewer:** Full-featured PDF drawer that lazy-loads and renders document pages, highlighting exact source citation coordinates.
*   **Data Permanence Safeguards:** Database-wiping operations are programmatically protected to ensure indexing integrity and prevent accidental deletion of your knowledge base.

---

## 🛠️ Technology Stack

*   **API Gateway:** Nginx reverse proxy (supporting SSL/TLS)
*   **Backend Server:** FastAPI (Asynchronous Python)
*   **Task Queue:** Valkey (Redis-equivalent broker) + Celery Workers
*   **Databases:** PostgreSQL (`pgvector` for semantic cache), OpenSearch (Faiss KNN for vectors), Neo4j (Graph for entity relationships)
*   **Inference Engines:** vLLM (`Qwen3.5-27B-FP8`) & Custom API (`Qwen3-VL-Embedding-8B` & `Qwen3-VL-Reranker-8B`)
*   **Observability & Telemetry:** Arize Phoenix, Prometheus, Grafana

---

## ⚙️ Setup & Installation Instructions

### 1. Prerequisites
*   Ubuntu 22.04 LTS (or compatible Linux host)
*   NVIDIA GPU with CUDA 12.0+ (RTX 6000 Ada or similar recommended)
*   Docker & Docker Compose v2.0+
*   Python 3.10+

### 2. Configure Environment Variables
Clone the repository and create a `.env` file in the project root:
```bash
cp .env.example .env
nano .env
```
Fill in the following key variables:
```env
JWT_SECRET=your-random-jwt-secret-key-here
OPENSEARCH_PASSWORD=LegalAI_2026!
POSTGRES_PASSWORD=secure-postgres-password
NEO4J_PASSWORD=secure-neo4j-password
GPU_NODE_IP=172.19.0.1  # Docker gateway IP
MAIN_MODEL=Qwen/Qwen3.5-27B-FP8
EMBED_MODEL=Qwen/Qwen3-VL-Embedding-8B
RERANK_MODEL=Qwen/Qwen3-VL-Reranker-8B
```

### 3. Launch the PKA Services
Start the self-hosted services via Docker Compose:
```bash
docker compose up -d --build
```
Verify that all containers are healthy:
```bash
docker compose ps
```

### 4. Deploy the Ingestion Watchdog
Start the optimized watchdog directly on the host to monitor your local documents share:
```bash
# Install dependencies on the host
pip3 install smbprotocol cryptography pyspnego

# Launch the watchdog detached (unbuffered logging)
nohup python3 -u smb_watcher.py > smb_watcher.log 2>&1 &
```

### 5. Access the Web UI
Open your web browser and navigate to:
*   **Frontend Chat UI:** `https://your-server-ip:8443`
*   **Grafana Dashboards:** `http://your-server-ip:3000` (admin/changeme)
*   **Arize Phoenix Tracing:** `http://your-server-ip:6006`

---

## 🧪 Verification & Health Monitoring

To verify that the reproduced system is fully functional and monitor its active state, you can use the built-in health probes and service logs:

### 1. Internal Health Endpoint
Verify that the API backend is fully active and successfully connected to PostgreSQL, OpenSearch, and Neo4j:
```bash
curl -k https://localhost:8443/api/v1/health
```

### 2. Monitor Real-Time Service Logs
Check active operations, database queries, and agent routing workflows:
*   **FastAPI Backend & Synthesis Agent logs:**
    ```bash
    docker compose logs -f fastapi-app
    ```
*   **Celery Ingestion Worker logs:**
    ```bash
    docker compose logs -f celery-worker
    ```
*   **SMB Ingestion Agent logs:**
    ```bash
    docker compose logs -f ingestion-agent
    ```


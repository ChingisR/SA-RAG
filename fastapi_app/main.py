"""
SA-RAG Main Application Entrypoint

This module initializes the FastAPI application and configures:
1. OpenTelemetry tracing (via Arize Phoenix).
2. CORS and Rate Limiting (SlowAPI).
3. Database and Model configurations (LlamaIndex, OpenAI, vLLM).
4. Sub-routers for authentication, document ingestion, GraphRAG, and chat sessions.

It acts as the primary orchestrator for incoming HTTP requests, delegating
complex logic to the core domain modules and background Celery workers.
"""

import os
import sys
import uuid

# MOCK NLTK DOWNLOAD to prevent hanging on startup
try:
    import nltk
    def _mock_download(*args, **kwargs):
        print(f"Skipping nltk.download({args}, {kwargs})", file=sys.stderr)
        return True
    nltk.download = _mock_download
except ImportError:
    pass


import re
import json
import base64
import shutil
import psycopg2
import nest_asyncio
from typing import List, Optional
from engines.hybrid_search import get_hybrid_rrf_engine, run_opensearch_engine, _build_os_vector_store
from engines.sql_engine import get_sql_engine, _build_sql_engine, execute_hr_sql
from engines.graph_engine import get_graph_engine
from engines.vision_engine import get_vision_engine
from fastapi import FastAPI, UploadFile, File, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel
import jwt
from datetime import datetime, timedelta, timezone
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
import structlog

# Initialize structured JSON logger
structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer()
    ]
)
logger = structlog.get_logger()
try:
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.util import get_remote_address
    from slowapi.errors import RateLimitExceeded
    def _get_user_key(request: Request) -> str:
        """Key rate limits by JWT email, falling back to IP address."""
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            try:
                payload = jwt.decode(auth[7:], options={"verify_signature": False})
                return payload.get("sub", get_remote_address(request))
            except Exception:
                pass
        return get_remote_address(request)
    limiter = Limiter(key_func=_get_user_key)
    RATE_LIMIT_ENABLED = True
except ImportError:
    RATE_LIMIT_ENABLED = False
    limiter = None

# Critical: Allows LlamaIndex's internal async loops to run safely inside FastAPI
nest_asyncio.apply()

# --- BCRYPT MONKEY PATCH ---
# Direct bcrypt usage instead of passlib to avoid Python 3.12+ compatibility issues
import bcrypt

import time
from threading import Lock

class GPUCircuitBreaker:
    def __init__(self, failure_threshold=3, recovery_timeout=60):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failures = 0
        self.last_failure_time = 0
        self.state = "CLOSED"  # CLOSED → OPEN → HALF_OPEN
        self._lock = Lock()

    def can_execute(self) -> bool:
        with self._lock:
            if self.state == "CLOSED": return True
            if self.state == "OPEN":
                if time.time() - self.last_failure_time > self.recovery_timeout:
                    self.state = "HALF_OPEN"
                    return True
                return False
            return True

    def record_success(self):
        with self._lock:
            self.failures = 0
            self.state = "CLOSED"

    def record_failure(self):
        with self._lock:
            self.failures += 1
            self.last_failure_time = time.time()
            if self.failures >= self.failure_threshold:
                self.state = "OPEN"

gpu_breaker = GPUCircuitBreaker(failure_threshold=3, recovery_timeout=60)

from core.security import hash_password, verify_password
# ---------------------------

from sqlalchemy import create_engine
from llama_index.core import VectorStoreIndex, StorageContext, Settings, PromptTemplate, SQLDatabase, PropertyGraphIndex, SimpleDirectoryReader
from llama_index.core.tools import QueryEngineTool, ToolMetadata
from llama_index.core.query_engine import RouterQueryEngine, NLSQLTableQueryEngine, SubQuestionQueryEngine
from llama_index.core.selectors import LLMSingleSelector
from llama_index.core.chat_engine import CondenseQuestionChatEngine
from llama_index.core.llms import ChatMessage, MessageRole
from llama_index.core.vector_stores import ExactMatchFilter, MetadataFilters
from llama_index.core.agent import ReActAgent

# vLLM exposes an OpenAI-compatible REST API — use OpenAI-like integrations
from llama_index.llms.openai_like import OpenAILike
from llama_index.embeddings.openai import OpenAIEmbedding
from llama_index.core.postprocessor import LLMRerank

from llama_index.vector_stores.opensearch import OpensearchVectorClient, OpensearchVectorStore
from llama_index.graph_stores.neo4j import Neo4jPropertyGraphStore
from llama_index.core.retrievers import BaseRetriever
from llama_index.core.schema import NodeWithScore, TextNode, QueryBundle
from llama_index.core.query_engine import RetrieverQueryEngine
from llama_index.core.response_synthesizers import get_response_synthesizer
# from pgvector.psycopg2 import register_vector  # semantic cache disabled
from worker import process_document_task
import numpy as np
import requests
from urllib3.exceptions import InsecureRequestWarning
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

import asyncio
from contextlib import asynccontextmanager
from openinference.instrumentation.llama_index import LlamaIndexInstrumentor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

try:
    phoenix_endpoint = os.getenv("PHOENIX_COLLECTOR_ENDPOINT", "http://phoenix:4318/v1/traces")
    if phoenix_endpoint:
        tracer_provider = TracerProvider()
        tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(phoenix_endpoint)))
        LlamaIndexInstrumentor().instrument(tracer_provider=tracer_provider)
        try:
            from openinference.instrumentation.langchain import LangChainInstrumentor
            LangChainInstrumentor().instrument(tracer_provider=tracer_provider)
            print("✅ Arize Telemetry active for LangChain & LlamaIndex")
        except ImportError:
            pass
except Exception as e:
    print(f"⚠️ Telemetry setup failed: {e}")

# NOTE: lifespan is defined after module-level singletons are declared (~line 685).
# FastAPI accepts the name as a forward reference because Python resolves it at
# call time, not at import time — so this is safe as long as lifespan is defined
# before the ASGI server begins handling requests.
app = FastAPI(title="Agentic RAG — vLLM Edition", lifespan=None)  # lifespan patched below

if RATE_LIMIT_ENABLED:
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS: read allowed origins from env to prevent wildcard exposure in production
_raw_origins = os.getenv("CORS_ORIGINS", "")
ALLOWED_ORIGINS = [o.strip() for o in _raw_origins.split(",") if o.strip()]
if not ALLOWED_ORIGINS or ALLOWED_ORIGINS == [""]:
    ALLOWED_ORIGINS = ["https://10.242.102.2:8443"]  # restrictive default
    print("⚠️  CORS_ORIGINS not set — using restrictive default")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from routers.auth import router as auth_router
from routers.sessions import router as sessions_router
from routers.documents import router as documents_router
from routers.feedback import router as feedback_router
from routers.analytics import router as analytics_router
from routers.graphrag import router as graphrag_router

app.include_router(auth_router)
app.include_router(sessions_router)
app.include_router(documents_router)
app.include_router(feedback_router)
app.include_router(analytics_router)
app.include_router(graphrag_router)

# ── Prometheus Metrics ─────────────────────────────────────────────────────
# Exposes /metrics for Prometheus scraping. Collected metrics:
#   http_requests_total{method, endpoint, status} — request count per route
#   http_request_duration_seconds{...}            — latency histogram
#   http_requests_in_progress{...}                — current inflight requests
try:
    from prometheus_fastapi_instrumentator import Instrumentator as _PFI
    _PFI().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)
    print("✅ Prometheus metrics exposed at /metrics")
except ImportError:
    print("⚠️ prometheus-fastapi-instrumentator not installed — /metrics unavailable")

@app.middleware("http")
async def add_request_id_middleware(request: Request, call_next):
    req_id = str(uuid.uuid4())
    structlog.contextvars.bind_contextvars(request_id=req_id)
    try:
        response = await call_next(request)
        response.headers["X-Request-ID"] = req_id
        return response
    finally:
        structlog.contextvars.clear_contextvars()

from core.security import get_current_user, SECRET_KEY, ALGORITHM, ACCESS_TOKEN_EXPIRE_MINUTES

CORPORATE_AGENT_CONTEXT = (
    "You are a specialized Enterprise Assistant for KMG Kashagan B.V., managing the Kazakhstani share in the North Caspian PSA. "
    "When a user asks a follow-up question, you MUST resolve all pronouns (he, she, they, it, his, her, etc.) "
    "to the specific person, offshore asset, Kashagan field well, facility, or event mentioned in the earlier conversation history. "
    "ALWAYS formulate a complete, standalone query when calling your search tools. "
    "CRITICAL TOOL ROUTING RULE: "
    "1. The 'structured_asset_database' contains ONLY metric and status records (asset type, location, installation date, operating status). "
    "It DOES NOT contain unstructured business operations, HSE policies, hydrogen sulfide (H2S) guidelines, or complex engineering architectures. "
    "2. If you are asked about standard operating procedures, workflows, engineering architectures, "
    "HSE incident reports, harsh winter offshore conditions, contracts, or general corporate knowledge regarding the Kashagan field, "
    "you MUST use the 'unstructured_pdf_docs' tool or 'knowledge_graph'. "
    "DO NOT rely solely on the SQL tool for these topics."
)

@app.get("/health")
@app.get("/health/live")
def health_live():
    """Lightweight liveness probe — Docker & Portainer healthcheck target."""
    return {"status": "ok", "service": "fastapi-agentic-rag"}

@app.get("/health/ready")
def health_ready():
    """Deep readiness probe: Checks Postgres and Core Services."""
    status_dict = {"status": "ok", "checks": {}}
    
    # 1. Database connection check
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.close()
        release_db_conn(conn)
        status_dict["checks"]["postgres"] = "up"
    except Exception as e:
        status_dict["status"] = "error"
        status_dict["checks"]["postgres"] = f"down: {str(e)}"
        logger.error("readiness_probe_db_fail", error=str(e))
    
    # 2. VLLM / GPU circuit breaker check
    if not gpu_breaker.can_execute():
        status_dict["status"] = "error"
        status_dict["checks"]["vllm"] = "circuit_breaker_open"
        logger.error("readiness_probe_vllm_fail", state="circuit_breaker_open")
    else:
        status_dict["checks"]["vllm"] = "up"
        
    # 3. OpenSearch cluster check
    os_url  = os.getenv("OPENSEARCH_URL", "https://opensearch:9200")
    os_user = os.getenv("OPENSEARCH_USER")
    os_pass = os.getenv("OPENSEARCH_PASSWORD")
    if os_user and os_pass:
        try:
            res = requests.get(f"{os_url}/_cluster/health", auth=(os_user, os_pass), verify=False, timeout=3)
            res.raise_for_status()
            os_status = res.json().get("status", "unknown")
            status_dict["checks"]["opensearch"] = os_status
            if os_status == "red":
                status_dict["status"] = "error"
                logger.error("readiness_probe_os_red_status")
        except Exception as e:
            status_dict["status"] = "error"
            status_dict["checks"]["opensearch"] = f"down: {str(e)}"
            logger.error("readiness_probe_os_fail", error=str(e))

    # 4. Arize Phoenix telemetry check
    try:
        phoenix_url = os.getenv("PHOENIX_COLLECTOR_ENDPOINT", "http://phoenix:4318/v1/traces")
        # Derive the Phoenix web UI origin (port 6006) from the OTLP endpoint
        phoenix_host = phoenix_url.split(":")[1].lstrip("/")  # e.g. "phoenix"
        phoenix_health = requests.get(f"http://{phoenix_host}:6006", timeout=2, allow_redirects=False)
        status_dict["checks"]["phoenix"] = "up" if phoenix_health.status_code < 500 else "degraded"
    except Exception:
        # Phoenix is non-critical — log warning but do NOT degrade the overall status
        status_dict["checks"]["phoenix"] = "unreachable"
        logger.warning("readiness_probe_phoenix_unreachable")
        
    if status_dict["status"] != "ok":
        raise HTTPException(status_code=503, detail=status_dict)
    
    logger.info("readiness_probe_success", checks=status_dict["checks"])
    return status_dict


# --- Titan VL Architecture: Cross-Server Configuration ---
from core.config import GPU_NODE_IP, VLLM_URL, INFINITY_EMBED_URL, PG_DSN, MAIN_MODEL, EMBED_MODEL, DIMENSIONS, CACHE_DIM, mm_llm
from core.db import get_db_conn, release_db_conn, init_db, init_db_pool, close_db_pool

os.makedirs("data", exist_ok=True)
os.makedirs("images", exist_ok=True)



def ensure_opensearch_pipeline():
    """Ensure the RRF search pipeline exists in OpenSearch."""
    os_url  = os.getenv("OPENSEARCH_URL", "https://opensearch:9200")
    os_user = os.getenv("OPENSEARCH_USER")
    os_pass = os.getenv("OPENSEARCH_PASSWORD")
    if not os_user or not os_pass:
        print("⚠️  OpenSearch credentials missing — skipping pipeline init.")
        return
    pipeline_url = f"{os_url}/_search/pipeline/rrf-pipeline"
    
    pipeline_config = {
        "description": "RRF normalization pipeline",
        "phase_results_processors": [
            {
                "score-ranker-processor": {
                    "combination": { 
                        "technique": "rrf"
                    }
                }
            }
        ]
    }
    for i in range(10):  # Retry for 50 seconds
        try:
            resp = requests.put(pipeline_url, json=pipeline_config, auth=(os_user, os_pass), verify=False, timeout=5)
            if resp.status_code in [200, 201]:
                print("✅ OpenSearch: 'rrf-pipeline' initialized.")
                return
            else:
                print(f"⏳ OpenSearch Pipeline (Attempt {i+1}/10): {resp.text}")
        except Exception as e:
            print(f"⏳ OpenSearch Pipeline (Attempt {i+1}/10): Waiting for API...")
        time.sleep(5)
    print("❌ OpenSearch Pipeline Init Failed after 10 attempts.")
    
def ensure_opensearch_index():
    """Proactively create the k-NN index with FAISS engine to avoid nmslib deprecation errors."""
    os_url  = os.getenv("OPENSEARCH_URL", "https://opensearch:9200")
    os_user = os.getenv("OPENSEARCH_USER")
    os_pass = os.getenv("OPENSEARCH_PASSWORD")
    if not os_user or not os_pass:
        print("⚠️  OpenSearch credentials missing — skipping index init.")
        return
    index_url = f"{os_url}/universal_docs_v1"
    
    # 1. Check if index exists
    res = requests.get(index_url, auth=(os_user, os_pass), verify=False, timeout=5)
    
    should_create = False
    if res.status_code == 200:
        # Check engine & dimension
        eb_props = res.json().get("universal_docs_v1", {}).get("mappings", {}).get("properties", {}).get("embedding", {})
        current_engine = eb_props.get("method", {}).get("engine")
        current_dim = eb_props.get("dimension")
        if current_dim != DIMENSIONS:
            error_msg = f"FATAL: Dimension mismatch (index: {current_dim}, target: {DIMENSIONS}). Refusing to auto-delete production index. Manually delete index if intended."
            print(f"🚨 {error_msg}")
            raise RuntimeError(error_msg)
        elif current_engine == "nmslib":
            error_msg = "FATAL: Found deprecated 'nmslib' engine. Refusing to auto-delete production index. Manually delete index if intended."
            print(f"🚨 {error_msg}")
            raise RuntimeError(error_msg)
    else:
        should_create = True
        
    if should_create:
        mapping = {
            "settings": {"index": {"knn": True}},
            "mappings": {
                "properties": {
                    "embedding": {
                        "type": "knn_vector",
                        "dimension": DIMENSIONS,  # Unified via EMBED_DIM env var
                        "method": {
                            "name": "hnsw",
                            "engine": "faiss",
                            "space_type": "l2"
                        }
                    },
                    "content": {"type": "text"},
                    "metadata": {"type": "object"}
                }
            }
        }
        res = requests.put(index_url, json=mapping, auth=(os_user, os_pass), verify=False, timeout=5)
        if res.status_code in [200, 201]:
            print(f"✅ OpenSearch Index 'universal_docs_v1' initialized with FAISS engine (dim={DIMENSIONS}).")
        else:
            print(f"❌ Failed to initialize OpenSearch index: {res.text}")

# ──────────────────────────────────────────────────────────────────────────
# MODULE-LEVEL ENGINE SINGLETONS — built once at startup, reused per request
# ──────────────────────────────────────────────────────────────────────────
_whisper_model = None  # Loaded once at startup — avoids 10s per-request load



@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan: startup → yield → graceful shutdown."""
    global _whisper_model
    # ── Startup ────────────────────────────────────────────────────────────
    init_db_pool()
    init_db()
    ensure_opensearch_pipeline()
    ensure_opensearch_index()
    _build_sql_engine()
    _build_os_vector_store()
    yield  # app is running

    # ── Shutdown ───────────────────────────────────────────────────────────
    close_db_pool()

# Wire the lifespan now that it's defined (was forward-ref at app creation)
app.router.lifespan_context = lifespan

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "50"))

# All formats that worker.py can now ingest
ALLOWED_MIME_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # .docx
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",        # .xlsx
    "application/vnd.ms-excel",                                                   # .xls
    "application/vnd.openxmlformats-officedocument.presentationml.presentation", # .pptx
    "text/plain",    # .txt
    "text/markdown", # .md
    "text/csv",      # .csv
    "image/jpeg",    # .jpg, .jpeg
    "image/png",     # .png
    "application/octet-stream",  # fallback for poorly typed clients
}
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".xls", ".pptx", ".txt", ".md", ".csv", ".jpg", ".jpeg", ".png"}



class MessageDict(BaseModel):
    role: str
    content: str

class QueryRequest(BaseModel):
    query: str
    session_id: Optional[str] = None
    chat_history: List[MessageDict] = []
    image_paths: Optional[List[str]] = []
    similarity_top_k: int = 20
    rerank_top_n: int = 5 
    temperature: float = 0.1
    user_role: str = "Operations_Admin"
    framework: str = "langchain"
    output_thinking: bool = True
    doc_type: Optional[str] = None
    tenant_id: Optional[str] = None


from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import sys

async def async_mcp_web_search(query: str) -> str:
    """CRITICAL: Search the public internet or current events using the external MCP Server."""
    try:
        server_params = StdioServerParameters(command=sys.executable, args=["mcp_server.py"])
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("public_web_search", arguments={"query": query})
                return result.content[0].text
    except Exception as e:
        return f"MCP Web search failed: {str(e)}"


# ──────────────────────────────────────────────────────────────────────────
# LANGGRAPH MULTI-AGENT ORCHESTRATION 
# ──────────────────────────────────────────────────────────────────────────
try:
    from langchain_core.messages import HumanMessage, AIMessage
except ImportError:
    pass

from core.langgraph_workflow import build_langgraph_workflow

# NOTE: The unauthenticated /analytics stub has been removed.
# The canonical, secured endpoint is /api/analytics (line ~314) which
# enforces JWT auth and returns real data from Postgres.
# Any frontend that calls /analytics must be updated to call /api/analytics.

# --- DUPLICATE STUBS REMOVED ---
# The following were shadowing the real authenticated endpoints
# defined at L336 (list_documents) and L405 (submit_feedback).
# @app.get("/documents")
# async def get_documents():
#     return {"documents": [], "total": 0}
#
# class FeedbackRequest(BaseModel):  # already defined at L400
#     message_id: str
#     rating: int
#     comment: str | None = None
#
# @app.post("/feedback")
# async def submit_feedback(fb: FeedbackRequest):
#     return {"status": "success"}

class StreamFilter:
    """Stateful tokenizer that buffers raw XML tool calls and emits clean status updates."""
    def __init__(self):
        self.buffer = ""
        self.tool_call_accumulator = ""
        self.state = "NORMAL"  # "NORMAL", "IN_TOOL_CALL", "DISCARDING"

    def is_suppressed_tag(self, tag: str) -> bool:
        tag = tag.strip()
        if tag in ["<tool_call>", "</tool_call>", "</function>", "</parameter>"]:
            return True
        if tag.startswith("<function") and tag.endswith(">"):
            return True
        if tag.startswith("<parameter") and tag.endswith(">"):
            return True
        return False

    def is_partial_suppressed_tag(self, s: str) -> bool:
        if not s:
            return False
        if self.is_suppressed_tag(s):
            return True
        for tag in ["<tool_call>", "</tool_call>", "</function>", "</parameter>"]:
            if tag.startswith(s):
                return True
        if "<function".startswith(s) or s.startswith("<function"):
            if ">" not in s:
                return True
        if "<parameter".startswith(s) or s.startswith("<parameter"):
            if ">" not in s:
                return True
        return False

    def render_tool_status(self, content: str) -> str:
        # Extract function name and arguments
        func_match = re.search(r"function=(\w+)", content)
        func_name = func_match.group(1) if func_match else "unknown_tool"
        
        # Try to extract search or SQL queries
        param_match = re.search(r"<parameter=[^>]+>(.*?)<\/parameter>", content, re.DOTALL)
        query_val = param_match.group(1).strip() if param_match else ""
        
        emoji_map = {
            "document_agent_tool": "🔍 **Searching internal document repository**",
            "sql_agent_tool": "📊 **Querying corporate asset database**",
            "graph_agent_tool": "🕸️ **Analyzing Neo4j knowledge graph**",
            "vision_agent_tool": "👁️ **Analyzing image and visual charts**",
            "web_search_tool": "🌐 **Searching public web**",
        }
        
        title = emoji_map.get(func_name, "⚙️ **Running reasoning engine**")
        details = f" for: *\"{query_val}\"*..." if query_val else "..."
        return f"\n\n{title}{details}\n\n"

    def process_char(self, c: str):
        if self.state == "NORMAL":
            yield c
        elif self.state == "IN_TOOL_CALL":
            self.tool_call_accumulator += c
        elif self.state == "DISCARDING":
            pass

    def process_token(self, token: str) -> str:
        output_chars = []
        for c in token:
            if self.buffer or c == '<':
                self.buffer += c
                if self.is_partial_suppressed_tag(self.buffer):
                    if self.is_suppressed_tag(self.buffer):
                        tag = self.buffer
                        self.buffer = ""
                        
                        if self.state == "NORMAL":
                            if tag == "<tool_call>":
                                self.state = "IN_TOOL_CALL"
                                self.tool_call_accumulator = "<tool_call>"
                        elif self.state == "IN_TOOL_CALL":
                            self.tool_call_accumulator += tag
                            if tag == "</function>":
                                status = self.render_tool_status(self.tool_call_accumulator)
                                self.state = "DISCARDING"
                                self.tool_call_accumulator = ""
                                output_chars.append(status)
                            elif tag == "</tool_call>":
                                self.state = "NORMAL"
                                self.tool_call_accumulator = ""
                        elif self.state == "DISCARDING":
                            if tag == "</tool_call>":
                                self.state = "NORMAL"
                else:
                    flush_content = self.buffer
                    self.buffer = ""
                    for char in flush_content:
                        output_chars.extend(self.process_char(char))
            else:
                output_chars.extend(self.process_char(c))
                
        return "".join(output_chars)

    def flush(self) -> str:
        """Returns any un-flushed buffer at the end of the stream, clearing raw suppressed tag prefixes."""
        if self.buffer:
            out = self.buffer
            self.buffer = ""
            if self.is_partial_suppressed_tag(out):
                return ""
            if self.state == "IN_TOOL_CALL" and self.tool_call_accumulator:
                try:
                    status = self.render_tool_status(self.tool_call_accumulator)
                    self.tool_call_accumulator = ""
                    return status
                except Exception:
                    return ""
            return out
            
        if self.state == "IN_TOOL_CALL" and self.tool_call_accumulator:
            try:
                status = self.render_tool_status(self.tool_call_accumulator)
                self.tool_call_accumulator = ""
                return status
            except Exception:
                return ""
        return ""


@app.post("/query")
async def query_index(request: QueryRequest, http_request: Request, current_user: dict = Depends(get_current_user)):
    print(f"DEBUG: Received request with framework: {request.framework}")
    if request.session_id:
        try:
            conn = get_db_conn()
            try:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO chat_messages (session_id, role, content) VALUES (%s, %s, %s)",
                    (request.session_id, "user", request.query)
                )
                conn.commit()
                cur.close()
            finally:
                release_db_conn(conn)
        except Exception as e:
            print(f"🚨 Failed to save user message: {e}")

    async def event_generator():
        # query_embedding = None  # semantic cache disabled
        # Inherit trust relationship from the verified JWT claim
        # OVERRIDE: Using universal GLOBAL_READ token for Default-Open ACL architecture
        verified_role = "GLOBAL_READ"
        try:
            # ── Semantic cache (pgvector) disabled — OpenSearch hybrid is the primary path.
            # To re-enable: uncomment the SEMANTIC CACHE CHECK and SAVE TO CACHE blocks,
            # restore 'from pgvector.psycopg2 import register_vector', and fix RBAC
            # (replace hardcoded verified_role="GLOBAL_READ" with actual JWT role).
            #
            # --- SEMANTIC CACHE CHECK (disabled) ---
            # query_embedding = None
            # try:
            #     import socket as _socket
            #     _embed_host = os.getenv("GPU_NODE_IP", "172.18.0.1")
            #     _sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            #     _sock.settimeout(0.5)
            #     _embed_up = _sock.connect_ex((_embed_host, 8082)) == 0
            #     _sock.close()
            #     if _embed_up:
            #         effective_query = f"search_query: {request.query}" if "nomic-embed-text" in os.getenv("EMBED_MODEL", "") else request.query
            #         loop = asyncio.get_event_loop()
            #         query_embedding = np.array(
            #             await asyncio.wait_for(
            #                 loop.run_in_executor(None, Settings.embed_model.get_text_embedding, effective_query),
            #                 timeout=5.0
            #             ),
            #             dtype=np.float32
            #         )
            #         conn = get_db_conn()
            #         try:
            #             register_vector(conn)
            #             cur = conn.cursor()
            #             cur.execute(
            #                 """
            #                 SELECT response_text,
            #                        1 - (embedding <=> %s::vector) AS similarity
            #                 FROM semantic_cache
            #                 WHERE embedding <=> %s::vector < 0.15
            #                   AND %s = ANY(allowed_roles)
            #                 ORDER BY embedding <=> %s::vector
            #                 LIMIT 1
            #                 """,
            #                 (query_embedding[:CACHE_DIM], query_embedding[:CACHE_DIM], verified_role, query_embedding[:CACHE_DIM])
            #             )
            #             row = cur.fetchone()
            #             cur.close()
            #         finally:
            #             release_db_conn(conn)
            #         if row and row[1] > 0.92:
            #             cached_response = row[0]
            #             yield "⚡ *(Cached Response)*\n\n"
            #             for i in range(0, len(cached_response), 50):
            #                 yield cached_response[i:i+50]
            #                 await asyncio.sleep(0.01)
            #             return
            #     else:
            #         logger.info("semantic_cache_skipped", reason="embed_server_unreachable")
            # except Exception as cache_e:
            #     logger.error("semantic_cache_lookup_failed", error=str(cache_e))
            # --- END SEMANTIC CACHE CHECK (disabled) ---

            Settings.llm.temperature = max(0.1, request.temperature)  # Prevent infinite loops from T=0

            # ── Framework Routing: LlamaIndex vs LangGraph ─────────
            if request.framework == "llamaindex":
                # --- LLAMAINDEX REACT AGENT PATH ---
                llama_history = [
                    ChatMessage(role=MessageRole.USER if msg.role == "user" else MessageRole.ASSISTANT, content=msg.content)
                    for msg in request.chat_history[-20:]
                ]
                
                hybrid_engine = get_hybrid_rrf_engine(request.similarity_top_k, request.rerank_top_n, verified_role)
                sql_engine = get_sql_engine()
                graph_engine = get_graph_engine()
                
                tools_list = [
                    # sql_engine,  # Disabled for grounding validation
                    QueryEngineTool(
                        query_engine=hybrid_engine,
                        metadata=ToolMetadata(name="unstructured_pdf_docs", description="Searches OpenSearch for unstructured documents, manuals, CVs, policies.")
                    ),
                    QueryEngineTool(
                        query_engine=graph_engine,
                        metadata=ToolMetadata(name="knowledge_graph", description="Searches Neo4j for entity relationships, obligations, and corporate structures.")
                    )
                ]
                
                if request.image_paths:
                    vision_engine = get_vision_engine(request.image_paths)
                    tools_list.append(
                        QueryEngineTool(
                            query_engine=vision_engine,
                            metadata=ToolMetadata(name="vision_analysis", description="Analyzes an uploaded image, chart, or graph.")
                        )
                    )
                
                agent = ReActAgent.from_tools(
                    tools=tools_list,
                    llm=Settings.llm,
                    chat_history=llama_history,
                    context=CORPORATE_AGENT_CONTEXT,
                    verbose=True
                )
                
                res = await agent.astream_chat(request.query)
                full_response = ""
                async for text_chunk in res.async_response_gen():
                    full_response += text_chunk
                    yield text_chunk
                
                # Fallback safety if streaming failed
                if not full_response:
                    full_response = str(res)
                    yield full_response
                return

            # ── LangGraph Orchestration (Universal — ReAct deprecated) ─────────
            # Sliding window for chat history (last 20 messages)
            MAX_HISTORY = 20
            trimmed_history = request.chat_history[-MAX_HISTORY:] if len(request.chat_history) > MAX_HISTORY else request.chat_history

            langchain_history = [
                HumanMessage(content=msg.content) if msg.role == "user" else AIMessage(content=msg.content)
                for msg in trimmed_history
            ]
            langchain_history.append(HumanMessage(content=request.query))

            graph = build_langgraph_workflow(
                request.similarity_top_k, request.rerank_top_n,
                verified_role, request.temperature, request.output_thinking, request.image_paths,
                request.doc_type, request.tenant_id
            )

            # Each session gets a unique thread_id so the MemorySaver checkpointer
            # keeps individual conversation histories properly isolated.
            thread_id = request.session_id or "default_session"
            config = {"configurable": {"thread_id": thread_id}}

            if not gpu_breaker.can_execute():
                yield "⚠️ GPU inference service is temporarily unavailable (circuit breaker open). Please try again in 60 seconds."
                return

            # ── vLLM queue saturation guard ───────────────────────────────────
            # Reject immediately when vLLM already has many requests waiting,
            # rather than silently queuing behind them and timing out in 5+ min.
            _VLLM_MAX_WAITING = int(os.getenv("VLLM_MAX_WAITING", "250"))
            try:
                import httpx as _httpx
                _metrics_url = VLLM_URL.replace("/v1", "/metrics")
                _mres = _httpx.get(_metrics_url, timeout=2.0)
                for _line in _mres.text.splitlines():
                    if "vllm:num_requests_waiting" in _line and not _line.startswith("#"):
                        _waiting = float(_line.split()[-1])
                        if _waiting > _VLLM_MAX_WAITING:
                            yield (
                                f"⏳ The AI inference server is currently busy "
                                f"({int(_waiting)} requests queued). "
                                f"Please try again in a moment."
                            )
                            return
                        break
            except Exception:
                pass  # If metrics unreachable, proceed normally

            # Async graph execution with keepalive —
            # Run the LangGraph in a background task. Capture final node
            # outputs (not individual tokens) to avoid leaking internal
            # Supervisor/Evaluator monologue. Send keepalive whitespace
            # every 3s to keep the connection alive.
            full_response = ""
            response = None
            token_queue: asyncio.Queue = asyncio.Queue()
            _SENTINEL = object()  # marks end of stream

            async def _run_graph():
                """Background task: execute graph, push node results into queue."""
                try:
                    async for event in graph.astream_events(
                        {"messages": langchain_history}, config, version="v2"
                    ):
                        kind = event.get("event")
                        node_name = event.get("metadata", {}).get("langgraph_node", "")

                        # Token-by-token streaming for primary generation agents
                        if kind == "on_chat_model_stream" and node_name not in ["Supervisor", "Evaluation_Agent"]:
                            token = event.get("data", {}).get("chunk", "")
                            if hasattr(token, "content") and token.content:
                                await token_queue.put(token.content)

                        # Capture full Parallel_Branch merged output (doesn't stream tokens natively)
                        if kind == "on_chain_end":
                            if node_name == "Parallel_Branch":
                                output = event.get("data", {}).get("output", {})
                                msgs = output.get("messages", []) if isinstance(output, dict) else []
                                for msg in msgs:
                                    content = str(msg.content).strip() if hasattr(msg, "content") else ""
                                    if content and len(content) > 30:
                                        await token_queue.put(content)
                except Exception as exc:
                    await token_queue.put(exc)
                finally:
                    await token_queue.put(_SENTINEL)

            graph_task = asyncio.create_task(_run_graph())
            stream_filter = StreamFilter()

            # Pull results or send keepalive whitespace
            breaker_tripped = False
            timed_out = False
            start_time = time.time()
            while True:
                if time.time() - start_time > 1800:
                    graph_task.cancel()
                    yield "\n\n⚠️ *(System: Query timed out after 30 minutes)*"
                    timed_out = True
                    break
                try:
                    item = await asyncio.wait_for(token_queue.get(), timeout=3.0)
                except asyncio.TimeoutError:
                    yield " "  # keepalive
                    continue
                if item is _SENTINEL:
                    break
                if isinstance(item, Exception):
                    gpu_breaker.record_failure()
                    breaker_tripped = True
                    raise item
                
                filtered_item = stream_filter.process_token(item)
                if filtered_item:
                    full_response += filtered_item
                    yield filtered_item

            trail = stream_filter.flush()
            if trail:
                full_response += trail
                yield trail

            if not breaker_tripped and full_response:
                gpu_breaker.record_success()

            # After the graph completes, clean up evaluator verdicts
            clean_resp = re.sub(r"<think>.*?</think>", "", full_response, flags=re.DOTALL).strip()
            if clean_resp.upper().startswith("YES") or clean_resp.upper().startswith("NO -"):
                full_response = ""

            # Fallback: if streaming yielded nothing useful, walk backwards through
            # the graph state to find the last substantive AI response.
            if not timed_out and not full_response.strip():
                print(f"DEBUG FALLBACK: full_response is empty, entering fallback...")
                snapshot = graph.get_state(config)
                msgs = snapshot.values.get("messages", [])
                print(f"DEBUG FALLBACK: {len(msgs)} messages in graph state")
                fallback_resp = ""
                # Walk backwards looking for a substantial AI or merged Human message
                for idx, msg in enumerate(reversed(msgs)):
                    content = str(msg.content).strip()
                    msg_type = type(msg).__name__
                    # Strip any <think>...</think> blocks for classification
                    clean = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
                    print(f"DEBUG FALLBACK [{idx}] {msg_type}: clean_starts={clean[:50]!r}... len={len(clean)}")
                    # Skip evaluator verdicts (YES/NO with any length)
                    if clean.upper().startswith("YES"):
                        print(f"  → SKIPPED (starts with YES)")
                        continue
                    if clean.upper().startswith("NO -") or clean.upper().startswith("NO\n"):
                        print(f"  → SKIPPED (starts with NO)")
                        continue
                    if content.startswith("CRITICAL FEEDBACK FROM SUPERVISOR"):
                        print(f"  → SKIPPED (supervisor feedback)")
                        continue
                    # Skip short routing labels from Supervisor
                    if len(clean) < 20 and any(label in clean for label in ["Document_Agent", "SQL_Agent", "Vision_Agent", "Graph_Agent", "Parallel_Branch"]):
                        print(f"  → SKIPPED (routing label)")
                        continue
                    # Skip the original user query
                    if msg_type in ["HumanMessage", "HumanMessageChunk"]:
                        print(f"  → SKIPPED (HumanMessage)")
                        continue
                    if len(clean) < 50 and clean.lower() in ["hello", "hello, what can you do?", "what can you do?", "say hello"]:
                        print(f"  → SKIPPED (user query)")
                        continue
                    # Found a substantive message (merged results or agent output)
                    if len(clean) > 30:
                        fallback_resp = content
                        print(f"  → SELECTED as fallback! length={len(content)}")
                        break
                    print(f"  → SKIPPED (too short: {len(clean)} chars)")
                if fallback_resp:
                    for i in range(0, len(fallback_resp), 30):
                        yield fallback_resp[i:i+30]
                        await asyncio.sleep(0.005)
                    full_response = fallback_resp
                else:
                    print("DEBUG FALLBACK: No suitable fallback found!")

            # --- EXTRACT CITATIONS (Structured JSON for Chainlit cl.Pdf) ---
            sources = []
            
            # Scrape graph state ToolMessages for injected CITATIONS_JSON blocks
            snapshot = graph.get_state(config)
            for msg in snapshot.values.get("messages", []):
                msg_content = str(getattr(msg, "content", ""))
                if "<!--CITATIONS_JSON:" in msg_content:
                    match = re.search(r"<!--CITATIONS_JSON:(.*?)-->", msg_content)
                    if match:
                        try:
                            sources.extend(json.loads(match.group(1)))
                        except Exception:
                            pass
            
            # Deduplicate sources based on filename and page
            unique_sources = []
            seen = set()
            for s in sources:
                key = f"{s.get('filename')}:{s.get('page')}"
                if key not in seen:
                    seen.add(key)
                    unique_sources.append(s)
            sources = unique_sources

            if sources:
                # Emit a machine-parseable JSON block that app.py reads;
                # NOT added to full_response so the cache stays text-only.
                citation_block = f"\n<!--CITATIONS_JSON:{json.dumps(sources)}-->"
                yield citation_block
            # -------------------------
                
            # --- SAVE TO CACHE (disabled) ---
            # if query_embedding is not None and full_response:
            #     try:
            #         conn = get_db_conn()
            #         try:
            #             register_vector(conn)
            #             conn.autocommit = True
            #             if sources:
            #                 intersecting_roles = set(sources[0].get("allowed_roles", [verified_role]))
            #                 for s in sources[1:]:
            #                     intersecting_roles.intersection_update(s.get("allowed_roles", [verified_role]))
            #                 allowed_roles = list(intersecting_roles)
            #                 if not allowed_roles:
            #                     allowed_roles = [verified_role]
            #             else:
            #                 allowed_roles = [verified_role]
            #             cur.execute(
            #                 "INSERT INTO semantic_cache (query_text, response_text, embedding, allowed_roles) VALUES (%s, %s, %s, %s)",
            #                 (request.query, full_response, query_embedding[:CACHE_DIM], allowed_roles)
            #             )
            #             cur.close()
            #             print(f"✅ Cache: Saved embedding for query '{request.query[:60]}...' with ACL {allowed_roles}")
            #         finally:
            #             conn.autocommit = False
            #             release_db_conn(conn)
            #     except Exception as e:
            #         print(f"Cache Save Error: {e}")
            # --- END SAVE TO CACHE (disabled) ---

            # --- SAVE TO CHAT MESSAGES ---
            if request.session_id and full_response:
                try:
                    conn = get_db_conn()
                    try:
                        cur = conn.cursor()
                        cur.execute(
                            "INSERT INTO chat_messages (session_id, role, content, sources_json) VALUES (%s, %s, %s, %s)",
                            (request.session_id, "assistant", full_response, json.dumps(sources) if sources else None)
                        )
                        conn.commit()
                        cur.close()
                    finally:
                        release_db_conn(conn)
                except Exception as e:
                    print(f"🚨 Failed to save assistant message: {e}")
            # --- END CHAT MESSAGES ---
                
        except Exception as e:
            error_msg = f"\n\n🚨 **Backend Error:** `{str(e)}`\n"
            print(error_msg)
            import traceback
            traceback.print_exc()
            yield error_msg

    try:
        return StreamingResponse(event_generator(), media_type="text/plain")
    except Exception as e:
        logger.error("streaming_response_error", error=str(e))
        raise e
        return StreamingResponse(event_generator(), media_type="text/plain")
    except Exception as e:
        logger.error("streaming_response_error", error=str(e))
        raise e
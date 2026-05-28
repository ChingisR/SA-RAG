# PROJECT: Agentic RAG (Stabilized v1.5)

## FILE: main.py
`python
import os
import shutil
import psycopg2
import nest_asyncio
from typing import List, Optional
from fastapi import FastAPI, UploadFile, File, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from fastapi.responses import StreamingResponse
import jwt
from datetime import datetime, timedelta
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

# Critical: Allows LlamaIndex's internal async loops to run safely inside FastAPI
nest_asyncio.apply()

# --- BCRYPT MONKEY PATCH ---
import passlib.handlers.bcrypt
import bcrypt

def _check_hash(secret, hash):
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    if isinstance(hash, str):
        hash = hash.encode("utf-8")
    return bcrypt.checkpw(secret, hash)

passlib.handlers.bcrypt.bcrypt._check_hash = _check_hash
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
from llama_index.graph_stores.neo4j import Neo4jGraphStore
from llama_index.core.retrievers import BaseRetriever
from llama_index.core.schema import NodeWithScore, TextNode, QueryBundle
from llama_index.core.query_engine import RetrieverQueryEngine
from llama_index.core.response_synthesizers import get_response_synthesizer
from pgvector.psycopg2 import register_vector
from worker import process_document_task
import numpy as np
import requests
from urllib3.exceptions import InsecureRequestWarning
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

from openinference.instrumentation.llama_index import LlamaIndexInstrumentor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

try:
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(os.getenv("PHOENIX_COLLECTOR_ENDPOINT", "http://phoenix:4318/v1/traces"))))
    LlamaIndexInstrumentor().instrument(tracer_provider=tracer_provider)
except Exception as e:
    print(f"⚠️ Telemetry setup failed: {e}")

app = FastAPI(title="Agentic RAG — vLLM Edition")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
def health_check():
    return {"status": "ok", "service": "fastapi-agentic-rag"}

# --- AUTHENTICATION SETUP ---
SECRET_KEY = os.getenv("JWT_SECRET", "super-secret-key-for-legal-hr-2026")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

LEGAL_HR_AGENT_CONTEXT = (
    "You are a specialized Legal and HR AI Assistant. "
    "When a user asks a follow-up question, you MUST resolve all pronouns (he, she, they, it, his, her, etc.) "
    "to the specific person or entity mentioned in the earlier conversation history. "
    "ALWAYS formulate a complete, standalone query when calling your search tools. "
    "CRITICAL TOOL ROUTING RULE: "
    "1. The 'structured_hr_database' contains ONLY numeric and categorical records (salary, department, hire date). "
    "It DOES NOT contain professional history or experience. "
    "2. If you are asked about a person's 'experience', 'background', 'achievements', 'bio', or 'career history', "
    "you MUST use the 'unstructured_pdf_docs' tool or 'knowledge_graph'. "
    "DO NOT rely solely on the SQL tool for these topics."
)


# The frontend uses /api/login, which is stripped by Nginx proxy_pass to /login
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")

def get_current_user(token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        role: str = payload.get("role")
        if email is None:
            raise credentials_exception
        return {"email": email, "role": role}
    except jwt.PyJWTError:
        raise credentials_exception

class LoginRequest(BaseModel):
    email: str
    password: str

@app.post("/login")
async def login(req: LoginRequest):
    try:
        conn = psycopg2.connect(PG_DSN)
        cur = conn.cursor()
        cur.execute("SELECT password_hash, role FROM users WHERE email = %s", (req.email,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        
        if row and pwd_context.verify(req.password, row[0]):
            access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
            expire = datetime.utcnow() + access_token_expires
            to_encode = {"sub": req.email, "role": row[1], "exp": expire}
            encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
            return {"access_token": encoded_jwt, "token_type": "bearer", "user": {"email": req.email, "role": row[1]}}
    except Exception as e:
        print(f"Login error: {e}")
        
    raise HTTPException(status_code=401, detail="Incorrect email or password")
# ----------------------------

from fastapi.responses import FileResponse

@app.get("/files/{filename}")
async def serve_file(filename: str):
    """Serve uploaded PDF files so Chainlit can display them with cl.Pdf."""
    file_path = f"data/{filename}"
    if os.path.exists(file_path):
        return FileResponse(file_path, media_type="application/pdf")
    from fastapi import HTTPException
    raise HTTPException(status_code=404, detail=f"File '{filename}' not found.")

SGLANG_URL       = os.getenv("SGLANG_BASE_URL", "http://sglang:8000/v1")
SGLANG_EMBED_URL = os.getenv("SGLANG_EMBED_URL", "http://vllm-embed:8000/v1")
PG_DSN           = os.getenv("POSTGRES_URL", "postgresql://admin:secret@postgres:5432/universal_rag_db")
MAIN_MODEL       = os.getenv("MAIN_MODEL", "Qwen/Qwen2.5-VL-7B-Instruct-AWQ")
EMBED_MODEL      = os.getenv("EMBED_MODEL", "nomic-ai/nomic-embed-text-v1.5")

# 1. Embedding model — served by sglang-embed, OpenAI /v1/embeddings compatible
Settings.embed_model = OpenAIEmbedding(
    model_name=EMBED_MODEL,
    api_base=SGLANG_EMBED_URL,
    api_key="placeholder",
    timeout=120.0,
)

# 2. Main LLM — served by sglang with RadixAttention, OpenAI-compatible
Settings.llm = OpenAILike(
    model=MAIN_MODEL,
    api_base=SGLANG_URL,
    api_key="placeholder",
    is_chat_model=True,
    is_function_calling_model=True,
    max_tokens=4096,
    context_window=16384,
    timeout=900.0,
)

# 3. MultiModal LLM — Qwen2.5-VL vision via chat completions (same sglang endpoint)
mm_llm = OpenAILike(
    model=MAIN_MODEL,
    api_base=SGLANG_URL,
    api_key="placeholder",
    is_chat_model=True,
    is_function_calling_model=False,
    max_tokens=2048,
    timeout=900.0,
)

os.makedirs("data", exist_ok=True)
os.makedirs("images", exist_ok=True)

def init_db():
    try:
        conn = psycopg2.connect(PG_DSN)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        # --- FORCE RECREATION of Cache Table if Mismatched ---
        cur.execute("DROP TABLE IF EXISTS semantic_cache;")
        cur.execute("CREATE TABLE IF NOT EXISTS semantic_cache (id SERIAL PRIMARY KEY, query_text TEXT, response_text TEXT, embedding vector(768));")
        cur.execute("CREATE TABLE IF NOT EXISTS hr_employees (employee_id SERIAL PRIMARY KEY, name VARCHAR(100), department VARCHAR(50), role VARCHAR(50), salary INTEGER, hire_date DATE);")
        
        cur.execute("SELECT COUNT(*) FROM hr_employees;")
        if cur.fetchone()[0] == 0:
            cur.execute("""
                INSERT INTO hr_employees (name, department, role, salary, hire_date) VALUES
                ('Chingis Rustemov', 'Engineering', 'AI Tech Lead', 150000, '2023-01-15'),
                ('Anna', 'HR', 'HR Director', 110000, '2021-05-20'),
                ('Boris', 'Engineering', 'DevOps Engineer', 95000, '2024-02-10'),
                ('Dinara', 'Finance', 'Financial Analyst', 85000, '2022-08-01');
            """)
        cur.close()
        conn.close()
    except Exception as e:
        print(f"🚨 DB Init Error: {e}")

@app.on_event("startup")
async def startup_event():
    init_db()


@app.post("/upload-pdf")
async def upload_pdf(file: UploadFile = File(...), sync: bool = Query(False), current_user: dict = Depends(get_current_user)):
    file_location = f"data/{file.filename}"
    with open(file_location, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    if sync:
        process_document_task(file_location, file.filename)
        return {"status": "success", "message": f"Processed {file.filename} synchronously."}
    else:
        process_document_task.delay(file_location, file.filename)
        return {"status": "success", "message": f"File {file.filename} sent to background."}

@app.post("/upload-image")
async def upload_image(file: UploadFile = File(...), current_user: dict = Depends(get_current_user)):
    file_location = f"images/{file.filename}"
    with open(file_location, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    return {"status": "success", "image_path": file_location}

class MessageDict(BaseModel):
    role: str
    content: str

class QueryRequest(BaseModel):
    query: str
    chat_history: List[MessageDict] = []
    image_paths: Optional[List[str]] = []
    similarity_top_k: int = 20
    rerank_top_n: int = 5 
    temperature: float = 0.1
    user_role: str = "HR_Admin"

def get_reranker(top_n: int):
    # This natively uses qwen3.5:27b to score chunks! No extra containers needed.
    return LLMRerank(choice_batch_size=5, top_n=top_n, llm=Settings.llm)

from llama_index.core.tools import FunctionTool
import urllib.request
import urllib.parse
import json

def web_search_fallback(query: str) -> str:
    """CRITICAL: Use this tool to search the public internet or current events ONLY when the internal HR/Legal database does not have the answer."""
    try:
        url = "https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch=" + urllib.parse.quote(query) + "&utf8=&format=json"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 Enterprise-Agent/1.0'})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode())
            results = data.get('query', {}).get('search', [])
            if results:
                snippet = results[0]['snippet'].replace('<span class="searchmatch">', '').replace('</span>', '')
                return f"Public Web Result for '{query}': {snippet}"
            return "No external web results found."
    except Exception as e:
        return f"Web search failed: {e}"

web_tool = FunctionTool.from_defaults(fn=web_search_fallback, name="public_web_search")

# ──────────────────────────────────────────────────────────────────────────
# HYBRID SEARCH: BM25 + Dense Vectors with Reciprocal Rank Fusion (RRF)
# ──────────────────────────────────────────────────────────────────────────
class HybridRRFRetriever(BaseRetriever):
    """
    Runs BM25 (keyword) and Dense Vector (semantic) searches independently
    against OpenSearch, then merges the ranked lists using the RRF algorithm.
    RRF Score = sum(1 / (k + rank_i)) for each source list.
    """
    def __init__(self, endpoint: str, index: str, auth: tuple,
                 top_k: int, user_role: str, rrf_k: int = 60):
        self._endpoint = endpoint.rstrip("/")
        self._index = index
        self._auth = auth
        self._top_k = top_k
        self._user_role = user_role
        self._rrf_k = rrf_k
        super().__init__()

    def _dense_search(self, query: str) -> list[dict]:
        """Execute kNN / Dense Vector search against OpenSearch."""
        # --- NOMIC EMBED PREFIX: Required for nomic-embed-text-v1.5 ---
        effective_query = f"search_query: {query}" if "nomic-embed-text" in os.getenv("EMBED_MODEL", "") else query
        embedding = Settings.embed_model.get_text_embedding(effective_query)
        body = {
            "size": self._top_k,
            "query": {
                "bool": {
                    "must": [
                        {"knn": {"embedding": {"vector": embedding, "k": self._top_k}}}
                    ],
                    "filter": [{"term": {"metadata.allowed_roles.keyword": self._user_role}}]
                }
            },
            "_source": True
        }
        resp = requests.post(
            f"{self._endpoint}/{self._index}/_search",
            json=body, auth=self._auth, verify=False, timeout=30
        )
        return resp.json().get("hits", {}).get("hits", [])

    def _bm25_search(self, query: str) -> list[dict]:
        """Execute BM25 keyword search against OpenSearch."""
        body = {
            "size": self._top_k,
            "query": {
                "bool": {
                    "must": [{"match": {"content": {"query": query}}}],
                    "filter": [{"term": {"metadata.allowed_roles.keyword": self._user_role}}]
                }
            },
            "_source": True
        }
        resp = requests.post(
            f"{self._endpoint}/{self._index}/_search",
            json=body, auth=self._auth, verify=False, timeout=30
        )
        return resp.json().get("hits", {}).get("hits", [])

    @staticmethod
    def _rrf_fuse(lists: list[list[dict]], k: int = 60) -> list[tuple[str, float, dict]]:
        """Fuse multiple ranked lists using Reciprocal Rank Fusion."""
        scores: dict[str, float] = {}
        docs: dict[str, dict] = {}
        for ranked_list in lists:
            for rank, hit in enumerate(ranked_list, start=1):
                doc_id = hit["_id"]
                scores[doc_id] = scores.get(doc_id, 0.0) + (1.0 / (k + rank))
                docs[doc_id] = hit
        fused = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return [(doc_id, rrf_score, docs[doc_id]) for doc_id, rrf_score in fused]

    def _retrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
        query = query_bundle.query_str
        dense_hits = self._dense_search(query)
        bm25_hits  = self._bm25_search(query)
        fused      = self._rrf_fuse([dense_hits, bm25_hits], k=self._rrf_k)

        nodes_with_scores: list[NodeWithScore] = []
        for doc_id, rrf_score, hit in fused[:self._top_k]:
            src   = hit.get("_source", {})
            text  = src.get("content", src.get("text", ""))
            meta  = src.get("metadata", {})
            node  = TextNode(text=text, id_=doc_id, metadata=meta)
            nodes_with_scores.append(NodeWithScore(node=node, score=rrf_score))
        return nodes_with_scores

def get_hybrid_rrf_engine(top_k: int, top_n: int, user_role: str):
    """Factory: returns a RetrieverQueryEngine backed by HybridRRFRetriever + LLMRerank."""
    os_url  = os.getenv("OPENSEARCH_URL", "https://opensearch:9200")
    os_user = os.getenv("OPENSEARCH_USER", "admin")
    os_pass = os.getenv("OPENSEARCH_PASSWORD", "LegalAI_2026!")
    retriever = HybridRRFRetriever(
        endpoint=os_url, index="universal_docs_v1",
        auth=(os_user, os_pass), top_k=top_k, user_role=user_role
    )
    return RetrieverQueryEngine.from_args(
        retriever=retriever,
        node_postprocessors=[],
        response_synthesizer=get_response_synthesizer(llm=Settings.llm)
    )


def get_sql_engine():
    engine = create_engine(PG_DSN)
    sql_database = SQLDatabase(engine, include_tables=["hr_employees"])
    return NLSQLTableQueryEngine(sql_database=sql_database, tables=["hr_employees"], llm=Settings.llm)

def get_graph_engine():
    graph_store = Neo4jGraphStore(username=os.getenv("NEO4J_USER"), password=os.getenv("NEO4J_PASSWORD"), url=os.getenv("NEO4J_URI"))
    index = PropertyGraphIndex.from_existing(property_graph_store=graph_store, llm=Settings.llm)
    return index.as_query_engine()

def get_vision_engine(image_paths: List[str]):
    image_documents = SimpleDirectoryReader(input_files=image_paths).load_data()
    index = VectorStoreIndex.from_documents(image_documents)
    return index.as_query_engine(llm=mm_llm)

@app.post("/query")
async def query_index(request: QueryRequest, current_user: dict = Depends(get_current_user)):
    async def event_generator():
        query_embedding = None
        # Inherit trust relationship from the verified JWT claim
        verified_role = current_user.get("role", "HR_Admin")
        try:
            # --- SEMANTIC CACHE CHECK ---
            # query_embedding is hoisted here so the save-to-cache block can
            # always access it, even if the cache lookup below throws.
            query_embedding = None
            try:
                # --- NOMIC EMBED PREFIX: Required for nomic-embed-text-v1.5 ---
                effective_query = f"search_query: {request.query}" if "nomic-embed-text" in os.getenv("EMBED_MODEL", "") else request.query
                query_embedding = np.array(
                    Settings.embed_model.get_text_embedding(effective_query),
                    dtype=np.float32
                )
                conn = psycopg2.connect(PG_DSN)
                register_vector(conn)          # REQUIRED: registers pgvector type on this connection
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT response_text,
                           1 - (embedding <=> %s::vector) AS similarity
                    FROM semantic_cache
                    ORDER BY embedding <=> %s::vector
                    LIMIT 1
                    """,
                    (query_embedding, query_embedding)
                )
                row = cur.fetchone()
                cur.close()
                conn.close()

                if row and row[1] > 0.95:
                    cached_response = row[0]
                    yield "⚡ *(Cached Response)*\n\n"
                    import asyncio
                    for i in range(0, len(cached_response), 50):
                        yield cached_response[i:i+50]
                        await asyncio.sleep(0.01)
                    return
            except Exception as cache_e:
                print(f"Cache Lookup Error: {cache_e}")
            # --- END SEMANTIC CACHE CHECK ---

            Settings.llm.temperature = request.temperature
            llama_history = [ChatMessage(role=MessageRole.USER if msg.role == "user" else MessageRole.ASSISTANT, content=msg.content) for msg in request.chat_history]

            os_tool = QueryEngineTool(
                query_engine=get_hybrid_rrf_engine(request.similarity_top_k, request.rerank_top_n, verified_role),
                metadata=ToolMetadata(name="unstructured_pdf_docs", description="Use for detailed person profiles, candidate CVs, legal policies, contracts, and HR documents.")
            )

            
            sql_tool = QueryEngineTool(
                query_engine=get_sql_engine(),
                metadata=ToolMetadata(name="structured_hr_database", description="Use for searching employees by name, department, salary, or hire date.")
            )
            
            tools_list = [os_tool, sql_tool, web_tool]

            if request.image_paths:
                vision_tool = QueryEngineTool(
                    query_engine=get_vision_engine(request.image_paths),
                    metadata=ToolMetadata(name="vision_analysis", description="CRITICAL: Use this tool ONLY when the user asks about an uploaded image, chart, or visual diagram.")
                )
                tools_list.append(vision_tool)

            try:
                graph_tool = QueryEngineTool(
                    query_engine=get_graph_engine(),
                    metadata=ToolMetadata(name="knowledge_graph", description="Use when asked about relationships.")
                )
                tools_list.append(graph_tool)
            except Exception:
                pass

            try:
                sub_query_engine = SubQuestionQueryEngine.from_defaults(query_engine_tools=[os_tool], llm=Settings.llm, verbose=True)
                sub_query_tool = QueryEngineTool(
                    query_engine=sub_query_engine,
                    metadata=ToolMetadata(name="comparative_analysis", description="Use when comparing documents.")
                )
                tools_list.append(sub_query_tool)
            except Exception:
                pass

            agent = ReActAgent.from_tools(
                tools=tools_list,
                llm=Settings.llm,
                chat_history=llama_history,
                context=LEGAL_HR_AGENT_CONTEXT,
                verbose=True
            )


            response = await agent.astream_chat(request.query)
            full_response = ""
            
            async_gen = response.async_response_gen() if callable(getattr(response, "async_response_gen", None)) else response.async_response_gen
            
            async for text_chunk in async_gen:
                full_response += text_chunk
                yield text_chunk
                
            # --- EXTRACT CITATIONS (Structured JSON for Chainlit cl.Pdf) ---
            import json as _json
            sources = []
            if hasattr(response, "source_nodes") and response.source_nodes:
                seen = set()
                for node in response.source_nodes:
                    filename = node.metadata.get("filename", node.metadata.get("file_name", ""))
                    page     = node.metadata.get("page_label", node.metadata.get("page_number", "1"))
                    snippet  = (node.text or "")[:300].strip().replace("\n", " ")
                    key = f"{filename}:{page}"
                    if key not in seen and filename:
                        seen.add(key)
                        sources.append({
                            "filename": filename,
                            "page":     str(page),
                            "snippet":  snippet,
                        })

            if sources:
                # Emit a machine-parseable JSON block that app.py reads;
                # NOT added to full_response so the cache stays text-only.
                citation_block = f"\n<!--CITATIONS_JSON:{_json.dumps(sources)}-->"
                yield citation_block
            # -------------------------
                
            # --- SAVE TO CACHE ---
            if query_embedding is not None and full_response:
                try:
                    conn = psycopg2.connect(PG_DSN)
                    register_vector(conn)      # REQUIRED: registers pgvector type on this connection
                    conn.autocommit = True
                    cur = conn.cursor()
                    cur.execute(
                        "INSERT INTO semantic_cache (query_text, response_text, embedding) VALUES (%s, %s, %s)",
                        (request.query, full_response, query_embedding)
                    )
                    cur.close()
                    conn.close()
                    print(f"✅ Cache: Saved embedding for query '{request.query[:60]}...'")
                except Exception as e:
                    print(f"Cache Save Error: {e}")
            # --- END SAVE TO CACHE ---
                
        except Exception as e:
            error_msg = f"\n\n🚨 **Backend Error:** `{str(e)}`\n"
            print(error_msg)
            yield error_msg

    return StreamingResponse(event_generator(), media_type="text/plain")
`

## FILE: worker.py
`python
import os
import nest_asyncio
from celery import Celery

# Critical: Allows LlamaIndex's internal async loops to run safely inside threads
nest_asyncio.apply()

# --- BCRYPT MONKEY PATCH ---
import passlib.handlers.bcrypt
import bcrypt
import requests
import time

def _check_hash(secret, hash):
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    if isinstance(hash, str):
        hash = hash.encode("utf-8")
    return bcrypt.checkpw(secret, hash)

passlib.handlers.bcrypt.bcrypt._check_hash = _check_hash
# ---------------------------

from llama_index.core import VectorStoreIndex, StorageContext, Settings, PropertyGraphIndex
from llama_index.core.node_parser import SemanticSplitterNodeParser
from llama_index.llms.openai_like import OpenAILike
from llama_index.embeddings.openai import OpenAIEmbedding
from llama_index.vector_stores.opensearch import OpensearchVectorClient, OpensearchVectorStore
from llama_index.graph_stores.neo4j import Neo4jGraphStore
from llama_index.readers.file import PyMuPDFReader

VALKEY_URL = os.getenv("VALKEY_URL", "redis://valkey:6379/0")
celery_app = Celery("rag_tasks", broker=VALKEY_URL)

OLLAMA_URL       = os.getenv("OLLAMA_URL", "http://ollama:11434")
SGLANG_URL       = os.getenv("SGLANG_BASE_URL", "http://sglang:8000/v1")
SGLANG_EMBED_URL = os.getenv("SGLANG_EMBED_URL", "http://vllm-embed:8000/v1")
MAIN_MODEL       = os.getenv("MAIN_MODEL", "Qwen/Qwen2.5-VL-7B-Instruct-AWQ")
EMBED_MODEL      = os.getenv("EMBED_MODEL", "nomic-ai/nomic-embed-text-v1.5")

Settings.embed_model = OpenAIEmbedding(
    model_name=EMBED_MODEL,
    api_base=SGLANG_EMBED_URL,
    api_key="placeholder",
    timeout=120.0,
)

Settings.llm = OpenAILike(
    model=MAIN_MODEL,
    api_base=SGLANG_URL,
    api_key="placeholder",
    is_chat_model=True,
    is_function_calling_model=True,
    max_tokens=4096,
    context_window=16384,
    timeout=900.0,
)

@celery_app.task
def process_document_task(file_path: str, filename: str):
    print(f"--- 🚀 MULTIMODAL INGESTION: Started processing {filename} ---")
    
    # --- SGLANG / vLLM READINESS CHECK ---
    max_retries = 24 # 4 minutes total
    for i in range(max_retries):
        try:
            # Test the connection by doing a simple completion
            Settings.llm.complete("ping")
            break
        except Exception:
            if i == max_retries - 1:
                print("❌ SGLANG/vLLM not ready after 4 minutes. Proceeding anyway...")
            else:
                print(f"⏳ Waiting for SGLANG/vLLM warmup (attempt {i+1}/{max_retries})...")
                time.sleep(10)

    try:
        documents = PyMuPDFReader().load(file_path)
        
        # --- NOMIC EMBED PREFIX: Required for nomic-embed-text-v1.5 ---
        for doc in documents:
            if "nomic-embed-text" in EMBED_MODEL:
                doc.text = f"search_document: {doc.text}"
        
        # --- AUTOMATED METADATA ENRICHMENT ---
        summary = "No summary available."
        doc_type = "Corporate Document"
        try:
            sample_text = documents[0].text[:3000] if documents else ""
            if sample_text:
                prompt = f"Analyze this text and provide a 1-sentence summary and the likely document type (e.g., NDA, Policy, Contract). Text: {sample_text} \n\nFormat: Summary: <summary> | Type: <type>"
                response = Settings.llm.complete(prompt)
                parts = str(response).split("|")
                summary = parts[0].replace("Summary:", "").strip() if len(parts) > 0 else str(response).strip()
                doc_type = parts[1].replace("Type:", "").strip() if len(parts) > 1 else doc_type
        except Exception as e:
            print(f"⚠️ LLM Metadata Extraction Failed: {e}")

        for doc in documents:
            doc.metadata = {
                "filename": filename, 
                "allowed_roles": ["HR_Admin", "Legal_Executive"],
                "document_summary": summary,
                "document_type": doc_type
            }

        # --- MULTI-MODAL IMAGE EXTRACTION ---
        try:
            from llama_index.core.schema import ImageDocument
            from llama_index.multi_modal_llms.ollama import OllamaMultiModal
            import fitz
            mm_llm = OllamaMultiModal(model=MAIN_MODEL, base_url=OLLAMA_URL, request_timeout=900.0)
            doc_pdf = fitz.open(file_path)
            extracted_image_texts = []
            for page_num in range(len(doc_pdf)):
                for img_index, img_info in enumerate(doc_pdf[page_num].get_images()):
                    try:
                        xref = img_info[0]
                        base_image = doc_pdf.extract_image(xref)
                        img_path = f"data/temp_{filename}_{page_num}_{img_index}.png"
                        with open(img_path, "wb") as f:
                            f.write(base_image["image"])
                        img_doc = ImageDocument(image_path=img_path)
                        res = mm_llm.complete(prompt="Extract any text, data, or describe this image in detail.", image_documents=[img_doc])
                        if res.text:
                            extracted_image_texts.append(f"Image on page {page_num}: {res.text}")
                        os.remove(img_path)
                    except Exception as e:
                        print(f"Image extraction error: {e}")
            
            if extracted_image_texts and documents:
                documents[0].text += "\n\n--- EXTRACTED VISUAL DATA ---\n" + "\n\n".join(extracted_image_texts)
        except Exception as e:
            print(f"Multi-modal extraction skipped: {e}")
        # ------------------------------------

        semantic_parser = SemanticSplitterNodeParser(buffer_size=1, breakpoint_percentile_threshold=95, embed_model=Settings.embed_model)
        nodes = semantic_parser.get_nodes_from_documents(documents)

        # --- OPENSEARCH RESET & INITIALIZATION ---
        os_url  = os.getenv("OPENSEARCH_URL", "https://opensearch:9200")
        os_user = os.getenv("OPENSEARCH_USER", "admin")
        os_pass = os.getenv("OPENSEARCH_PASSWORD", "LegalAI_2026!")
        
        # 1. Force delete existing index to clear 4096-dim or nmslib remnants
        requests.delete(f"{os_url}/universal_docs_v1", auth=(os_user, os_pass), verify=False, timeout=5)

        # 2. Manually create index with FAISS engine (Fixes nmslib deprecation in OS 3.0+)
        mapping = {
            "settings": {"index": {"knn": True}},
            "mappings": {
                "properties": {
                    "embedding": {
                        "type": "knn_vector",
                        "dimension": 768,
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
        requests.put(f"{os_url}/universal_docs_v1", json=mapping, auth=(os_user, os_pass), verify=False, timeout=5)
        print("✅ Re-initialized OpenSearch index 'universal_docs_v1' with FAISS engine")

        os_client = OpensearchVectorClient(
            endpoint=os_url,
            index="universal_docs_v1", dim=768, 
            http_auth=(os_user, os_pass), 
            use_ssl=True, verify_certs=False
        )
        vector_store = OpensearchVectorStore(client=os_client)
        storage_context = StorageContext.from_defaults(vector_store=vector_store)
        VectorStoreIndex(nodes, storage_context=storage_context)

        graph_store = Neo4jGraphStore(
            username=os.getenv("NEO4J_USER"), password=os.getenv("NEO4J_PASSWORD"), url=os.getenv("NEO4J_URI")
        )
        # Fix for 'Neo4jGraphStore' object has no attribute 'supports_vector_queries'
        graph_store.supports_vector_queries = False

        
        from llama_index.core.indices.property_graph import SchemaLLMPathExtractor
        from typing import Literal
        from pydantic import BaseModel, Field

        # ── Entity Types ──────────────────────────────────────────────────────────
        # Every node in the graph must be one of these Legal/HR domain concepts.
        EntityType = Literal[
            "Employee",         # A person employed by the organization
            "Manager",          # An employee with direct reports
            "Department",       # Organizational unit (e.g., Legal, HR, Finance)
            "Organization",     # A company or external legal entity
            "Role",             # A job title or function (e.g., AI Tech Lead)
            "Policy",           # An internal HR or compliance rule (e.g., leave policy)
            "Contract",         # A legally binding agreement (e.g., NDA, employment contract)
            "Clause",           # A specific numbered section inside a contract or policy
            "Obligation",       # A duty or requirement imposed by a clause or policy
            "Right",            # An entitlement granted to an employee or party
            "Jurisdiction",     # A governing law or legal territory (e.g., Kazakhstan law)
            "Date",             # A specific effective date, deadline, or expiry
            "Penalty",          # A financial or legal consequence for breach
            "Benefit",          # Compensation, bonus, or non-wage benefit
        ]

        # ── Relation Types ────────────────────────────────────────────────────────
        # Every edge in the graph must be one of these directed relations.
        RelationType = Literal[
            "WORKS_IN",         # Employee -> Department
            "REPORTS_TO",       # Employee -> Manager
            "HAS_ROLE",         # Employee -> Role
            "EMPLOYED_BY",      # Employee -> Organization
            "SIGNED",           # Employee/Organization -> Contract
            "PARTY_TO",         # Organization -> Contract
            "GOVERNED_BY",      # Contract/Policy -> Jurisdiction
            "CONTAINS",         # Contract/Policy -> Clause
            "IMPOSES",          # Clause -> Obligation/Penalty
            "GRANTS",           # Clause/Policy -> Right/Benefit
            "OVERRIDES",        # Policy -> Policy (newer supersedes older)
            "EFFECTIVE_FROM",   # Contract/Policy -> Date
            "EXPIRES_ON",       # Contract/Policy -> Date
            "APPLIES_TO",       # Policy/Clause -> Employee/Department/Role
            "SUBJECT_TO",       # Employee/Contract -> Obligation/Penalty
            "COMPENSATED_WITH", # Employee -> Benefit
            "AMENDED_BY",       # Contract -> Contract (amendment relationship)
            "REFERENCES",       # Clause -> Clause (cross-reference within documents)
        ]

        LEGAL_HR_EXTRACTION_PROMPT = (
            "You are a Legal and HR Knowledge Graph extraction specialist.\n"
            "Your task is to extract structured triples (subject, relation, object) "
            "from the provided text, focusing EXCLUSIVELY on the enterprise Legal and HR domain.\n\n"
            "STRICT RULES:\n"
            "- Only extract entities of these types: Employee, Manager, Department, Organization, "
            "Role, Policy, Contract, Clause, Obligation, Right, Jurisdiction, Date, Penalty, Benefit.\n"
            "- Only use these relations: WORKS_IN, REPORTS_TO, HAS_ROLE, EMPLOYED_BY, SIGNED, "
            "PARTY_TO, GOVERNED_BY, CONTAINS, IMPOSES, GRANTS, OVERRIDES, EFFECTIVE_FROM, "
            "EXPIRES_ON, APPLIES_TO, SUBJECT_TO, COMPENSATED_WITH, AMENDED_BY, REFERENCES.\n"
            "- Do NOT extract generic entities (e.g., 'the document', 'this agreement', 'Section').\n"
            "- Be specific: use exact names, dates, and clause numbers from the text.\n"
            "- Each triple must be (EntityType: Name) -> RELATION -> (EntityType: Name).\n\n"
            "Text to analyze:\n{text}"
        )

        from llama_index.core.indices.property_graph import SimpleLLMPathExtractor
        kg_extractor = SimpleLLMPathExtractor(
            llm=Settings.llm,
            extract_prompt=LEGAL_HR_EXTRACTION_PROMPT,
            num_workers=4,
        )

        
        # Create a specific event loop for this thread's async operations
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        PropertyGraphIndex.from_documents(documents, property_graph_store=graph_store, kg_extractors=[kg_extractor])

        print(f"✅ INGESTION: Successfully Indexed {filename} into Vector & Graph DBs")
    except Exception as e:
        print(f"❌ INGESTION ERROR: {e}")
`


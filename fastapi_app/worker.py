import os
import io
import base64
import nest_asyncio
from celery import Celery
from celery.signals import task_prerun, task_postrun, task_failure

# Critical: Allows LlamaIndex's internal async loops to run safely inside threads
nest_asyncio.apply()

# --- BCRYPT MONKEY PATCH ---
import passlib.handlers.bcrypt
import bcrypt
import requests
import time

# ── OpenTelemetry: Celery Task Instrumentation ──────────────────────────────
# Sends trace spans for every Celery task to Arize Phoenix so that document
# ingestion and GraphRAG build pipelines appear alongside FastAPI request spans.
try:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry import trace as otel_trace

    _phoenix_endpoint = os.getenv("PHOENIX_COLLECTOR_ENDPOINT", "http://phoenix:4318/v1/traces")
    _tracer_provider = TracerProvider()
    _tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(_phoenix_endpoint)))
    otel_trace.set_tracer_provider(_tracer_provider)
    _worker_tracer = otel_trace.get_tracer("celery.worker")
    _OTEL_ENABLED = True
    print(f"✅ Worker: OpenTelemetry tracing → {_phoenix_endpoint}")
except Exception as _otel_err:
    _OTEL_ENABLED = False
    print(f"⚠️ Worker: OpenTelemetry setup failed: {_otel_err}")

# Active span registry — maps Celery task_id → OTel span so postrun/failure
# signals can finish the span that prerun opened.
_active_spans: dict = {}

def _check_hash(secret, hash):
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    if isinstance(hash, str):
        hash = hash.encode("utf-8")
    return bcrypt.checkpw(secret, hash)

passlib.handlers.bcrypt.bcrypt._check_hash = _check_hash
# ---------------------------

from llama_index.core import VectorStoreIndex, StorageContext, Settings, PropertyGraphIndex
from llama_index.llms.openai_like import OpenAILike
from llama_index.embeddings.openai import OpenAIEmbedding
from llama_index.vector_stores.opensearch import OpensearchVectorClient, OpensearchVectorStore
from llama_index.graph_stores.neo4j import Neo4jPropertyGraphStore
from llama_index.readers.file import PyMuPDFReader
from core.ingestion_workflow import ingestion_workflow

VALKEY_URL       = os.getenv("VALKEY_URL", "redis://valkey:6379/0")
# Result backend enables task status polling from the /task-status/{id} API endpoint
celery_app = Celery("rag_tasks", broker=VALKEY_URL, backend=VALKEY_URL)

import psycopg2
from psycopg2 import pool as _pg_pool
from pgvector.psycopg2 import register_vector
PG_DSN = os.getenv("POSTGRES_URL")

# ── Connection Pool ─────────────────────────────────────────────────────────
# With --concurrency=100 gevent tasks, raw connect()/close() per task exhausts
# Postgres max_connections.  A pool caps the number of live connections.
_worker_pg_pool: _pg_pool.ThreadedConnectionPool | None = None

def _get_worker_conn():
    """Borrow a connection from the worker pool (lazy init)."""
    global _worker_pg_pool
    if _worker_pg_pool is None or _worker_pg_pool.closed:
        _worker_pg_pool = _pg_pool.ThreadedConnectionPool(minconn=2, maxconn=20, dsn=PG_DSN)
    return _worker_pg_pool.getconn()

def _release_worker_conn(conn):
    """Return a connection to the pool."""
    if _worker_pg_pool and not _worker_pg_pool.closed:
        _worker_pg_pool.putconn(conn)

GPU_NODE_IP      = os.getenv("GPU_NODE_IP", "172.18.0.1")
VLLM_URL         = os.getenv("VLLM_BASE_URL", f"http://{GPU_NODE_IP}:8081/v1")
INFINITY_EMBED_URL = os.getenv("INFINITY_EMBED_URL", f"http://{GPU_NODE_IP}:8082/v1")
# NOTE: Qwen3.5-27B-FP8 IS a vision-language model (Qwen3_5ForConditionalGeneration).
# OCR calls go directly to vLLM at port 8081. The only constraint is max_model_len=32768,
# so we must render images at low DPI to keep visual token count well below that limit.
MAIN_MODEL       = os.getenv("MAIN_MODEL", "Qwen/Qwen3.5-27B-FP8")
EMBED_MODEL      = os.getenv("EMBED_MODEL", "Qwen/Qwen3-VL-Embedding-8B")
# Single source of truth — must match main.py
DIMENSIONS       = int(os.getenv("EMBED_DIM", "4096"))
CACHE_DIM        = 1536

Settings.embed_model = OpenAIEmbedding(
    model_name=EMBED_MODEL,
    api_base=INFINITY_EMBED_URL,
    api_key="placeholder",
    timeout=30.0,
)

Settings.llm = OpenAILike(
    model=MAIN_MODEL,
    api_base=VLLM_URL,
    api_key="placeholder",
    is_chat_model=True,
    is_function_calling_model=True,
    max_tokens=2048,
    context_window=32768,
    timeout=900.0,
)

@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def process_document_task(self, file_path: str, filename: str, file_hash: str = ""):
    """
    Celery Task: Ingest a single document through the multimodal pipeline.
    
    1. Determines file type (PDF, DOCX, XLSX, images, etc.).
    2. Parses raw text.
    3. For PDFs, identifies scanned pages and renders them at 72 DPI (1.0x scale) 
       for visual text extraction (OCR) via Qwen-VL without blowing out the 32K context limit.
    4. Enriches with LLM metadata (Summary, DocType).
    5. Dispatches to the LangGraph `ingestion_workflow` for final OpenSearch & Neo4j insertion.
    """
    print(f"--- 🚀 MULTIMODAL INGESTION: Started processing {filename} (attempt {self.request.retries + 1}/4) ---")

    # ── Open a trace span for the full ingestion lifecycle ───────────────────
    _span = None
    if _OTEL_ENABLED:
        _span = _worker_tracer.start_span(
            "celery.process_document",
            attributes={
                "celery.task_id": self.request.id or "",
                "document.filename": filename,
                "document.file_hash": file_hash or "",
                "celery.retries": self.request.retries,
            }
        )
        _active_spans[self.request.id] = _span
    
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
        import fitz  # PyMuPDF

        ext = os.path.splitext(filename)[1].lower()

        is_pdf = False
        is_raw_image = False

        # ── Multi-format document reader dispatch ────────────────────────────
        if ext in (".docx",):
            from llama_index.readers.file import DocxReader
            raw_docs = DocxReader().load_data(file=file_path)
        elif ext in (".xlsx", ".xls"):
            from llama_index.readers.file import PandasExcelReader
            raw_docs = PandasExcelReader().load_data(file=file_path)
        elif ext in (".pptx",):
            from llama_index.readers.file import PptxReader
            raw_docs = PptxReader().load_data(file=file_path)
        elif ext in (".txt", ".md", ".csv"):
            from llama_index.core import SimpleDirectoryReader
            raw_docs = SimpleDirectoryReader(input_files=[file_path]).load_data()
        elif ext in (".jpg", ".jpeg", ".png"):
            from llama_index.core.schema import Document
            raw_docs = [Document(text="[Raw Source Image - Extracting...]", metadata={"page_label": "1"})]
            is_raw_image = True
        else:
            raw_docs = PyMuPDFReader().load(file_path)
            is_pdf = True

        # ── Per-page image rendering for VL multimodal embedding (PDF only) ──
        # For each PDF page: render PNG, check text density, mark scanned pages.
        SCAN_TEXT_THRESHOLD = 80   # chars per page — below this = likely scanned
        EMBED_URL = INFINITY_EMBED_URL.rstrip("/")

        def is_scanned_page(text: str, page: fitz.Page) -> bool:
            """Return True if the page lacks sufficient text."""
            try:
                # If the page already has sufficient digital text, skip expensive OCR entirely!
                if len(text.strip()) >= SCAN_TEXT_THRESHOLD:
                    return False
                
                # Check for visual elements only if digital text is lacking
                images = page.get_images(full=True)
                if len(images) > 0:
                    return True
                if len(page.get_drawings()) > 0:
                    return True
            except Exception:
                return True # Fallback to True if fitz fails
                
            return False

        def extract_vision_text(img_b64: str) -> str:
            """
            Extract text from a scanned page image via Qwen3.5-27B-FP8 (VL model).
            Image is rendered at 1.0x scale (72 DPI) before calling this,
            to balance high transcription accuracy with optimal visual token count (~2,550 tokens).
            """
            payload = {
                "model": MAIN_MODEL,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{img_b64}"
                                }
                            },
                            {
                                "type": "text",
                                "text": "Read the image and transcribe every piece of text you can see. Reply with only the extracted text, nothing else."
                            }
                        ]
                    }
                ],
                "max_tokens": 2048,
                "temperature": 0.1,
                "chat_template_kwargs": {"enable_thinking": False}
            }
            try:
                response = requests.post(
                    f"{VLLM_URL}/chat/completions",
                    json=payload,
                    headers={"Authorization": "Bearer placeholder"},
                    timeout=300
                )
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"].strip()
                import re
                content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
                return content
            except Exception as e:
                print(f"Vision extraction failed: {e}")
                if hasattr(e, "response") and e.response is not None:
                    print(f"Vision response error: {e.response.text[:500]}")
                return ""

        if is_pdf:
            # ── Enrich documents with page images for scanned pages ──────────
            pdf_doc = fitz.open(file_path)
            num_pages = len(pdf_doc)

            documents = []
            pages_to_ocr = [] # Holds (page_idx, img_b64, page_label)

            for page_idx, doc in enumerate(raw_docs):
                try:
                    page_num = int(doc.metadata.get("page_label", page_idx + 1))
                except ValueError:
                    page_num = page_idx + 1

                try:
                    real_page_idx = min(page_num - 1, num_pages - 1)
                    page = pdf_doc[real_page_idx]
                    
                    if is_scanned_page(doc.text, page):
                        doc.metadata["is_scanned"] = True
                        pages_to_ocr.append((page_idx, real_page_idx, real_page_idx+1))
                    else:
                        doc.metadata["is_scanned"] = False
                except IndexError as page_err:
                    print(f"⚠️ Skipping vision for page {page_num} due to corrupted PDF: {page_err}")
                    doc.metadata["is_scanned"] = False
                
                documents.append(doc)

            # Phase 2: Parallel Render & OCR Execution
            if pages_to_ocr:
                from concurrent.futures import ThreadPoolExecutor, as_completed
                # Since the server has 95% CPU idle and low load average, we can safely increase this.
                # However, with 4 Celery workers, a limit of 8 means up to 32 parallel render threads.
                # This is a safe and massive speedup without starving other background services.
                max_workers_ocr = min(len(pages_to_ocr), 8)
                print(f"🚀 Running Parallel Render+OCR for {len(pages_to_ocr)} pages with {max_workers_ocr} threads...")
                
                def process_single_page(item):
                    idx, real_p_idx, label = item
                    try:
                        t0 = time.time()
                        # 1. Render in parallel (Releases GIL)
                        print(f"📷 Page {label}: rendering image for VL embedding in background...")
                        page = pdf_doc[real_p_idx]
                        mat = fitz.Matrix(1.0, 1.0)
                        pix = page.get_pixmap(matrix=mat)
                        img_b64 = base64.b64encode(pix.tobytes("png")).decode("utf-8")
                        
                        # 2. Extract OCR
                        ocr_text = extract_vision_text(img_b64)
                        dt = time.time() - t0
                        return idx, img_b64, ocr_text, label, dt, None
                    except Exception as err:
                        return idx, None, "", label, 0.0, err

                with ThreadPoolExecutor(max_workers=max_workers_ocr) as executor:
                    futures = [executor.submit(process_single_page, item) for item in pages_to_ocr]
                    for fut in as_completed(futures):
                        idx, img_b64, ocr_text, label, dt, err = fut.result()
                        doc = documents[idx]
                        if err:
                            print(f"   ↳ Render/OCR failed for page {label}: {err} — using original text")
                            doc.metadata["is_scanned"] = False
                        else:
                            doc.metadata["image_b64"] = img_b64
                            if len(ocr_text) > 20:
                                doc.text = ocr_text
                                print(f"   ↳ [Parallel Render+OCR] Page {label} extracted ({len(ocr_text)} chars) in {dt:.1f}s via {MAIN_MODEL}")
                            else:
                                print(f"   ↳ [Parallel Render+OCR] Page {label} returned empty/short text in {dt:.1f}s")
            pdf_doc.close()
        elif is_raw_image:
            documents = []
            img_doc = raw_docs[0]
            try:
                with open(file_path, "rb") as f:
                    img_bytes = f.read()
                img_b64 = base64.b64encode(img_bytes).decode("utf-8")
                img_doc.metadata["image_b64"] = img_b64
                img_doc.metadata["is_scanned"] = True
                print("📷 Processing raw image / blueprint file")
                
                try:
                    ocr_text = extract_vision_text(img_b64)
                    if len(ocr_text) > 20:
                        img_doc.text = ocr_text
                        print(f"   ↳ Blueprint vision text extracted ({len(ocr_text)} chars) via {MAIN_MODEL}")
                except Exception as ocr_err:
                    print(f"   ↳ Blueprint Vision OCR failed: {ocr_err}")
            except Exception as e:
                print(f"   ↳ Raw Image handling failed: {e}")
            documents.append(img_doc)
        else:
            # Non-PDF formats: no page rendering needed
            documents = list(raw_docs)
        # ─────────────────────────────────────────────────────────────────────


        # --- NOMIC EMBED PREFIX: Required for nomic-embed-text-v1.5 ---
        for doc in documents:
            if "nomic-embed-text" in EMBED_MODEL:
                doc.text = f"search_document: {doc.text}"
            elif "Qwen2" in EMBED_MODEL or "Qwen3" in EMBED_MODEL:
                doc.text = f"instruct: {doc.text}"
        
        # --- AUTOMATED METADATA ENRICHMENT ---
        summary = "No summary available."
        doc_type = "Corporate Document"
        # except Exception as e:
        #     # print(f"⚠️ LLM Metadata Extraction Failed: {e}\nRaw LLM response: {resp_text if 'resp_text' in locals() else 'None'}")
        #     pass

        for doc in documents:
            # Preserve per-page enrichment keys while appending document-level fields
            img_b64 = doc.metadata.get("image_b64")
            is_scanned = doc.metadata.get("is_scanned", False)
            doc.metadata = {
                "filename": filename,
                "allowed_roles": ["GLOBAL_READ"],
                "document_summary": summary[:300] + "..." if len(summary) > 300 else summary,
                "document_type": doc_type[:100] + "..." if len(doc_type) > 100 else doc_type,
                "is_scanned": is_scanned,
            }
            if file_hash:
                doc.metadata["document_version_hash"] = file_hash
                
            doc.excluded_embed_metadata_keys = ["document_summary", "allowed_roles", "is_scanned", "document_type", "document_version_hash"]
            doc.excluded_llm_metadata_keys = ["document_summary", "allowed_roles", "is_scanned", "document_type", "document_version_hash"]

        # --- EVENT-DRIVEN SEMANTIC CACHE INVALIDATION ---
        try:
            if PG_DSN and summary and summary != "No summary available.":
                summary_emb = Settings.embed_model.get_text_embedding(summary)
                sliced_cache_emb = summary_emb[:CACHE_DIM]
                
                conn = _get_worker_conn()
                try:
                    conn.autocommit = True
                    register_vector(conn)
                    cur = conn.cursor()
                    cur.execute("DELETE FROM semantic_cache WHERE embedding <=> %s::vector < 0.15;", (sliced_cache_emb,))
                    deleted_rows = cur.rowcount
                    cur.close()
                    print(f"✅ CACHE FLUSH: Cleared {deleted_rows} cached queries semantically overlapping: '{summary[:60]}...'")
                finally:
                    conn.autocommit = False
                    _release_worker_conn(conn)
        except Exception as cache_e:
            print(f"⚠️ Dynamic Cache Flushing Encountered an error: {cache_e}")
        # ------------------------------------------------

        # NOTE: Multi-modal image extraction via SGLang vision (Qwen3.5-VL)
        # is handled at query time via the vision_analysis tool, not at ingestion.
        # The Ollama-based extraction path has been removed as there is no Ollama
        # service in this stack — the SGLang endpoint already serves vision queries.


        state = {
            "documents": documents,
            "document_text": "\n\n".join([doc.text for doc in documents]),
            "filename": filename,
            "file_hash": file_hash,
            "summary": summary,
            "doc_type": doc_type,
            "attempt_count": 0,
        }
        
        max_workflow_retries = 3
        for attempt in range(max_workflow_retries):
            try:
                result_state = ingestion_workflow.invoke(state)
                break
            except Exception as e:
                if attempt == max_workflow_retries - 1:
                    raise
                print(f"⚠️ LangGraph Workflow encountered an error on attempt {attempt+1}: {e}. Retrying in 10s...")
                time.sleep(10)
                state["attempt_count"] = attempt + 1

        if _span:
            _span.set_attribute("document.chunks_indexed", len(result_state.get("nodes", [])))
            _span.set_status(otel_trace.StatusCode.OK if _OTEL_ENABLED else None)
            _span.end()
            _active_spans.pop(self.request.id, None)
        print(f"✅ INGESTION: Successfully Indexed {filename} via LangGraph Workflow")
    except Exception as e:
        if _span:
            _span.set_attribute("error", str(e))
            _span.set_status(otel_trace.StatusCode.ERROR if _OTEL_ENABLED else None, str(e))
            _span.end()
            _active_spans.pop(self.request.id, None)
        print(f"❌ INGESTION ERROR (attempt {self.request.retries + 1}/4): {e}")
        # Exponential backoff: 60s → 120s → 240s before final failure
        raise self.retry(exc=e, countdown=60 * (2 ** self.request.retries))



# ─────────────────────────────────────────────────────────────────────────────
# GraphRAG: Global Community Summaries Task
# Clusters structurally dense hub nodes inside Neo4J, synthesizes a written
# paragraph per cluster using vLLM, and pushes the resulting global context
# paragraphs into OpenSearch for macro-level enterprise queries.
# ─────────────────────────────────────────────────────────────────────────────
@celery_app.task(name="build_graphrag_summaries")
def build_graphrag_summaries():
    """
    Celery Task: Global GraphRAG Community Summarization.
    
    This task operates asynchronously to enrich the OpenSearch vector database 
    with macro-level knowledge extracted from the Neo4j Graph.
    
    1. Identifies "Hub Nodes" in Neo4j (Projects, Policies, Organizations).
    2. Extracts 1-hop relation sub-graphs (triplets).
    3. Uses vLLM to synthesize a natural language paragraph describing the hub's ecosystem.
    4. Embeds and upserts the summary into OpenSearch for global query retrieval.
    """
    print("🌐 GRAPHRAG: Starting Global Community Summary Builder...")

    # ── Trace the entire GraphRAG build as a single span ─────────────────────
    _span = None
    if _OTEL_ENABLED:
        _span = _worker_tracer.start_span("celery.build_graphrag_summaries")
    try:
        from neo4j import GraphDatabase

        neo4j_uri  = os.getenv("NEO4J_URI", "bolt://neo4j:7687")
        neo4j_user = os.getenv("NEO4J_USER")
        neo4j_pass = os.getenv("NEO4J_PASSWORD")
        if not neo4j_user or not neo4j_pass:
            raise RuntimeError("FATAL: NEO4J_USER and NEO4J_PASSWORD must be set via environment variables.")

        driver = GraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_pass))

        # ── 1. Identify Hub Nodes (high connectivity entity types) ───────────
        HUB_TYPES = ["Project", "Department", "System", "Policy", "Organization"]

        os_url     = os.getenv("OPENSEARCH_URL", "https://opensearch:9200")
        os_user_os = os.getenv("OPENSEARCH_USER")
        os_pass_os = os.getenv("OPENSEARCH_PASSWORD")
        if not os_user_os or not os_pass_os:
            raise RuntimeError("FATAL: OPENSEARCH_USER and OPENSEARCH_PASSWORD must be set via environment variables.")
        index_url  = f"{os_url}/universal_docs_v1/_doc"

        summaries_created = 0

        with driver.session() as session:
            for hub_type in HUB_TYPES:
                # Pull all hub nodes of this type
                hubs = session.run(
                    f"MATCH (h:{hub_type}) RETURN h.name AS name LIMIT 100"
                ).data()

                for hub in hubs:
                    hub_name = hub.get("name")
                    if not hub_name:
                        continue

                    # ── 2. Extract 1-hop sub-graph around this hub ───────────
                    rels = session.run(
                        "MATCH (h {name: $name})-[r]-(n) "
                        "RETURN type(r) AS relation, labels(n)[0] AS target_type, n.name AS target_name "
                        "LIMIT 50",
                        name=hub_name
                    ).data()

                    if not rels:
                        continue

                    # ── 3. Serialize triples into plain text for LLM ─────────
                    triples_text = "\n".join(
                        f"  - ({hub_type}: {hub_name}) --[{r['relation']}]--> "
                        f"({r['target_type'] or 'Entity'}: {r['target_name'] or 'Unknown'})"
                        for r in rels
                    )

                    prompt = (
                        f"You are an enterprise knowledge analyst.\n"
                        f"Below are structured relationship facts from our internal corporate knowledge graph about: '{hub_name}'\n\n"
                        f"{triples_text}\n\n"
                        f"Write a comprehensive, natural-language paragraph (3-5 sentences) that summarizes "
                        f"what the organization knows about '{hub_name}', its connections, responsibilities, and role "
                        f"within the enterprise. Do not reference 'the graph' — write as if this is a professional summary."
                    )

                    # ── 4. Synthesize summary via vLLM ───────────────────────
                    try:
                        response = Settings.llm.complete(prompt)
                        summary_text = str(response).strip()
                    except Exception as llm_e:
                        print(f"  ⚠️ GRAPHRAG: LLM error for '{hub_name}': {llm_e}")
                        continue

                    if not summary_text:
                        continue

                    # ── 5. Embed summary and upsert into OpenSearch ──────────
                    # Use a deterministic doc_id so re-runs overwrite rather
                    # than accumulate duplicate summaries.
                    try:
                        embedding = Settings.embed_model.get_text_embedding(summary_text)
                        doc_id = f"graphrag_{hub_type}_{hub_name}".replace(" ", "_").lower()
                        doc = {
                            "content": summary_text,
                            "embedding": embedding,
                            "metadata": {
                                "node_type": "GraphRAG_Global_Summary",
                                "hub_entity_type": hub_type,
                                "hub_entity_name": hub_name,
                                "filename": f"graphrag_summary_{hub_type}_{hub_name}",
                                "document_type": "GraphRAG Community Summary",
                                "allowed_roles": ["Operations_Admin"]
                            }
                        }
                        upsert_url = f"{os_url}/universal_docs_v1/_doc/{doc_id}"
                        res = requests.put(
                            upsert_url,
                            json=doc,
                            auth=(os_user_os, os_pass_os),
                            verify=False,
                            timeout=30
                        )
                        if res.status_code in (200, 201):
                            summaries_created += 1
                            print(f"  ✅ GRAPHRAG: Upserted summary for ({hub_type}: {hub_name})")
                        else:
                            print(f"  ⚠️ GRAPHRAG: OS rejected summary for '{hub_name}': {res.text[:200]}")
                    except Exception as emb_e:
                        print(f"  ⚠️ GRAPHRAG: Embedding/Index error for '{hub_name}': {emb_e}")

        driver.close()
        if _span:
            _span.set_attribute("graphrag.summaries_created", summaries_created)
            _span.set_status(otel_trace.StatusCode.OK if _OTEL_ENABLED else None)
            _span.end()
        print(f"✅ GRAPHRAG: Completed. {summaries_created} community summaries indexed into OpenSearch.")
        return {"status": "done", "summaries_created": summaries_created}

    except Exception as e:
        if _span:
            _span.set_attribute("error", str(e))
            _span.set_status(otel_trace.StatusCode.ERROR if _OTEL_ENABLED else None, str(e))
            _span.end()
        print(f"❌ GRAPHRAG ERROR: {e}")
        return {"status": "error", "detail": str(e)}
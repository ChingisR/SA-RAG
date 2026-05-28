import os
import base64
import requests
import json
import nest_asyncio
from typing import List, Optional

nest_asyncio.apply()

from pydantic import Field
from llama_index.core.postprocessor.types import BaseNodePostprocessor
from llama_index.core.schema import NodeWithScore, QueryBundle
from llama_index.core.vector_stores.types import VectorStoreQueryMode, MetadataFilters, ExactMatchFilter
from llama_index.core import VectorStoreIndex

from llama_index.vector_stores.opensearch import OpensearchVectorClient, OpensearchVectorStore
from core.config import DIMENSIONS, GPU_NODE_IP

_os_vector_store_singleton = None
_os_index_singleton = None

def _build_os_vector_store():
    global _os_vector_store_singleton
    if _os_vector_store_singleton is None:
        os_url  = os.getenv("OPENSEARCH_URL", "https://opensearch:9200")
        os_user = os.getenv("OPENSEARCH_USER")
        os_pass = os.getenv("OPENSEARCH_PASSWORD")
        os_client = OpensearchVectorClient(
            endpoint=os_url,
            index="universal_docs_v1",
            dim=DIMENSIONS,
            http_auth=(os_user, os_pass),
            use_ssl=True, verify_certs=False,
            search_pipeline="rrf-pipeline",
            method={"name": "hnsw", "engine": "faiss", "space_type": "l2"}
        )
        _os_vector_store_singleton = OpensearchVectorStore(client=os_client)
    return _os_vector_store_singleton


class SGLangRerank(BaseNodePostprocessor):
    model: str = Field(description="Reranker model name.")
    base_url: str = Field(description="Base URL for the reranker HTTP endpoint.")
    top_n: int = Field(description="Top N nodes to return.")
    
    @classmethod
    def class_name(cls) -> str:
        return "SGLangRerank"
        
    def _postprocess_nodes(self, nodes: List[NodeWithScore], query_bundle: Optional[QueryBundle]) -> List[NodeWithScore]:
        if query_bundle is None or len(nodes) == 0:
            return nodes

        texts = [n.node.get_content() for n in nodes]

        # ── Vision-Language Reranking: extract page images from node metadata ──
        # Nodes ingested from PDFs may have an 'image_path' or 'image_b64' field
        # stored during ingestion. If present, we encode them and pass to the
        # Qwen3-VL-Reranker for true cross-modal relevance scoring.
        doc_images: List[Optional[str]] = []
        for n in nodes:
            img_b64 = n.node.metadata.get("image_b64")
            if not img_b64:
                img_path = n.node.metadata.get("image_path")
                if img_path:
                    try:
                        with open(img_path, "rb") as f:
                            img_b64 = base64.b64encode(f.read()).decode("utf-8")
                    except Exception:
                        img_b64 = None
            doc_images.append(img_b64)

        has_vision = any(img is not None for img in doc_images)

        payload = {
            "model": self.model,
            "query": query_bundle.query_str,
            "texts": texts,
            "documents": texts,
            "top_n": self.top_n,
        }
        if has_vision:
            payload["doc_images"] = doc_images
            print(f"🖼️ VL-Reranker: Scoring {sum(1 for x in doc_images if x)} visual nodes multimodally.")

        try:
            resp = requests.post(f"{self.base_url.rstrip('/')}/rerank", json=payload, timeout=600)
            resp.raise_for_status()
            results = resp.json().get("results", [])

            new_nodes = []
            for res in results:
                idx = res.get("index")
                score = res.get("relevance_score")
                if idx is not None and idx < len(nodes):
                    nodes[idx].score = score
                    new_nodes.append(nodes[idx])

            if new_nodes:
                return sorted(new_nodes, key=lambda x: x.score or 0.0, reverse=True)[:self.top_n]
        except Exception as e:
            print(f"⚠️ SGLang Rerank fallback to default: {e}")

        return nodes[:self.top_n]

def get_reranker(top_n: int):
    return SGLangRerank(
        model=os.getenv("RERANK_MODEL", "Qwen/Qwen3-VL-Reranker-8B"),
        base_url=os.getenv("RERANK_URL", f"http://{GPU_NODE_IP}:8082/v1"),
        top_n=top_n
    )

# ── Role Hierarchy: which roles each level can read ─────────────────────────
ROLE_READABLE_ROLES = {
    "Operations_Admin": None,   # None = bypass filter entirely (sees all docs)
    "HSE_Manager":      ["HSE_Manager", "standard"],
    "standard":         ["standard"],
}

def get_hybrid_rrf_engine(top_k: int, top_n: int, user_role: str, doc_type: str = None, tenant_id: str = None):
    """Returns a Hybrid Retriever using the cached vector store and index singletons."""
    global _os_index_singleton
    if _os_index_singleton is None:
        vector_store = _build_os_vector_store()
        _os_index_singleton = VectorStoreIndex.from_vector_store(vector_store=vector_store)

    # Determine which allowed_roles values this user can read
    readable = ROLE_READABLE_ROLES.get(user_role, [user_role])

    base_filters = []
    if doc_type:
        base_filters.append(ExactMatchFilter(key="doc_type", value=doc_type))
    if tenant_id:
        base_filters.append(ExactMatchFilter(key="tenant_id", value=tenant_id))

    if readable is None:
        # HR_Admin: no role filter — sees everything
        if base_filters:
            final_filters = MetadataFilters(filters=base_filters, condition="and")
            return _os_index_singleton.as_query_engine(
                vector_store_query_mode=VectorStoreQueryMode.HYBRID,
                similarity_top_k=top_k,
                node_postprocessors=[get_reranker(top_n)],
                filters=final_filters,
            )
        else:
            return _os_index_singleton.as_query_engine(
                vector_store_query_mode=VectorStoreQueryMode.HYBRID,
                similarity_top_k=top_k,
                node_postprocessors=[get_reranker(top_n)],
            )

    role_filters = []
    for r in readable:
        role_filters.append(ExactMatchFilter(key="allowed_roles", value=r))
    
    role_metadata_filter = MetadataFilters(filters=role_filters, condition="or")
    
    if base_filters:
        final_filters = MetadataFilters(filters=[role_metadata_filter, *base_filters], condition="and")
    else:
        final_filters = role_metadata_filter

    return _os_index_singleton.as_query_engine(
        vector_store_query_mode=VectorStoreQueryMode.HYBRID,
        similarity_top_k=top_k,
        node_postprocessors=[get_reranker(top_n)],
        filters=final_filters,
    )

async def run_opensearch_engine(query_str: str, top_k: int, top_n: int, user_role: str, doc_type: str = None, tenant_id: str = None) -> str:
    engine = get_hybrid_rrf_engine(top_k, top_n, user_role, doc_type=doc_type, tenant_id=tenant_id)
    
    # Bypass the inner LLM synthesis step (engine.aquery) to avoid context limit errors
    # and redundant token generation in LangGraph.
    from llama_index.core.schema import QueryBundle
    query_bundle = QueryBundle(query_str)
    
    # 1. Retrieve initial nodes
    nodes = await engine.aretrieve(query_bundle)
    
    # 2. Apply node postprocessors (Reranker)
    nodes = get_reranker(top_n).postprocess_nodes(nodes, query_bundle)
        
    # 3. Format raw context and inject citations
    sources = []
    context_text = ""
    total_chars = 0
    MAX_CHARS = 40000  # Cap at ~10,000 tokens to safely fit within 20k context window
    
    for node_with_score in nodes:
        node = node_with_score.node
        filename = node.metadata.get("filename", node.metadata.get("file_name", ""))
        page     = node.metadata.get("page_label", node.metadata.get("page_number", "1"))
        snippet  = (node.text or "")[:300].strip().replace("\n", " ")
        roles = node.metadata.get("allowed_roles", ["standard"])
        if isinstance(roles, str):
            roles = [roles]
            
        key = f"{filename}:{page}"
        sources.append({"filename": filename, "page": str(page), "snippet": snippet, "allowed_roles": roles})
        
        # Build context
        chunk_text = node.text or ""
        header = f"\n--- Source: {filename} (Page {page}) ---\n"
        
        if total_chars + len(header) + len(chunk_text) > MAX_CHARS:
            allowed_chars = MAX_CHARS - total_chars - len(header)
            if allowed_chars > 0:
                context_text += header + chunk_text[:allowed_chars] + "...\n"
                total_chars += allowed_chars + len(header)
            break
        else:
            context_text += header + chunk_text + "\n"
            total_chars += len(header) + len(chunk_text)
            
    citation_block = f"\n<!--CITATIONS_JSON:{json.dumps(sources)}-->" if sources else ""
    return context_text + citation_block


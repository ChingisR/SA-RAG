"""
LangGraph Ingestion Workflow Module

This module defines a state machine (LangGraph) for processing, evaluating, 
and indexing enterprise documents into a Hybrid RAG architecture:
1. OpenSearch (Vector/Dense Search via FAISS HNSW)
2. Valkey/Redis (Document Store)
3. Neo4j (Knowledge Graph / Entity extraction)

The workflow consists of three main nodes:
- structural_splitting_node: Uses regex or Markdown heuristics to chunk text.
- structure_evaluator_node: LLM critic evaluates the chunks and routes back if poor.
- indexing_node: Performs the final embeddings and bulk graph database insertions.
"""

import os
import re
import json
import requests
from typing import TypedDict, List, Optional, Any, Literal
from llama_index.core.schema import Document, BaseNode, TextNode, NodeRelationship, RelatedNodeInfo
from llama_index.core.node_parser import MarkdownNodeParser, SentenceSplitter
from llama_index.core.node_parser import get_leaf_nodes
from llama_index.core import VectorStoreIndex, StorageContext, Settings, PropertyGraphIndex
from llama_index.vector_stores.opensearch import OpensearchVectorClient, OpensearchVectorStore
from llama_index.graph_stores.neo4j import Neo4jPropertyGraphStore
from llama_index.core.indices.property_graph import SimpleLLMPathExtractor

from langgraph.graph import StateGraph, END

# Import unified environment vars from core.config if needed, or re-declare
DIMENSIONS = int(os.getenv("EMBED_DIM", "4096"))
OPENSEARCH_URL = os.getenv("OPENSEARCH_URL", "https://opensearch:9200")
OPENSEARCH_USER = os.getenv("OPENSEARCH_USER")
OPENSEARCH_PASSWORD = os.getenv("OPENSEARCH_PASSWORD")
VALKEY_URL = os.getenv("VALKEY_URL", "redis://valkey:6379/0")
NEO4J_URI = os.getenv("NEO4J_URI")
NEO4J_USER = os.getenv("NEO4J_USER")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")
GPU_NODE_IP = os.getenv("GPU_NODE_IP", "172.18.0.1")
INFINITY_EMBED_URL = os.getenv("INFINITY_EMBED_URL", f"http://{GPU_NODE_IP}:8082/v1")

KASHAGAN_STRATEGIES = {
    # --- TECHNICAL & OPERATIONAL ---
    "WELL_PROGNOSIS_AND_LOGS": re.compile(r"^(?:MD|TVD) \d{3,5}m:|^Phase \d+:|^Formation:", re.MULTILINE | re.IGNORECASE),
    "PRODUCTION_FACILITY_REPORT": re.compile(r"^(?:Train|Unit) \d{1,2} Status:|^Downtime Event", re.MULTILINE),
    "HSE_PROTOCOL_H2S": re.compile(r"^(?:Hazard|Risk) Level [1-5]:|^Mitigation Action \d+:|^Section \d+:", re.MULTILINE),
    
    # --- BUSINESS & LEGAL (KMG KASHAGAN REGISTRY) ---
    "PSA_AND_JOA_LEGAL": re.compile(
        r"^(?:Article|Section) \d+[\.\:]\s*|^Annex [A-Z]\b|^Schedule \d+\b|^Resolution No\.", 
        re.MULTILINE | re.IGNORECASE
    ),
    "FINANCE_AND_AFE": re.compile(
        r"^AFE Number:|^WBS Element:|^Cost Category:|^Cash Call Reference:|^Audit Exception \d+:", 
        re.MULTILINE | re.IGNORECASE
    ),
    "PROCUREMENT_AND_EPC": re.compile(
        r"^Scope of Work:|^Deliverable \d+[\.\:]|^Milestone [A-Z\d]+:|^Exhibit [A-Z\d]+:|^Technical Clarification \d+:", 
        re.MULTILINE | re.IGNORECASE
    ),
    "CORPORATE_AND_MINUTES": re.compile(
        r"^Agenda Item \d+|^Action Item(?:s)?:|^Decision:|^Policy \d+\.\d+:|^(?:To|From|Date|Subject):", 
        re.MULTILINE | re.IGNORECASE
    ),
    "UNIVERSAL_OUTLINE": re.compile(
        r"^[IVX]+\.\s+|^\d+\.\d+(?:\.\d+)?\s+", 
        re.MULTILINE
    ),
    
    "LOOSE_MARKDOWN": re.compile(r"^#{1,4} ", re.MULTILINE)
}

class IngestionState(TypedDict):
    documents: List[Document]
    document_text: str
    suggested_strategy: Optional[str]
    nodes: List[BaseNode]
    attempt_count: int
    is_valid: bool
    filename: str
    file_hash: str
    summary: str
    doc_type: str

def structural_splitting_node(state: IngestionState):
    """
    LangGraph Node: Splits the raw text into logical structural chunks.
    
    Tries to intelligently apply regex-based business domain strategies 
    (like HSE Protocols or Legal Documents). If no specific strategy is selected, 
    falls back to a default Markdown parser.
    """
    print(f"🔄 Node 1: Structural Splitting (Attempt {state.get('attempt_count', 0) + 1})")
    strategy_name = state.get("suggested_strategy")
    text = state.get("document_text", "")
    
    # Initialize attempt_count if not present
    attempt_count = state.get("attempt_count", 0)
    
    # Heuristic: If no strategy is suggested by LLM yet (Attempt 1), try to auto-detect best business strategy
    if not strategy_name:
        max_matches = 0
        for name, pattern in KASHAGAN_STRATEGIES.items():
            if name == "LOOSE_MARKDOWN": continue
            matches = len(pattern.findall(text))
            if matches > max_matches:
                max_matches = matches
                strategy_name = name
        
    # Check if a strategy is active
    if strategy_name and strategy_name in KASHAGAN_STRATEGIES:
        print(f"   ↳ Applying Kashagan Strategy: {strategy_name}")
        pattern = KASHAGAN_STRATEGIES[strategy_name]
        
        # Use regex to split, keeping the delimiter as the header for the chunk
        splits = re.split(f"({pattern.pattern})", text)
        chunks = []
        
        # Re-stitch the header to its content
        if len(splits) > 0:
            # The first element might be text before the first match
            if splits[0].strip():
                chunks.append(splits[0])
            for i in range(1, len(splits), 2):
                header = splits[i]
                content = splits[i+1] if i+1 < len(splits) else ""
                chunks.append(header + content)
            
        # Convert raw text chunks to LlamaIndex Nodes
        # Pass along metadata from the original documents to the nodes
        base_metadata = state["documents"][0].metadata if state["documents"] else {}
        nodes = [TextNode(text=c.strip(), metadata=base_metadata) for c in chunks if c.strip()]
        
    else:
        print("   ↳ Applying Default Markdown Strategy")
        # Default Attempt 1: Standard Markdown structural parse
        parser = MarkdownNodeParser()
        # We process the original documents to retain any rich page-level metadata
        nodes = parser.get_nodes_from_documents(state["documents"])
        
    return {"nodes": nodes, "attempt_count": attempt_count + 1}

def structure_evaluator_node(state: IngestionState):
    print("🧠 Node 2: LLM Structure Evaluation (BYPASSED FOR MASS INGESTION)")
    return {"is_valid": True}

def route_validation(state: IngestionState) -> Literal["structural_splitting_node", "indexing_node"]:
    if state.get("is_valid"):
        return "indexing_node"
    return "structural_splitting_node"

def indexing_node(state: IngestionState):
    """
    LangGraph Node: Final storage insertion into OpenSearch, Valkey, and Neo4j.
    
    This function handles:
    1. Pre-computing embeddings for Vision models via custom APIs.
    2. Upserting OpenSearch Indices with FAISS HNSW.
    3. Performing highly optimized `UNWIND` bulk Cypher queries for Neo4j.
    4. Running an asynchronous LLM Path Extractor for semantic triplets.
    """
    print("💾 Node 4: Indexing to OpenSearch, Valkey, and Neo4j")
    nodes = state.get("nodes", [])
    if not nodes:
        print("⚠️ No nodes to index.")
        return {}

    md_leaf_nodes = get_leaf_nodes(nodes)
    md_parent_nodes = [n for n in nodes if n not in md_leaf_nodes]

    # Fine-grained Splitting for OpenSearch
    leaf_parser = SentenceSplitter(chunk_size=512, chunk_overlap=64)
    all_nodes = list(md_parent_nodes)
    searchable_leaf_nodes = []

    for leaf in md_leaf_nodes:
        chunks = leaf_parser.get_nodes_from_documents([leaf])
        if len(chunks) > 1:
            leaf.relationships[NodeRelationship.CHILD] = [
                RelatedNodeInfo(node_id=c.node_id) for c in chunks
            ]
            for c in chunks:
                c.relationships[NodeRelationship.PARENT] = RelatedNodeInfo(node_id=leaf.node_id)
                searchable_leaf_nodes.append(c)
            all_nodes.append(leaf)
            all_nodes.extend(chunks)
        else:
            if chunks:
                searchable_leaf_nodes.append(chunks[0])
                all_nodes.append(chunks[0])
                
    if not all_nodes:
        print("⚠️ No nodes parsed, skipping indexing.")
        return {}

    # Pre-compute multimodal embeddings for scanned images
    embed_url = INFINITY_EMBED_URL.rstrip("/")
    for node in searchable_leaf_nodes:
        if node.metadata.get("is_scanned") and "image_b64" in node.metadata:
            try:
                payload = {
                    "input": node.text,
                    "images": [node.metadata["image_b64"]],
                    "model": "default"
                }
                res = requests.post(f"{embed_url}/embeddings", json=payload, timeout=120)
                res.raise_for_status()
                data = res.json()
                if "data" in data and len(data["data"]) > 0:
                    node.embedding = data["data"][0]["embedding"]
                    print(f"📷 Assigned pre-computed VL embedding for scanned node {node.node_id}")
            except Exception as e:
                print(f"⚠️ VL embedding pre-computation failed for node {node.node_id}: {e}")

    # OpenSearch Indexing
    if not OPENSEARCH_USER or not OPENSEARCH_PASSWORD:
        raise RuntimeError("FATAL: OPENSEARCH_USER and OPENSEARCH_PASSWORD must be set.")
        
    index_url = f"{OPENSEARCH_URL}/universal_docs_v1"
    res = requests.get(index_url, auth=(OPENSEARCH_USER, OPENSEARCH_PASSWORD), verify=False, timeout=5)

    is_missing = False
    if res.status_code == 200:
        index_data = res.json().get("universal_docs_v1", {})
        emb_props  = index_data.get("mappings", {}).get("properties", {}).get("embedding", {})
        current_dim    = emb_props.get("dimension")
        if current_dim != DIMENSIONS:
            raise RuntimeError(f"FATAL: Dimension mismatch (current: {current_dim}, target: {DIMENSIONS}).")
    elif res.status_code == 404:
        is_missing = True

    if is_missing:
        mapping = {
            "settings": {"index": {"knn": True}},
            "mappings": {
                "properties": {
                    "embedding": {
                        "type": "knn_vector",
                        "dimension": DIMENSIONS,
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
        requests.put(index_url, json=mapping, auth=(OPENSEARCH_USER, OPENSEARCH_PASSWORD), verify=False, timeout=5)
        print(f"✅ Initialized OpenSearch index 'universal_docs_v1' with FAISS engine (dim={DIMENSIONS})")

    # Storage Context setup
    import redis
    from llama_index.storage.docstore.redis import RedisDocumentStore
    
    valkey_client = redis.Redis.from_url(VALKEY_URL)
    docstore = RedisDocumentStore.from_redis_client(valkey_client, namespace="valkey_docstore")

    os_client = OpensearchVectorClient(
        endpoint=OPENSEARCH_URL,
        index="universal_docs_v1",
        dim=DIMENSIONS,
        http_auth=(OPENSEARCH_USER, OPENSEARCH_PASSWORD),
        use_ssl=True, verify_certs=False,
        search_pipeline="rrf-pipeline",
        method={"name": "hnsw", "engine": "faiss", "space_type": "l2"}
    )
    vector_store = OpensearchVectorStore(client=os_client)
    
    storage_context = StorageContext.from_defaults(
        docstore=docstore,
        vector_store=vector_store
    )
    
    # Store all nodes in Valkey
    storage_context.docstore.add_documents(all_nodes)
    
    # Embed leaf nodes in OpenSearch
    VectorStoreIndex(searchable_leaf_nodes, storage_context=storage_context)

    # Neo4j Graph mapping
    graph_store = Neo4jPropertyGraphStore(
        username=NEO4J_USER, password=NEO4J_PASSWORD, url=NEO4J_URI
    )

    from neo4j import GraphDatabase
    neo4j_driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    
    with neo4j_driver.session() as session:
        doc_node_id = state.get("file_hash") or state.get("filename")
        session.run("""
            MERGE (d:Document {id: $id})
            SET d.name = $filename, d.summary = $summary, d.type = $doc_type
        """, id=doc_node_id, filename=state.get("filename"), summary=state.get("summary"), doc_type=state.get("doc_type"))
        
        # Collect clauses, parent relationships, and document relationships to execute in bulk
        clauses_batch = []
        clause_relationships_batch = []
        doc_relationships_batch = []
        
        for n in all_nodes:
            clauses_batch.append({
                "id": n.node_id,
                "text": n.text[:300]
            })
            
            if NodeRelationship.PARENT in n.relationships:
                parent_id = n.relationships[NodeRelationship.PARENT].node_id
                clause_relationships_batch.append({
                    "pid": parent_id,
                    "cid": n.node_id
                })
            else:
                doc_relationships_batch.append({
                    "did": doc_node_id,
                    "cid": n.node_id
                })
                
        # Execute Neo4j operations in 3 highly efficient bulk statements
        if clauses_batch:
            session.run("""
                UNWIND $clauses AS clause
                MERGE (c:Clause {id: clause.id})
                SET c.text = clause.text
            """, clauses=clauses_batch)
            print(f"   ↳ [Neo4j Bulk] Upserted {len(clauses_batch)} Clause nodes.")
            
        if clause_relationships_batch:
            session.run("""
                UNWIND $relationships AS rel
                MATCH (p:Clause {id: rel.pid})
                MATCH (c:Clause {id: rel.cid})
                MERGE (p)-[:CONTAINS]->(c)
            """, relationships=clause_relationships_batch)
            print(f"   ↳ [Neo4j Bulk] Created {len(clause_relationships_batch)} Clause-to-Clause relationships.")
            
        if doc_relationships_batch:
            session.run("""
                UNWIND $relationships AS rel
                MATCH (d:Document {id: rel.did})
                MATCH (c:Clause {id: rel.cid})
                MERGE (d)-[:CONTAINS]->(c)
            """, relationships=doc_relationships_batch)
            print(f"   ↳ [Neo4j Bulk] Created {len(doc_relationships_batch)} Document-to-Clause relationships.")
                
    # Targeted LLM Extraction Pass
    # TEMPORARILY BYPASSED TO SPEED UP MASS INGESTION
    print("   ↳ [Neo4j] LLM Knowledge Graph Extraction (BYPASSED)")
    # LEGAL_EXTRACTION_PROMPT = ( ... )
    # kg_extractor = SimpleLLMPathExtractor( ... )
    # import asyncio
    # try:
    #     loop = asyncio.get_running_loop()
    # except RuntimeError:
    #     loop = asyncio.new_event_loop()
    #     asyncio.set_event_loop(loop)
    #     
    # PropertyGraphIndex.from_documents(
    #     state["documents"],
    #     property_graph_store=graph_store, 
    #     kg_extractors=[kg_extractor]
    # )

    print("✅ Indexing Complete.")
    return {}

# Build the Graph
ingestion_graph = StateGraph(IngestionState)

ingestion_graph.add_node("structural_splitting_node", structural_splitting_node)
ingestion_graph.add_node("structure_evaluator_node", structure_evaluator_node)
ingestion_graph.add_node("indexing_node", indexing_node)

ingestion_graph.set_entry_point("structural_splitting_node")
ingestion_graph.add_edge("structural_splitting_node", "structure_evaluator_node")
ingestion_graph.add_conditional_edges("structure_evaluator_node", route_validation)
ingestion_graph.add_edge("indexing_node", END)

ingestion_workflow = ingestion_graph.compile()

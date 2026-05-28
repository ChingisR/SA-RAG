import json
from engines.hybrid_search import get_hybrid_rrf_engine

def run_opensearch_engine(query_str: str, top_k: int, top_n: int, user_role: str) -> str:
    engine = get_hybrid_rrf_engine(top_k, top_n, user_role)
    response = engine.query(query_str)
    
    # Inject citation metadata into the response string so LangGraph ToolMessage captures it
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
                sources.append({"filename": filename, "page": str(page), "snippet": snippet})
    
    citation_block = f"\n<!--CITATIONS_JSON:{json.dumps(sources)}-->" if sources else ""
    return str(response) + citation_block

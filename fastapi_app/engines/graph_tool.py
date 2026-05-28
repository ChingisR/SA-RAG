import os
from llama_index.graph_stores.neo4j import Neo4jPropertyGraphStore
from llama_index.core import PropertyGraphIndex, Settings

_graph_engine_singleton = None

def get_graph_engine():
    global _graph_engine_singleton
    if _graph_engine_singleton is None:
        graph_store = Neo4jPropertyGraphStore(username=os.getenv("NEO4J_USER"), password=os.getenv("NEO4J_PASSWORD"), url=os.getenv("NEO4J_URI"))
        index = PropertyGraphIndex.from_existing(property_graph_store=graph_store, llm=Settings.llm)
        _graph_engine_singleton = index.as_query_engine()
    return _graph_engine_singleton

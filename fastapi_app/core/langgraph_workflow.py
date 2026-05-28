"""
LangGraph ReAct Multi-Agent Orchestrator

This module dynamically builds a stateful, multi-agent conversational graph using LangGraph.
It manages an ensemble of specialized sub-agents (Document, Graph, Vision, Web) coordinated
by a central Supervisor router. 

Key features:
- Tool call parsing (supports both raw ReAct and <tool_call> XML structures).
- Language persistence (forces the LLM to reply in the user's initial query language).
- Thread-safe checkpointing (PostgresSaver) for long-running conversational memory.
- Dynamic tool binding based on the authenticated user's tenant_id and role.
"""

import os
import re
import json
import asyncio
from typing import Literal, List

try:
    from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
    from langchain_core.tools import tool
    from langchain_core.runnables import RunnableConfig
    from langchain_openai import ChatOpenAI
    from langgraph.graph import StateGraph, MessagesState, END
    from langgraph.prebuilt import ToolNode
    from langgraph.checkpoint.memory import MemorySaver
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
        _POSTGRES_CHECKPOINTER_AVAILABLE = True
    except ImportError:
        _POSTGRES_CHECKPOINTER_AVAILABLE = False
except ImportError:
    _POSTGRES_CHECKPOINTER_AVAILABLE = False

from core.config import MAIN_MODEL, VLLM_URL, PG_DSN
from engines.hybrid_search import run_opensearch_engine
from engines.sql_engine import execute_hr_sql
from engines.graph_engine import get_graph_engine
from engines.vision_engine import get_vision_engine

# LangGraph compiled workflow cache — keyed by (role, temperature) tuple.
_langgraph_cache: dict = {}

# Global Checkpointer Instance to prevent connection leaking
_global_checkpointer = None

def build_langgraph_workflow(top_k: int, top_n: int, role: str, temperature: float, output_thinking: bool, image_paths: List[str] = None, doc_type: str = None, tenant_id: str = None):
    """
    Factory function to dynamically build or retrieve a cached LangGraph workflow.
    
    The graph is customized per request with user-specific parameters (tenant_id, role),
    ensuring Row-Level Security (RLS) is preserved during tool execution.
    
    Args:
        top_k: Number of initial retrieval chunks.
        top_n: Number of reranked chunks.
        role: User role (e.g., 'admin', 'standard').
        temperature: LLM sampling temperature.
        output_thinking: Whether to return internal <think> logs to the frontend.
        image_paths: Temporary files for Vision capabilities (forces fresh graph compilation).
        doc_type: Optional filter.
        tenant_id: Organization context.
    """
    # ── Singleton cache ── image_paths workflows are always fresh (per-request state)
    cache_key = (role, top_k, top_n, output_thinking, doc_type, tenant_id)
    if not image_paths and cache_key in _langgraph_cache:
        return _langgraph_cache[cache_key]
        
    @tool
    def sql_agent_tool(sql_query: str) -> str:
        """Executes a PostgreSQL query on universal_rag_db. Input must be a valid SQL SELECT query."""
        from engines.sql_engine import execute_hr_sql
        return execute_hr_sql(sql_query, role)

    @tool
    async def document_agent_tool(search_query: str, query_doc_type: str = None, query_tenant_id: str = None) -> str:
        """Searches the OpenSearch unstructured document repository for CVs, policies, or candidate experiences.
        Optional filters:
        - query_doc_type: filter by document type (e.g., 'cv', 'policy', 'contract')
        - query_tenant_id: filter by a specific tenant ID if requested"""
        print(f"!!! TOOL CALL: search_query={repr(search_query)}, doc_type={repr(query_doc_type)}, tenant_id={repr(query_tenant_id)} !!!")
        if not search_query or not search_query.strip():
            return "Error: search_query must not be empty."
        final_doc_type = query_doc_type or doc_type
        final_tenant_id = query_tenant_id or tenant_id
        return await run_opensearch_engine(search_query, top_k, top_n, role, doc_type=final_doc_type, tenant_id=final_tenant_id)
        
    @tool
    async def vision_agent_tool(vision_query: str) -> str:
        """Analyzes an uploaded image, chart, or graph using a specialized multimodal Vision Engine."""
        if not image_paths:
            return "No image is currently uploaded for context."
        engine = get_vision_engine(image_paths)
        resp = await engine.aquery(vision_query)
        return str(resp)

    @tool
    async def graph_agent_tool(relationship_query: str) -> str:
        """Searches the Neo4j Knowledge Graph for entity relationships, obligations, and corporate structures."""
        try:
            resp = await get_graph_engine().aquery(relationship_query)
            return str(resp)
        except Exception as e:
            return f"Graph query failed: {e}"

    @tool
    def web_search_tool(search_query: str) -> str:
        """Searches the public internet for current events, news, or information not found in the corporate document repository."""
        import mcp_server
        return mcp_server.public_web_search(search_query)

    # Pass chat_template_kwargs to vLLM 0.19.0 via langchain's extra_body parameter.
    # Using extra_body is the correct way — passing it as a top-level kwarg causes:
    #   TypeError: Completions.create() got an unexpected keyword argument 'chat_template_kwargs'
    # enable_thinking=False skips Qwen3's <think> block for the router (fast path).
    # Reduced max_tokens (1024 for agent, 32 for router) to limit KV cache pressure.
    llm = ChatOpenAI(
        model=MAIN_MODEL, base_url=VLLM_URL, api_key="placeholder",
        max_tokens=1024, temperature=temperature, streaming=True,
        timeout=300.0, request_timeout=300.0,
        model_kwargs={"extra_body": {"chat_template_kwargs": {"enable_thinking": bool(output_thinking)}}},
    )
    # Dedicated non-streaming LLM for the supervisor router — short max_tokens and
    # thinking disabled to get a fast routing decision (avoids 45s <think> overhead).
    router_llm = ChatOpenAI(
        model=MAIN_MODEL, base_url=VLLM_URL, api_key="placeholder",
        max_tokens=32, temperature=0.0, streaming=False, timeout=60.0,
        model_kwargs={"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}},
    )
    
    # Tool Nodes
    sql_tool_node = ToolNode([sql_agent_tool])
    doc_tool_node = ToolNode([document_agent_tool])
    vis_tool_node = ToolNode([vision_agent_tool])
    graph_tool_node = ToolNode([graph_agent_tool])
    web_tool_node = ToolNode([web_search_tool])

    think_instruction = (
        " You MUST wrap any internal reasoning inside <think>...</think> tags before your final response if you need to reason."
        " CRITICAL: You MUST answer the user in the EXACT same language as their initial query (e.g. if the user asks in Russian, your final response must be in Russian; if in Kazakh, in Kazakh). Keep the final answer in the user's language even when self-correcting or reflecting on supervisor feedback."
        if output_thinking else 
        " CRITICAL: DO NOT use <think> tags. Do not output 'Thinking Process:'. Answer directly."
        " CRITICAL: You MUST answer the user in the EXACT same language as their initial query (e.g. if the user asks in Russian, your final response must be in Russian; if in Kazakh, in Kazakh). Keep the final answer in the user's language even when self-correcting or reflecting on supervisor feedback."
    )

    def parse_react_tool_call(response, expected_tool_name: str, expected_arg_name: str):
        if not getattr(response, "tool_calls", None):
            content_str = str(response.content)
            try:
                # 1. Parse ReAct format
                if "Action:" in content_str:
                    action_match = re.search(r"Action:\s*(\w+)", content_str)
                    input_match = re.search(r"Action Input:\s*(\{.*?\})", content_str, re.DOTALL)
                    if action_match and input_match:
                        tool_args = json.loads(input_match.group(1).strip())
                        if "input" in tool_args and expected_arg_name != "input":
                            val = tool_args.pop("input")
                            tool_args[expected_arg_name] = val
                        elif expected_arg_name not in tool_args and len(tool_args) > 0:
                            first_key = list(tool_args.keys())[0]
                            val = tool_args.pop(first_key)
                            tool_args[expected_arg_name] = val
                        response.tool_calls = [{"name": expected_tool_name, "args": tool_args, "id": f"call_{expected_tool_name}"}]
                # 2. Parse XML format
                elif "<tool_call>" in content_str:
                    param_match = re.search(r"<parameter=[^>]+>(.*?)<\/parameter>", content_str, re.DOTALL)
                    if param_match:
                        tool_args = {expected_arg_name: param_match.group(1).strip()}
                        response.tool_calls = [{"name": expected_tool_name, "args": tool_args, "id": f"call_{expected_tool_name}"}]
            except Exception:
                pass
        return response

    # Agent Nodes
    def conversational_agent(state: MessagesState, config: RunnableConfig):
        sys_msg = SystemMessage(content=(
            "You are a helpful HR and Legal AI Assistant for KMG Kashagan B.V. The user asked a conversational or small-talk question. Answer it politely and briefly directly. If they asked what you can do, tell them you can search knowledge graphs, HR databases, and enterprise documents. Do NOT use any tools."
            " CRITICAL: You MUST answer the user in the EXACT same language as their initial query (e.g. if the user asks in Russian, your final response must be in Russian; if in Kazakh, in Kazakh). DO NOT translate or answer in English."
        ))
        response = llm.invoke([sys_msg] + state["messages"], config=config)
        return {"messages": [response]}

    def sql_agent(state: MessagesState, config: RunnableConfig):
        sys_msg = SystemMessage(content="You are the SQL Agent. You must generate queries for the enterprise_assets or hr_employees tables." + think_instruction)
        response = llm.bind_tools([sql_agent_tool]).invoke([sys_msg] + state["messages"], config=config)
        return {"messages": [parse_react_tool_call(response, "sql_agent_tool", "sql_query")]}

    def document_agent(state: MessagesState, config: RunnableConfig):
        sys_msg = SystemMessage(content="You are the Document Search Agent. Search OpenSearch for files." + think_instruction)
        response = llm.bind_tools([document_agent_tool]).invoke([sys_msg] + state["messages"], config=config)
        return {"messages": [parse_react_tool_call(response, "document_agent_tool", "search_query")]}
        
    def vision_agent(state: MessagesState, config: RunnableConfig):
        sys_msg = SystemMessage(content="You are the Vision Agent. You analyze images and charts." + think_instruction)
        response = llm.bind_tools([vision_agent_tool]).invoke([sys_msg] + state["messages"], config=config)
        return {"messages": [parse_react_tool_call(response, "vision_agent_tool", "vision_query")]}

    def graph_agent(state: MessagesState, config: RunnableConfig):
        sys_msg = SystemMessage(content="You are the Knowledge Graph Agent. Find relationship paths via Neo4j." + think_instruction)
        response = llm.bind_tools([graph_agent_tool]).invoke([sys_msg] + state["messages"], config=config)
        return {"messages": [parse_react_tool_call(response, "graph_agent_tool", "relationship_query")]}

    def web_agent(state: MessagesState, config: RunnableConfig):
        sys_msg = SystemMessage(content="You are the Web Search Agent. You search the public internet for news, current events, and external knowledge." + think_instruction)
        response = llm.bind_tools([web_search_tool]).invoke([sys_msg] + state["messages"], config=config)
        return {"messages": [parse_react_tool_call(response, "web_search_tool", "search_query")]}

    def evaluation_agent(state: MessagesState, config: RunnableConfig):
        sys_msg = SystemMessage(content="You are the Evaluator. Review the last AI message. If it directly and fully answers the user's initial query without errors, reply 'YES'. If it fails, errors out, or asks the user for more info it couldn't find, reply 'NO - [reason]'. Keep your reason strictly technical so the agent knows what tool to try next or how to fix its arguments.")
        response = llm.invoke([sys_msg] + state["messages"], config=config)
        
        reply_content = str(response.content)
        if reply_content.strip().startswith("NO"):
            feedback = HumanMessage(content=f"CRITICAL FEEDBACK FROM SUPERVISOR: Your previous action failed to answer the user fully. Evaluation: {reply_content}. You MUST self-correct: pick a different tool, restructure your SQL/Graph arguments, or try a different search strategy. IMPORTANT: You MUST respond in the EXACT same language as the user's initial query (e.g. Russian or Kazakh) - DO NOT translate or answer in English.")
            return {"messages": [feedback]}
        
        return {"messages": [response]}

    # Routers
    def sql_router(state: MessagesState) -> Literal["sql_tools", "Evaluation_Agent"]:
        return "sql_tools" if state["messages"][-1].tool_calls else "Evaluation_Agent"
        
    def doc_router(state: MessagesState) -> Literal["doc_tools", "Evaluation_Agent"]:
        return "doc_tools" if state["messages"][-1].tool_calls else "Evaluation_Agent"
        
    def vis_router(state: MessagesState) -> Literal["vis_tools", "Evaluation_Agent"]:
        return "vis_tools" if state["messages"][-1].tool_calls else "Evaluation_Agent"
        
    def graph_router(state: MessagesState) -> Literal["graph_tools", "Evaluation_Agent"]:
        return "graph_tools" if state["messages"][-1].tool_calls else "Evaluation_Agent"

    def web_router(state: MessagesState) -> Literal["web_tools", "Evaluation_Agent"]:
        return "web_tools" if state["messages"][-1].tool_calls else "Evaluation_Agent"

    def evaluation_router(state: MessagesState) -> Literal["Supervisor", "__end__"]:
        if len(state["messages"]) > 6:
            return "__end__" # Prevent infinite looping
        last_msg = state["messages"][-1].content
        if "YES" in last_msg[:10]:
            return "__end__"
        return "Supervisor"

    def supervisor_node(state: MessagesState, config: RunnableConfig):
        return {}

    # Supervisor Router — uses router_llm (non-streaming, short timeout)
    # Must remain a sync function: LangGraph calls conditional edges synchronously.
    def supervisor(state: MessagesState, config: RunnableConfig = None) -> Literal["SQL_Agent", "Document_Agent", "Vision_Agent", "Graph_Agent", "Parallel_Branch", "Web_Agent", "Conversational_Agent"]:
        """
        The central brain of the LangGraph network.
        
        Evaluates the user's entire conversation context and selects the single most appropriate
        sub-agent to handle the task. Uses a lightweight LLM configuration to minimize latency.
        """
        sys_msg = SystemMessage(content=(
            "You are the global Supervisor Router.\n"
            "Read the user request and reply with EXACTLY ONE of these labels:\n"
            "  'Conversational_Agent' — greetings, 'hello', 'what can you do?', general small-talk chat\n"
            "  'Graph_Agent'     — entity relationships, corporate structures, org charts\n"
            "  'Vision_Agent'    — image analysis, charts, photos, diagrams\n"
            "  'Parallel_Branch' — complex questions needing BOTH documents AND relationships\n"
            "  'Web_Agent'       — current events, public information, external research\n"
            "  'Document_Agent'  — salary, headcount, tabular database queries, asset details, and all other general business document questions\n"
            "Reply with ONLY the label, nothing else."
        ))
        try:
            response = router_llm.invoke([sys_msg] + state["messages"])
            route = (response.content or "").strip().strip("'\"")
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"Supervisor router failed ({e}), defaulting to Document_Agent")
            route = ""
        if not route:
            return "Document_Agent"
        if "Conversation" in route: return "Conversational_Agent"
        if "hello" in route.lower(): return "Conversational_Agent"
        if "Parallel" in route: return "Parallel_Branch"
        if "Graph" in route:    return "Graph_Agent"
        if "SQL" in route:      return "Document_Agent"  # Redirected for grounding
        if "Vision" in route:   return "Vision_Agent"
        if "Web" in route:      return "Web_Agent"
        return "Document_Agent"

    async def parallel_branch(state: MessagesState, config: RunnableConfig):
        doc_sys = SystemMessage(content="You are the Document Search Agent. Search OpenSearch for relevant business documents and summarize findings directly." + think_instruction)
        grp_sys = SystemMessage(content="You are the Knowledge Graph Agent. Find entity relationships via Neo4j and return direct answers." + think_instruction)

        async def run_doc():
            _llm = ChatOpenAI(model=MAIN_MODEL, base_url=VLLM_URL, api_key="placeholder", max_tokens=2048, temperature=temperature, streaming=False, timeout=300.0, request_timeout=300.0, extra_body={"chat_template_kwargs": {"enable_thinking": bool(output_thinking)}})
            return await _llm.bind_tools([document_agent_tool]).ainvoke([doc_sys] + state["messages"], config=config)

        async def run_graph():
            _llm = ChatOpenAI(model=MAIN_MODEL, base_url=VLLM_URL, api_key="placeholder", max_tokens=2048, temperature=temperature, streaming=False, timeout=300.0, request_timeout=300.0, extra_body={"chat_template_kwargs": {"enable_thinking": bool(output_thinking)}})
            return await _llm.bind_tools([graph_agent_tool]).ainvoke([grp_sys] + state["messages"], config=config)

        doc_resp, graph_resp = await asyncio.gather(run_doc(), run_graph())

        merged = HumanMessage(content=(
            f"[Document Search Result]\n{doc_resp.content}\n\n"
            f"[Knowledge Graph Result]\n{graph_resp.content}"
        ))
        return {"messages": [merged]}

    # Graph
    workflow = StateGraph(MessagesState)
    workflow.add_node("Supervisor", supervisor_node)
    workflow.add_node("Conversational_Agent", conversational_agent)
    workflow.add_node("SQL_Agent", sql_agent)
    workflow.add_node("Document_Agent", document_agent)
    workflow.add_node("Vision_Agent", vision_agent)
    workflow.add_node("Graph_Agent", graph_agent)
    workflow.add_node("Web_Agent", web_agent)
    workflow.add_node("Parallel_Branch", parallel_branch)
    workflow.add_node("Evaluation_Agent", evaluation_agent)
    workflow.add_node("sql_tools", sql_tool_node)
    workflow.add_node("doc_tools", doc_tool_node)
    workflow.add_node("vis_tools", vis_tool_node)
    workflow.add_node("graph_tools", graph_tool_node)
    workflow.add_node("web_tools", web_tool_node)

    workflow.set_entry_point("Supervisor")

    # Routing Configuration
    workflow.add_conditional_edges("Supervisor", supervisor, {
        "Conversational_Agent": "Conversational_Agent",
        "SQL_Agent": "SQL_Agent",
        "Document_Agent": "Document_Agent",
        "Vision_Agent": "Vision_Agent",
        "Graph_Agent": "Graph_Agent",
        "Web_Agent": "Web_Agent",
        "Parallel_Branch": "Parallel_Branch",
    })

    workflow.add_edge("Conversational_Agent", "__end__")

    workflow.add_conditional_edges("SQL_Agent", sql_router)
    workflow.add_edge("sql_tools", "SQL_Agent")

    workflow.add_conditional_edges("Document_Agent", doc_router)
    workflow.add_edge("doc_tools", "Document_Agent")

    workflow.add_conditional_edges("Vision_Agent", vis_router)
    workflow.add_edge("vis_tools", "Vision_Agent")

    workflow.add_conditional_edges("Graph_Agent", graph_router)
    workflow.add_edge("graph_tools", "Graph_Agent")
    
    workflow.add_conditional_edges("Web_Agent", web_router)
    workflow.add_edge("web_tools", "Web_Agent")

    workflow.add_edge("Parallel_Branch", "Evaluation_Agent")
    workflow.add_conditional_edges("Evaluation_Agent", evaluation_router)

    global _global_checkpointer
    if _global_checkpointer is None:
        _global_checkpointer = MemorySaver()
        if _POSTGRES_CHECKPOINTER_AVAILABLE:
            try:
                from psycopg_pool import ConnectionPool
                pg_dsn_sync = PG_DSN.replace("postgresql://", "postgresql+psycopg://") if PG_DSN else None
                if pg_dsn_sync:
                    pool = ConnectionPool(
                        conninfo=pg_dsn_sync,
                        max_size=20,
                        kwargs={"autocommit": True}
                    )
                    from langgraph.checkpoint.postgres import PostgresSaver
                    _global_checkpointer = PostgresSaver(pool)
                    _global_checkpointer.setup()
                    print("✅ LangGraph: Using PostgresSaver (ConnectionPool) for persistent memory.")
            except Exception as pg_e:
                print(f"⚠️ LangGraph: Postgres checkpointer unavailable ({pg_e}), falling back to MemorySaver.")

    compiled = workflow.compile(checkpointer=_global_checkpointer)
    if not image_paths:
        if len(_langgraph_cache) >= 30:
            _langgraph_cache.pop(next(iter(_langgraph_cache)))
        _langgraph_cache[cache_key] = compiled
    return compiled

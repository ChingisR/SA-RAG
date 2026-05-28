"""
Core configuration module for the SA-RAG backend.

This module loads environment variables, sets up endpoint URLs,
and configures the default LlamaIndex global settings for:
1. Embedding Models (served by Infinity/Custom API)
2. Main Generative LLMs (served by vLLM)
3. MultiModal LLMs
"""

import os
from llama_index.core import Settings
from llama_index.embeddings.openai import OpenAIEmbedding
from llama_index.llms.openai_like import OpenAILike

# ---------------------------------------------------------
# Environment Variables & Core Settings
# ---------------------------------------------------------
# GPU_NODE_IP is the IP address of the remote inference server.
GPU_NODE_IP      = os.getenv("GPU_NODE_IP", "172.18.0.1")
# VLLM_BASE_URL points to the vLLM completion endpoints.
VLLM_URL         = os.getenv("VLLM_BASE_URL", f"http://{GPU_NODE_IP}:8081/v1")
# OLLAMA_URL is the local/remote Ollama instance endpoint.
OLLAMA_URL       = os.getenv("OLLAMA_URL", "http://ollama:11434")
# INFINITY_EMBED_URL points to the custom Infinity/Qwen FP8 API.
INFINITY_EMBED_URL = os.getenv("INFINITY_EMBED_URL", f"http://{GPU_NODE_IP}:8082/v1")

# PostgreSQL Data Source Name for the relational database.
PG_DSN           = os.getenv("POSTGRES_URL")
if not PG_DSN:
    raise RuntimeError("FATAL: POSTGRES_URL environment variable is not set. Refusing to start.")

# Model definitions
MAIN_MODEL       = os.getenv("MAIN_MODEL", "Qwen/Qwen3.5-27B-FP8")
EMBED_MODEL      = os.getenv("EMBED_MODEL", "Qwen/Qwen3-VL-Embedding-8B")
DIMENSIONS       = int(os.getenv("EMBED_DIM", "4096"))
CACHE_DIM        = 1536

# ---------------------------------------------------------
# LlamaIndex Global Settings Configurations
# ---------------------------------------------------------

# 1. Embedding model — served by Infinity (OpenAI compatible)
# Configured with a 600s timeout to handle potentially large batch requests.
Settings.embed_model = OpenAIEmbedding(
    model_name=EMBED_MODEL,
    api_base=INFINITY_EMBED_URL,
    api_key="none",  # API Key is mocked as it is internally routed
    max_retries=0,
    timeout=600.0,
)

# 2. Main LLM — vLLM serving Qwen3.5-27B-FP8 on GPU Node
# This is the primary reasoning engine for the ReAct/LangGraph agents.
Settings.llm = OpenAILike(
    model=MAIN_MODEL,
    api_base=VLLM_URL,
    api_key="placeholder",
    is_chat_model=True,
    is_function_calling_model=True,
    max_tokens=1024,
    context_window=32768,  # Configured for 32K context window
    timeout=120.0,
    # Stop words are crucial to prevent the model from hallucinating tool outputs
    additional_kwargs={"stop": ["<|im_end|>", "Observation:", "\nObservation:", "\n\nObservation:"]},
    # System prompt heavily constrains the model to prevent verbose reasoning (<think>) leaking
    system_prompt="You are a strict data-execution Agent. CRITICAL: DO NOT use <think> tags. DO NOT output 'Thinking Process:'. Answer directly and adhere strictly to outputting the final SQL schema, Action, or requested data without any prior narrative or reasoning blocks."
)

# 3. MultiModal LLM — Same vLLM endpoint, but configured without function calling.
# Used specifically for evaluating images/visual elements where tool calling isn't needed.
mm_llm = OpenAILike(
    model=MAIN_MODEL,
    api_base=VLLM_URL,
    api_key="placeholder",
    is_chat_model=True,
    is_function_calling_model=False,
    max_tokens=1024,
    context_window=32768,
    timeout=120.0,
)

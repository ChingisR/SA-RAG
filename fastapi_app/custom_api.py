"""
Qwen3-VL Custom Embedding & Reranker Server v2.0
- Embedding: Qwen3-VL-Embedding-8B  (text + image multimodal)
- Reranker:  Qwen3-VL-Reranker-8B   (text + image multimodal)
- Port: 8082
- Device: CPU by default (change DEVICE to "cuda" when MIG slice is available)
"""

import io
import base64
import torch
import torch.nn.functional as F
import asyncio
from fastapi import FastAPI
from pydantic import BaseModel
from typing import List, Optional, Union
import uvicorn
from PIL import Image
from transformers import AutoModelForCausalLM, AutoTokenizer, AutoModel, AutoProcessor

app = FastAPI(title="Qwen3-VL Custom Server", version="2.0")

# ──────────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────────
EMBED_MODEL_NAME  = "Qwen/Qwen3-VL-Embedding-8B"
RERANK_MODEL_NAME = "Qwen/Qwen3-VL-Reranker-8B"
import os
DEVICE = os.getenv("EMBED_DEVICE", "cuda")

# High-concurrency limit
GPU_SEMAPHORE = asyncio.Semaphore(16)

# ──────────────────────────────────────────────────────────────────────────────
# Load Embedding Model
# ──────────────────────────────────────────────────────────────────────────────
print(f"Loading embedding model: {EMBED_MODEL_NAME}...")
try:
    embed_processor = AutoProcessor.from_pretrained(EMBED_MODEL_NAME, trust_remote_code=True)
    print("✅ AutoProcessor loaded for embedding.")
except Exception as e:
    embed_processor = None
    print(f"⚠️  AutoProcessor not available: {e}. Will use tokenizer fallback.")

embed_tokenizer = AutoTokenizer.from_pretrained(EMBED_MODEL_NAME, trust_remote_code=True)
try:
    embed_model = AutoModel.from_pretrained(
        EMBED_MODEL_NAME, trust_remote_code=True,
        device_map=DEVICE, torch_dtype=torch.bfloat16
    )
except Exception as e:
    print(f"AutoModel failed ({e}), falling back to AutoModelForCausalLM...")
    embed_model = AutoModelForCausalLM.from_pretrained(
        EMBED_MODEL_NAME, trust_remote_code=True,
        device_map=DEVICE, torch_dtype=torch.bfloat16
    )
embed_model.eval()
print(f"✅ Embedding model ready.")

# ──────────────────────────────────────────────────────────────────────────────
# Load Reranker Model (upgraded to 8B)
# ──────────────────────────────────────────────────────────────────────────────
print(f"Loading reranker model: {RERANK_MODEL_NAME}...")
try:
    rerank_processor = AutoProcessor.from_pretrained(RERANK_MODEL_NAME, trust_remote_code=True)
    print("✅ AutoProcessor loaded for reranker.")
except Exception as e:
    rerank_processor = None
    print(f"⚠️  Reranker AutoProcessor not available: {e}.")

rerank_tokenizer = AutoTokenizer.from_pretrained(RERANK_MODEL_NAME, trust_remote_code=True)
try:
    rerank_model = AutoModelForCausalLM.from_pretrained(
        RERANK_MODEL_NAME, trust_remote_code=True,
        device_map="cpu", torch_dtype=torch.bfloat16
    )
except Exception as e:
    print(f"AutoModelForCausalLM reranker failed ({e}), trying AutoModel...")
    rerank_model = AutoModel.from_pretrained(
        RERANK_MODEL_NAME, trust_remote_code=True,
        device_map="cpu", torch_dtype=torch.bfloat16
    )
rerank_model.eval()
print(f"✅ Reranker model ready.")

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────
def decode_image(b64_str: str) -> Image.Image:
    """Decode a base64-encoded image string into a PIL Image."""
    img_bytes = base64.b64decode(b64_str)
    return Image.open(io.BytesIO(img_bytes)).convert("RGB")


def _mean_pool(hidden: torch.Tensor, attention_mask: torch.Tensor) -> torch.Tensor:
    """Mean pool hidden states, masked by attention."""
    mask = attention_mask.unsqueeze(-1).expand(hidden.size()).float()
    return torch.sum(hidden * mask, 1) / torch.clamp(mask.sum(1), min=1e-9)


def _get_hidden(outputs, encoded) -> torch.Tensor:
    """Extract last-layer hidden states from model output."""
    if hasattr(outputs, "hidden_states") and outputs.hidden_states:
        return outputs.hidden_states[-1]
    if hasattr(outputs, "last_hidden_state"):
        return outputs.last_hidden_state
    return outputs[0]


def embed_single(text: str, image_b64: Optional[str]) -> torch.Tensor:
    """
    Embed one text+image pair.
    - If an image is provided and AutoProcessor is available → VL (multimodal) path
    - Otherwise → text-only path
    """
    import os
    target_dim = int(os.environ.get("EMBED_DIM", "4096"))

    if image_b64 and embed_processor:
        try:
            pil_image = decode_image(image_b64)
            inputs = embed_processor(
                text=[text or ""],
                images=[pil_image],
                return_tensors="pt",
                padding=True,
                truncation=True,
            )
            with torch.no_grad():
                inputs = {k: v.to(DEVICE) if isinstance(v, torch.Tensor) else v for k, v in inputs.items()}
                outputs = embed_model(**inputs, output_hidden_states=True)
                hidden = _get_hidden(outputs, inputs)
                
                pooled = hidden[:, -1, :] 
                if target_dim < pooled.shape[1]:
                    pooled = pooled[:, :target_dim]
                    
                return F.normalize(pooled, p=2, dim=1).squeeze(0)
        except Exception as e:
            print(f"⚠️  VL embedding failed, falling back to text-only: {e}")

    # Text-only fallback
    encoded = embed_tokenizer(
        [text or ""], return_tensors="pt", padding=True, truncation=True
    )
    with torch.no_grad():
        encoded = {k: v.to(DEVICE) if isinstance(v, torch.Tensor) else v for k, v in encoded.items()}
        outputs = embed_model(**encoded, output_hidden_states=True)
        hidden = _get_hidden(outputs, encoded)
        pooled = _mean_pool(hidden, encoded["attention_mask"])
        
        if target_dim < pooled.shape[1]:
            pooled = pooled[:, :target_dim]
            
        return F.normalize(pooled, p=2, dim=1).squeeze(0)



# ──────────────────────────────────────────────────────────────────────────────
# Embedding Endpoint
# ──────────────────────────────────────────────────────────────────────────────
class EmbedRequest(BaseModel):
    input: Union[str, List[str]]
    images: Optional[List[Optional[str]]] = None  # base64 PNG/JPEG, one per text input
    model: str = "default"
    encoding_format: str = "float"


async def _safe_embed_single(text: str, img: Optional[str]):
    async with GPU_SEMAPHORE:
        return await asyncio.to_thread(embed_single, text, img)

@app.post("/v1/embeddings")
async def create_embeddings(req: EmbedRequest):
    texts = req.input if isinstance(req.input, list) else [req.input]
    images = list(req.images) if req.images else []
    # Pad images list to match texts length
    while len(images) < len(texts):
        images.append(None)

    all_embeddings = await asyncio.gather(
        *[_safe_embed_single(text, img) for text, img in zip(texts, images)]
    )
    embeddings = torch.stack(all_embeddings)

    return {
        "object": "list",
        "data": [
            {"object": "embedding", "index": i, "embedding": emb.tolist()}
            for i, emb in enumerate(embeddings)
        ],
        "model": req.model,
        "usage": {"prompt_tokens": len(texts), "total_tokens": len(texts)},
    }


# ──────────────────────────────────────────────────────────────────────────────
# Reranker Endpoint
# ──────────────────────────────────────────────────────────────────────────────
class RerankRequest(BaseModel):
    query: str
    query_image: Optional[str] = None       # base64 image of the visual query (optional)
    texts: Optional[List[str]] = None
    documents: Optional[List[str]] = None
    doc_images: Optional[List[Optional[str]]] = None  # one base64 image per document
    top_n: Optional[int] = 5


def rerank_single(query: str, text: str, doc_image_b64: Optional[str]) -> float:
    """Score one (query, document) pair. Optionally include page image."""
    prompt = (
        f"Query: {query}\n"
        f"Document: {text}\n"
        f"Is this document highly relevant to the query? Answer Yes or No:"
    )

    if doc_image_b64 and rerank_processor:
        try:
            pil_image = decode_image(doc_image_b64)
            inputs = rerank_processor(
                text=[prompt], images=[pil_image], return_tensors="pt"
            )
            with torch.no_grad():
                inputs = {k: v.to("cpu") if isinstance(v, torch.Tensor) else v for k, v in inputs.items()}
                outputs = rerank_model(**inputs)
                logits = outputs.logits[0, -1, :]
                yes_id = rerank_tokenizer.convert_tokens_to_ids("Yes")
                no_id  = rerank_tokenizer.convert_tokens_to_ids("No")
                if yes_id and no_id:
                    probs = F.softmax(torch.tensor([logits[yes_id], logits[no_id]]), dim=0)
                    return probs[0].item()
        except Exception as e:
            print(f"⚠️  VL rerank failed, falling back to text-only: {e}")

    # Text-only fallback
    encoded = rerank_tokenizer(prompt, return_tensors="pt")
    with torch.no_grad():
        encoded = {k: v.to("cpu") if isinstance(v, torch.Tensor) else v for k, v in encoded.items()}
        outputs = rerank_model(**encoded)
        logits = (
            outputs.logits[0, -1, :]
            if hasattr(outputs, "logits")
            else outputs[0][0, -1, :]
        )
        yes_id = rerank_tokenizer.convert_tokens_to_ids("Yes")
        no_id  = rerank_tokenizer.convert_tokens_to_ids("No")
        if yes_id and no_id:
            probs = F.softmax(torch.tensor([logits[yes_id], logits[no_id]]), dim=0)
            return probs[0].item()
    return 0.5


async def _safe_rerank_single(query: str, text: str, img: Optional[str]):
    async with GPU_SEMAPHORE:
        return await asyncio.to_thread(rerank_single, query, text, img)

@app.post("/v1/rerank")
@app.post("/rerank")
async def create_rerank(req: RerankRequest):
    docs = req.texts if req.texts is not None else req.documents
    if not docs:
        return {"results": []}

    doc_images = list(req.doc_images) if req.doc_images else []
    while len(doc_images) < len(docs):
        doc_images.append(None)

    scores = await asyncio.gather(
        *[_safe_rerank_single(req.query, text, img) for text, img in zip(docs, doc_images)]
    )
    
    results = [
        {"index": i, "relevance_score": score}
        for i, score in enumerate(scores)
    ]
    results.sort(key=lambda x: x["relevance_score"], reverse=True)
    return {"results": results[: req.top_n], "model": RERANK_MODEL_NAME}


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "embed_model": EMBED_MODEL_NAME,
        "rerank_model": RERANK_MODEL_NAME,
        "device": DEVICE,
        "vl_embedding": embed_processor is not None,
        "vl_reranking": rerank_processor is not None,
    }


if __name__ == "__main__":
    print("🚀 Starting Qwen3-VL Custom Server on port 8082...")
    uvicorn.run(app, host="0.0.0.0", port=8082)

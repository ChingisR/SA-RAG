from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from fastapi.responses import FileResponse, StreamingResponse
import io
import os
import shutil
import requests
import tempfile
from core.security import get_current_user
from worker import process_document_task

router = APIRouter(tags=["documents"])

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "50"))

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".xls", ".pptx", ".txt", ".md", ".csv", ".jpg", ".jpeg", ".png"}

@router.get("/documents")
async def list_documents(current_user: dict = Depends(get_current_user)):
    """List all documents indexed in OpenSearch with their metadata."""
    os_url  = os.getenv("OPENSEARCH_URL", "https://opensearch:9200")
    os_user = os.getenv("OPENSEARCH_USER")
    os_pass = os.getenv("OPENSEARCH_PASSWORD")
    if not os_user or not os_pass:
        raise HTTPException(status_code=500, detail="OpenSearch credentials not configured.")
    try:
        body = {
            "size": 0,
            "aggs": {
                "by_filename": {
                    "terms": {"field": "metadata.filename.keyword", "size": 500},
                    "aggs": {
                        "doc_type":    {"terms": {"field": "metadata.document_type.keyword", "size": 1}},
                        "doc_summary": {"terms": {"field": "metadata.document_summary.keyword", "size": 1}},
                    }
                }
            }
        }
        resp = requests.post(
            f"{os_url}/universal_docs_v1/_search",
            json=body, auth=(os_user, os_pass), verify=False, timeout=10
        )
        resp.raise_for_status()
        buckets = resp.json().get("aggregations", {}).get("by_filename", {}).get("buckets", [])
        docs = []
        for b in buckets:
            filename = b["key"]
            chunk_count = b["doc_count"]
            doc_type    = b["doc_type"]["buckets"][0]["key"] if b["doc_type"]["buckets"] else "Unknown"
            summary     = b["doc_summary"]["buckets"][0]["key"] if b["doc_summary"]["buckets"] else ""
            docs.append({"filename": filename, "chunks": chunk_count, "document_type": doc_type, "summary": summary})
        return {"documents": docs, "total": len(docs)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OpenSearch error: {str(e)}")

@router.delete("/documents/{filename}")
async def delete_document(filename: str, current_user: dict = Depends(get_current_user)):
    """Delete all chunks of a specific document from OpenSearch by filename."""
    user_role = current_user.get("role", "standard")
    if user_role not in ("Operations_Admin", "admin"):
        raise HTTPException(status_code=403, detail="Only admins can delete documents.")
    os_url  = os.getenv("OPENSEARCH_URL", "https://opensearch:9200")
    os_user = os.getenv("OPENSEARCH_USER")
    os_pass = os.getenv("OPENSEARCH_PASSWORD")
    if not os_user or not os_pass:
        raise HTTPException(status_code=500, detail="OpenSearch credentials not configured.")
    try:
        body = {"query": {"term": {"metadata.filename.keyword": filename}}}
        resp = requests.post(
            f"{os_url}/universal_docs_v1/_delete_by_query",
            json=body, auth=(os_user, os_pass), verify=False, timeout=120
        )
        resp.raise_for_status()
        deleted = resp.json().get("deleted", 0)
        return {"status": "success", "deleted_chunks": deleted, "filename": filename}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Delete failed: {str(e)}")

@router.get("/files/{filename}")
async def serve_file(filename: str, current_user: dict = Depends(get_current_user)):
    """Serve uploaded PDF files securely."""
    base_dir = os.path.realpath("data")
    # Sanitize inputs
    safe_filename = os.path.basename(filename)
    file_path = os.path.realpath(os.path.join(base_dir, safe_filename))
    
    # Path traversal guard
    if not file_path.startswith(base_dir):
        raise HTTPException(status_code=403, detail="Invalid file path.")

    if os.path.exists(file_path):
        return FileResponse(file_path, media_type="application/pdf")
    raise HTTPException(status_code=404, detail=f"File '{safe_filename}' not found.")

@router.post("/upload-pdf")
async def upload_pdf(
    file: UploadFile = File(...),
    sync: bool = Query(False),
    file_hash: str = Query(""),
    current_user: dict = Depends(get_current_user)
):
    import time
    
    # --- Validation ---
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type '{ext}'. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
        )

    # Sanitize physical filename to prevent collisions on disk
    base_name = os.path.basename(file.filename).replace("..", "")
    physical_safe_name = f"{int(time.time())}_{base_name}"

    # Read content
    contents = await file.read()

    file_location = f"data/{physical_safe_name}"
    with open(file_location, "wb") as buffer:
        buffer.write(contents)

    # Pass physical_safe_name to Celery so it saves as a uniquely named file in OpenSearch!
    if sync:
        process_document_task(file_location, physical_safe_name, file_hash)
        return {"status": "success", "message": f"Processed {physical_safe_name} synchronously."}
    else:
        task = process_document_task.delay(file_location, physical_safe_name, file_hash)
    return {"status": "queued", "task_id": task.id, "message": f"File {physical_safe_name} sent to background."}

@router.post("/append-document")
async def append_document(
    target_filename: str = Query(..., description="The exact existing filename in OpenSearch you want to append chunks to."),
    file: UploadFile = File(...),
    sync: bool = Query(False),
    file_hash: str = Query(""),
    current_user: dict = Depends(get_current_user)
):
    import time
    
    # --- Validation ---
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type '{ext}'. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
        )

    # Sanitize physical filename to prevent collisions while preserving the target logical filename
    base_name = os.path.basename(file.filename).replace("..", "")
    physical_safe_name = f"{int(time.time())}_{base_name}"

    # Read content
    contents = await file.read()

    file_location = f"data/{physical_safe_name}"
    with open(file_location, "wb") as buffer:
        buffer.write(contents)

    # CRITICAL FIX: Pass target_filename as the logical filename to the Celery worker
    # This guarantees the new vectors are seamlessly injected into the existing OpenSearch footprint!
    if sync:
        process_document_task(file_location, target_filename, file_hash)
        return {"status": "success", "message": f"Appended new pages to {target_filename} synchronously."}
    else:
        task = process_document_task.delay(file_location, target_filename, file_hash)
    return {"status": "queued", "task_id": task.id, "message": f"Appending new pages to {target_filename} in the background."}

@router.get("/task-status/{task_id}")
async def get_task_status(task_id: str, current_user: dict = Depends(get_current_user)):
    """Poll the status of a background document ingestion task."""
    from celery.result import AsyncResult
    result = AsyncResult(task_id, app=process_document_task.app)
    if result.state == "PENDING":
        return {"task_id": task_id, "status": "pending", "detail": "Task is queued or unknown."}
    elif result.state == "SUCCESS":
        return {"task_id": task_id, "status": "success", "detail": "Document indexed successfully."}
    elif result.state == "FAILURE":
        return {"task_id": task_id, "status": "failure", "detail": str(result.info)}
    else:
        return {"task_id": task_id, "status": result.state.lower(), "detail": "Task is running."}

@router.post("/upload-image")
async def upload_image(file: UploadFile = File(...), current_user: dict = Depends(get_current_user)):
    file_location = f"images/{file.filename}"
    with open(file_location, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    return {"status": "success", "image_path": file_location}

# Global whisper model for the router
_whisper_model = None

@router.post("/transcribe")
async def transcribe_audio(file: UploadFile = File(...), current_user: dict = Depends(get_current_user)):
    global _whisper_model
    if _whisper_model is None:
        try:
            import whisper as _whisper
            print("⏳ Lazy-loading Whisper 'base' model...")
            _whisper_model = _whisper.load_model("base")
            print("✅ Whisper 'base' model loaded.")
        except ImportError:
            return {"status": "success", "text": "Mock transcription — Whisper not installed. Run: pip install openai-whisper"}
    try:
        # Reuse the singleton model after it's loaded
        with tempfile.NamedTemporaryFile(delete=False, suffix=".webm") as temp_audio:
            temp_audio.write(await file.read())
            temp_audio_path = temp_audio.name
        result = _whisper_model.transcribe(temp_audio_path)
        os.remove(temp_audio_path)
        return {"status": "success", "text": result["text"]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Transcription failed: {e}")


@router.get("/files/{filename}/page-count")
async def get_page_count(filename: str, current_user: dict = Depends(get_current_user)):
    """Get the total page count of a PDF file."""
    base_dir = os.path.realpath("data")
    safe_filename = os.path.basename(filename)
    file_path = os.path.realpath(os.path.join(base_dir, safe_filename))
    
    if not file_path.startswith(base_dir):
        raise HTTPException(status_code=403, detail="Invalid file path.")
    
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail=f"File '{safe_filename}' not found.")
        
    try:
        import fitz
        doc = fitz.open(file_path)
        page_count = len(doc)
        doc.close()
        return {"page_count": page_count}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read PDF: {str(e)}")


@router.get("/files/{filename}/pages/{page}/image")
async def serve_page_image(filename: str, page: int, current_user: dict = Depends(get_current_user)):
    """Render and serve a specific PDF page as a high-resolution image on-the-fly."""
    base_dir = os.path.realpath("data")
    safe_filename = os.path.basename(filename)
    file_path = os.path.realpath(os.path.join(base_dir, safe_filename))
    
    # Path traversal guard
    if not file_path.startswith(base_dir):
        raise HTTPException(status_code=403, detail="Invalid file path.")
    
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail=f"File '{safe_filename}' not found.")
        
    try:
        import fitz
        doc = fitz.open(file_path)
        if page < 1 or page > len(doc):
            raise HTTPException(status_code=400, detail=f"Page number {page} out of bounds (1-{len(doc)}).")
            
        page_obj = doc.load_page(page - 1)
        # Render at high-quality 150 DPI for crisp visual presentation in React
        pix = page_obj.get_pixmap(dpi=150)
        img_data = pix.tobytes("png")
        doc.close()
        
        return StreamingResponse(io.BytesIO(img_data), media_type="image/png")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to render page: {str(e)}")


@router.get("/files/{filename}/pages/{page}/highlight")
async def serve_page_highlight(
    filename: str, 
    page: int, 
    snippet: str = Query(..., description="The citation snippet to search for"),
    current_user: dict = Depends(get_current_user)
):
    """Locate the bounding box coordinates of a snippet on a PDF page dynamically."""
    base_dir = os.path.realpath("data")
    safe_filename = os.path.basename(filename)
    file_path = os.path.realpath(os.path.join(base_dir, safe_filename))
    
    # Path traversal guard
    if not file_path.startswith(base_dir):
        raise HTTPException(status_code=403, detail="Invalid file path.")
    
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail=f"File '{safe_filename}' not found.")
        
    try:
        import fitz
        doc = fitz.open(file_path)
        if page < 1 or page > len(doc):
            raise HTTPException(status_code=400, detail=f"Page number {page} out of bounds (1-{len(doc)}).")
            
        page_obj = doc.load_page(page - 1)
        width = page_obj.rect.width
        height = page_obj.rect.height
        
        # Strip common formatting characters
        clean_snippet = snippet.replace("**", "").replace("__", "").replace("`", "").strip()
        
        highlights = []
        # 1. Primary search for the exact snippet
        rects = page_obj.search_for(clean_snippet)
        
        # 2. Fallback: Search for the first 4 words of the snippet (handles multi-line wraps)
        if not rects and len(clean_snippet.split()) > 3:
            fallback_words = " ".join(clean_snippet.split()[:4])
            rects = page_obj.search_for(fallback_words)
            
        # 3. Super-fallback: Search for the first 2 words
        if not rects and len(clean_snippet.split()) > 1:
            fallback_words_short = " ".join(clean_snippet.split()[:2])
            rects = page_obj.search_for(fallback_words_short)
            
        # Map absolute point coordinates to relative viewport percentages
        for rect in rects:
            h_left = (rect.x0 / width) * 100
            h_top = (rect.y0 / height) * 100
            h_width = ((rect.x1 - rect.x0) / width) * 100
            h_height = ((rect.y1 - rect.y0) / height) * 100
            highlights.append({
                "left": round(h_left, 3),
                "top": round(h_top, 3),
                "width": round(h_width, 3),
                "height": round(h_height, 3)
            })
            
        has_text = False
        try:
            txt = page_obj.get_text()
            if txt and len(txt.strip()) > 0:
                has_text = True
        except Exception:
            pass
            
        doc.close()
        
        return {
            "highlights": highlights,
            "page_width": width,
            "page_height": height,
            "has_text_layer": has_text
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to find highlights: {str(e)}")


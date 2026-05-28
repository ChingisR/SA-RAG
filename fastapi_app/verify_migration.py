import requests
import time
import os
import json

# Configuration (using internal container address)
API_BASE_URL = "http://localhost:8000"
LOGIN_DATA = {
    "email": "admin@enterprise.com",
    "password": "admin123"
}
# The file is relative to /app inside the container
TEST_PDF_PATH = "/app/Chingis Rustemov AI Architect_CV.pdf"

def verify_migration():
    global TEST_PDF_PATH
    print("--- 🔍 Starting Migration Verification (Internal Container) ---")
    
    # 1. Login
    print("1. Logging in...")
    try:
        resp = requests.post(f"{API_BASE_URL}/login", json=LOGIN_DATA, timeout=60)
        resp.raise_for_status()
        token = resp.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        print("✅ Login successful.")
    except Exception as e:
        print(f"❌ Login failed: {e}")
        return

    # 2. Ingest Document
    print(f"2. Ingesting {os.path.basename(TEST_PDF_PATH)}...")
    try:
        if not os.path.exists(TEST_PDF_PATH):
             print(f"🚨 File not found: {TEST_PDF_PATH}. Trying 'data/' prefix...")
             TEST_PDF_PATH_ALT = "/app/data/Chingis Rustemov AI Architect_CV.pdf"
             if os.path.exists(TEST_PDF_PATH_ALT):
                 TEST_PDF_PATH = TEST_PDF_PATH_ALT
             else:
                 print(f"❌ Could not find test PDF in /app or /app/data.")
                 return

        with open(TEST_PDF_PATH, "rb") as f:
            files = {"file": (os.path.basename(TEST_PDF_PATH), f, "application/pdf")}
            # sync=True to wait for the celery task to finish
            resp = requests.post(f"{API_BASE_URL}/upload-pdf?sync=True", files=files, headers=headers, timeout=120)
            resp.raise_for_status()
            print(f"✅ Ingestion successful: {resp.json()['message']}")
    except Exception as e:
        print(f"❌ Ingestion failed: {e}")
        return

    # 3. Wait for indexing finalization
    print("3. Waiting for indexing to finalize (5s)...")
    time.sleep(5)

    # 4. Query
    query = "Who is Chingis Rustemov?"
    print(f"4. Querying: '{query}'")
    query_payload = {
        "query": query,
        "chat_history": [],
        "image_paths": []
    }
    
    try:
        # FastAPI's /query uses StreamingResponse
        resp = requests.post(f"{API_BASE_URL}/query", json=query_payload, headers=headers, stream=True, timeout=120)
        resp.raise_for_status()
        
        print("\n--- 🤖 LLM RESPONSE ---\n")
        full_text = ""
        for chunk in resp.iter_lines():
            if chunk:
                text_chunk = chunk.decode("utf-8")
                if "<!--CITATIONS_JSON:" not in text_chunk:
                    full_text += text_chunk
                    print(text_chunk, end="", flush=True)
        print("\n\n--- END OF RESPONSE ---\n")
        
        if "Chingis" in full_text or "Rustemov" in full_text or "Architect" in full_text:
            print("✅ Verification PASSED: LLM correctly identified the data from the imported CV.")
        else:
            print("⚠️ Verification INCOMPLETE: LLM response did not contain key details. Check the response quality above.")
            
    except Exception as e:
        print(f"❌ Query failed: {e}")

if __name__ == "__main__":
    verify_migration()

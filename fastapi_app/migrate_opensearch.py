import os
import requests
import json
import time

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

OPENSEARCH_URL = os.getenv("OPENSEARCH_URL", "https://localhost:9200")
OPENSEARCH_USER = os.getenv("OPENSEARCH_USER", "admin")
OPENSEARCH_PASSWORD = os.getenv("OPENSEARCH_PASSWORD")
EMBED_DIM = int(os.getenv("EMBED_DIM", "4096"))
INFINITY_EMBED_URL = os.getenv("INFINITY_EMBED_URL", "http://127.0.0.1:8082/v1")
EMBED_MODEL = os.getenv("EMBED_MODEL", "Qwen/Qwen3-VL-Embedding-8B")

INDEX_NAME = "universal_docs_v1"
TEMP_INDEX_NAME = "universal_docs_temp"

AUTH = (OPENSEARCH_USER, OPENSEARCH_PASSWORD)

def get_embedding(text):
    # Prefix text if needed for the specific model
    if "nomic-embed-text" in EMBED_MODEL:
        text = f"search_document: {text}"
    elif "Qwen2" in EMBED_MODEL or "Qwen3" in EMBED_MODEL:
        text = f"instruct: {text}"
    
    payload = {
        "model": EMBED_MODEL,
        "input": [text]
    }
    
    # Call infinity embed URL
    res = requests.post(f"{INFINITY_EMBED_URL}/embeddings", json=payload, timeout=30)
    res.raise_for_status()
    return res.json()["data"][0]["embedding"]

def create_index(index_name):
    index_url = f"{OPENSEARCH_URL}/{index_name}"
    mapping = {
        "settings": {"index": {"knn": True}},
        "mappings": {
            "properties": {
                "embedding": {
                    "type": "knn_vector",
                    "dimension": EMBED_DIM,
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
    res = requests.put(index_url, json=mapping, auth=AUTH, verify=False, timeout=10)
    if res.status_code in (200, 201, 400): # 400 means already exists
        print(f"✅ Created index {index_name} with dim={EMBED_DIM}")
    else:
        print(f"❌ Failed to create index {index_name}: {res.text}")

def delete_index(index_name):
    index_url = f"{OPENSEARCH_URL}/{index_name}"
    res = requests.delete(index_url, auth=AUTH, verify=False, timeout=10)
    if res.status_code == 200 or res.status_code == 404:
        print(f"✅ Deleted index {index_name}")
    else:
        print(f"❌ Failed to delete index {index_name}: {res.text}")

def migrate():
    if not OPENSEARCH_PASSWORD:
        print("ERROR: OPENSEARCH_PASSWORD must be set in environment variables.")
        return

    print("🚀 Starting OpenSearch Migration Process...")
    print(f"Current Target Embedding Dimension: {EMBED_DIM}")
    print(f"Current Target Embedding Model: {EMBED_MODEL}")
    
    # 1. Check if original index exists
    res = requests.get(f"{OPENSEARCH_URL}/{INDEX_NAME}", auth=AUTH, verify=False)
    if res.status_code == 404:
        print(f"Index {INDEX_NAME} does not exist. Nothing to migrate.")
        return
        
    print(f"Found existing index {INDEX_NAME}. Creating temp index...")
    # Delete temp index if it exists
    delete_index(TEMP_INDEX_NAME)
    # Create temp index with NEW schema
    create_index(TEMP_INDEX_NAME)
    
    # 2. Scroll through original index
    print("Scrolling through existing index and re-embedding data...")
    scroll_url = f"{OPENSEARCH_URL}/{INDEX_NAME}/_search?scroll=5m"
    scroll_payload = {
        "size": 100,
        "query": {
            "match_all": {}
        }
    }
    
    res = requests.post(scroll_url, json=scroll_payload, auth=AUTH, verify=False, headers={"Content-Type": "application/json"})
    if res.status_code != 200:
        print(f"❌ Failed to start scroll: {res.text}")
        return
        
    data = res.json()
    scroll_id = data.get("_scroll_id")
    hits = data.get("hits", {}).get("hits", [])
    
    total_migrated = 0
    
    while hits:
        # Re-embed and insert into TEMP_INDEX_NAME
        for hit in hits:
            doc_id = hit["_id"]
            source = hit["_source"]
            content = source.get("content", "")
            metadata = source.get("metadata", {})
            
            if not content:
                continue
                
            try:
                new_embedding = get_embedding(content)
            except Exception as e:
                print(f"⚠️ Failed to get embedding for doc {doc_id}: {e}")
                continue
                
            new_doc = {
                "content": content,
                "metadata": metadata,
                "embedding": new_embedding
            }
            
            # Put into temp index
            put_url = f"{OPENSEARCH_URL}/{TEMP_INDEX_NAME}/_doc/{doc_id}"
            put_res = requests.put(put_url, json=new_doc, auth=AUTH, verify=False, headers={"Content-Type": "application/json"})
            if put_res.status_code in (200, 201):
                total_migrated += 1
                if total_migrated % 50 == 0:
                    print(f"  ... Migrated {total_migrated} documents")
            else:
                print(f"⚠️ Failed to index doc {doc_id} to temp index: {put_res.text}")
                
        # Get next batch
        scroll_req = {
            "scroll": "5m",
            "scroll_id": scroll_id
        }
        res = requests.post(f"{OPENSEARCH_URL}/_search/scroll", json=scroll_req, auth=AUTH, verify=False, headers={"Content-Type": "application/json"})
        data = res.json()
        scroll_id = data.get("_scroll_id")
        hits = data.get("hits", {}).get("hits", [])

    print(f"✅ Re-embedding complete. Total documents migrated to temp index: {total_migrated}")
    
    # 3. Swap indices
    print(f"Replacing old {INDEX_NAME} with newly schema-compliant version...")
    delete_index(INDEX_NAME)
    create_index(INDEX_NAME)
    
    print(f"Reindexing data back to {INDEX_NAME}...")
    reindex_payload = {
        "source": {"index": TEMP_INDEX_NAME},
        "dest": {"index": INDEX_NAME}
    }
    reindex_res = requests.post(f"{OPENSEARCH_URL}/_reindex?wait_for_completion=true", json=reindex_payload, auth=AUTH, verify=False, headers={"Content-Type": "application/json"})
    
    if reindex_res.status_code == 200:
        print(f"✅ Reindexing successful.")
        delete_index(TEMP_INDEX_NAME)
        print("🎉 Migration completed successfully!")
    else:
        print(f"❌ Reindexing failed: {reindex_res.text}")
        print(f"⚠️ Your data is safe in {TEMP_INDEX_NAME}. Manual intervention required.")

if __name__ == "__main__":
    migrate()

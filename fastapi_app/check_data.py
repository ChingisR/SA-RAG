import requests
import json

# Connection settings from main.py
OS_URL = "https://opensearch:9200/universal_docs_v1/_search"
OS_AUTH = ("admin", "LegalAI_2026!")

def check_index():
    print(f"--- 🔍 Querying OpenSearch Index ---")
    try:
        # Request the first 3 chunks to verify content
        query = {
            "size": 3,
            "query": { "match_all": {} }
        }
        
        response = requests.get(
            OS_URL, 
            auth=OS_AUTH, 
            json=query, 
            verify=False
        )
        
        if response.status_code == 200:
            data = response.json()
            total_docs = data['hits']['total']['value']
            print(f"✅ Connection Successful!")
            print(f"📊 Total Chunks in Index: {total_docs}")
            
            if total_docs > 0:
                print("\n--- 📄 Sample Content from first chunk ---")
                first_hit = data['hits']['hits'][0]['_source']
                # Print a snippet of the text and the source filename
                print(f"Source File: {first_hit.get('metadata', {}).get('filename', 'Unknown')}")
                print(f"Content Snippet: {first_hit.get('content', '')[:300]}...")
            else:
                print("❌ Index is EMPTY. Please upload a PDF in the UI first.")
        else:
            print(f"❌ Error {response.status_code}: {response.text}")
            
    except Exception as e:
        print(f"📡 Connection Failed: {e}")

if __name__ == "__main__":
    check_index()
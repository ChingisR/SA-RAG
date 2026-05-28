import os
import asyncio
from datetime import date
from faker import Faker
import psycopg2
from sqlalchemy import create_engine, text
from llama_index.core import SimpleDirectoryReader, VectorStoreIndex, StorageContext, Settings
from llama_index.core.ingestion import IngestionPipeline
from llama_index.vector_stores.opensearch import OpensearchVectorClient, OpensearchVectorStore
# Hardcoded config since we execute inside docker
OPENSEARCH_URL = os.getenv("OPENSEARCH_URL", "https://opensearch:9200")
SGLANG_EMBED_URL = os.getenv("SGLANG_EMBED_URL", "http://host.docker.internal:8082" if os.name == 'nt' else "http://host.docker.internal:8082")
PG_DSN = os.getenv("POSTGRES_URL", "postgresql://admin:supersecret@postgres:5432/universal_rag_db")

from llama_index.embeddings.openai import OpenAIEmbedding
Settings.embed_model = OpenAIEmbedding(
    model_name=os.getenv("EMBED_MODEL", "Qwen/Qwen3-VL-Embedding-8B"),
    api_base=SGLANG_EMBED_URL,
    api_key="placeholder"
)
fake = Faker()

def ingest_postgres():
    print("🚀 Starting Postgres HR Data Ingestion...")
    engine = create_engine(PG_DSN)
    with engine.connect() as conn:
        with conn.begin():
            # Create table if not exists (usually done in main, but let's be safe)
            conn.execute(text("DROP TABLE IF EXISTS hr_employees CASCADE"))
            conn.execute(text("""
            CREATE TABLE hr_employees (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100),
                department VARCHAR(50),
                salary INTEGER,
                hire_date DATE
            )
            """))
            # Clear existing data
            conn.execute(text("TRUNCATE TABLE hr_employees RESTART IDENTITY CASCADE"))
            
            # Generate 50 realistic employees
            departments = ["Engineering", "Legal", "HR", "Sales", "Marketing", "Finance"]
            print("⏳ Generating 50 fake HR profiles...")
            for _ in range(50):
                name = fake.name()
                dept = fake.random_element(elements=departments)
                salary = fake.random_int(min=50000, max=180000)
                hire_date = fake.date_between(start_date='-5y', end_date='today')
                
                conn.execute(text("""
                INSERT INTO hr_employees (name, department, salary, hire_date)
                VALUES (:name, :dept, :salary, :hire_date)
                """), {"name": name, "dept": dept, "salary": salary, "hire_date": hire_date})
                
    print("✅ Postgres ingestion complete. Inserted 50 employees.")

def ingest_opensearch():
    print("🚀 Starting OpenSearch Document Ingestion...")
    # Generate some mock PDF/txt content if folder is empty
    docs_dir = "/app/data/HR_Docs"
    os.makedirs(docs_dir, exist_ok=True)
    
    mock_file = os.path.join(docs_dir, "company_policy_2026.txt")
    if not os.path.exists(mock_file):
        with open(mock_file, "w", encoding="utf-8") as f:
            f.write("COMPANY POLICY 2026\n\n1. All employees must take mandatory cybersecurity training.\n")
            f.write("2. Vacation policy: 20 days per year for full-time employees.\n")
            f.write("3. CEO is John Doe, CTO is Jane Smith.\n")
            f.write("4. Core working hours are 10 AM to 4 PM.\n")
            f.write("5. Health insurance is provided by BlueCross.\n")
    
    print(f"⏳ Reading documents from {docs_dir}")
    documents = SimpleDirectoryReader(input_dir=docs_dir, recursive=True).load_data()
    print(f"📖 Loaded {len(documents)} documents.")
    
    from main import ensure_opensearch_pipeline, ensure_opensearch_index
    ensure_opensearch_pipeline()
    ensure_opensearch_index()
    
    client = OpensearchVectorClient(
        endpoint=OPENSEARCH_URL,
        index="universal_docs_v1",
        dim=int(os.getenv("EMBED_DIM", "4096")),
        embedding_field="embedding",
        text_field="content",
        search_pipeline="rrf-pipeline",
        http_auth=(os.getenv("OPENSEARCH_USER", "admin"), os.getenv("OPENSEARCH_PASSWORD", "LegalAI_2026!")),
        use_ssl=True,
        verify_certs=False,
        ssl_show_warn=False,
        method={"name": "hnsw", "engine": "faiss", "space_type": "l2"}
    )
    vector_store = OpensearchVectorStore(client)
    storage_context = StorageContext.from_defaults(vector_store=vector_store)
    
    print("🧠 Ingesting into OpenSearch Vector Store...")
    # Use global settings defined in main or define embedding here
    # Since main is already loaded, Settings.embed_model should be valid.
    index = VectorStoreIndex.from_documents(documents, storage_context=storage_context, show_progress=True)
    print("✅ OpenSearch ingestion complete.")

if __name__ == "__main__":
    ingest_postgres()
    try:
        ingest_opensearch()
    except Exception as e:
        print(f"❌ Document Ingestion failed (OpenSearch might need pipeline setup first): {e}")
        print("Note: In our architecture, the pipeline is lazy-loaded by `main.py` on startup. If it fails here, make sure main.py has run at least once.")

from typing import List
from llama_index.core import VectorStoreIndex, SimpleDirectoryReader
from core.config import mm_llm

def get_vision_engine(image_paths: List[str]):
    image_documents = SimpleDirectoryReader(input_files=image_paths).load_data()
    index = VectorStoreIndex.from_documents(image_documents)
    return index.as_query_engine(llm=mm_llm)

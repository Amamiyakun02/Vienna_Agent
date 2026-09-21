import asyncio
from typing import List
from services.qdrant_service import get_qdrant_client
from services.rag_ingestion_service import DOCUMENT_COLLECTION
from utils.embedding import gpt_embedding

async def search_documents(query_text: str, limit: int = 3, collection_name: str = DOCUMENT_COLLECTION) -> List[str]:
    """
    Performs a semantic vector search on the Qdrant collection_name for the given query.
    Generates embedding for the query and retrieves the closest matching document chunks.
    """
    if not query_text or not query_text.strip():
        return []

    try:
        # Generate embedding vector for the query text
        vector = await asyncio.to_thread(gpt_embedding, query_text)
        if not vector:
            print("[RAG SEARCH WARNING] Failed to generate embedding for search query.")
            return []

        qdrant_client = await get_qdrant_client()
        
        # Search Qdrant using the query_points API (new qdrant-client version)
        response = await qdrant_client.query_points(
            collection_name=collection_name,
            query=vector,
            limit=limit
        )
        hits = response.points

        contexts = []
        for hit in hits:
            payload = hit.payload or {}
            chunk_text = payload.get("text", "")
            # Find document title if available
            doc_id = payload.get("document_id", "")
            source_type = payload.get("source_type", "")
            
            if chunk_text:
                context_str = f"Source [{source_type}] (Score: {hit.score:.4f}):\n{chunk_text}"
                contexts.append(context_str)
                
        return contexts

    except Exception as e:
        print(f"[RAG SEARCH ERROR] Failed to perform semantic search in Qdrant: {e}")
        return []

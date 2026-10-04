import os
import uuid
import asyncio
from typing import Optional, List
from bson import ObjectId
from qdrant_client import models

from services.qdrant_service import get_qdrant_client
from services.mongo_service import (
    documents_col,
    pa_documents_col
)
from utils.embedding import embed_text, EMBEDDING_DIM, EMBEDDING_PROVIDER

# Names of collections in Qdrant
DOCUMENT_COLLECTION = "document_chunk"
PORTFOLIO_DOCUMENT_COLLECTION = "portfolio_document_chunk"

from qdrant_client.http.exceptions import UnexpectedResponse

async def _ensure_collection(client, name: str):
    """Pastikan koleksi ada dengan dimensi sesuai EMBEDDING_PROVIDER aktif.

    Bila koleksi ada tapi dimensinya beda (mis. pindah openai 1536 -> gemini
    768), koleksi di-recreate kosong — vektor lama beda ruang embedding dan
    tidak bisa dipakai query baru. Isi ulang via ingest_document_embedding.
    """
    try:
        info = await client.get_collection(name)
        current_size = info.config.params.vectors.size
        if current_size == EMBEDDING_DIM:
            print(f"[OK] Qdrant collection '{name}' already exists "
                  f"(dim={current_size}, provider={EMBEDDING_PROVIDER}).")
            return
        print(f"[MIGRATE] Collection '{name}' dim={current_size} != {EMBEDDING_DIM} "
              f"(provider={EMBEDDING_PROVIDER}) — recreating...")
        await client.delete_collection(name)
    except UnexpectedResponse as e:
        if e.status_code != 404:
            print(f"[ERROR] Qdrant connection/response error for '{name}': {e}")
            raise e
    except Exception as e:
        # delete_collection pada koleksi tak ada / error koneksi — lanjut ke create
        print(f"[INFO] Recreating Qdrant collection '{name}': {e}")
    print(f"Creating Qdrant collection '{name}' (dim={EMBEDDING_DIM})...")
    await client.create_collection(
        collection_name=name,
        vectors_config=models.VectorParams(
            size=EMBEDDING_DIM,
            distance=models.Distance.COSINE
        )
    )
    print(f"[OK] Qdrant collection '{name}' created successfully.")


async def initialize_qdrant_collections():
    """
    Ensures that Qdrant collections exist with dimensions matching the
    active EMBEDDING_PROVIDER (gemini=768, openai=1536), Cosine distance.
    Dipanggil tiap startup (lihat lifespan di main.py).
    """
    client = await get_qdrant_client()

    # Removed product collection logic

    # 2. Document Chunk Collection
    await _ensure_collection(client, DOCUMENT_COLLECTION)

    # Ensure document_id payload index exists for deletes & filters
    try:
        await client.create_payload_index(
            collection_name=DOCUMENT_COLLECTION,
            field_name="document_id",
            field_schema=models.PayloadSchemaType.KEYWORD
        )
        print(f"[OK] Payload index for 'document_id' in '{DOCUMENT_COLLECTION}' verified.")
    except Exception as e:
        print(f"[WARNING] Could not create/verify payload index: {e}")

    # 3. Portfolio Document Chunk Collection
    await _ensure_collection(client, PORTFOLIO_DOCUMENT_COLLECTION)

    # Ensure document_id payload index exists for portfolio deletes & filters
    try:
        await client.create_payload_index(
            collection_name=PORTFOLIO_DOCUMENT_COLLECTION,
            field_name="document_id",
            field_schema=models.PayloadSchemaType.KEYWORD
        )
        print(f"[OK] Payload index for 'document_id' in '{PORTFOLIO_DOCUMENT_COLLECTION}' verified.")
    except Exception as e:
        print(f"[WARNING] Could not create/verify payload index: {e}")





async def ingest_document_embedding(
    document_id: str,
    title: str,
    content: Optional[str],
    source_type: str,
    product_id: Optional[str] = None,
    collection_name: str = DOCUMENT_COLLECTION,
    is_portfolio: bool = False
):
    """
    Ingests document content into Qdrant collection as chunks.
    Performs chunking (500 chars size, 100 overlap).
    Generates embedding for each chunk and uploads them in a single batch.
    Updates document state in MongoDB.
    """
    try:
        client = await get_qdrant_client()

        # 1. Clean previous chunks for this document first (to support clean updates)
        await client.delete(
            collection_name=collection_name,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="document_id",
                            match=models.MatchValue(value=str(document_id))
                        )
                    ]
                )
            )
        )

        # 2. Text chunking (500 chars, 100 overlap)
        text = content if content and content.strip() else title
        chunk_size = 500
        overlap = 100
        chunks = []

        i = 0
        while i < len(text):
            chunk = text[i : i + chunk_size].strip()
            if chunk:
                chunks.append(chunk)
            i += (chunk_size - overlap)

        if not chunks:
            chunks = [title]

        # 3. Create embeddings for each chunk and prepare Qdrant points
        points = []
        for idx, chunk_text in enumerate(chunks):
            embed_source = f"Document: {title} | Chunk {idx+1}/{len(chunks)}:\n{chunk_text}"

            vector = await asyncio.to_thread(embed_text, embed_source)
            if not vector:
                print(f"[ERROR] Failed to generate embedding for doc {document_id} chunk {idx}.")
                continue

            # Deterministic chunk UUID
            chunk_uuid = uuid.uuid5(uuid.NAMESPACE_DNS, f"doc-{document_id}-chunk-{idx}")

            point = models.PointStruct(
                id=str(chunk_uuid),
                vector=vector,
                payload={
                    "document_id": str(document_id),
                    "source_type": str(source_type),
                    "product_id": str(product_id) if product_id else None,
                    "chunk_index": int(idx),
                    "text": str(chunk_text),
                    "page_number": 1
                }
            )
            points.append(point)

        # 4. Upsert points into Qdrant in batch
        if points:
            await client.upsert(
                collection_name=collection_name,
                points=points
            )

        # 5. Update MongoDB document
        col = pa_documents_col if is_portfolio else documents_col
        await col.update_one(
            {"_id": ObjectId(document_id)},
            {"$set": {
                "chunk_count": len(chunks),
                "qdrant_indexed": True
            }}
        )
        print(f"[OK] Successfully indexed document '{title}' ({document_id}) with {len(points)} chunks in Qdrant collection '{collection_name}'.")
        return True

    except Exception as e:
        print(f"[ERROR] Error during document ingestion in Qdrant: {e}")
        return False


async def delete_document_embedding(document_id: str, collection_name: str = DOCUMENT_COLLECTION):
    """Deletes all chunks belonging to a document from Qdrant."""
    try:
        client = await get_qdrant_client()
        await client.delete(
            collection_name=collection_name,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="document_id",
                            match=models.MatchValue(value=str(document_id))
                        )
                    ]
                )
            )
        )
        print(f"[OK] Deleted all document chunks for doc {document_id} from Qdrant collection '{collection_name}'.")
    except Exception as e:
        print(f"Error deleting document {document_id} from Qdrant: {e}")

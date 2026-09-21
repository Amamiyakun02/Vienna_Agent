import os
import base64
import httpx
from datetime import datetime, timezone
from bson import ObjectId
from services.mongo_service import pa_documents_col as documents_col
from services.rag_ingestion_service import PORTFOLIO_DOCUMENT_COLLECTION, ingest_document_embedding, delete_document_embedding

async def sync_github_repositories() -> dict:
    """
    Synchronizes repositories from GitHub for the user specified in the .env configuration.
    Fetches the metadata and README of each repository, stores/updates it in MongoDB,
    and creates embeddings in Qdrant.
    """
    username = os.getenv("GITHUB_USERNAME", "Amamiyakun02")
    token = os.getenv("GITHUB_TOKEN", "")

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Vienna-AI-Twin-App"
    }
    if token:
        headers["Authorization"] = f"token {token}"
        # Fetch authenticated user's repos (includes private if authorized by token)
        url = "https://api.github.com/user/repos?per_page=100&type=owner"
    else:
        # Fetch public repos for the specific username
        url = f"https://api.github.com/users/{username}/repos?per_page=100"

    print(f"[GITHUB SYNC] Starting sync for user: '{username}' (Token provided: {bool(token)})")
    
    stats = {
        "total_repos_found": 0,
        "successfully_synced": 0,
        "failed": 0,
        "repos": []
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.get(url, headers=headers)
            if response.status_code != 200:
                error_msg = f"GitHub API error: {response.status_code} - {response.text}"
                print(f"[GITHUB SYNC ERROR] {error_msg}")
                return {"status": "error", "message": error_msg}

            repos = response.json()
            if not isinstance(repos, list):
                error_msg = f"Unexpected response format from GitHub: {repos}"
                print(f"[GITHUB SYNC ERROR] {error_msg}")
                return {"status": "error", "message": error_msg}

            stats["total_repos_found"] = len(repos)
            print(f"[GITHUB SYNC] Found {len(repos)} repositories to process.")

            for repo in repos:
                repo_name = repo.get("name")
                html_url = repo.get("html_url")
                description = repo.get("description") or ""
                language = repo.get("language") or "Unknown"
                is_fork = repo.get("fork", False)
                owner = repo.get("owner", {}).get("login", username)

                # Skip forks to focus only on original projects built by Amamiya
                if is_fork:
                    print(f"[GITHUB SYNC] Skipping fork: {repo_name}")
                    continue

                print(f"[GITHUB SYNC] Processing repository: {repo_name}...")
                
                # Fetch README content
                readme_content = ""
                readme_api_url = f"https://api.github.com/repos/{owner}/{repo_name}/readme"
                
                try:
                    readme_response = await client.get(readme_api_url, headers=headers)
                    if readme_response.status_code == 200:
                        readme_data = readme_response.json()
                        encoded_content = readme_data.get("content", "")
                        # GitHub returns base64 content with newlines, remove them for decoding
                        clean_encoded = encoded_content.replace("\n", "").replace("\r", "")
                        readme_content = base64.b64decode(clean_encoded).decode("utf-8", errors="ignore")
                    else:
                        print(f"[GITHUB SYNC] README not found or inaccessible for {repo_name} (Status: {readme_response.status_code})")
                except Exception as e:
                    print(f"[GITHUB SYNC WARNING] Failed to fetch README for {repo_name}: {e}")

                # Build rich textual representation of the project
                full_content_parts = [
                    f"Repository Name: {repo_name}",
                    f"Owner: {owner}",
                    f"URL: {html_url}",
                    f"Main Language: {language}",
                    f"Description: {description}",
                ]
                if readme_content:
                    full_content_parts.append(f"\n--- README.md ---\n{readme_content}")
                else:
                    full_content_parts.append("\n(No README.md content available for this repository)")

                full_content = "\n".join(full_content_parts)

                title = f"GitHub: {repo_name}"
                
                # Check if document already exists in MongoDB
                existing_doc = await documents_col.find_one({"source_type": "github", "file_url": html_url})
                
                if existing_doc:
                    doc_id = existing_doc["_id"]
                    # Update MongoDB
                    await documents_col.update_one(
                        {"_id": doc_id},
                        {"$set": {
                            "title": title,
                            "content": full_content,
                            "status": "active",
                            "updated_at": datetime.now(timezone.utc)
                        }}
                    )
                    print(f"[GITHUB SYNC] Updated existing MongoDB document for {repo_name}.")
                else:
                    # Insert to MongoDB
                    new_doc = {
                        "_id": ObjectId(),
                        "title": title,
                        "source_type": "github",
                        "file_url": html_url,
                        "status": "active",
                        "content": full_content,
                        "chunk_count": 0,
                        "product_id": None,
                        "created_at": datetime.now(timezone.utc),
                        "updated_at": datetime.now(timezone.utc)
                    }
                    await documents_col.insert_one(new_doc)
                    doc_id = new_doc["_id"]
                    print(f"[GITHUB SYNC] Created new MongoDB document for {repo_name}.")

                # Trigger Qdrant Ingestion
                success = await ingest_document_embedding(
                    document_id=str(doc_id),
                    title=title,
                    content=full_content,
                    source_type="github",
                    collection_name=PORTFOLIO_DOCUMENT_COLLECTION,
                    is_portfolio=True
                )

                if success:
                    stats["successfully_synced"] += 1
                    stats["repos"].append({"name": repo_name, "status": "success", "id": str(doc_id)})
                else:
                    stats["failed"] += 1
                    stats["repos"].append({"name": repo_name, "status": "failed_indexing"})
                    print(f"[GITHUB SYNC ERROR] Failed to index {repo_name} in Qdrant.")

        except Exception as e:
            error_msg = f"Unexpected error during GitHub sync process: {e}"
            print(f"[GITHUB SYNC CRITICAL ERROR] {error_msg}")
            return {"status": "error", "message": error_msg}

    print(f"[GITHUB SYNC COMPLETED] Synced {stats['successfully_synced']}/{stats['total_repos_found']} original repos.")
    return {"status": "success", "stats": stats}

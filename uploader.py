"""Upload Markdown docs to OpenAI Vector Store and manage the OptiBot assistant."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

ROOT = Path(__file__).parent
DOCS_DIR = ROOT / "docs"
STATE_PATH = ROOT / "state.json"

SYSTEM_PROMPT = """You are OptiBot, the customer-support bot for OptiSigns.com.

• Tone: helpful, factual, concise.

• Only answer using the uploaded docs.

• Max 5 bullet points; else link to the doc.

• Cite up to 3 "Article URL:" lines per reply."""

VECTOR_STORE_NAME = os.getenv("OPENAI_VECTOR_STORE_NAME", "kb-sync-agent")
ASSISTANT_NAME = os.getenv("OPENAI_ASSISTANT_NAME", "OptiBot")
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")


def get_client() -> OpenAI:
    api_key = os.getenv("OPENAI_API_KEY") or os.getenv("API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY is missing. Copy .env.sample to .env and set your key.")
    return OpenAI(api_key=api_key)


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"vector_store_id": None, "assistant_id": None, "files": {}}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_frontmatter(text: str) -> dict:
    match = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not match:
        return {}
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip()
    return meta


def log(message: str) -> None:
    print(message, flush=True)


def find_vector_store_by_name(client: OpenAI, name: str) -> str | None:
    stores = client.vector_stores.list(limit=100)
    for store in stores.data:
        if store.name == name:
            return store.id
    return None


def get_or_create_vector_store(client: OpenAI, state: dict) -> str:
    if state.get("vector_store_id"):
        return state["vector_store_id"]

    existing = find_vector_store_by_name(client, VECTOR_STORE_NAME)
    if existing:
        state["vector_store_id"] = existing
        return existing

    store = client.vector_stores.create(name=VECTOR_STORE_NAME)
    state["vector_store_id"] = store.id
    return store.id


def get_or_create_assistant(client: OpenAI, state: dict, vector_store_id: str) -> str:
    if state.get("assistant_id"):
        return state["assistant_id"]

    assistant = client.beta.assistants.create(
        name=ASSISTANT_NAME,
        model=MODEL,
        instructions=SYSTEM_PROMPT,
        tools=[{"type": "file_search"}],
        tool_resources={"file_search": {"vector_store_ids": [vector_store_id]}},
    )
    state["assistant_id"] = assistant.id
    return assistant.id


def wait_for_file_batch(client: OpenAI, vector_store_id: str, batch_id: str, timeout: int = 600) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        batch = client.vector_stores.file_batches.retrieve(
            vector_store_id=vector_store_id,
            batch_id=batch_id,
        )
        counts = batch.file_counts
        log(f"  indexing... completed={counts.completed} in_progress={counts.in_progress} failed={counts.failed}")
        if batch.status == "completed":
            if counts.failed:
                raise RuntimeError(f"Batch finished with {counts.failed} failed files")
            return
        if batch.status in {"failed", "cancelled", "expired"}:
            raise RuntimeError(f"File batch failed: {batch.status}")
        time.sleep(3)
    raise TimeoutError(f"Timed out waiting for file batch {batch_id}")


def remove_file(client: OpenAI, vector_store_id: str, file_id: str) -> None:
    try:
        client.vector_stores.files.delete(vector_store_id=vector_store_id, file_id=file_id)
    except Exception:
        pass
    try:
        client.files.delete(file_id)
    except Exception:
        pass


def upload_batch(client: OpenAI, vector_store_id: str, paths: list[Path]) -> list[str]:
    """Upload multiple markdown files in one vector-store batch."""
    if not paths:
        return []

    log(f"Uploading {len(paths)} files to OpenAI Files API...")
    file_ids: list[str] = []
    for index, path in enumerate(paths, start=1):
        with path.open("rb") as handle:
            uploaded = client.files.create(file=handle, purpose="assistants")
        file_ids.append(uploaded.id)
        log(f"  [{index}/{len(paths)}] {path.name}")

    log("Indexing all files in one batch (this may take 1-3 minutes)...")
    batch = client.vector_stores.file_batches.create(
        vector_store_id=vector_store_id,
        file_ids=file_ids,
    )
    wait_for_file_batch(client, vector_store_id, batch.id)
    return file_ids


def sync_docs(docs_dir: Path = DOCS_DIR) -> dict:
    client = get_client()
    state = load_state()
    vector_store_id = get_or_create_vector_store(client, state)
    assistant_id = get_or_create_assistant(client, state, vector_store_id)

    stats = {"added": 0, "updated": 0, "skipped": 0, "files": [], "chunks": 0}
    pending: list[tuple[Path, dict, str, dict | None]] = []

    for path in sorted(docs_dir.glob("*.md")):
        current_hash = file_hash(path)
        meta = parse_frontmatter(path.read_text(encoding="utf-8"))
        filename = path.name
        previous = state["files"].get(filename)

        if previous and previous.get("content_hash") == current_hash:
            stats["skipped"] += 1
            continue

        pending.append((path, meta, current_hash, previous))

    if pending:
        log(f"Preparing {len(pending)} changed files...")
        for path, _meta, _hash, previous in pending:
            if previous and previous.get("openai_file_id"):
                remove_file(client, vector_store_id, previous["openai_file_id"])
                stats["updated"] += 1
            else:
                stats["added"] += 1

        paths = [item[0] for item in pending]
        file_ids = upload_batch(client, vector_store_id, paths)

        for (path, meta, current_hash, _previous), file_id in zip(pending, file_ids):
            filename = path.name
            state["files"][filename] = {
                "article_id": meta.get("article_id"),
                "article_url": meta.get("article_url"),
                "content_hash": current_hash,
                "openai_file_id": file_id,
            }
            stats["files"].append(filename)

    save_state(state)

    store = client.vector_stores.retrieve(vector_store_id)
    stats["vector_store_id"] = vector_store_id
    stats["assistant_id"] = assistant_id
    stats["embedded_files"] = store.file_counts.completed
    stats["chunks"] = store.file_counts.completed

    return stats


def ask_assistant(question: str, assistant_id: str | None = None) -> str:
    client = get_client()
    state = load_state()
    assistant_id = assistant_id or state.get("assistant_id")
    if not assistant_id:
        raise ValueError("No assistant_id in state. Run sync_docs() first.")

    thread = client.beta.threads.create()
    client.beta.threads.messages.create(thread_id=thread.id, role="user", content=question)
    run = client.beta.threads.runs.create(thread_id=thread.id, assistant_id=assistant_id)

    deadline = time.time() + 120
    while time.time() < deadline:
        run = client.beta.threads.runs.retrieve(thread_id=thread.id, run_id=run.id)
        if run.status == "completed":
            break
        if run.status in {"failed", "cancelled", "expired"}:
            raise RuntimeError(f"Assistant run failed: {run.status}")
        time.sleep(2)
    else:
        raise TimeoutError("Assistant run timed out")

    messages = client.beta.threads.messages.list(thread_id=thread.id, order="desc", limit=1)
    parts = []
    for block in messages.data[0].content:
        if block.type == "text":
            parts.append(block.text.value)
    return "\n".join(parts)


if __name__ == "__main__":
    result = sync_docs()
    print(json.dumps(result, indent=2))
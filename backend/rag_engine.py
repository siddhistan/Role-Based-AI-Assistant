import os
import re
import hashlib
import shutil
from dotenv import load_dotenv

from langchain_community.document_loaders import (
    TextLoader,
    CSVLoader,
    PyPDFLoader,
    Docx2txtLoader,
)
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import Chroma
from langchain_community.embeddings import HuggingFaceEmbeddings
from groq import Groq

load_dotenv()

# Role → allowed data folders
ROLE_FOLDERS = {
    "finance":     ["finance"],
    "hr":          ["hr", "general"],       # HR needs handbook for policy questions
    "engineering": ["engineering"],
    "marketing":   ["marketing"],
    "employee":    ["general"],
    "c-level":     ["finance", "hr", "engineering", "marketing", "general"],
}

# Using a set gives O(1) lookup for RBAC enforcement and ensures we have
# a single source of truth for valid roles.
VALID_ROLES = set(ROLE_FOLDERS.keys())

# We build a reverse mapping of folder → allowed roles to enforce
# escalation prevention at runtime.
FOLDER_ALLOWED_ROLES: dict = {}
for _role, _folders in ROLE_FOLDERS.items():
    '''This loop builds a reverse mapping of folder → allowed roles for runtime RBAC enforcement.
    Even if someone mutates ROLE_FOLDERS at runtime to add a new folder to a role, they won't
    be able to access it unless they also update FOLDER_ALLOWED_ROLES, which is a separate
    data structure. This adds an extra layer of security by ensuring that any changes to
    ROLE_FOLDERS alone won't grant unintended access to new folders.'''
    for _folder in _folders:
        '''For each folder that a role is allowed to access,
        we add that role to the set of permitted roles for that folder in FOLDER_ALLOWED_ROLES.
        This allows us to check at runtime if a role is trying to access a folder it shouldn't
        be able to, preventing any escalation of privileges even if ROLE_FOLDERS is mutated
        after startup.'''
        FOLDER_ALLOWED_ROLES.setdefault(_folder, set()).add(_role)


def enforce_rbac(role: str) -> tuple:
    """
    Two-layer RBAC check:
      1. Strict role validation  — role must exist in VALID_ROLES exactly.
      2. Escalation prevention   — each folder the role claims must list
                                   that role as permitted in FOLDER_ALLOWED_ROLES.
                                   Catches any runtime mutation of ROLE_FOLDERS.
    Returns (is_valid: bool, error_message: str).
    """
    # Layer 1 — type + whitespace + existence check
    # isinstance(role, str) checks if 'role' is a string, preventing non-string
    # types (e.g. None, int) from passing through.
    if not isinstance(role, str) or not role.strip():
        return False, "Role must be a non-empty string."

    role_clean = role.strip().lower()

    if role_clean not in VALID_ROLES:
        return False, (
            f"Access denied: '{role_clean}' is not a recognised role. "
            f"Valid roles: {sorted(VALID_ROLES)}."
        )

    # Layer 2 — escalation check
    # Layer 1 only checks "is this a valid role name?"
    # It doesn't check what folders that role is trying to touch at runtime.
    # Layer 2 does that.
    claimed_folders = ROLE_FOLDERS.get(role_clean, [])
    for folder in claimed_folders:
        permitted = FOLDER_ALLOWED_ROLES.get(folder, set())
        if role_clean not in permitted:
            return False, (
                f"Access denied: role '{role_clean}' attempted to access "
                f"folder '{folder}' which it is not permitted to use. "
                f"Possible escalation attempt blocked."
            )

    return True, ""


# Singleton: load embeddings once, reuse across all queries
_embedding_model = None

def get_embedding_model():
    global _embedding_model
    if _embedding_model is None:
        _embedding_model = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")
    return _embedding_model


# ── Helpers ───────────────────────────────────────────────────────────────────

def compute_folder_hash(folders: list, base_path: str) -> str:
    """
    Build an MD5 fingerprint of all file names + last-modified times
    for the role's allowed folders.
    If any file is added, removed, or edited → hash changes → ChromaDB rebuilds.
    This prevents the silent stale-data bug where updated documents are never
    re-indexed because the chroma_db_{role} folder already exists.
    """
    hasher = hashlib.md5()
    for folder in sorted(folders):              # sorted = deterministic order
        folder_path = os.path.join(base_path, folder)
        if not os.path.exists(folder_path):
            continue
        for fname in sorted(os.listdir(folder_path)):   # sorted = deterministic
            fpath = os.path.join(folder_path, fname)
            stat  = os.stat(fpath)              # read file metadata (no file open)
            # fname = catches renames, st_mtime = catches edits, st_size = extra safety
            hasher.update(f"{fname}{stat.st_mtime}{stat.st_size}".encode())
    return hasher.hexdigest()                   # 32-char hex string e.g. "a3f8c2d1..."


def extract_file_tags(fname: str) -> dict:
    """
    Parse useful tags from a filename to inject into chunk content.
    Examples:
      marketing_report_q1_2024.md   → {quarter: "Q1", year: "2024", doc_type: "marketing report q1 2024"}
      quarterly_financial_report.md → {quarter: "annual", year: "", doc_type: "financial report"}
      hr_data.csv                   → {quarter: "annual", year: "", doc_type: "hr data"}
    """
    name = fname.lower().replace("_", " ").replace("-", " ")
    name = re.sub(r"\.(md|csv|pdf|docx|txt)$", "", name)

    # Detect quarter
    quarter_match = re.search(r"\bq([1-4])\b", name)
    quarter = f"Q{quarter_match.group(1)}" if quarter_match else "annual"

    # Detect year
    year_match = re.search(r"\b(202[0-9])\b", name)
    year = year_match.group(1) if year_match else ""

    return {
        "quarter":  quarter,
        "year":     year,
        "doc_type": name.strip(),
    }


def load_documents(folders: list, base_path: str) -> list:
    """Load all supported files from the given folders."""
    documents = []
    for folder in folders:
        folder_path = os.path.join(base_path, folder)
        if not os.path.exists(folder_path):
            print(f"[WARNING] Folder not found: {folder_path}")
            continue
        for fname in os.listdir(folder_path):
            fpath = os.path.join(folder_path, fname)
            try:
                if fname.endswith(".md") or fname.endswith(".txt"):
                    loader = TextLoader(fpath, encoding="utf-8")
                elif fname.endswith(".csv"):
                    loader = CSVLoader(fpath, encoding="utf-8")
                elif fname.endswith(".pdf"):
                    loader = PyPDFLoader(fpath)
                elif fname.endswith(".docx"):
                    loader = Docx2txtLoader(fpath)
                else:
                    continue  # skip unsupported file types

                docs = loader.load()
                tags = extract_file_tags(fname)

                for doc in docs:
                    # Store rich metadata on every chunk
                    doc.metadata["source"]   = fname
                    doc.metadata["folder"]   = folder
                    doc.metadata["quarter"]  = tags["quarter"]
                    doc.metadata["year"]     = tags["year"]
                    doc.metadata["doc_type"] = tags["doc_type"]

                    # KEY FIX: Prepend a metadata header to the chunk text itself.
                    # This makes the embedding vector aware of WHICH document/quarter
                    # the content belongs to, preventing semantic similarity collisions
                    # across Q1/Q2/Q3/Q4 finance and marketing files.
                    period = f"{tags['quarter']} {tags['year']}".strip()
                    header = (
                        f"[Document: {fname}] "
                        f"[Type: {tags['doc_type']}] "
                        f"[Period: {period}]\n\n"
                    )
                    doc.page_content = header + doc.page_content

                documents.extend(docs)
            except Exception as e:
                print(f"[ERROR] Could not load {fpath}: {e}")
    return documents


def get_or_build_vectorstore(role: str, folders: list, base_path: str) -> Chroma:
    """
    Return a ChromaDB instance for the role.

    Smart rebuild logic:
      - First run: builds ChromaDB from documents and saves a hash fingerprint.
      - Subsequent runs: compares current file hash to saved hash.
          match   → load existing ChromaDB (fast, milliseconds)
          mismatch → wipe old ChromaDB + rebuild (data changed since last run)

    This prevents the silent stale-data bug where updated documents are never
    re-indexed because the chroma_db_{role} folder already exists on disk.
    """
    embedding    = get_embedding_model()
    persist_dir  = f"./chroma_db_{role}"
    hash_file    = f"{persist_dir}/.hash"
    current_hash = compute_folder_hash(folders, base_path)

    # Default: assume rebuild needed
    needs_rebuild = True

    # Only skip rebuild if BOTH the DB folder AND hash file exist AND hashes match
    if os.path.exists(persist_dir) and os.path.exists(hash_file):
        with open(hash_file, "r") as f:
            saved_hash = f.read().strip()
        if saved_hash == current_hash:
            needs_rebuild = False   # data unchanged → safe to load existing DB

    if needs_rebuild:
        print(f"[INFO] Building/rebuilding ChromaDB for role: {role}")
        documents = load_documents(folders, base_path)

        if not documents:
            raise ValueError(
                f"No documents found for role '{role}'. Check your data folders."
            )

        splitter = RecursiveCharacterTextSplitter(
            chunk_size=2000, chunk_overlap=200
        )
        chunks = splitter.split_documents(documents)

        # Wipe old DB completely so no stale vectors remain from deleted files
        if os.path.exists(persist_dir):
            shutil.rmtree(persist_dir)

        db = Chroma.from_documents(
            chunks, embedding, persist_directory=persist_dir
        )

        # Save fingerprint so next startup can skip rebuild if data unchanged
        os.makedirs(persist_dir, exist_ok=True)
        with open(hash_file, "w") as f:
            f.write(current_hash)

    else:
        print(f"[INFO] Loading existing ChromaDB for role: {role}")
        db = Chroma(
            persist_directory=persist_dir,
            embedding_function=embedding
        )

    return db


# ── Main entry point ──────────────────────────────────────────────────────────

def ask_question(role: str, query: str, debug: bool = False) -> dict:
    """
    Retrieve relevant context for the role and generate an answer via Groq LLaMA.
    Returns {"answer": str, "sources": list[str]}
    """
    BASE_PATH = "../data"

    # 1. RBAC enforcement — strict validation + escalation prevention
    is_valid, error_msg = enforce_rbac(role)
    if not is_valid:
        return {"answer": error_msg, "sources": []}

    role = role.strip().lower()
    allowed_folders = ROLE_FOLDERS[role]  # safe — enforce_rbac() confirmed it exists

    # 2. Get (or build) vector store
    try:
        db = get_or_build_vectorstore(role, allowed_folders, BASE_PATH)
    except ValueError as e:
        return {"answer": str(e), "sources": []}

    # 3. Retrieve top-k relevant chunks using MMR for diversity.
    # MMR (Maximal Marginal Relevance) avoids returning 7 chunks all from
    # the same quarter/document — ensures spread across relevant sources.
    retrieved_docs = db.max_marginal_relevance_search(query, k=10, fetch_k=30)

    if debug:
        print("\n[DEBUG] Retrieved chunks:")
        for i, doc in enumerate(retrieved_docs):
            print(f"\n--- Chunk {i+1} | Source: {doc.metadata.get('source')} | Quarter: {doc.metadata.get('quarter')} ---")
            print(doc.page_content[:300])

    if not retrieved_docs:
        return {
            "answer": "I do not have access to that information.",
            "sources": []
        }

    # 4. Build context — include source filename so LLM can reference it
    context_parts = []
    for doc in retrieved_docs:
        source = doc.metadata.get("source", "unknown")
        context_parts.append(f"[Source: {source}]\n{doc.page_content}")
    context = "\n\n---\n\n".join(context_parts)

    # 5. Deduplicate sources
    sources = sorted(set(
        doc.metadata.get("source", "unknown")
        for doc in retrieved_docs
    ))

    # 6. Call Groq LLaMA
    client = Groq(api_key=os.getenv("GROQ_API_KEY"))

    prompt = f"""You are a secure enterprise AI assistant for FinSolve Technologies.

STRICT RULES:
- Answer ONLY using the provided context below.
- Do NOT use any outside or prior knowledge.
- If the answer cannot be found in the context, respond exactly with:
  "I do not have access to that information."
- Never guess or fabricate data.
- Be concise, professional, and accurate.
- When referencing data, mention the source document name.
- For financial questions, look carefully through ALL sections including
  quarterly overviews, annual summaries, and expense breakdowns.
- Numbers may appear in bullet points or tables — scan thoroughly.

---
CONTEXT:
{context}
---

QUESTION: {query}

ANSWER:"""

    try:
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=1024,
        )
        answer = response.choices[0].message.content.strip()
    except Exception as e:
        answer = f"LLM call failed: {e}"

    return {
        "answer": answer,
        "sources": sources
    }
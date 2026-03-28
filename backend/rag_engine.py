#  What this file does 
# This file is the brain of the chatbot.
# It does 3 things:
#   1. Checks if the user has permission to access data (RBAC)
#   2. Finds relevant documents from ChromaDB (Retrieval)
#   3. Sends those documents + question to LLaMA to get an answer (Generation)
# Together this is called RAG — Retrieval Augmented Generation
# ─────────────────────────────────────────────────────────────────────────────

# Standard Python libraries
import os        # for reading files and folders
import re        # for pattern matching in filenames (finding "q1", "2024" etc.)
import hashlib   # for creating a fingerprint of files to detect changes
import shutil    # for deleting folders when we need to rebuild ChromaDB

# Loads environment variables from .env file (e.g. GROQ_API_KEY)
from dotenv import load_dotenv

# LangChain loaders — each one knows how to read a different file type
from langchain_community.document_loaders import (
    TextLoader,      # reads .md and .txt files
    CSVLoader,       # reads .csv files (like hr_data.csv)
    PyPDFLoader,     # reads .pdf files
    Docx2txtLoader,  # reads .docx Word files
)

# Splits large documents into smaller chunks so they fit in the LLM context
from langchain_text_splitters import RecursiveCharacterTextSplitter

# ChromaDB — our vector database that stores document chunks as embeddings
from langchain_community.vectorstores import Chroma

# HuggingFace model that converts text into numbers (vectors/embeddings)
from langchain_community.embeddings import HuggingFaceEmbeddings

# Groq client — used to call the LLaMA model for generating answers
from groq import Groq

# Load the .env file so we can use os.getenv("GROQ_API_KEY") later
load_dotenv()


# ── Step 1: Define who can access what ────────────────────────────────────────
# This dictionary maps each role to the folders they are allowed to read from.
# For example, "finance" can only read from the "finance" folder.
# "c-level" can read from ALL folders.

ROLE_FOLDERS = {
    "finance":     ["finance"],
    "hr":          ["hr", "general"],   # HR needs handbook for policy questions
    "engineering": ["engineering"],
    "marketing":   ["marketing"],
    "employee":    ["general"],         # employees only see the handbook
    "c-level":     ["finance", "hr", "engineering", "marketing", "general"],
}

# A set of all valid role names — used for fast lookup
# set() gives O(1) lookup — much faster than checking a list
VALID_ROLES = set(ROLE_FOLDERS.keys())

# Build a reverse map: folder → which roles are allowed in it
# Example: {"finance": {"finance", "c-level"}, "general": {"employee", "hr", "c-level"}}
# This is used in Layer 2 of RBAC to prevent privilege escalation
FOLDER_ALLOWED_ROLES = {}
for _role, _folders in ROLE_FOLDERS.items():
    for _folder in _folders:
        # setdefault creates an empty set if key doesn't exist, then adds the role
        FOLDER_ALLOWED_ROLES.setdefault(_folder, set()).add(_role)


# ── Step 2: RBAC — check if the user is allowed ───────────────────────────────
# RBAC = Role Based Access Control
# This function checks two things:x
#   Layer 1: Is the role a valid known role?
#   Layer 2: Is the role actually allowed in the folders it wants to access?

def enforce_rbac(role: str) -> tuple:
    # Layer 1: basic checks
    # isinstance() checks if role is actually a string (not None or a number)
    if not isinstance(role, str) or role.strip() == "":
        return False, "Role must be a non-empty string."

    # remove spaces and make lowercase so "Finance " == "finance"
    role_clean = role.strip().lower()

    # Check if role exists in our allowed roles
    if role_clean not in VALID_ROLES:
        return False, f"Access denied: '{role_clean}' is not a recognised role. Valid roles: {sorted(VALID_ROLES)}."

    # Layer 2: escalation check
    # Even if role is valid, verify it's actually allowed in each folder it claims
    # This prevents someone from sneaking extra folder access at runtime
    for folder in ROLE_FOLDERS.get(role_clean, []):
        if role_clean not in FOLDER_ALLOWED_ROLES.get(folder, set()):
            return False, f"Access denied: role '{role_clean}' is not permitted to access folder '{folder}'."

    # Both checks passed — role is valid and permitted
    return True, ""


# ── Step 3: Load embedding model once ─────────────────────────────────────────
# The embedding model converts text into vectors (lists of numbers).
# Similar text produces similar vectors — this is what powers semantic search.
# We load it once and reuse it for all queries (singleton pattern).
# Without this, the 90MB model would reload on every single query — very slow.

_embedding_model = None  # starts as None, gets filled on first use

def get_embedding_model():
    global _embedding_model
    if _embedding_model is None:
        # Load the model from HuggingFace (only happens once)
        _embedding_model = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")
    return _embedding_model


# ── Step 4: Detect if data files have changed ─────────────────────────────────
# We create a fingerprint (hash) of all files in the role's folders.
# If any file is added, edited, or deleted — the hash changes.
# This tells us to rebuild ChromaDB instead of using the old stale one.

def compute_folder_hash(folders: list, base_path: str) -> str:
    hasher = hashlib.md5()  # MD5 creates a 32-character fingerprint string

    for folder in sorted(folders):  # sorted = consistent order every time
        folder_path = os.path.join(base_path, folder)
        if not os.path.exists(folder_path):
            continue

        for fname in sorted(os.listdir(folder_path)):  # sorted = consistent order
            fpath = os.path.join(folder_path, fname)
            stat = os.stat(fpath)  # get file info without opening the file
            # Include filename + last modified time + file size in the hash
            hasher.update(f"{fname}{stat.st_mtime}{stat.st_size}".encode())

    return hasher.hexdigest()  # returns something like "a3f8c2d19e4b..."


# ── Step 5: Extract quarter and year from filename ────────────────────────────
# This function reads the filename and figures out which quarter/year it belongs to.
# Example: "marketing_report_q1_2024.md" → quarter="Q1", year="2024"
# This is important for the semantic collision fix — see Step 6 for why.

def extract_file_tags(fname: str) -> dict:
    
    # 1. Clean up the filename manually (No Regex needed for this!)
    name = fname.lower()
    name = name.replace(".md", "").replace(".csv", "").replace(".pdf", "").replace(".docx", "").replace(".txt", "")
    name = name.replace("_", " ").replace("-", " ")
    name = name.strip()

    # 2. Set default values just in case we don't find anything
    final_quarter = "annual"
    final_year = ""

    # 3. Check for the quarter (Q1, Q2, Q3, Q4)
    if "q1" in name:
        final_quarter = "Q1"
    elif "q2" in name:
        final_quarter = "Q2"
    elif "q3" in name:
        final_quarter = "Q3"
    elif "q4" in name:
        final_quarter = "Q4"

    # 4. Check for the year (Looking for 2020 through 2029)
    # split() turns "marketing report 2024" into ["marketing", "report", "2024"]
    words_in_name = name.split() 
    for word in words_in_name:
        if word.startswith("202") and len(word) == 4:
            final_year = word
            break # Stop looking once we find the year

    # 5. Return the clean, easy-to-read dictionary
    return {
        "quarter": final_quarter,
        "year": final_year,
        "doc_type": name
    }


# ── Step 6: Load documents from allowed folders ───────────────────────────────
# This function reads all files from the role's allowed folders.
# KEY FIX: We prepend a metadata header to every chunk BEFORE embedding.
#
# Why? Because Q1 and Q4 financial reports talk about the same topics
# (revenue, vendor costs, gross margin). The embedding model sees them
# as nearly identical vectors — so it mixes up Q1 and Q4 answers.
#
# Solution: Add "[Document: file.md] [Period: Q1 2024]" to the START of
# every chunk. Now the embedding encodes WHICH quarter it belongs to,
# making Q1 and Q4 vectors genuinely different.

def load_documents(folders: list, base_path: str) -> list:
    documents = []

    for folder in folders:
        folder_path = os.path.join(base_path, folder)

        # Skip if folder doesn't exist
        if not os.path.exists(folder_path):
            print(f"[WARNING] Folder not found: {folder_path}")
            continue

        for fname in os.listdir(folder_path):
            fpath = os.path.join(folder_path, fname)

            try:
                # Pick the right loader based on file extension
                if fname.endswith(".md") or fname.endswith(".txt"):
                    loader = TextLoader(fpath, encoding="utf-8")
                elif fname.endswith(".csv"):
                    loader = CSVLoader(fpath, encoding="utf-8")
                elif fname.endswith(".pdf"):
                    loader = PyPDFLoader(fpath)
                elif fname.endswith(".docx"):
                    loader = Docx2txtLoader(fpath)
                else:
                    continue  # skip unsupported files silently

                # Load the file — returns a list of Document objects
                docs = loader.load()

                # Get quarter/year/doc_type from the filename
                tags = extract_file_tags(fname)
                period = f"{tags['quarter']} {tags['year']}".strip()

                # Build the metadata header that goes at the top of every chunk
                header = f"[Document: {fname}] [Type: {tags['doc_type']}] [Period: {period}]\n\n"

                for doc in docs:
                    # Save metadata so we can show sources in the answer
                    doc.metadata["source"]   = fname
                    doc.metadata["folder"]   = folder
                    doc.metadata["quarter"]  = tags["quarter"]
                    doc.metadata["year"]     = tags["year"]
                    doc.metadata["doc_type"] = tags["doc_type"]

                    # Prepend header to chunk text — this is the collision fix
                    doc.page_content = header + doc.page_content

                documents.extend(docs)

            except Exception as e:
                # If one file fails, skip it and continue with the rest
                print(f"[ERROR] Could not load {fpath}: {e}")

    return documents


# ── Step 7: Build or load ChromaDB ────────────────────────────────────────────
# ChromaDB stores all document chunks as vectors on disk.
# Each role gets its own separate ChromaDB folder.
# Smart logic:
#   - If data files haven't changed → load existing DB (fast)
#   - If data files changed → wipe old DB and rebuild (accurate)

def get_or_build_vectorstore(role: str, folders: list, base_path: str) -> Chroma:
    embedding   = get_embedding_model()
    persist_dir = f"./chroma_db_{role}"  # e.g. chroma_db_finance, chroma_db_hr
    hash_file   = f"{persist_dir}/.hash" # where we save the fingerprint
    current_hash = compute_folder_hash(folders, base_path)

    needs_rebuild = True  # default: assume we need to rebuild

    # Check if DB already exists AND fingerprint matches current files
    if os.path.exists(persist_dir) and os.path.exists(hash_file):
        with open(hash_file, "r") as f:
            saved_hash = f.read().strip()
        if saved_hash == current_hash:
            needs_rebuild = False  # files unchanged — safe to load existing DB

    if needs_rebuild:
        print(f"[INFO] Building ChromaDB for role: {role}")

        # Load all documents from allowed folders
        documents = load_documents(folders, base_path)

        if not documents:
            raise ValueError(f"No documents found for role '{role}'. Check your data folders.")

        # Split documents into chunks of 2000 characters with 200 overlap
        # Overlap ensures sentences at chunk boundaries aren't cut in half
        chunks = RecursiveCharacterTextSplitter(
            chunk_size=2000,
            chunk_overlap=200
        ).split_documents(documents)

        # Delete old DB if it exists (removes stale vectors from deleted files)
        if os.path.exists(persist_dir):
            shutil.rmtree(persist_dir)

        # Create new ChromaDB — converts chunks to vectors and saves to disk
        db = Chroma.from_documents(chunks, embedding, persist_directory=persist_dir)

        # Save fingerprint so next run can detect if files changed
        os.makedirs(persist_dir, exist_ok=True)
        with open(hash_file, "w") as f:
            f.write(current_hash)

    else:
        print(f"[INFO] Loading existing ChromaDB for role: {role}")
        # Load existing DB from disk — much faster than rebuilding
        db = Chroma(persist_directory=persist_dir, embedding_function=embedding)

    return db


# ── Step 8: Main function — tie everything together ───────────────────────────
# This is the function called by FastAPI when a user asks a question.
# It runs the full RAG pipeline:
#   Check role → Get DB → Search docs → Build prompt → Get answer

def ask_question(role: str, query: str) -> dict:

    BASE_PATH = "../data"  # where all the data folders live

    # --- 1. Check if the role is valid ---
    is_valid, error_msg = enforce_rbac(role)
    if not is_valid:
        # Return error immediately — no DB or LLM work wasted
        return {"answer": error_msg, "sources": []}

    # Normalize role — "Finance " becomes "finance"
    role = role.strip().lower()

    # Get the list of folders this role is allowed to read
    allowed_folders = ROLE_FOLDERS[role]

    # --- 2. Get the ChromaDB for this role ---
    try:
        db = get_or_build_vectorstore(role, allowed_folders, BASE_PATH)
    except ValueError as e:
        return {"answer": str(e), "sources": []}

    # --- 3. Search for relevant chunks ---
    # MMR = Maximal Marginal Relevance
    # fetch_k=30: first fetch 30 similar chunks
    # k=10: then pick the 10 most DIVERSE ones from those 30
    # This prevents getting 10 chunks all from the same document
    retrieved_docs = db.max_marginal_relevance_search(query, k=10, fetch_k=30)

    if not retrieved_docs:
        return {"answer": "I do not have access to that information.", "sources": []}

    # --- 4. Build context from retrieved chunks ---
    # Join all chunks into one big string with source labels
    # Example: "[Source: quarterly_financial_report.md]\n...content..."
        # 1. Create an empty list to hold our formatted chunks
    formatted_chunks = []

    # 2. Loop through the documents normally
    for doc in retrieved_docs:
        
        # 3. Get the filename safely
        filename = doc.metadata.get('source', 'unknown')
        
        # 4. Format the text block
        text_block = f"[Source: {filename}]\n{doc.page_content}"
        
        # 5. Add it to our list
        formatted_chunks.append(text_block)

    # 6. Glue the whole list together using our visual divider
    context = "\n\n---\n\n".join(formatted_chunks)

    # Get unique source filenames for the response
    # 1. Create an empty list to hold our source filenames
    sources = []

    # 2. Loop through our 10 relevant document chunks
    for doc in retrieved_docs:
        
        # 3. Get the filename safely
        filename = doc.metadata.get("source", "unknown")
        
        # 4. Check if this filename is ALREADY in our list
        if filename not in sources:
            # 5. If it is brand new, add it to the list
            sources.append(filename)

    # 6. Alphabetize the final list so it looks nice for the user
    sources.sort()

    # --- 5. Build the prompt and call LLaMA ---
    # The prompt tells LLaMA exactly what to do and what NOT to do
    prompt = f"""You are a secure enterprise AI assistant for FinSolve Technologies.

RULES:
- Answer ONLY using the context provided below.
- If the answer is not in the context, say: "I do not have access to that information."
- Never guess or make up data.
- Be concise and professional.
- Always mention the source document name when referencing data.

CONTEXT:
{context}

QUESTION: {query}

ANSWER:"""

    try:
        client = Groq(api_key=os.getenv("GROQ_API_KEY"))
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",  # LLaMA 70B model on Groq
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,    # low = more factual, less creative
            max_tokens=1024,    # max length of the answer (~750 words)
        )
        answer = response.choices[0].message.content.strip()
    except Exception as e:
        answer = f"LLM call failed: {e}"

    # --- 6. Return answer + sources ---
    return {"answer": answer, "sources": sources}
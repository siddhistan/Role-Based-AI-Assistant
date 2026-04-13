# FinSolve — RAG-Based Role-Access Enterprise Chatbot

An enterprise-grade Retrieval-Augmented Generation (RAG) chatbot built over the [Codebasics FinSol dataset](https://codebasics.io/) — a structured dataset simulating real HR, finance, engineering, and marketing data, segmented by organizational role.

Users authenticate, receive a JWT, and ask questions. The system retrieves only from documents that their role is permitted to see, then generates a grounded answer using LLaMA-3.3-70B via Groq.

---

## Architecture

```
POST /login  (username + password)
      │
      ▼
authenticate_user()
  ├── Argon2 hash verify
  ├── Account lock check
  └── 24hr failed-attempt reset
      │
      ▼
Returns: JWT access token + refresh token (SHA-256 hashed in DB)

─────────────────────────────────────────

POST /ask  (token + question)
      │
      ▼
verify_token()  → decode JWT, check signature + expiry
      │
      ▼
Role fetched from DB  (NOT from JWT payload — prevents privilege escalation)
      │
      ▼
enforce_rbac()
  ├── Layer 1: is role a known valid role?
  └── Layer 2: is role actually allowed in the folders it maps to?
      │
      ▼
get_or_build_vectorstore()
  ├── MD5 hash of data files → compare with stored hash
  ├── If match → load existing ChromaDB (fast path)
  └── If mismatch → wipe and rebuild ChromaDB
      │
      ▼
max_marginal_relevance_search()  (fetch_k=30, k=10)
      │
      ▼
Prompt → Groq (LLaMA-3.3-70B) → Answer + Sources
```

---

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| POST | `/login` | Authenticate and receive JWT + refresh token |
| POST | `/refresh` | Rotate refresh token and get new access token |
| POST | `/ask` | Ask a question (requires valid JWT) |

---

## Authentication System

Built in `auth.py` — full production-security implementation:

| Feature | Implementation |
|---|---|
| Password hashing | Argon2 (via `passlib`) |
| Access token | JWT signed with HS256, short-lived |
| Refresh token | `secrets.token_urlsafe(32)`, SHA-256 hashed before storage |
| Token rotation | Every refresh issues a new refresh token; old one is invalidated |
| Account lockout | Locked after 5 failed attempts |
| Exponential backoff | Each subsequent lockout (groups of 5 failures) doubles the lockout duration: `base_minutes × 2^(lock_count - 1)` |
| Last attempt warning | Returns `LAST_ATTEMPT` warning on the 4th failed try |
| 24-hour reset | Failed attempt counter resets automatically after 24 hours of no activity |
| Absolute session expiry | Sessions expire after a fixed window regardless of refresh token activity |

Role is **always fetched from the database** on the `/ask` endpoint — never trusted from the JWT payload. This prevents a scenario where an admin changes a user's role but the old JWT is still in circulation.

---

## RBAC — Role Based Access Control

Defined in `rag_engine.py`:

```python
ROLE_FOLDERS = {
    "finance":     ["finance"],
    "hr":          ["hr", "general"],
    "engineering": ["engineering"],
    "marketing":   ["marketing"],
    "employee":    ["general"],
    "c-level":     ["finance", "hr", "engineering", "marketing", "general"],
}
```

**Two-layer enforcement:**
- **Layer 1** — Is the role a valid known role?
- **Layer 2** — Is the role actually permitted in each folder it maps to? (prevents privilege escalation at runtime)

Each role gets its own isolated ChromaDB vector store (`chroma_db_finance/`, `chroma_db_hr/`, etc.). A query from `hr` never touches the `finance` vector store.

---

## RAG Configuration

| Parameter | Value |
|---|---|
| Embedding model | `all-MiniLM-L6-v2` (HuggingFace) |
| Chunk size | 2000 characters |
| Chunk overlap | 200 characters |
| Retrieval strategy | Maximal Marginal Relevance (fetch_k=30, k=10) |
| LLM | LLaMA-3.3-70B via Groq |
| Temperature | 0.2 (factual, low creativity) |
| Max tokens | 1024 |

**MMR vs plain top-k:** Maximal Marginal Relevance first fetches the 30 most similar chunks, then selects the 10 most *diverse* among them. This prevents 10 results all from the same document when multiple documents are relevant.

---

## Semantic Collision Fix

Q1 and Q4 financial reports discuss the same topics (revenue, vendor costs, gross margin). Standard embeddings produce nearly identical vectors for these — causing the retriever to mix up quarterly data.

**Fix:** A metadata header is prepended to every chunk *before* embedding:

```
[Document: quarterly_finance_q1_2024.md] [Type: quarterly finance q1 2024] [Period: Q1 2024]

<actual chunk content>
```

This encodes document identity into the embedding itself, making Q1 and Q4 vectors genuinely distinct.

---

## Smart Cache Invalidation

Before loading ChromaDB, an MD5 fingerprint is computed from all files in the role's folders (filename + last-modified time + file size). If the fingerprint matches the stored `.hash` file, the existing DB is reused. If anything changed (file added, edited, deleted), the DB is wiped and rebuilt automatically.

---

## Document Support

Supported formats: `.md`, `.txt`, `.csv`, `.pdf`, `.docx`

File tags (`quarter`, `year`, `doc_type`) are extracted from filenames during loading and stored as ChromaDB metadata for source attribution in responses.

---

## Tech Stack

| Layer | Technology |
|---|---|
| API Framework | FastAPI |
| Vector Store | ChromaDB |
| Embeddings | HuggingFace `all-MiniLM-L6-v2` |
| LLM | LLaMA-3.3-70B via Groq |
| Orchestration | LangChain |
| Auth | JWT (`python-jose`) + Argon2 (`passlib`) |
| Dataset | Codebasics FinSol |

---

## Setup

### Prerequisites
- Python 3.10+
- Groq API key ([get one here](https://console.groq.com/))

### Installation

```bash
git clone https://github.com/siddhistan/Role-Based-AI-Assistant
cd Role-Based-AI-Assistant
pip install -r requirements.txt
```

### Environment Variables

Create a `.env` file in the `backend/` directory:

```env
SECRET_KEY=your_jwt_secret_here
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=30
REFRESH_TOKEN_EXPIRE_DAYS=7
ABSOLUTE_SESSION_EXPIRE_DAYS=30
MAX_FAILED_ATTEMPTS=5
LOCKOUT_BASE_MINUTES=15
LOCKOUT_RESET_HOURS=24
GROQ_API_KEY=your_groq_api_key_here
```

### Run

```bash
cd backend
uvicorn main:app --reload
```

---

## My Contributions

This was a team project. My ownership:

**`rag_engine.py`**
- Two-layer RBAC enforcement (`enforce_rbac()`)
- Document loading pipeline with multi-format support
- Metadata extraction from filenames (quarter, year, doc_type)
- Semantic collision fix — metadata header prepended to chunks before embedding
- MD5-based cache invalidation (`compute_folder_hash()`)
- HuggingFace embedding integration (singleton pattern)
- ChromaDB vector store management — per-role isolation, build/load logic
- MMR retrieval configuration

**`auth.py`** — entire file:
- Argon2 password hashing
- JWT access token creation and verification
- Refresh token generation (SHA-256 hashed before storage)
- Token rotation on refresh
- Account lockout with exponential backoff
- 24-hour failed-attempt counter reset
- Absolute session expiry

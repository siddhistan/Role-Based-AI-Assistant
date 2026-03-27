import os
from dotenv import load_dotenv
from rag_engine import ask_question, ROLE_FOLDERS

load_dotenv()

# ── Role selection ────────────────────────────────────────────────────────────
print("\n" + "="*55)
print("  FinSolve Technologies — Enterprise AI Assistant")
print("="*55)
print(f"\nAvailable roles: {', '.join(sorted(ROLE_FOLDERS.keys()))}")

role = input("\nEnter your role: ").strip()

# Quick pre-check for friendly message
# Full RBAC enforcement happens inside ask_question() via enforce_rbac()
if role.strip().lower() not in ROLE_FOLDERS:
    print(f"\nInvalid role: '{role}'")
    print(f"Valid roles: {', '.join(sorted(ROLE_FOLDERS.keys()))}")
    exit()

print(f"\nAccess granted for role: {role.lower()}")
print("ChromaDB loading... (first run may take a minute)\n")

# ── Chat loop ─────────────────────────────────────────────────────────────────
while True:
    query = input("\nAsk your question (or type 'exit' to quit): ").strip()

    if query.lower() == "exit":
        print("\nGoodbye!")
        break

    if not query:
        print("Please enter a question.")
        continue

    # Full improved pipeline:
    #   enforce_rbac()
    #   → get_or_build_vectorstore() with hash-based auto-rebuild
    #   → load_documents() with metadata prefix (collision fix)
    #   → MMR search (diversity across quarters)
    #   → Groq LLaMA answer generation
    result = ask_question(role, query, debug=True)

    print("\n" + "-"*55)
    print("ANSWER:\n")
    print(result["answer"])

    print("\nSOURCES:")
    if result["sources"]:
        for source in result["sources"]:
            print(f"  - {source}")
    else:
        print("  No sources found.")
    print("-"*55)
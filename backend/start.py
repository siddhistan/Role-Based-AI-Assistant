

# ASK ROLE 
role = input("Enter your role: ").lower()

if role not in ROLE_FOLDERS:
    print("Invalid role.")
    exit()

print(f"\nAccess granted for role: {role}\n")

#  LOAD ONLY ALLOWED DOCS
BASE_PATH = "../data"
documents = []

allowed_folders = ROLE_FOLDERS[role]

for folder in allowed_folders:
    folder_path = os.path.join(BASE_PATH, folder)

    for file in os.listdir(folder_path):
        if file.endswith(".md"):
            loader = TextLoader(os.path.join(folder_path, file), encoding="utf-8")
            documents.extend(loader.load())

#  EMBEDDINGS
embedding = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")
db = Chroma.from_documents(documents, embedding)

# QUESTION
query = input("Ask your question: ")

retrieved_docs = db.similarity_search(query, k=3)
context = "\n\n".join([doc.page_content for doc in retrieved_docs])

# LLM CALL
client = Groq(api_key=os.getenv("GROQ_API_KEY"))

prompt = f"""
You are an internal company assistant.
Answer the question ONLY using the context below.

Context:
{context}

Question:
{query}

Answer clearly and concisely.
"""

response = client.chat.completions.create(
    model="llama-3.1-8b-instant",
    messages=[
        {"role": "user", "content": prompt}
    ],
    temperature=0.2
)

print("\n Answer:\n")
print(response.choices[0].message.content)

print("\n Sources:\n")
for doc in retrieved_docs:
    print(doc.metadata.get("source"))

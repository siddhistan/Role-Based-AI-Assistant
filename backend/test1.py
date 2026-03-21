from langchain_community.document_loaders import PyPDFLoader
from langchain_community.document_loaders import Docx2txtLoader

from langchain_text_splitters import RecursiveCharacterTextSplitter
import os
from dotenv import load_dotenv
from langchain_community.document_loaders import TextLoader
from langchain_community.vectorstores import Chroma
from langchain_community.embeddings import HuggingFaceEmbeddings
from groq import Groq

load_dotenv()

ROLE_FOLDERS = {
    "finance": ["finance"],
    "hr": ["hr"],
    "engineering": ["engineering"],
    "marketing": ["marketing"],
    "employee": ["general"],
    "c-level": ["finance", "hr", "engineering", "marketing", "general"]
}

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
        filepath = os.path.join(folder_path, file)
        if file.endswith(".md"):
                loader = TextLoader(filepath, encoding="utf-8")
                documents.extend(loader.load())
        elif file.endswith(".csv"):
                from langchain_community.document_loaders import CSVLoader
                loader = CSVLoader(filepath, encoding="utf-8")
                documents.extend(loader.load())
        elif file.endswith(".pdf"):
                loader = PyPDFLoader(filepath)
                documents.extend(loader.load())
        elif file.endswith(".docx"):
                loader = Docx2txtLoader(filepath)
                documents.extend(loader.load())

splitter = RecursiveCharacterTextSplitter(
        chunk_size=1500,
        chunk_overlap=100
    )
documents = splitter.split_documents(documents)

#  EMBEDDINGS
embedding = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")
db = Chroma.from_documents(documents, embedding)

while True:
    query = input("\nAsk your question (or type 'exit' to quit): ")
    if query.lower() == 'exit':
        break
    
    retrieved_docs = db.similarity_search(query, k=7)
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
          model="llama-3.3-70b-versatile",
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
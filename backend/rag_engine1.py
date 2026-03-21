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

def ask_question(role, query):
    
    #  LOAD ONLY ALLOWED DOCS
    BASE_PATH = "../data"
    documents = []

    allowed_folders = ROLE_FOLDERS.get(role)

    if not allowed_folders:
        return "Invalid role."

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
                    
    # ADD HERE — after loading, before embeddings
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=500,
        chunk_overlap=50
    )
    documents = splitter.split_documents(documents)
    
    #  EMBEDDINGS
    embedding = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")

    if not os.path.exists(f"./chroma_db_{role}"):
            db = Chroma.from_documents(
                 documents,
                 embedding,
                 persist_directory=f"./chroma_db_{role}"
            )
    else:
        db = Chroma(
            persist_directory=f"./chroma_db_{role}",
            embedding_function=embedding
        )


    #SEARCH FOR RELEVANT DOCS
    retrieved_docs = db.similarity_search(query, k=5)

    context = "\n\n".join([doc.page_content for doc in retrieved_docs])

    # LLM CALL
    client = Groq(api_key=os.getenv("GROQ_API_KEY"))

    prompt = f"""
    You are a secure enterprise AI assistant.

    IMPORTANT RULES:
    - Answer ONLY from the provided context.
    - Do NOT use outside knowledge.
    - If the answer is not present in the context, say:
    "I do not have access to that information."
    - Do NOT guess.
    - Do NOT fabricate information.

    Context:
    {context}

    Question:
    {query}

    Provide a clear, professional answer.
    """
    
    response = client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2
    )

    sources = [doc.metadata.get("source") for doc in retrieved_docs]

    return {
        "answer": response.choices[0].message.content,
        "sources": sources
    }

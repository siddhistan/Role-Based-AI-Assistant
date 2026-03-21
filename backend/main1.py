from fastapi import FastAPI
from pydantic import BaseModel

from rag_engine1 import ask_question

app = FastAPI()


class QueryRequest(BaseModel):
    role: str
    question: str


@app.post("/ask")
def ask_ai(request: QueryRequest):

    result = ask_question(request.role.lower(), request.question)
    return result

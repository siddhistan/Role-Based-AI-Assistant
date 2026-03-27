from fastapi import FastAPI, HTTPException   # FastAPI framework, HTTPException for error responses
from pydantic import BaseModel               # Used to define request body structure (JSON input)

from rag_engine import ask_question          # Your existing RAG function
from auth import authenticate_user, create_access_token, verify_token   # NEW: Import auth functions

app = FastAPI()                              # Create FastAPI app


# NEW: Model for login request body
class LoginRequest(BaseModel):
    username: str
    password: str


# CHANGED: Old QueryRequest had role + question
# Now role is removed and replaced with token
class QueryRequest(BaseModel):
    token: str
    question: str


# NEW: Login endpoint
@app.post("/login")
def login(request: LoginRequest):

    # NEW: Check if username + password are correct
    user = authenticate_user(request.username, request.password)

    # If authentication fails → return error
    if not user:
        raise HTTPException(status_code=401, detail="Invalid username or password")

    # NEW: Create JWT token with username and role inside payload
    token = create_access_token({
        "username": request.username.lower(),
        "role": user["role"]
    })

    # Send token back to user
    return {"access_token": token}


# CHANGED: This endpoint is now protected
@app.post("/ask")
def ask_ai(request: QueryRequest):

    # NEW: Verify token and extract role from JWT
    role = verify_token(request.token)

    # If token invalid or expired → error
    if not role:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    # Same as before, but role now comes from token (not user input)
    result = ask_question(role, request.question)
    return result
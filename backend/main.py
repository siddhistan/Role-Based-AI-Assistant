from fastapi import FastAPI, HTTPException   # FastAPI framework, HTTPException for error responses
from pydantic import BaseModel               # Used to define request body structure (JSON input)

from rag_engine import ask_question          # Your existing RAG function
from auth import authenticate_user, create_access_token, verify_token   # NEW: Import auth functions
from auth import create_refresh_token,load_users,save_users 
import time

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
    
    if user=="LOCKED":
        raise HTTPException(status_code=403, detail="Account locked. Try again later.")
    
    if user=="LAST_ATTEMPT":
        raise HTTPException(status_code=401, detail="Invalid username or password. Warning. Last Attempt left.")

    # If authentication fails → return error
    if not user:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    
    #The session clock starts, absolute session is calculated with env variable
    users=load_users()
    users[request.username.lower()]["session_start"] = time.time()
    save_users(users)

    # NEW: Create JWT token with username and role inside payload
    access_token = create_access_token({
        "sub": request.username.lower(),
        "role": user["role"]
    })
    
    refresh_token = create_refresh_token(request.username.lower())

    # Send token back to user
    return { 
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer"
           }

class RefreshRequest(BaseModel):
    refresh_token: str
    
@app.post("/refresh")
def refresh(request: RefreshRequest):
    from auth import refresh_access_token
    tokens = refresh_access_token(request.refresh_token)

    if not tokens:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

    return tokens
    
    
@app.post("/ask")
def ask_ai(request: QueryRequest):
    
    # NEW: Verify token and extract payload from JWT
    payload = verify_token(request.token)

    # If token invalid or expired → error
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    
    username = payload["sub"]

    # Load user from database
    users = load_users()
    user = users.get(username)

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Check if account is locked
    if user["lock_until"] > time.time():
        raise HTTPException(status_code=403, detail="Account is locked")

    # IMPORTANT: Get role from database, not from JWT
    role = user["role"]

    result = ask_question(role, request.question)
    return result
    
from fastapi import FastAPI, HTTPException   # FastAPI framework, HTTPException for error responses
from pydantic import BaseModel               # Used to define request body structure (JSON input)

from rag_engine import ask_question          # Your existing RAG function
from auth import authenticate_user, create_access_token, verify_token   # NEW: Import auth functions
from auth import create_refresh_token, get_user, update_user
import time  #used for session tracking and lock checks
from dotenv import load_dotenv #loads environment file into the environment
import os

app = FastAPI()                              # Create FastAPI app

# This reads our .env file and store values into  environment
load_dotenv()

ABSOLUTE_SESSION_EXPIRE_DAYS = float(os.getenv("ABSOLUTE_SESSION_EXPIRE_DAYS"))

ALLOWED_ROLES = {"admin", "hr", "engineering", "employee", "marketing", "finance", "c-level"} #List for allowed folders


# NEW: Model for login request body
class LoginRequest(BaseModel):
    emp_id: str
    password: str


# CHANGED: Old QueryRequest had role + question
# Now role is removed and replaced with token
class QueryRequest(BaseModel):
    token: str
    question: str


# NEW: Login endpoint  (When the user hits login)
@app.post("/login")
def login(request: LoginRequest):

    # NEW: Check if emp_id + password are correct, we go to auth.py file to check
    user = authenticate_user(request.emp_id, request.password)
    
    if user=="LOCKED": #if account is locked
        raise HTTPException(status_code=403, detail="Account locked. Try again later.") #We send hhtp response back to the client and the client understands the type of error
    
    if user=="LAST_ATTEMPT":   #if account has last attempt remaining
        raise HTTPException(status_code=401, detail="Invalid emp_id or password. Warning. Last Attempt left.")

    # If authentication fails → return error, user has entered either wrong emp_id or password
    if not user:   
        raise HTTPException(status_code=401, detail="Invalid emp_id or password")
    
    #lowering emp_id so that emp101 and EMP101 is same
    emp_id = request.emp_id.lower()

    user_db = get_user(emp_id)   # fetch user from DB by his emp_id
    
    if not user_db:
        raise HTTPException(status_code=404, detail="User not found")
    
    user_db["session_start"] = time.time()   #The session clock starts, it is updated with current time, everytime we login, session time resets
    update_user(emp_id, user_db)   # save updated data to DB

    session_start = user_db["session_start"]   #session_start variable holds the session start time which we will include in our jwt
     
    # NEW: Create JWT token with emp_id and role inside payload
    access_token = create_access_token({   #We call the auth.py file and it creates access token and returns it to main
        "sub": emp_id,
        "role": user["role"] ,     
        "session_start": session_start     #adding session_start_time in jwt   
    })
    #we are passing emp_id and his role as parameters to auth file, which creates jwt token and returns it to main in access_token variable
    
    refresh_token = create_refresh_token(emp_id) #We call creeate_refresh function and  pass the emp_id as parameter to create_refresh function defined in auth file, which creates and returns the refresh token to us

    # Send token back to user, “We send this data as an HTTP response back to the client”
    return { 
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer"  #Bearer = whoever holds the token is the user                                          It tells the client how to use the token, typically indicating it should be sent as a Bearer token in the Authorization header.
           }
    

class RefreshRequest(BaseModel):
    refresh_token: str
    
@app.post("/refresh")   # called when frontend requests token refresh 
def refresh(request: RefreshRequest):      #defining refresh api
    from auth import refresh_access_token
    tokens = refresh_access_token(request.refresh_token)

    if not tokens:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

    return tokens  #shows both tokens to the user as http response, visible on frontend
    
'''# Client sends refresh token (string) to this endpoint
# This endpoint is called by frontend when access token expires (not a UI button defined here)

# We call refresh_access_token() from auth.py and pass the refresh token
# Control goes to auth.py where:
#   - The refresh token is validated (hash match, expiry, session, lock checks)
#   - If valid, a NEW access token and NEW refresh token are generated
#   - The new refresh token replaces the old one in users.json (rotation)

# The function returns the new tokens back to main.py

# If validation fails → raise HTTPException (401 Unauthorized)
# If successful → return new tokens as HTTP response (JSON) to the client(user)'''
    
@app.post("/ask")  #we click the ask button
def ask_ai(request: QueryRequest):   #defining the ask endpoint here, user provides token and question
    
    # NEW: Verify token and extract payload from JWT, verify function is called here and executes in auth.py file
    payload = verify_token(request.token) #We pass access token as argument, the function returns payload which contains emp_id, role and the expiry date of the token

    # If token invalid or expired → error is shown to user
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    
    emp_id = payload["sub"].lower()  #We store the emp_id of user from the returned payload in the emp_id variable,

    # We Load users from database
   
    user = get_user(emp_id)   # fetch user from DB using his emp_id

    if not user:  #if user not found, error is raised
        raise HTTPException(status_code=404, detail="User not found")
    
    # absolute session expiry check
    ABSOLUTE_SESSION_EXPIRE_SECONDS = ABSOLUTE_SESSION_EXPIRE_DAYS * 86400
    if time.time() > user.get("session_start", 0) + ABSOLUTE_SESSION_EXPIRE_SECONDS:
            raise HTTPException(status_code=401, detail="Session expired. Please login again.")
    
    token_session = payload.get("session_start")

    #if user isn't logged in (he has logged out), session_start_time wouldn't match. Here, we are checking session_start time of jwt with Database
    if token_session != user.get("session_start"):
        raise HTTPException(status_code=401, detail="Session expired. Please login again.")

    # Check if account is locked, if it is, then error is raised
    #If the account is locked, then even if access token(JWT) is valid, you can't ask and error is raised
    if user["lock_until"] > time.time():
        raise HTTPException(status_code=403, detail="Account is locked")

    # IMPORTANT: # Get role from database (do NOT trust role from JWT for security reasons), also if admin changed user's role, so we are checking to make sure the role is correct 
    role = user["role"]

    # Pass user's role and the question
    # This function performs RBAC (Role Based Authentication Checks), retrieves documents, calls LLM, and returns answer
    result = ask_question(role, request.question) 
    
    return result  #Return the result (answer + sources) as HTTP response to the client
    
class LogoutRequest(BaseModel):
    refresh_token: str
    
#When logout endpoint is called
@app.post("/logout")
def logout(request: LogoutRequest):
    from auth import logout_user

    #we call the logout function and it executes in auth.py
    #on successful logout, true is returned and on unsuccessful logout, false is returned, and we store it in result
    result = logout_user(request.refresh_token)   

    #if logout is unsuccessful
    if not result: 
       raise HTTPException(status_code=401, detail="Invalid refresh token")

    #if logout is successful
    return {"message": "Logged out successfully"}



#End points for admin
class CreateUserRequest(BaseModel):
    emp_id: str
    role:   str
    name:   str
    
# if admin wants to add a new user
@app.post("/admin/create-user")
def create_user(request: CreateUserRequest, token: str):
    from auth import verify_token
    from shared_cons import connection_pool

    payload = verify_token(token)

    #checking if admin's jwt is valid or not
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid token")

    #  Extract admin's identity(emp_id which is admin) from JWT
    emp_id_from_token = payload["sub"].lower()

    #now we are checking the db to ensure if this emp_id (that we extracted from jwt) exist or not in db
    user = get_user(emp_id_from_token)  #so user contains all the fields of emp_id=admin in db
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Fetch admin's role from DB (NOT JWT)
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    #Session binding check, If Admin has logged out, he should not be able to create_new_user
    token_session = payload.get("session_start")
    if token_session != user.get("session_start"):
            raise HTTPException(status_code=401, detail="Session expired. Please login again.")

    # Lock check, If Admin's account is locked, jwt shouldn't be allowed to create new user
    if user["lock_until"] > time.time():
           raise HTTPException(status_code=403, detail="Account is locked")

    emp_id = request.emp_id.lower()  #converting new emp_id provided by admin of the new user to lowercase
    
    #Removes spaces from start and end, ex: "  Sid " becomes "Sid" and "  " becomes ""
    name = request.name.strip()
    #if admin has enetered an empty name, then we raise an error
    if not name:
        raise HTTPException(status_code=400, detail="Name cannot be empty")

    # Check if emp_id already exists in db
    existing_user = get_user(emp_id)
    if existing_user:
        raise HTTPException(status_code=400, detail="User already exists")

    #getting the new user's role from admin
    role = request.role.lower()

    #checking if admin created user role exists or not 
    if role not in ALLOWED_ROLES:
            raise HTTPException(status_code=400, detail="Invalid role")
    
    if role == "admin":
        raise HTTPException(status_code=403, detail="Cannot create admin users")

    # Create new user WITHOUT password
    conn = connection_pool.getconn()
    try:
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO users (
                emp_id, name, password_hash, role,
                failed_attempts, lock_until, lock_count,
                last_failed_login, refresh_token,
                refresh_token_expiry, session_start
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            emp_id,
            name,
            "",
            role,
            0,
            0,
            0,
            0,
            "",
            0,
            0
        ))

        conn.commit()
        cur.close()
    finally:
        connection_pool.putconn(conn)

    return {"message": f"User {emp_id} created successfully"}



#allowing user to set password only if user exists and password is currently empty
class SetPasswordRequest(BaseModel):
    emp_id: str
    new_password: str
    
#user enters his emp_id and new_password to set
@app.post("/set-password")
def set_password(request: SetPasswordRequest, token: str = None):
    from auth import set_user_password, verify_token
    
    #Empty password (like "" or "  ") is not allowed
    new_password = request.new_password.strip()
    if not new_password:
        raise HTTPException(status_code=400, detail="Password cannot be empty")

    emp_id = request.emp_id.lower()

    user = get_user(emp_id)

    #if user does not exist, we throw error
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # CASE 1: First-time password setup (no password exists)
    # we allow this WITHOUT requiring token (onboarding flow)
    if not user.get("password_hash"):
        result = set_user_password(emp_id,new_password)

        if result == "USER_NOT_FOUND":
            raise HTTPException(status_code=404, detail="User not found")

        return {"message": "Password set successfully"}

    # CASE 2: Password already exists → require authentication
    #we first verify the jwt token provided by user
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required")

    payload = verify_token(token)

    #if token is invalid or expired, we throw error
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    #we extract emp_id of the logged in user from jwt
    emp_id_from_token = payload["sub"].lower()

    #we ensure that user can only set his OWN password and not someone else's
    if emp_id_from_token != emp_id:
        raise HTTPException(status_code=403, detail="Not authorized to set this password")

    #if password already exists, we do not allow resetting here
    raise HTTPException(status_code=400, detail="Password already set. Use change-password instead.")


#endpoint for changing password
class ChangePasswordRequest(BaseModel):
    emp_id: str
    old_password: str
    new_password: str
    
#user enters his emp_id, old password to verify and the new password he wants to replace the old password with
@app.post("/change-password")
def change_password(request: ChangePasswordRequest):
    from auth import change_user_password
    
    #old password can't be replaced with Empty password 
    new_password = request.new_password.strip()
    if not new_password:
        raise HTTPException(status_code=400, detail="Password cannot be empty")

    #if password is correct, success message is returned, else some kind of error message is returned
    result = change_user_password(
        request.emp_id,
        request.old_password,
        new_password
    )

    if result == "USER_NOT_FOUND":
        raise HTTPException(status_code=404, detail="User not found")

    if result == "LOCKED":
        raise HTTPException(status_code=403, detail="Account is locked")

    if result == "PASSWORD_NOT_SET":
        raise HTTPException(status_code=400, detail="Password not set yet")

    if result == "WRONG_PASSWORD":
        raise HTTPException(status_code=401, detail="Incorrect old password")

    return {"message": "Password changed successfully. Please login again."}

import json
import os
from datetime import datetime, timedelta, timezone
import time

from dotenv import load_dotenv
from jose import jwt
from passlib.context import CryptContext

import secrets
import hashlib

# Load environment variables
load_dotenv()

SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM = os.getenv("ALGORITHM")
ACCESS_TOKEN_EXPIRE_MINUTES = float(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES"))

REFRESH_TOKEN_EXPIRE_DAYS = float(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS"))
ABSOLUTE_SESSION_EXPIRE_DAYS = float(os.getenv("ABSOLUTE_SESSION_EXPIRE_DAYS"))

MAX_FAILED_ATTEMPTS = int(os.getenv("MAX_FAILED_ATTEMPTS"))
LOCKOUT_BASE_MINUTES = float(os.getenv("LOCKOUT_BASE_MINUTES"))
LOCKOUT_RESET_HOURS = float(os.getenv("LOCKOUT_RESET_HOURS"))

# Use Argon2 for hashing
pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")


# Load users from users.json
def load_users():
    with open("users.json", "r") as file:
        return json.load(file)
    
def save_users(users):
    with open("users.json", "w") as file:
        json.dump(users, file, indent=4)


# Verify password
def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)


# Authenticate user during login
def authenticate_user(username, password):
    users = load_users()
    username = username.lower()
    user = users.get(username)

    if not user:
        return None

    current_time = time.time()

    # Auto reset after 24 hours
    if user["last_failed_login"] != 0:
        if current_time - user["last_failed_login"] > (LOCKOUT_RESET_HOURS * 3600):
            user["failed_attempts"] = 0
            user["lock_count"] = 0

    # Check if account is locked
    if user["lock_until"] > current_time:
        return "LOCKED"

    # Check password
    if verify_password(password, user["password_hash"]):
        # Correct password → reset everything
        user["failed_attempts"] = 0
        user["lock_count"] = 0
        user["lock_until"] = 0
        user["last_failed_login"] = 0

        save_users(users)
        return user

    else:
        # Wrong password
        user["failed_attempts"] += 1
        user["last_failed_login"] = current_time

        # If 4 attempts → warning
        if user["failed_attempts"] == MAX_FAILED_ATTEMPTS-1:
            save_users(users)
            return "LAST_ATTEMPT"

        # Lock account after 5 failed attempts
        if user["failed_attempts"] >= MAX_FAILED_ATTEMPTS:
            user["lock_count"] += 1
            lock_minutes = LOCKOUT_BASE_MINUTES * (2 ** (user["lock_count"] - 1))
            user["lock_until"] = current_time + (lock_minutes * 60)
            user["failed_attempts"] = 0

            save_users(users)
            return "LOCKED"

        save_users(users)
        return None

# Create JWT token
def create_access_token(data: dict):
    to_encode = data.copy()

    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": int(expire.timestamp())})  # convert to UNIX timestamp

    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

#Creating Refresh Token
def create_refresh_token(username: str):
    users = load_users()

    refresh_token = secrets.token_urlsafe(32)
    refresh_token_hash = hashlib.sha256(refresh_token.encode()).hexdigest()
    
    current_time=time.time()
    
    refresh_token_expiry= current_time + (REFRESH_TOKEN_EXPIRE_DAYS *24 *60 *60)
    
    users[username]["refresh_token"] = refresh_token_hash
    users[username]["refresh_token_expiry"] = refresh_token_expiry

    save_users(users)

    return refresh_token

#Function to refresh the access token
def refresh_access_token(refresh_token: str):
    users = load_users()
    refresh_token_hash = hashlib.sha256(refresh_token.encode()).hexdigest()
    current_time = time.time()

    for username, user in users.items():

        # Check if refresh token matches
        if user.get("refresh_token") == refresh_token_hash:
            
            #if account is locked, then deny refresh
            if user["lock_until"] > current_time:
                return None
            
            #Absolute session expiry check
            session_start= user.get("session_start")
            #If no active session, i.e, session=0 or missing in users.json, refresh not allowed
            if not session_start:
                return None
            
            absolute_expiry=session_start+ (ABSOLUTE_SESSION_EXPIRE_DAYS * 24 * 60 *60)
            
            if current_time>absolute_expiry:
                return None

            # Check if refresh token expired
            if user.get("refresh_token_expiry", 0) < current_time:
                return None

            # Create new access token
            access_token = create_access_token({
                "sub": username,
                "role": user["role"]
            })

            # Create NEW refresh token (rotation)
            new_refresh_token = secrets.token_urlsafe(32)
            new_refresh_token_hash = hashlib.sha256(new_refresh_token.encode()).hexdigest()
            new_expiry = current_time + (REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 *60)

            user["refresh_token"] = new_refresh_token_hash
            user["refresh_token_expiry"] = new_expiry

            save_users(users)

            return {
                "access_token": access_token,
                "refresh_token": new_refresh_token
            }

    return None

# Verify JWT token
from jose import JWTError, ExpiredSignatureError

def verify_token(token: str):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    
    except ExpiredSignatureError:
        print("Token expired")
        return None
    
    except JWTError:
        print("Invalid token")
        return None
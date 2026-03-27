import json
import os
from datetime import datetime, timedelta, timezone
import time

from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
from jose import jwt
from passlib.context import CryptContext

# Load environment variables
load_dotenv()

SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM = os.getenv("ALGORITHM")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES"))

# Use Argon2 for hashing
pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")


# Load users from users.json
def load_users():
    with open("users.json", "r") as file:
        return json.load(file)


# Verify password
def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)


# Authenticate user during login
def authenticate_user(username, password):
    users = load_users()
    username = username.lower()        # convert input to lowercase
    user = users.get(username)

    if not user:
        return None

    if not verify_password(password, user["password"]):
        return None

    return user


# Create JWT token
def create_access_token(data: dict):
    to_encode = data.copy()

    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": int(expire.timestamp())})  # convert to UNIX timestamp

    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


# Verify JWT token
from jose import JWTError, ExpiredSignatureError

def verify_token(token: str):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload["role"]
    except ExpiredSignatureError:
        print("Token expired")
        return None
    except JWTError:
        print("Invalid token")
        return None
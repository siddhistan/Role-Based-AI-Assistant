from shared_cons import connection_pool            #importing the connection_pool module from shared_cons.py file
import os #used for reading environment variables
from datetime import datetime, timedelta, timezone  #used for creating expiry times for jwt
import time #The time module is used to get the current Unix timestamp (seconds since 1970 UTC), which is used for session tracking, account lock timing, token expiry, and security checks

from dotenv import load_dotenv #loads environment file into the environment
from jose import jwt  #used for creating and verifying jwt
from passlib.context import CryptContext #used for hashing passwords (Argon2 algorithm)

import secrets #generates secure tokens(used for refresh tokens)
import hashlib #used for hashing refresh token before storing


# This reads our .env file and store values into  environment
load_dotenv()

#This block loads configuration values from the .env file and stores them as variables used for JWT, token expiry, and security settings
SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM = os.getenv("ALGORITHM")

#These variables  are stored as int and float,cuz they are stored as strings in env file, and we will need these values to do some math
ACCESS_TOKEN_EXPIRE_MINUTES = float(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES"))

REFRESH_TOKEN_EXPIRE_DAYS = float(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS"))
ABSOLUTE_SESSION_EXPIRE_DAYS = float(os.getenv("ABSOLUTE_SESSION_EXPIRE_DAYS"))

MAX_FAILED_ATTEMPTS = int(os.getenv("MAX_FAILED_ATTEMPTS"))  
LOCKOUT_BASE_MINUTES = float(os.getenv("LOCKOUT_BASE_MINUTES"))
LOCKOUT_RESET_HOURS = float(os.getenv("LOCKOUT_RESET_HOURS"))


# Use Argon2 for hashing
#It creates a password hashing manager
#“This object knows how to hash passwords and verify them using Argon2
pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")


## This function fetches user data from PostgreSQL and returns it as a dictionary
'''# THis function is the bridge between the api and the db
# Called from multiple endpoints (/login, /ask) and auth functions
# Returns data as a dictionary (username → user details)'''

def get_user(emp_id):
    
    conn = connection_pool.getconn()
    
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE emp_id = %s", (emp_id,))  #executes sql queries and can be used to fetch their results in tuple format
        row = cur.fetchone()
        cur.close()
    finally:
        connection_pool.putconn(conn)

    if not row:
        return None
    
    #returns data again in json format
    return {
        "name": row[1],
        "password_hash": row[2],
        "role": row[3],
        "failed_attempts": row[4],
        "lock_until": row[5],
        "lock_count": row[6],
        "last_failed_login": row[7],
        "refresh_token": row[8],
        "refresh_token_expiry": row[9],
        "session_start": row[10]
    }



# Update specific user fields in PostgreSQL database
# Called after modifying user data (login, failed attempts, tokens, etc.)
def update_user(emp_id, user):
    

    # Connect to PostgreSQL database
    conn = connection_pool.getconn()

    try:
        # Create cursor to execute SQL queries
        cur = conn.cursor()

        # Update user data in DB using SQL UPDATE query
        # We only update fields that can change during runtime
        cur.execute("""
            UPDATE users SET
                failed_attempts = %s,
                lock_until = %s,
                lock_count = %s,
                last_failed_login = %s,
                refresh_token = %s,
                refresh_token_expiry = %s,
                session_start = %s,
                password_hash = %s
            WHERE emp_id = %s
        """, (
            user["failed_attempts"],          # number of failed login attempts
            user["lock_until"],               # account lock expiry timestamp
            user["lock_count"],               # number of times account was locked
            user["last_failed_login"],        # last failed login time
            user.get("refresh_token"),        # hashed refresh token
            user.get("refresh_token_expiry"), # refresh token expiry time
            user.get("session_start"),        # session start time
            user["password_hash"],            # password hash (for set/change password)
            emp_id                            # identify which user to update
        ))

        # Save changes permanently in database
        conn.commit()

        # Close cursor and connection to free resources
        cur.close()

    finally:
        connection_pool.putconn(conn)



# Verify password
'''
# Verify user password using Argon2 hashing
# Called in this file only inside authenticate_user() during login
# Compares plain password with stored hash
'''

#parameters are plain_password that user typed and the already stored hash in db 
#the plain password is then hased to check it with the hashed paswsord in db
def verify_password(plain_password, hashed_password):            
    return pwd_context.verify(plain_password, hashed_password) 
    '''this line pwd line does this internally:

         Takes the plain password
         Uses same algorithm (Argon2)
         Re-hashes it
         Compares with stored hash
         It returns true if password is correct else false'''


# Authenticate user during login
#called from login endpoint
def authenticate_user(emp_id, password):
    emp_id = emp_id.lower()   # moved before get_user
    #loads users
    user = get_user(emp_id)

    if not user:
        return None

    current_time = time.time()  #this gets current time

    # Auto reset after 24 hours
    if user["last_failed_login"] != 0:
        if current_time - user["last_failed_login"] > (LOCKOUT_RESET_HOURS * 3600):
            user["failed_attempts"] = 0
            user["lock_count"] = 0

    # Check if account is locked
    if user["lock_until"] > current_time:
        return "LOCKED"

    #Before checking paswsord we check if user has atleast setup his password once or it is empty in db,
    # if empty then login should fail immediately, we do this to avoid crash
    if not user["password_hash"]:
        return None

    # Check password
    if verify_password(password, user["password_hash"]): 
        # If true is returned, then Correct password → reset everything
        user["failed_attempts"] = 0
        user["lock_count"] = 0
        user["lock_until"] = 0
        user["last_failed_login"] = 0

        update_user(emp_id, user)   # replaced save_users
        return user           #returns user object to main, basically it returns the specific user's data as dictionary

    else:
        # Wrong password
        user["failed_attempts"] += 1
        user["last_failed_login"] = current_time

        # If 4 attempts → warning
        if user["failed_attempts"] == MAX_FAILED_ATTEMPTS-1:
            update_user(emp_id, user)   # replaced save_users
            return "LAST_ATTEMPT"

        # Lock account after 5 failed attempts
        if user["failed_attempts"] >= MAX_FAILED_ATTEMPTS:
            user["lock_count"] += 1
            lock_minutes = LOCKOUT_BASE_MINUTES * (2 ** (user["lock_count"] - 1))
            user["lock_until"] = current_time + (lock_minutes * 60)
            user["failed_attempts"] = 0

            update_user(emp_id, user)   # replaced save_users
            return "LOCKED"

        update_user(emp_id, user)   # replaced save_users
        return None             #go back to main, login fails

# Create JWT token, called from login endpoint
def create_access_token(data: dict):       #emp_id and role is sent as parameter here
    to_encode = data.copy()             

    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)  #creates expiry time
    to_encode.update({"exp": int(expire.timestamp())})  # convert to UNIX timestamp, add expiry to payload of jwt

    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)  #encodes the jwt, signs it using secret key
    return encoded_jwt    #returns the jwt payload to main


#Creating Refresh Token, called from login endpoint
def create_refresh_token(emp_id: str):
    emp_id = emp_id.lower()
    user = get_user(emp_id)
    if not user:
        return None

    refresh_token = secrets.token_urlsafe(32)  #generates random token string
    refresh_token_hash = hashlib.sha256(refresh_token.encode()).hexdigest()  #hashes the token using this algo
    
    current_time = time.time()  #gets current time
    
    refresh_token_expiry = current_time + (REFRESH_TOKEN_EXPIRE_DAYS *24 *60 *60)  #creates refresh expiry time
    
    user["refresh_token"] = refresh_token_hash            #hash and its expiry time is stored in db
    user["refresh_token_expiry"] = refresh_token_expiry

    update_user(emp_id, user)  #saves the db

    return refresh_token    #returns the refresh token to main



#Function to refresh the access token, called from refresh end point
def refresh_access_token(refresh_token: str): #recieves the prev refresh token as parameter
    

    refresh_token_hash = hashlib.sha256(refresh_token.encode()).hexdigest() #hashes the incoming token
    current_time = time.time() #gets current time

    # Connect to DB
    conn = connection_pool.getconn()
    try:
        cur = conn.cursor()

        # Fetch user directly using refresh token
        cur.execute("SELECT * FROM users WHERE refresh_token = %s", (refresh_token_hash,))
        row = cur.fetchone()

        cur.close()
    finally:
        connection_pool.putconn(conn)

    if not row:
        return None

    emp_id = row[0]

    user = {
        "name": row[1],
        "password_hash": row[2],
        "role": row[3],
        "failed_attempts": row[4],
        "lock_until": row[5],
        "lock_count": row[6],
        "last_failed_login": row[7],
        "refresh_token": row[8],
        "refresh_token_expiry": row[9],
        "session_start": row[10]
    }

    #if account is locked, then deny refresh
    if user["lock_until"] > current_time:
        return None
    
    #Absolute session expiry check
    session_start = user.get("session_start")

    if not session_start:
        return None
    
    absolute_expiry = session_start + (ABSOLUTE_SESSION_EXPIRE_DAYS * 24 * 60 *60)
    
    if current_time > absolute_expiry:
        return None

    # Check if refresh token expired
    if user.get("refresh_token_expiry", 0) < current_time:
        return None

    # Create new access token
    access_token = create_access_token({
        "sub": emp_id,
        "role": user["role"],
        "session_start": user["session_start"]
    })

    # Create NEW refresh token (rotation)
    new_refresh_token = secrets.token_urlsafe(32)
    new_refresh_token_hash = hashlib.sha256(new_refresh_token.encode()).hexdigest()
    new_expiry = current_time + (REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 *60)

    user["refresh_token"] = new_refresh_token_hash
    user["refresh_token_expiry"] = new_expiry

    update_user(emp_id, user)  # replace save_users

    return {
        "access_token": access_token,
        "refresh_token": new_refresh_token
    }
    
    

# Verify JWT token
from jose import JWTError, ExpiredSignatureError    #These are exceptions thrown by jwt.decode(), and without catching them your program will crash.

#called from ask endpoint
def verify_token(token: str):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM]) #internally the function checks signature, algorithm, expiry and the token format
        
        # Even if JWT signature is valid, it does not guarantee required fields exist.
        # "sub" represents the user identity (emp_id), so we verify its presence
        # to prevent crashes and ensure correct authentication flow  
        if "sub" not in payload or "session_start" not in payload:      
            return None
        
        return payload       #if everything is valid, jwt token string is returned to main
    
    except ExpiredSignatureError:
        print("Token expired")         #if token has expired
        return None
    
    except JWTError:
        print("Invalid token")           #if token is tampered, malformed, wrong signature
        return None
    
    
    
#Implementing the logout feature 
def logout_user(refresh_token: str):
    

    refresh_token_hash = hashlib.sha256(refresh_token.encode()).hexdigest() #we hash the incoming token provided by user, cuz refresh token is stored as a hash in db also and we are gonna compare it

    # Connect to database
    conn = connection_pool.getconn()
    try:
        cur = conn.cursor()

        # Find user using refresh token
        cur.execute("SELECT * FROM users WHERE refresh_token = %s", (refresh_token_hash,))
        row = cur.fetchone()

        cur.close()
    finally:
        connection_pool.putconn(conn)

    if not row:
        return False

    emp_id = row[0]

    user = {
        "name": row[1],
        "password_hash": row[2],
        "role": row[3],
        "failed_attempts": row[4],
        "lock_until": row[5],
        "lock_count": row[6],
        "last_failed_login": row[7],
        "refresh_token": row[8],
        "refresh_token_expiry": row[9],
        "session_start": row[10]
    }

    user["refresh_token"] = ""                       #invalidating refresh token
    user["refresh_token_expiry"] = 0                 #setting its expiry time to 0
    user["session_start"] = 0                        #killing the prev session

    update_user(emp_id, user)                        #saving in db

    return True                                      #true means logout succeeded



#emp_id and new password entered by user are sent as parameters here
def set_user_password(emp_id: str, new_password: str):  
    emp_id = emp_id.lower()    #lowercase emp_id
    user = get_user(emp_id)    #load user from database

    # User must exist, if he doesn't exist in db, we return user not found
    if not user:
        return "USER_NOT_FOUND"

    # Password should NOT already exist, if it does, then we say password already set
    if user.get("password_hash"):
        return "ALREADY_SET"

    # Hash new password
    hashed_password = pwd_context.hash(new_password)

    # Store new hashed password in db
    user["password_hash"] = hashed_password

    update_user(emp_id, user)   #save to database

    return "SUCCESS"      #password is set successfully




#user enters his emp_id, old password, and new password which he wants to replace with the old pasword
def change_user_password(emp_id: str, old_password: str, new_password: str):
    emp_id = emp_id.lower()   #lower the emp_id
    user = get_user(emp_id)   #load user from database

    # User must exist, if he doesn't, then we return user not found
    if not user:
        return "USER_NOT_FOUND"

    # Account lock check, if locked, then user is not allowed to change his password
    if user["lock_until"] > time.time():
        return "LOCKED"

    # Password must already be set, if password is not set even one time, then there is nothing to change
    if not user.get("password_hash"):
        return "PASSWORD_NOT_SET"

    # Verify old password, we call the verify function, it executes in this file only
    #it hashes the provided the password and compares it with existing hash, if they match, it returns true, else false
    #if old password doesn't match, we return wrong password
    if not verify_password(old_password, user["password_hash"]):
        return "WRONG_PASSWORD"

    # Hash new password, if verify password returns true
    new_hash = pwd_context.hash(new_password)

    # Update new password in db
    user["password_hash"] = new_hash

    #We invalidate the complete session after password change, basically reset session time and tokens
    user["session_start"] = 0
    user["refresh_token"] = ""
    user["refresh_token_expiry"] = 0

    update_user(emp_id, user)   #save to database

    return "SUCCESS"    #password is successfully changed

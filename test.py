import hashlib

def hash_user_id(user_id):
    return hashlib.sha256(str(user_id).encode()).hexdigest()

print(hash_user_id("h"))
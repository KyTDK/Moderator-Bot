from cryptography.fernet import Fernet

# Only do this once and save securely (not in code!)
key = Fernet.generate_key()
print(key.decode())
import os

from cryptography.fernet import Fernet
from dotenv import load_dotenv

load_dotenv()

MYSQL_CONFIG = {
    "host": os.getenv("MYSQL_HOST"),
    "user": os.getenv("MYSQL_USER"),
    "password": os.getenv("MYSQL_PASSWORD"),
    "db": os.getenv("MYSQL_DATABASE"),
    "autocommit": False,
    "charset": "utf8mb4",
}

FERNET_KEY = os.getenv("FERNET_SECRET_KEY")
if FERNET_KEY is None:
    raise RuntimeError("FERNET_SECRET_KEY environment variable must be set")

fernet = Fernet(FERNET_KEY)

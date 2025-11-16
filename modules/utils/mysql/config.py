import os

from dotenv import load_dotenv

from modules.utils.fernet_utils import get_fernet, get_fernet_key

load_dotenv()

MYSQL_CONFIG = {
    "host": os.getenv("MYSQL_HOST"),
    "user": os.getenv("MYSQL_USER"),
    "password": os.getenv("MYSQL_PASSWORD"),
    "db": os.getenv("MYSQL_DATABASE"),
    "autocommit": False,
    "charset": "utf8mb4",
}

FERNET_KEY = get_fernet_key()
fernet = get_fernet()

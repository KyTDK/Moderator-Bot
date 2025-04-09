import logging
import mysql.connector
from mysql.connector import Error
import os
from dotenv import load_dotenv
from modules.config.settings_schema import SETTINGS_SCHEMA
import json
from cryptography.fernet import Fernet
from rapidfuzz import fuzz
import imagehash
from PIL import Image

load_dotenv()

MYSQL_CONFIG = {
    'host': os.getenv('MYSQL_HOST'),
    'user': os.getenv('MYSQL_USER'),
    'password': os.getenv('MYSQL_PASSWORD'),
    'database': os.getenv('MYSQL_DATABASE')
}

FERNET_KEY = os.getenv("FERNET_SECRET_KEY") 
fernet = Fernet(FERNET_KEY)

def execute_query(query, params=(), *, commit=True, fetch_one=False, fetch_all=False, buffered=True, database=None, user=None, password=None):
    db = get_connection(database=database, user=user, password=password)
    if not db:
        return None, 0
    try:
        with db.cursor(buffered=buffered) as cursor:
            cursor.execute(query, params)
            affected_rows = cursor.rowcount  # Capture the number of affected rows
            result = None
            if fetch_one:
                result = cursor.fetchone()
            elif fetch_all:
                result = cursor.fetchall()
            if commit:
                db.commit()
            return result, affected_rows  # Return both the result and affected rows
    except Exception as e:
        logging.error("Error executing query", exc_info=True)
        if commit:
            db.rollback()
        return None, 0  # Return 0 affected rows in case of error
    finally:
        db.close()

def get_connection(database=None, user=None, password=None, use_database=True):
    """Establish and return a database connection."""
    try:
        config = MYSQL_CONFIG.copy()
        if database:
            config["database"] = database
        if user:
            config["user"] = user
        if password:
            config["password"] = password
        if not use_database:
            config.pop('database', None)  # Connect without specifying a database.
        connection = mysql.connector.connect(**config)
        return connection
    except Error as e:
        logging.error(f"Error connecting to MySQL: {e}")
        return None
    
def get_strike_count(user_id, guild_id):
    result, _ = execute_query(
        "SELECT COUNT(*) FROM strikes WHERE user_id = %s AND guild_id = %s",
        (user_id, guild_id,), fetch_one=True
    )
    return result[0] if result else 0

def get_settings(guild_id, settings_key=None):
    """Retrieve the settings for a guild."""
    settings, _ = execute_query(
        "SELECT settings_json FROM settings WHERE guild_id = %s",
        (guild_id,),
        fetch_one=True
    )
    response = json.loads(settings[0]) if settings else json.loads("{}")
    encrypted = SETTINGS_SCHEMA.get(settings_key).encrypted if settings_key else False
    if response == "True":
        response = True
    elif response == "False":
        response = False
    value = response if settings_key is None else response.get(settings_key, SETTINGS_SCHEMA.get(settings_key).default)
    if encrypted and value:
        value = fernet.decrypt(value.encode()).decode()
    return value

def update_settings(guild_id, settings_key, settings_value):
    """Update the settings for a guild."""
    settings = get_settings(guild_id)
    success = False

    if settings_value is None:
        success = settings.pop(settings_key, None) is not None
    else:
        encrypt = SETTINGS_SCHEMA.get(settings_key).encrypted if settings_key else False
        if encrypt:
            settings_value = fernet.encrypt(settings_value.encode()).decode()
        settings[settings_key] = settings_value
        success = True

    settings_json = json.dumps(settings)
    execute_query(
        "INSERT INTO settings (guild_id, settings_json) VALUES (%s, %s) ON DUPLICATE KEY UPDATE settings_json = %s",
        (guild_id, settings_json, settings_json)
    )
    return success

def check_offensive_message(message, threshold=80):
    """
    Checks if a given message is similar to a known offensive message in the cache.
    Returns the category if found, else None.
    """
    result, _ = execute_query(
        "SELECT message, category FROM offensive_cache WHERE category IS NOT NULL",
        fetch_all=True
    )
    if not result:
        return None

    for cached_msg, category in result:
        similarity = fuzz.ratio(message.lower(), cached_msg.lower())
        if similarity >= threshold:
            return category
    return None

def cache_offensive_message(message, category):
    """
    Caches a message and its category into the offensive_cache table.
    """
    query = """
        INSERT INTO offensive_cache (message, category)
        VALUES (%s, %s)
        ON DUPLICATE KEY UPDATE category = VALUES(category)
    """
    _, rows = execute_query(query, (message, category))
    return rows > 0

def check_phash(phash, threshold=0.8):
    """
    Checks if a given perceptual hash is similar to any offensive ones in the cache.
    
    Parameters:
        phash (ImageHash or bytes or str): The perceptual hash to compare. If provided as an ImageHash object or str,
                                             it will be converted to bytes.
        threshold (float): The minimum similarity required (0.0 to 1.0).
        
    Returns:
        category (str): The category from the cache if similarity meets/exceeds threshold.
                        Returns None if no match is found.
    """
    # Convert the input phash into a bytes object.
    if isinstance(phash, imagehash.ImageHash):
        # Convert the ImageHash (via its hex representation) to bytes.
        phash_bytes = bytes.fromhex(str(phash))
    elif isinstance(phash, str):
        # If provided as a hex string, convert it to bytes.
        phash_bytes = bytes.fromhex(phash)
    elif isinstance(phash, bytes):
        phash_bytes = phash
    else:
        raise ValueError("Unsupported type for phash; must be ImageHash, str, or bytes.")

    # Retrieve stored hashes (stored as bytes) and their associated categories.
    result, _ = execute_query(
        "SELECT phash, category FROM phash_cache", 
        fetch_all=True
    )
    
    if not result:
        return None

    for cached_phash_bytes, category in result:
        # Convert stored bytes back to a hex string then to an ImageHash object.
        cached_hash = imagehash.hex_to_hash(cached_phash_bytes.hex())
        
        # Convert our input phash_bytes back to an ImageHash object for comparison.
        input_hash = imagehash.hex_to_hash(phash_bytes.hex())
        
        # Compute the Hamming distance between the two hashes.
        hamming_distance = input_hash - cached_hash
        
        # Typical pHash is 8x8 bits (64 bits total)
        similarity = 1 - (hamming_distance / 64.0)
        
        if similarity >= threshold:
            return category

    return None

def cache_phash(phash_value, category):
    """
    Caches an image identifier and its corresponding perceptual hash into the phash_cache table.
    The hash is stored as raw bytes derived from its hexadecimal representation.
    
    Returns True if a new record was inserted or the record was updated.
    """
    # Convert phash_value into bytes.
    if isinstance(phash_value, imagehash.ImageHash):
        phash_bytes = bytes.fromhex(str(phash_value))
    elif isinstance(phash_value, str):
        phash_bytes = bytes.fromhex(phash_value)
    elif isinstance(phash_value, bytes):
        phash_bytes = phash_value
    else:
        raise ValueError("Unsupported type for phash_value; must be ImageHash, str, or bytes.")
        
    query = """
        INSERT INTO phash_cache (phash, category)
        VALUES (%s, %s)
        ON DUPLICATE KEY UPDATE phash = VALUES(phash)
    """
    _, rows = execute_query(query, (phash_bytes, category))
    return rows > 0

# --- Database Initialization Function ---

def initialize_database():
    """Initialize the database schema."""
    db = get_connection(use_database=False)
    if not db:
        return
    try:
        with db.cursor(buffered=True) as cursor:
            cursor.execute(f"CREATE DATABASE IF NOT EXISTS {MYSQL_CONFIG['database']}")
            cursor.execute(f"USE {MYSQL_CONFIG['database']}")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS strikes (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    guild_id BIGINT,
                    user_id BIGINT,
                    reason VARCHAR(255),
                    striked_by_id BIGINT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    guild_id BIGINT PRIMARY KEY,
                    settings_json JSON
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS banned_words (
                    guild_id BIGINT,
                    word VARCHAR(255),
                    PRIMARY KEY (guild_id, word)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS offensive_cache (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    message TEXT NOT NULL,
                    category VARCHAR(255) DEFAULT NULL,
                    UNIQUE KEY unique_message (message(255))
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS phash_cache (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    phash BLOB NOT NULL UNIQUE,
                    category VARCHAR(255) DEFAULT NULL
                )
            """)
            db.commit()
    except Error as e:
        logging.error(f"Error initializing database: {e}")
    finally:
        db.close()
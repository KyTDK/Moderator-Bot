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
        print(similarity)
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
        phash (ImageHash or str): The perceptual hash to compare, either as an ImageHash object or a hexadecimal string.
        threshold (float): The minimum similarity required (0 to 1 range).
        
    Returns:
        category (str): The category from the cache if similarity exceeds threshold.
                        Returns None if no match is found.
    """
    # If the provided phash is a hex string, convert it to an ImageHash object
    if isinstance(phash, str):
        phash = imagehash.hex_to_hash(phash)
    
    # Execute the query to retrieve hashes and their associated categories.
    result, _ = execute_query(
        "SELECT phash, category FROM phash_cache", 
        fetch_all=True
    )
    
    if not result:
        return None

    for cached_phash, category in result:
        # Convert the stored hexadecimal hash to an ImageHash object.
        cached_hash = imagehash.hex_to_hash(cached_phash)
        
        # Calculate the Hamming distance between the two hashes.
        hamming_distance = phash - cached_hash
        
        # Since typical pHash is 8x8 bits, there are 64 bits total.
        similarity = 1 - (hamming_distance / 64.0)
        
        # Check if the similarity meets the threshold.
        if similarity >= threshold:
            return category

    return None

def cache_phash(phash_value, category):
    """
    Caches an image identifier and its corresponding perceptual hash into the phash_cache table.
    Returns True if a new record was inserted or the record was updated.
    """
    query = """
        INSERT INTO phash_cache (phash, category)
        VALUES (%s, %s)
        ON DUPLICATE KEY UPDATE phash = VALUES(phash)
    """
    _, rows = execute_query(query, (phash_value, category))
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
                    phash VARCHAR(255) NOT NULL UNIQUE,
                    category VARCHAR(255) DEFAULT NULL
                )
            """)
            db.commit()
    except Error as e:
        logging.error(f"Error initializing database: {e}")
    finally:
        db.close()
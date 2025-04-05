import logging
import mysql.connector
from mysql.connector import Error
import os
from dotenv import load_dotenv
from  modules.config.settings_schema import SETTINGS_SCHEMA
import json

load_dotenv()

MYSQL_CONFIG = {
    'host': os.getenv('MYSQL_HOST'),
    'user': os.getenv('MYSQL_USER'),
    'password': os.getenv('MYSQL_PASSWORD'),
    'database': os.getenv('MYSQL_DATABASE')
}

def execute_query(query, params=(), *, commit=True, fetch_one=False, fetch_all=False, buffered=True, database=None, user=None, password=None):
    db = get_connection(database=database, user=user, password=password)
    if not db:
        return None
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
    result = execute_query(
        "SELECT COUNT(*) FROM strikes WHERE user_id = %s AND guild_id = %s",
        (user_id, guild_id,), fetch_one=True
    )
    return result[0][0] if result else 0

def get_settings(guild_id, settings_key=None):
    """Retrieve the settings for a guild."""
    settings, _ = execute_query(
        "SELECT settings_json FROM settings WHERE guild_id = %s",
        (guild_id,),
        fetch_one=True
    )
    response = json.loads(settings[0]) if settings else json.loads("{}")
    return response if settings_key is None else response.get(settings_key, SETTINGS_SCHEMA.get(settings_key).default) if settings_key else response

def update_settings(guild_id, settings_key, settings_value):
    """Update the settings for a guild."""
    settings = get_settings(guild_id)
    success = False
    if settings_value is None:
        success = settings.pop(settings_key, None) is not None
    else:
        settings[settings_key] = settings_value
        success = True
    settings = json.dumps(settings)
    execute_query(
        "INSERT INTO settings (guild_id, settings_json) VALUES (%s, %s) ON DUPLICATE KEY UPDATE settings_json = %s",
        (guild_id, settings, settings)
    )
    return success
        
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
            db.commit()
    except Error as e:
        logging.error(f"Error initializing database: {e}")
    finally:
        db.close()
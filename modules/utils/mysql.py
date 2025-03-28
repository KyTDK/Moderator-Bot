import logging
import mysql.connector
from mysql.connector import Error
import os
from dotenv import load_dotenv

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
                    user_id BIGINT,
                    reason VARCHAR(255),
                    striked_by_id BIGINT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                           )
            """)
            db.commit()
    except Error as e:
        logging.error(f"Error initializing database: {e}")
    finally:
        db.close()
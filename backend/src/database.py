"""
Database setup
"""
import sqlite3
from pathlib import Path

def setup_status_db(path_to_db: Path):
    # Init status db
    conn = sqlite3.connect(path_to_db)
    cursor = conn.cursor()
    # Primary table with general status
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS status(
        ID INTEGER PRIMARY KEY AUTOINCREMENT, 
        mrn TEXT NOT NULL, 
        path TEXT NOT NULL, 
        process_datetime DATETIME NOT NULL,
        status TEXT NOT NULL)""")

    # For those that were successful, where are they now
    cursor.execute("""CREATE TABLE IF NOT EXISTS uploads(
        ID INTEGER PRIMARY KEY, 
        mrn TEXT NOT NULL,
        path TEXT NOT NULL,
        was_sent_to_remote BOOLEAN,
        remote_ip TEXT,
        remote_AE_title TEXT,
        remote_port INTEGER,
        was_uploaded_to_proknow BOOLEAN,
        proknow_collection TEXT,
        proknow_link TEXT,
        FOREIGN KEY (ID) REFERENCES status (ID)
        )""")

    # What happened to those that failed
    cursor.execute("""CREATE TABLE IF NOT EXISTS errors(
        ID INTEGER PRIMARY KEY, 
        mrn TEXT NOT NULL,
        path TEXT NOT NULL,
        error_message TEXT,
        FOREIGN KEY (ID) REFERENCES status (ID)
        )""")

    cursor.execute("""CREATE TABLE IF NOT EXISTS plans(
        ID INTEGER PRIMARY KEY, 
        mrn TEXT NOT NULL,
        path TEXT NOT NULL,
        plan_id INT NOT NULL,
        plan_name TEXT NOT NULL,
        status TEXT NOT NULL,
        error_message TEXT,
        FOREIGN KEY (ID) REFERENCES status (ID))
    """)
    # Additional tables to track jobs/patients/events while preserving legacy schema
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS jobs(
            job_id TEXT PRIMARY KEY,
            created_at TEXT,
            created_by TEXT,
            description TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS patients(
            job_id TEXT,
            mrn TEXT,
            input_path TEXT,
            created_at TEXT,
            PRIMARY KEY (job_id, mrn),
            FOREIGN KEY(job_id) REFERENCES jobs(job_id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS events(
            ID INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT,
            mrn TEXT,
            stage TEXT,
            event_type TEXT,
            ts TEXT,
            attempt INTEGER DEFAULT 1,
            error_message TEXT,
            details TEXT,
            FOREIGN KEY(job_id) REFERENCES jobs(job_id)
        )
    """)

    conn.commit()
    conn.close()
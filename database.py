import sqlite3
from contextlib import contextmanager

DB_NAME = "database.db"

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Tabela de Configurações
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS guild_configs (
                guild_id INTEGER PRIMARY KEY,
                scrims_channel INTEGER,
                freeagents_channel INTEGER,
                contracts_channel INTEGER,
                dismissals_channel INTEGER,
                logs_channel INTEGER,
                ticket_channel INTEGER,
                ticket_category INTEGER,
                admin_role INTEGER
            )
        """)

        # Tabela de Tickets
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tickets (
                ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER,
                channel_id INTEGER,
                user_id INTEGER,
                status TEXT
            )
        """)

        # Tabela de Punições
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS punishments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER,
                user_id INTEGER,
                moderator_id INTEGER,
                type TEXT,
                reason TEXT,
                duration TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Tabela de Times
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS teams (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER,
                name TEXT,
                acronym TEXT,
                owner_id INTEGER,
                logo_url TEXT,
                role_id INTEGER,
                UNIQUE(guild_id, name),
                UNIQUE(guild_id, acronym)
            )
        """)

        # Tabela de Contratos
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS contracts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER,
                team_id INTEGER,
                player_id INTEGER,
                role TEXT,
                position TEXT,
                status TEXT DEFAULT 'active'
            )
        """)

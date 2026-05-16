import sqlite3
import os
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(__file__), "usage.db")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def db():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                project TEXT,
                cwd TEXT,
                first_seen TEXT,
                last_seen TEXT,
                total_input_tokens INTEGER DEFAULT 0,
                total_output_tokens INTEGER DEFAULT 0,
                total_cache_read_tokens INTEGER DEFAULT 0,
                total_cache_creation_tokens INTEGER DEFAULT 0,
                model TEXT,
                message_count INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                message_uuid TEXT UNIQUE,
                timestamp TEXT,
                role TEXT,
                prompt_text TEXT,
                response_text TEXT,
                model TEXT,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cache_read_tokens INTEGER DEFAULT 0,
                cache_creation_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                estimated_cost_usd REAL DEFAULT 0,
                FOREIGN KEY (session_id) REFERENCES sessions(session_id)
            );

            CREATE TABLE IF NOT EXISTS optimization_suggestions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER,
                session_id TEXT,
                suggestion_type TEXT,
                original_tokens INTEGER,
                estimated_savings INTEGER,
                suggestion TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (message_id) REFERENCES messages(id)
            );

            CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
            CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
            CREATE INDEX IF NOT EXISTS idx_suggestions_message ON optimization_suggestions(message_id);
        """)


def upsert_session(session_id, project, cwd, timestamp, model):
    with db() as conn:
        conn.execute("""
            INSERT INTO sessions (session_id, project, cwd, first_seen, last_seen, model)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                last_seen = excluded.last_seen,
                model = excluded.model
        """, (session_id, project, cwd, timestamp, timestamp, model))


def insert_message(data: dict):
    with db() as conn:
        cursor = conn.execute("""
            INSERT OR IGNORE INTO messages
                (session_id, message_uuid, timestamp, role, prompt_text, response_text,
                 model, input_tokens, output_tokens, cache_read_tokens,
                 cache_creation_tokens, total_tokens, estimated_cost_usd)
            VALUES
                (:session_id, :message_uuid, :timestamp, :role, :prompt_text, :response_text,
                 :model, :input_tokens, :output_tokens, :cache_read_tokens,
                 :cache_creation_tokens, :total_tokens, :estimated_cost_usd)
        """, data)
        # Only update session aggregates if the row was actually inserted
        if cursor.rowcount > 0:
            conn.execute("""
                UPDATE sessions SET
                    total_input_tokens = total_input_tokens + :input_tokens,
                    total_output_tokens = total_output_tokens + :output_tokens,
                    total_cache_read_tokens = total_cache_read_tokens + :cache_read_tokens,
                    total_cache_creation_tokens = total_cache_creation_tokens + :cache_creation_tokens,
                    message_count = message_count + 1,
                    last_seen = :timestamp
                WHERE session_id = :session_id
            """, data)


def insert_suggestion(message_id, session_id, suggestion_type, original_tokens, savings, suggestion):
    with db() as conn:
        conn.execute("""
            INSERT INTO optimization_suggestions
                (message_id, session_id, suggestion_type, original_tokens, estimated_savings, suggestion)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (message_id, session_id, suggestion_type, original_tokens, savings, suggestion))


def get_dashboard_stats():
    with db() as conn:
        stats = conn.execute("""
            SELECT
                (SELECT COUNT(*) FROM sessions) as total_sessions,
                (SELECT COALESCE(SUM(input_tokens), 0) FROM messages) as total_input,
                (SELECT COALESCE(SUM(output_tokens), 0) FROM messages) as total_output,
                (SELECT COALESCE(SUM(cache_read_tokens), 0) FROM messages) as total_cache_read,
                (SELECT COALESCE(SUM(cache_creation_tokens), 0) FROM messages) as total_cache_creation,
                (SELECT COALESCE(SUM(estimated_cost_usd), 0) FROM messages) as total_cost,
                (SELECT COUNT(*) FROM messages) as total_messages
        """).fetchone()

        by_model = conn.execute("""
            SELECT model,
                   COUNT(*) as count,
                   SUM(input_tokens + output_tokens) as total_tokens,
                   SUM(estimated_cost_usd) as cost
            FROM messages
            WHERE model IS NOT NULL
            GROUP BY model
            ORDER BY total_tokens DESC
        """).fetchall()

        by_project = conn.execute("""
            SELECT s.project,
                   COUNT(DISTINCT s.session_id) as sessions,
                   SUM(s.total_input_tokens + s.total_output_tokens) as total_tokens,
                   SUM(m.estimated_cost_usd) as cost
            FROM sessions s
            LEFT JOIN messages m ON s.session_id = m.session_id
            GROUP BY s.project
            ORDER BY total_tokens DESC
            LIMIT 10
        """).fetchall()

        daily = conn.execute("""
            SELECT DATE(timestamp) as day,
                   SUM(input_tokens) as input_tokens,
                   SUM(output_tokens) as output_tokens,
                   SUM(estimated_cost_usd) as cost
            FROM messages
            GROUP BY day
            ORDER BY day DESC
            LIMIT 30
        """).fetchall()

        return {
            "stats": dict(stats),
            "by_model": [dict(r) for r in by_model],
            "by_project": [dict(r) for r in by_project],
            "daily": [dict(r) for r in daily],
        }


def get_sessions(limit=50, offset=0, project=None):
    with db() as conn:
        query = """
            SELECT s.*,
                   SUM(m.estimated_cost_usd) as cost
            FROM sessions s
            LEFT JOIN messages m ON s.session_id = m.session_id
        """
        params = []
        if project:
            query += " WHERE s.project = ?"
            params.append(project)
        query += " GROUP BY s.session_id ORDER BY s.last_seen DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def get_session_messages(session_id):
    with db() as conn:
        rows = conn.execute("""
            SELECT m.*, o.suggestion, o.suggestion_type, o.estimated_savings
            FROM messages m
            LEFT JOIN optimization_suggestions o ON o.message_id = m.id
            WHERE m.session_id = ?
            ORDER BY m.timestamp ASC
        """, (session_id,)).fetchall()
        return [dict(r) for r in rows]


def get_top_token_messages(limit=20):
    with db() as conn:
        rows = conn.execute("""
            SELECT m.*, s.project,
                   o.suggestion, o.estimated_savings
            FROM messages m
            LEFT JOIN sessions s ON m.session_id = s.session_id
            LEFT JOIN optimization_suggestions o ON o.message_id = m.id
            WHERE m.role = 'user'
            ORDER BY m.total_tokens DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]


def message_exists(uuid: str) -> bool:
    with db() as conn:
        row = conn.execute("SELECT 1 FROM messages WHERE message_uuid = ?", (uuid,)).fetchone()
        return row is not None


def get_insights():
    with db() as conn:
        # Hourly activity: 0-23 hours, count messages + tokens
        hourly = conn.execute("""
            SELECT CAST(strftime('%H', timestamp) AS INTEGER) as hour,
                   COUNT(*) as messages,
                   SUM(input_tokens + output_tokens + cache_read_tokens) as tokens,
                   SUM(estimated_cost_usd) as cost
            FROM messages
            WHERE timestamp IS NOT NULL
            GROUP BY hour
            ORDER BY hour
        """).fetchall()
        # Fill missing hours with zeros
        hourly_map = {r["hour"]: dict(r) for r in hourly}
        hourly_full = [
            hourly_map.get(h, {"hour": h, "messages": 0, "tokens": 0, "cost": 0.0})
            for h in range(24)
        ]

        # Weekly summary: this week vs last week
        weekly = conn.execute("""
            SELECT
                SUM(CASE WHEN DATE(timestamp) >= DATE('now', 'weekday 0', '-7 days') THEN 1 ELSE 0 END) as this_week_msgs,
                SUM(CASE WHEN DATE(timestamp) >= DATE('now', 'weekday 0', '-7 days') THEN estimated_cost_usd ELSE 0 END) as this_week_cost,
                SUM(CASE WHEN DATE(timestamp) >= DATE('now', 'weekday 0', '-7 days') THEN input_tokens + output_tokens ELSE 0 END) as this_week_tokens,
                SUM(CASE WHEN DATE(timestamp) >= DATE('now', 'weekday 0', '-14 days')
                          AND DATE(timestamp) < DATE('now', 'weekday 0', '-7 days') THEN 1 ELSE 0 END) as last_week_msgs,
                SUM(CASE WHEN DATE(timestamp) >= DATE('now', 'weekday 0', '-14 days')
                          AND DATE(timestamp) < DATE('now', 'weekday 0', '-7 days') THEN estimated_cost_usd ELSE 0 END) as last_week_cost,
                SUM(CASE WHEN DATE(timestamp) >= DATE('now', 'weekday 0', '-14 days')
                          AND DATE(timestamp) < DATE('now', 'weekday 0', '-7 days') THEN input_tokens + output_tokens ELSE 0 END) as last_week_tokens
            FROM messages
        """).fetchone()

        # Daily cost trend (last 30 days, line chart)
        cost_trend = conn.execute("""
            SELECT DATE(timestamp) as day,
                   SUM(estimated_cost_usd) as cost,
                   SUM(input_tokens + output_tokens) as tokens,
                   COUNT(*) as messages
            FROM messages
            WHERE DATE(timestamp) >= DATE('now', '-30 days')
            GROUP BY day
            ORDER BY day ASC
        """).fetchall()

        # Cache efficiency per day
        cache_trend = conn.execute("""
            SELECT DATE(timestamp) as day,
                   ROUND(
                       100.0 * SUM(cache_read_tokens) /
                       NULLIF(SUM(cache_read_tokens + cache_creation_tokens + input_tokens), 0),
                   2) as cache_hit_pct,
                   SUM(cache_read_tokens) as cache_read,
                   SUM(input_tokens) as input_tokens
            FROM messages
            WHERE DATE(timestamp) >= DATE('now', '-30 days')
            GROUP BY day
            ORDER BY day ASC
        """).fetchall()

        # Overall cache efficiency
        overall_cache = conn.execute("""
            SELECT ROUND(
                100.0 * SUM(cache_read_tokens) /
                NULLIF(SUM(cache_read_tokens + cache_creation_tokens + input_tokens), 0),
            2) as hit_pct,
            SUM(cache_read_tokens) as total_cache_read,
            SUM(input_tokens) as total_input
            FROM messages
        """).fetchone()

        # Session duration stats (from first_seen to last_seen)
        duration = conn.execute("""
            SELECT
                ROUND(AVG(
                    (julianday(last_seen) - julianday(first_seen)) * 24 * 60
                ), 1) as avg_duration_mins,
                ROUND(MAX(
                    (julianday(last_seen) - julianday(first_seen)) * 24 * 60
                ), 1) as max_duration_mins,
                ROUND(AVG(CAST(message_count AS REAL)), 1) as avg_msgs_per_session,
                ROUND(AVG(
                    CAST(total_input_tokens + total_output_tokens AS REAL) /
                    NULLIF(message_count, 0)
                ), 0) as avg_tokens_per_turn
            FROM sessions
            WHERE first_seen != last_seen
        """).fetchone()

        return {
            "hourly": hourly_full,
            "weekly": dict(weekly),
            "cost_trend": [dict(r) for r in cost_trend],
            "cache_trend": [dict(r) for r in cache_trend],
            "overall_cache": dict(overall_cache),
            "duration": dict(duration),
        }

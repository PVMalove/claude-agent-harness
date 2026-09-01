import sqlite3
from typing import Any
from ...domain.state import StateStore
from ...domain.execution import ExecutionPlan, ExecutionResult
import json

class SQLiteStateStore(StateStore):
    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path)
        self._init_db()

    def _init_db(self):
        cursor = self.conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS executions (
                execution_id TEXT PRIMARY KEY,
                skill TEXT,
                worker TEXT,
                status TEXT,
                plan_json TEXT,
                result_json TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                execution_id TEXT,
                event_type TEXT,
                payload_json TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS workflows (
                execution_id TEXT PRIMARY KEY,
                state_json TEXT,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        self.conn.commit()

    async def save_execution(self, context: Any) -> None:
        # Stub implementation
        pass

    async def get_execution(self, execution_id: str) -> Any:
        return None

    async def append_event(self, event: dict[str, Any]) -> None:
        pass

    async def save_workflow_execution(self, execution_id: str, state: dict[str, Any]) -> None:
        cursor = self.conn.cursor()
        cursor.execute(
            '''INSERT OR REPLACE INTO workflows (execution_id, state_json) VALUES (?, ?)''',
            (execution_id, json.dumps(state))
        )
        self.conn.commit()

    async def get_workflow_execution(self, execution_id: str) -> dict[str, Any] | None:
        cursor = self.conn.cursor()
        cursor.execute('SELECT state_json FROM workflows WHERE execution_id = ?', (execution_id,))
        row = cursor.fetchone()
        if row:
            return json.loads(row[0])
        return None

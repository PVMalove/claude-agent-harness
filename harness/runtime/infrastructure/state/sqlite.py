import sqlite3
import datetime
from typing import Any
from ...domain.state import StateStore
from ...domain.execution import ExecutionContext, ExecutionResult
import json
from pathlib import Path

class SQLiteStateStore(StateStore):
    def __init__(self, db_path: str = ":memory:"):
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
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

    async def save_execution(self, context: ExecutionContext) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO executions
               (execution_id, skill, status, plan_json, result_json)
               VALUES (?, ?, ?, ?, ?)""",
            (
                context.execution_id,
                context.skill,
                "RUNNING",
                json.dumps({
                    "session_id": context.session_id,
                    "parent_execution_id": context.parent_execution_id,
                    "caller": context.caller,
                    "project": context.project,
                    "depth": context.depth,
                    "metadata": context.metadata,
                }),
                None,
            ),
        )
        self.conn.commit()

    async def get_execution(self, execution_id: str) -> ExecutionContext | None:
        row = self.conn.execute(
            "SELECT execution_id, skill, plan_json FROM executions WHERE execution_id = ?",
            (execution_id,),
        ).fetchone()
        if row is None:
            return None
        details = json.loads(row[2] or "{}")
        return ExecutionContext(
            execution_id=row[0],
            session_id=details.get("session_id", ""),
            parent_execution_id=details.get("parent_execution_id"),
            caller=details.get("caller", "USER"),
            project=details.get("project", ""),
            depth=details.get("depth", 0),
            skill=row[1],
            metadata=details.get("metadata", {}),
        )

    async def append_event(self, event: dict[str, Any]) -> None:
        self.conn.execute(
            """INSERT INTO events (execution_id, event_type, payload_json)
               VALUES (?, ?, ?)""",
            (
                event.get("execution_id", ""),
                event.get("event_type", "unknown"),
                json.dumps(event, ensure_ascii=False),
            ),
        )
        self.conn.commit()

    async def save_execution_result(
        self, execution_id: str, result: ExecutionResult
    ) -> None:
        self.conn.execute(
            "UPDATE executions SET status = ?, result_json = ? WHERE execution_id = ?",
            (
                result.status,
                json.dumps({
                    "output": result.output,
                    "error": result.error,
                    "error_details": result.error_details,
                }, ensure_ascii=False),
                execution_id,
            ),
        )
        self.conn.commit()

    async def save_workflow_execution(self, execution_id: str, state: dict[str, Any]) -> None:
        state["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        cursor = self.conn.cursor()
        cursor.execute(
            '''INSERT OR REPLACE INTO workflows (execution_id, state_json) VALUES (?, ?)''',
            (execution_id, json.dumps(state, ensure_ascii=False)),
        )
        self.conn.commit()

    async def get_workflow_execution(self, execution_id: str) -> dict[str, Any] | None:
        cursor = self.conn.cursor()
        cursor.execute('SELECT state_json FROM workflows WHERE execution_id = ?', (execution_id,))
        row = cursor.fetchone()
        if row:
            return json.loads(row[0])
        return None

    async def cancel_workflow_execution(self, execution_id: str) -> dict[str, Any] | None:
        state = await self.get_workflow_execution(execution_id)
        if state is None:
            return None
        if state.get("status") not in {"COMPLETED", "CANCELLED"}:
            state["status"] = "CANCELLED"
            state["cancel_requested"] = True
            state["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            await self.save_workflow_execution(execution_id, state)
        return state

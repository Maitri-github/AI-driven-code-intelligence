import sqlite3
import json
import logging
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List
from app.config import settings

logger = logging.getLogger(__name__)


class CacheService:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or settings.cache_db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _get_connection(self):
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS file_cache (
                    content_hash TEXT PRIMARY KEY,
                    file_path TEXT NOT NULL,
                    language TEXT,
                    line_count INTEGER,
                    overview TEXT,
                    key_components_json TEXT,
                    flow TEXT,
                    notable_risks TEXT,
                    api_docs_json TEXT,
                    imports_json TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_file_cache_path ON file_cache(file_path);
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS repo_cache (
                    repo_id TEXT PRIMARY KEY,
                    repo_url TEXT NOT NULL,
                    branch TEXT,
                    results_json TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            conn.commit()

    def get_file_cache(self, file_path: str, content_hash: str) -> Optional[Dict[str, Any]]:
        """Retrieve cached explanation and API docs keyed by content hash."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT * FROM file_cache WHERE content_hash = ?
                """, (content_hash,))
                row = cursor.fetchone()
                if not row:
                    return None
                
                return {
                    "file_path": row["file_path"],
                    "content_hash": row["content_hash"],
                    "language": row["language"],
                    "line_count": row["line_count"],
                    "overview": row["overview"],
                    "key_components": json.loads(row["key_components_json"] or "[]"),
                    "flow": row["flow"],
                    "notable_risks": row["notable_risks"],
                    "api_docs": json.loads(row["api_docs_json"] or "[]"),
                    "imports": json.loads(row["imports_json"] or "[]"),
                    "cache_hit": True
                }
        except Exception as e:
            logger.warning(f"Failed to read from file_cache: {e}")
            return None

    def save_file_cache(
        self,
        file_path: str,
        content_hash: str,
        language: str,
        line_count: int,
        overview: str,
        key_components: List[str],
        flow: str,
        notable_risks: str,
        api_docs: List[Dict[str, Any]],
        imports: List[Dict[str, Any]]
    ):
        """Save file explanation, api docs, and imports keyed by content hash."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO file_cache (
                        content_hash, file_path, language, line_count,
                        overview, key_components_json, flow, notable_risks,
                        api_docs_json, imports_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(content_hash) DO UPDATE SET
                        file_path = excluded.file_path,
                        language = excluded.language,
                        line_count = excluded.line_count,
                        overview = excluded.overview,
                        key_components_json = excluded.key_components_json,
                        flow = excluded.flow,
                        notable_risks = excluded.notable_risks,
                        api_docs_json = excluded.api_docs_json,
                        imports_json = excluded.imports_json,
                        updated_at = CURRENT_TIMESTAMP
                """, (
                    content_hash,
                    file_path,
                    language,
                    line_count,
                    overview,
                    json.dumps(key_components),
                    flow,
                    notable_risks,
                    json.dumps(api_docs),
                    json.dumps(imports)
                ))
                conn.commit()
        except Exception as e:
            logger.warning(f"Failed to write to file_cache: {e}")

    def save_repo_results(self, repo_id: str, repo_url: str, branch: str, results_dict: Dict[str, Any]):
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO repo_cache (repo_id, repo_url, branch, results_json, updated_at)
                    VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(repo_id) DO UPDATE SET
                        results_json = excluded.results_json,
                        updated_at = CURRENT_TIMESTAMP
                """, (repo_id, repo_url, branch, json.dumps(results_dict)))
                conn.commit()
        except Exception as e:
            logger.warning(f"Failed to save repo_cache: {e}")

    def get_repo_results(self, repo_id: str) -> Optional[Dict[str, Any]]:
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT results_json FROM repo_cache WHERE repo_id = ?", (repo_id,))
                row = cursor.fetchone()
                if row and row["results_json"]:
                    return json.loads(row["results_json"])
                return None
        except Exception as e:
            logger.warning(f"Failed to get repo_cache: {e}")
            return None

    def get_stats(self) -> Dict[str, Any]:
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) as count FROM file_cache")
                files_count = cursor.fetchone()["count"]
                cursor.execute("SELECT COUNT(*) as count FROM repo_cache")
                repos_count = cursor.fetchone()["count"]
                
                size_bytes = self.db_path.stat().st_size if self.db_path.exists() else 0
                return {
                    "total_cached_files": files_count,
                    "total_cached_repos": repos_count,
                    "db_size_bytes": size_bytes,
                    "db_path": str(self.db_path)
                }
        except Exception as e:
            logger.error(f"Error reading cache stats: {e}")
            return {
                "total_cached_files": 0,
                "total_cached_repos": 0,
                "db_size_bytes": 0,
                "db_path": str(self.db_path)
            }

    def clear(self) -> int:
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM file_cache")
                cursor.execute("DELETE FROM repo_cache")
                conn.commit()
                cursor.execute("VACUUM")
                return 1
        except Exception as e:
            logger.error(f"Error clearing cache: {e}")
            return 0


cache_service = CacheService()

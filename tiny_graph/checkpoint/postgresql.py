import logging
import time
from datetime import datetime
from typing import List, Literal, Optional, Set

import psycopg2
from psycopg2 import DatabaseError, OperationalError
from psycopg2.extras import DictCursor, Json
from psycopg2.pool import ThreadedConnectionPool

from tiny_graph.checkpoint.base import StorageBackend
from tiny_graph.checkpoint.serialization import serialize_model
from tiny_graph.graph.executable import ChainStatus
from tiny_graph.models.checkpoint import Checkpoint
from tiny_graph.models.state import GraphState

logger = logging.getLogger(__name__)


class PostgreSQLStorage(StorageBackend):
    def __init__(
        self,
        dsn: str,
        min_connections: int = 1,
        max_connections: int = 10,
        connection_timeout: int = 30,
        retry_attempts: int = 3,
        isolation_level: Literal[
            "serializable", "repeatable read", "read committed", "read uncommitted"
        ] = "read committed",
    ):
        """Initialize PostgreSQL storage backend with enhanced configuration."""
        super().__init__()
        self.dsn = dsn
        self.retry_attempts = retry_attempts
        self.isolation_level = isolation_level
        self.connection_timeout = connection_timeout

        # Use connection factory with timeout and health checks
        self.pool = ThreadedConnectionPool(
            minconn=min_connections,
            maxconn=max_connections,
            dsn=dsn,
            connection_factory=self._create_connection_with_timeout,
        )

    def _create_connection_with_timeout(self, dsn=None):
        """Create connection with timeout and proper isolation level.

        Args:
            dsn: Database connection string. If None, uses self.dsn
        """
        dsn = dsn or self.dsn
        # First create the connection without isolation level
        conn = psycopg2.connect(
            dsn,
            connect_timeout=self.connection_timeout,
        )

        # Set isolation level after connection is established
        if self.isolation_level == "read committed":
            conn.isolation_level = psycopg2.extensions.ISOLATION_LEVEL_READ_COMMITTED
        elif self.isolation_level == "read uncommitted":
            conn.isolation_level = psycopg2.extensions.ISOLATION_LEVEL_READ_UNCOMMITTED
        elif self.isolation_level == "repeatable read":
            conn.isolation_level = psycopg2.extensions.ISOLATION_LEVEL_REPEATABLE_READ
        elif self.isolation_level == "serializable":
            conn.isolation_level = psycopg2.extensions.ISOLATION_LEVEL_SERIALIZABLE

        return conn

    def save_checkpoint(
        self,
        state_instance: GraphState,
        chain_id: str,
        chain_status: ChainStatus,
        checkpoint_id: Optional[str] = None,
        next_execution_node: Optional[str] = None,
        executed_nodes: Optional[Set[str]] = None,
    ) -> str:
        checkpoint_id = self._enforce_checkpoint_id(checkpoint_id)
        self._enforce_same_model_version(state_instance, chain_id)

        state_class_str = (
            f"{state_instance.__class__.__module__}.{state_instance.__class__.__name__}"
        )

        serialized_data = serialize_model(state_instance)

        sql = """
        INSERT INTO checkpoints (
            checkpoint_id, chain_id, chain_status, state_class, 
            state_version, data, timestamp, next_execution_node, executed_nodes
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
        ON CONFLICT (checkpoint_id) 
        DO UPDATE SET
            chain_status = EXCLUDED.chain_status,
            data = EXCLUDED.data,
            timestamp = EXCLUDED.timestamp,
            next_execution_node = EXCLUDED.next_execution_node,
            executed_nodes = EXCLUDED.executed_nodes
        """

        for attempt in range(self.retry_attempts):
            try:
                with self.pool.getconn() as conn:
                    with conn.cursor() as cur:
                        # Add advisory lock to prevent concurrent updates
                        cur.execute(
                            "SELECT pg_advisory_xact_lock(%s)", (hash(checkpoint_id),)
                        )

                        cur.execute(
                            sql,
                            (
                                checkpoint_id,
                                chain_id,
                                chain_status.value,
                                state_class_str,
                                getattr(state_instance, "version", None),
                                Json(serialized_data),
                                datetime.now(),
                                next_execution_node,
                                Json(list(executed_nodes)) if executed_nodes else None,
                            ),
                        )
                    conn.commit()
                    logger.info(f"Checkpoint '{checkpoint_id}' saved to PostgreSQL")
                    return checkpoint_id
            except (OperationalError, DatabaseError):
                if attempt == self.retry_attempts - 1:
                    raise
                time.sleep(0.1 * (2**attempt))  # Exponential backoff
            finally:
                self.pool.putconn(conn)

    def load_checkpoint(
        self, state_instance: GraphState, chain_id: str, checkpoint_id: str
    ) -> Checkpoint:
        self._enforce_same_model_version(state_instance, chain_id)

        sql = """
        SELECT * FROM checkpoints 
        WHERE chain_id = %s AND checkpoint_id = %s
        """

        with self.pool.getconn() as conn:
            try:
                with conn.cursor(cursor_factory=DictCursor) as cur:
                    cur.execute(sql, (chain_id, checkpoint_id))
                    result = cur.fetchone()

                    if not result:
                        raise KeyError(
                            f"Checkpoint '{checkpoint_id}' not found for chain '{chain_id}'"
                        )

                    return Checkpoint(
                        checkpoint_id=result["checkpoint_id"],
                        chain_id=result["chain_id"],
                        chain_status=ChainStatus(result["chain_status"]),
                        state_class=result["state_class"],
                        state_version=result["state_version"],
                        data=result["data"],
                        timestamp=result["timestamp"],
                        next_execution_node=result["next_execution_node"],
                        executed_nodes=set(result["executed_nodes"])
                        if result["executed_nodes"]
                        else None,
                    )
            finally:
                self.pool.putconn(conn)

    def list_checkpoints(self, chain_id: str) -> List[Checkpoint]:
        sql = """
        SELECT * FROM checkpoints 
        WHERE chain_id = %s 
        ORDER BY timestamp ASC
        """

        with self.pool.getconn() as conn:
            try:
                with conn.cursor(cursor_factory=DictCursor) as cur:
                    cur.execute(sql, (chain_id,))
                    results = cur.fetchall()

                    return [
                        Checkpoint(
                            checkpoint_id=row["checkpoint_id"],
                            chain_id=row["chain_id"],
                            chain_status=ChainStatus(row["chain_status"]),
                            state_class=row["state_class"],
                            state_version=row["state_version"],
                            data=row["data"],
                            timestamp=row["timestamp"],
                            next_execution_node=row["next_execution_node"],
                            executed_nodes=set(row["executed_nodes"])
                            if row["executed_nodes"]
                            else None,
                        )
                        for row in results
                    ]
            finally:
                self.pool.putconn(conn)

    def delete_checkpoint(self, checkpoint_id: str, chain_id: str) -> None:
        sql = """
        DELETE FROM checkpoints 
        WHERE chain_id = %s AND checkpoint_id = %s
        RETURNING checkpoint_id
        """

        with self.pool.getconn() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(sql, (chain_id, checkpoint_id))
                    if cur.rowcount == 0:
                        raise KeyError(
                            f"Checkpoint '{checkpoint_id}' not found for chain '{chain_id}'"
                        )
                conn.commit()
                logger.info(f"Checkpoint '{checkpoint_id}' deleted from PostgreSQL")
            finally:
                self.pool.putconn(conn)

    def get_last_checkpoint_id(self, chain_id: str) -> Optional[str]:
        sql = """
        SELECT checkpoint_id 
        FROM checkpoints 
        WHERE chain_id = %s 
        ORDER BY timestamp DESC 
        LIMIT 1
        """

        with self.pool.getconn() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(sql, (chain_id,))
                    result = cur.fetchone()
                    return result[0] if result else None
            finally:
                self.pool.putconn(conn)

    def __del__(self):
        """Cleanup connection pool on object destruction."""
        if hasattr(self, "pool"):
            self.pool.closeall()

    def check_schema(self) -> bool:
        """Check if the required tables and columns exist in the database.

        Returns:
            bool: True if schema is valid, False otherwise
        """
        check_table_sql = """
        SELECT EXISTS (
            SELECT FROM information_schema.tables 
            WHERE table_name = 'checkpoints'
        );
        """

        check_columns_sql = """
        SELECT column_name 
        FROM information_schema.columns 
        WHERE table_name = 'checkpoints';
        """

        required_columns = {
            "checkpoint_id",
            "chain_id",
            "chain_status",
            "state_class",
            "state_version",
            "data",
            "timestamp",
            "next_execution_node",
            "executed_nodes",
            "created_at",
        }

        with self.pool.getconn() as conn:
            try:
                with conn.cursor() as cur:
                    # Check if table exists
                    cur.execute(check_table_sql)
                    table_exists = cur.fetchone()[0]

                    if not table_exists:
                        logger.warning("Checkpoints table does not exist")
                        return False

                    # Check columns
                    cur.execute(check_columns_sql)
                    existing_columns = {row[0] for row in cur.fetchall()}

                    missing_columns = required_columns - existing_columns
                    if missing_columns:
                        logger.warning(f"Missing required columns: {missing_columns}")
                        return False

                    return True
            finally:
                self.pool.putconn(conn)

    @classmethod
    def from_url(cls, url: str, **kwargs) -> "PostgreSQLStorage":
        """Create a PostgreSQLStorage instance from a database URL.

        Args:
            url: Database URL in format:
                postgresql://user:password@host:port/dbname
            **kwargs: Additional connection pool parameters

        Returns:
            PostgreSQLStorage: Configured storage instance
        """
        return cls(dsn=url, **kwargs)

    @classmethod
    def from_config(
        cls,
        host: str,
        database: str,
        user: str,
        password: str,
        port: int = 5432,
        **kwargs,
    ) -> "PostgreSQLStorage":
        """Create a PostgreSQLStorage instance from individual configuration parameters.

        Args:
            host: Database host
            database: Database name
            user: Username
            password: Password
            port: Database port (default: 5432)
            **kwargs: Additional connection pool parameters

        Returns:
            PostgreSQLStorage: Configured storage instance
        """
        dsn = f"postgresql://{user}:{password}@{host}:{port}/{database}"
        return cls(dsn=dsn, **kwargs)

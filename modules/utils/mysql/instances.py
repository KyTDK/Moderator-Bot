from __future__ import annotations

from .connection import execute_query


async def update_instance_heartbeat(instance_id: str) -> None:
    """Mark an instance as alive by updating its heartbeat timestamp."""

    if not instance_id:
        return

    await execute_query(
        """
        INSERT INTO bot_instances (instance_id, last_seen)
        VALUES (%s, UTC_TIMESTAMP())
        ON DUPLICATE KEY UPDATE last_seen = VALUES(last_seen)
        """,
        (instance_id,),
    )


async def clear_instance_heartbeat(instance_id: str) -> None:
    """Remove any heartbeat record for the given instance."""

    if not instance_id:
        return

    await execute_query(
        "DELETE FROM bot_instances WHERE instance_id = %s",
        (instance_id,),
    )


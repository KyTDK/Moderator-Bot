from __future__ import annotations

from typing import Iterable, Optional

from modules.faq.models import FAQEntry
from modules.utils import mysql


def _row_to_entry(guild_id: int, row: Iterable) -> FAQEntry:
    entry_id = int(row[0])
    question = row[1] or ""
    answer = row[2] or ""
    raw_vector = row[3] if len(row) > 3 else None
    vector_id: Optional[int] = None
    if raw_vector is not None:
        try:
            vector_id = int(raw_vector)
        except (TypeError, ValueError):
            vector_id = None
    return FAQEntry(
        guild_id=guild_id,
        entry_id=entry_id,
        question=str(question),
        answer=str(answer),
        vector_id=vector_id,
    )


async def fetch_entries(guild_id: int) -> list[FAQEntry]:
    rows, _ = await mysql.execute_query(
        """
        SELECT entry_id, question, answer, vector_id
        FROM faq_entries
        WHERE guild_id = %s
        ORDER BY entry_id ASC
        """,
        (guild_id,),
        fetch_all=True,
        commit=False,
    )
    if not rows:
        return []
    return [_row_to_entry(guild_id, row) for row in rows]


async def fetch_entry(guild_id: int, entry_id: int) -> Optional[FAQEntry]:
    row, _ = await mysql.execute_query(
        """
        SELECT entry_id, question, answer, vector_id
        FROM faq_entries
        WHERE guild_id = %s AND entry_id = %s
        LIMIT 1
        """,
        (guild_id, entry_id),
        fetch_one=True,
        commit=False,
    )
    if not row:
        return None
    return _row_to_entry(guild_id, row)


async def count_entries(guild_id: int) -> int:
    row, _ = await mysql.execute_query(
        "SELECT COUNT(*) FROM faq_entries WHERE guild_id = %s",
        (guild_id,),
        fetch_one=True,
        commit=False,
    )
    if not row:
        return 0
    count = row[0]
    try:
        return int(count or 0)
    except (TypeError, ValueError):
        return 0


async def _allocate_entry_id(guild_id: int) -> int:
    row, _ = await mysql.execute_query(
        "SELECT MAX(entry_id) FROM faq_entries WHERE guild_id = %s",
        (guild_id,),
        fetch_one=True,
        commit=False,
    )
    max_id = 0
    if row and row[0] is not None:
        try:
            max_id = int(row[0])
        except (TypeError, ValueError):
            max_id = 0
    return max_id + 1


async def insert_entry(guild_id: int, question: str, answer: str) -> Optional[FAQEntry]:
    entry_id = await _allocate_entry_id(guild_id)
    _, affected = await mysql.execute_query(
        """
        INSERT INTO faq_entries (guild_id, entry_id, question, answer)
        VALUES (%s, %s, %s, %s)
        """,
        (guild_id, entry_id, question, answer),
    )
    if affected <= 0:
        return None
    return FAQEntry(
        guild_id=guild_id,
        entry_id=entry_id,
        question=question,
        answer=answer,
        vector_id=None,
    )


async def update_vector_id(guild_id: int, entry_id: int, vector_id: Optional[int]) -> bool:
    _, affected = await mysql.execute_query(
        """
        UPDATE faq_entries
        SET vector_id = %s
        WHERE guild_id = %s AND entry_id = %s
        """,
        (vector_id, guild_id, entry_id),
    )
    return affected > 0


async def delete_entry(guild_id: int, entry_id: int) -> Optional[FAQEntry]:
    entry = await fetch_entry(guild_id, entry_id)
    if entry is None:
        return None

    _, affected = await mysql.execute_query(
        "DELETE FROM faq_entries WHERE guild_id = %s AND entry_id = %s",
        (guild_id, entry_id),
    )
    if affected <= 0:
        return None
    return entry

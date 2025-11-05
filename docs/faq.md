# FAQ Module Overview

The FAQ subsystem lets guilds curate question / answer pairs, store them in MySQL, index their questions in Milvus for similarity search, and automatically reply when a member asks something that matches. This document explains every moving part so you can maintain the feature, hook it up to external services (e.g. a dashboard), and reason about runtime behaviour.

---

## High-Level Flow

1. **Configuration** — An admin uses the `/faq` slash commands (or a dashboard backed by them) to add or remove entries. These go through `modules.faq.service`, which performs validation, enforces plan limits, writes to MySQL, and manages Milvus vectors.
2. **Storage** — FAQ entries live in the `faq_entries` table (bootstrapped during MySQL init). Each row stores question, answer, auto-incremented `entry_id`, and the Milvus `vector_id` returned at insert time for later deletion.
3. **Indexing** — When an entry is added, `modules.faq.vector_store` calls into Milvus using the shared `MilvusVectorSpace` utilities. Vectors go into the `faq_text_vectors` collection and are partitioned per guild via the `category` field (`faq:{guild_id}`).
4. **Detection** — `FAQCog.handle_message` listens to Discord messages through `EventDispatcherCog`. If the guild has enabled FAQ responses, the cog calls `find_best_faq_answer`, which normalises the text, chunks it, runs Milvus similarity search, and returns the best matching FAQ above the configured threshold.
5. **Response** — Matches are logged with the standard moderation embed helper (`modules.utils.mod_logging.log_to_channel`), keeping behaviour consistent with the rest of the bot.

---

## Code Layout

| Path | Responsibility |
| ---- | -------------- |
| `modules/faq/models.py` | Lightweight dataclasses (`FAQEntry`, `FAQSearchResult`) shared across layers. |
| `modules/faq/storage.py` | Async MySQL helpers for CRUD operations on `faq_entries`, including vector id updates. |
| `modules/faq/vector_store.py` | Milvus integration using text embeddings; contains collection metadata and search helpers. |
| `modules/faq/service.py` | Core business logic (plan limits, validation, similar-answer lookup, threshold handling). |
| `modules/faq/constants.py` | Tunable threshold defaults/ranges. |
| `modules/faq/settings_keys.py` | Exposes the settings keys consumed elsewhere. |
| `cogs/faq/cog.py` | Slash commands, message listener, embed rendering. |
| `modules/config/settings_schema/faq.py` | Settings schema for FAQ feature toggles; plugged into global schema initialisation. |
| `locales/en/cogs.faq.json` & `locales/en/modules.config.settings_schema.json` | English localisation strings for commands/settings. |
| `tests/test_faq_service.py` | Unit tests covering plan limits, vector wiring, and similarity searches. |

---

## Database & Schema

`modules/utils/mysql/connection.py` ensures the following table exists when the pool initialises:

```sql
CREATE TABLE IF NOT EXISTS faq_entries (
    guild_id  BIGINT      NOT NULL,
    entry_id  INT         NOT NULL,
    question  TEXT        NOT NULL,
    answer    TEXT        NOT NULL,
    vector_id BIGINT      NULL,
    created_at DATETIME   DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME   DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (guild_id, entry_id),
    INDEX idx_faq_vector (vector_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

Entries receive an incremental `entry_id` per guild (`storage._allocate_entry_id`). The optional `vector_id` ties the row to the Milvus vector so we can delete it gracefully.

---

## Milvus Integration

- **Collection**: `faq_text_vectors` (defined in `modules/faq/vector_store.COLLECTION_NAME`).
- **Embedding Model**: Reuses the SentenceTransformer model configured in `modules/utils/text_vectors.embed_text_batch` (384-dim, cosine/IP similarity).
- **Namespace Isolation**: Each guild’s entries get a `category` of `faq:{guild_id}`. During search we filter on that category so we never leak cross-guild data.
- **Insert/Delete**: `vector_store.add_entry` runs in a thread pool via `asyncio.to_thread`, adding the question text along with metadata. The returned primary key is persisted back to MySQL so `vector_store.delete_vector` can remove it on deletion.
- **Threshold**: Searches respect the per-guild threshold (see settings below) by passing it into `MilvusVectorSpace.query_similar_batch`’s `threshold` argument.

If Milvus is unavailable (e.g. dependency missing or connection failure), the service simply skips vector operations. Responses fall back to `None` and the cog silently does nothing, matching existing handling for other scanners.

---

## Settings

The schema is defined in `modules/config/settings_schema/faq.py` and exposed globally via `modules/config/settings_schema.__init__`.

1. `faq-enabled` (bool, default `false`):
   - Toggles whether `FAQCog` will attempt lookups for a guild.
   - Stored in MySQL `settings` table like other guild settings.
   - Managed by dashboard via standard settings update flows.

2. `faq-threshold` (float, default `0.72`, clamped between `0.10` and `1.0`):
   - Controls the minimum Milvus similarity score required to treat a match as valid.
   - Validator raises a `LocalizedError` if the incoming value is outside the allowed range.
   - The service clamps any malformed/NaN values back to defaults as a final safety net.

Settings keys are centralised in `modules/faq/settings_keys.py` for import elsewhere.

---

## Service Layer (modules/faq/service.py)

Key functions:

- `add_faq_entry(guild_id, question, answer) -> FAQEntry`
  - Trims inputs and rejects empty strings.
  - Determines the guild’s plan (`mysql.resolve_guild_plan`) and enforces limits:
    - Free: 5 entries
    - Core: 20
    - Pro: 50
    - Ultra: unlimited
  - Writes to MySQL via `storage.insert_entry`.
  - If Milvus is available, adds the vector and backfills `vector_id`.

- `delete_faq_entry(guild_id, entry_id) -> FAQEntry`
  - Loads, deletes the DB row, and removes the Milvus vector if present.

- `list_faq_entries(guild_id) -> list[FAQEntry]`
  - Convenience wrapper used by commands/dashboard.

- `find_best_faq_answer(guild_id, message_content, *, threshold=None)`
  - Normalises the text (strips mentions/URLs, collapses whitespace).
  - Rejects short inputs (< 2 words) or empty results.
  - Chunks the text into overlapping windows for better recall on long messages.
  - Determines the effective threshold: uses the explicit `threshold` argument when provided; otherwise fetches `faq-threshold` from settings and clamps it.
  - Queries Milvus and sorts candidates by similarity, returning the top FAQ entry if it exceeds the threshold.

Supporting helpers (`_coerce_threshold`, `_chunk_text`, etc.) keep the logic tidy and testable.

---

## Slash Commands & Runtime Behaviour (cogs/faq/cog.py)

### Commands

- `/faq add question:<str> answer:<str>`
  - Uses the service to insert an entry and reports success/errors via localised strings.

- `/faq remove entry_id:<int>`
  - Deletes the specified entry; surfaces “not found” and generic error cases.

- `/faq list`
  - Displays up to 25 FAQs in an embed, with overflow information if more exist.

All command responses are ephemeral to avoid channel noise.

### Message Listener

`FAQCog.handle_message` runs on every guild message (wired in `cogs/event_dispatcher.py`). It:

1. Ignores bot/self messages.
2. Fetches both `faq-enabled` and `faq-threshold` via `mysql.get_settings`.
3. Exits early when disabled.
4. Calls `find_best_faq_answer` with the current threshold.
5. Sends an embed to the moderation logging channel using `mod_logging.log_to_channel`, mirroring other moderation events.

Embeds include the matching question/answer truncated to Discord limits and note the similarity score in the description.

---

## Settings Schema & Localisation

- `modules/config/settings_schema/faq.py` adds FAQ settings to the global schema, letting existing settings commands/dashboards manage them automatically.
- `locales/en/modules.config.settings_schema.json` and `locales/en/cogs.faq.json` supply English strings for descriptions, success/failure messages, and embed labels.
- The localisation structure mirrors other cogs so translators can extend it using the same workflows.

---

## Testing

`tests/test_faq_service.py` covers:

- Plan limit enforcement (`FAQLimitError`).
- Milvus vector ID plumbing during add (using monkeypatches).
- Successful similarity lookup with an explicit threshold.

The test module isolates dependencies by monkeypatching storage/vector functions, so it runs quickly without real database or Milvus instances. Additional integration tests can be layered on if you stub or stand up services.

---

## Dashboard / Backend Integration Notes

- Use the service layer, not raw SQL, for all CRUD operations so Milvus stays consistent and plan limits apply.
- Suggested REST endpoints:
  - `POST /guilds/{guild_id}/faq` → `add_faq_entry`
  - `PATCH /guilds/{guild_id}/faq/{entry_id}` → (optional future enhancement) delete + re-add to update text
  - `DELETE /guilds/{guild_id}/faq/{entry_id}` → `delete_faq_entry`
  - `GET /guilds/{guild_id}/faq` → `list_faq_entries`
  - `PATCH /guilds/{guild_id}/faq/settings` → toggle `faq-enabled`, set `faq-threshold`
- Surface service exceptions (e.g. `FAQLimitError`, validation messages) directly to the dashboard for clear UX.

---

## Operational Considerations

- **Milvus Connectivity**: The system degrades gracefully if Milvus is down—no responses are sent, but errors are logged. Keep an eye on logs so you can alert operators.
- **Plan Upgrades**: When a guild upgrades/downgrades, existing entries stay intact. Only new additions are blocked once the limit is hit.
- **Vector Refresh**: Editing an FAQ isn’t implemented yet. If you add an edit operation, delete the old vector (using the stored `vector_id`) before re-adding the updated question so search stays accurate.
- **Privacy**: Only questions are embedded; answers remain in MySQL. Metadata stored with the vector includes guild id and entry id for debugging.

---

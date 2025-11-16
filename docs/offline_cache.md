# Offline Cache & Disaster Recovery

Moderator Bot keeps a lightweight SQLite mirror of the live MySQL tables under `data/mysql_cache.sqlite3`. When MySQL is unreachable the bot transparently runs against this mirror, queues any pending writes, and replays them once the database comes back online.

## Forcing a Fresh Snapshot

1. Ensure the bot can reach a healthy MySQL instance (production or a local clone).
2. Run the snapshot utility:
   ```bash
   PYTHONPATH=. python scripts/offline_cache_snapshot.py --verbose
   ```
3. The tool will:
   - initialise the aiomysql pool using your `.env` credentials,
   - copy every table schema and row into the offline cache,
   - exit once the mirror is up-to-date.

## Using a `backup.sql` When MySQL Is Down

If the production MySQL cluster is unavailable but you have a recent `backup.sql`, you can seed the offline cache without waiting for the live database:

1. **Start a temporary MySQL container** (or local instance) and import the dump:
   ```bash
   docker run --name modbot-mysql -e MYSQL_ROOT_PASSWORD=localpass -e MYSQL_DATABASE=modbot -p 3306:3306 -d mysql:8
   cat /path/to/backup.sql | docker exec -i modbot-mysql mysql -uroot -plocalpass modbot
   ```
2. **Point your `.env` to the temporary instance**, e.g.:
   ```
   MYSQL_HOST=127.0.0.1
   MYSQL_PORT=3306
   MYSQL_USER=root
   MYSQL_PASSWORD=localpass
   MYSQL_DB=modbot
   ```
3. **Run the snapshot utility** to hydrate the offline cache:
   ```bash
   PYTHONPATH=. python scripts/offline_cache_snapshot.py --verbose
   ```
4. **Shut down the temporary MySQL** once the snapshot completes:
   ```bash
   docker rm -f modbot-mysql
   ```

The cached data now reflects the backup and will be used automatically if the real database remains offline. Store `backup.sql` (or any archives) under `backups/`—the directory is ignored by git and safe for local-only artifacts.

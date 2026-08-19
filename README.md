# Supabase → MongoDB Capacity Migration

Automatically migrate data from Supabase to MongoDB when database capacity reaches a configurable threshold, then stop when capacity falls to a target level.

**Data safety is the #1 priority.** A record is only deleted from Supabase after it has been successfully written to MongoDB, verified with a SHA-256 hash, and confirmed to exist. If anything goes wrong, the Supabase record is kept.

---

## Table of Contents

1. [Project Purpose](#1-project-purpose)
2. [Architecture](#2-architecture)
3. [Repository Structure](#3-repository-structure)
4. [Supabase Setup](#4-supabase-setup)
5. [MongoDB Setup](#5-mongodb-setup)
6. [GitHub Secrets Setup](#6-github-secrets-setup)
7. [Table Configuration](#7-table-configuration)
8. [Capacity Configuration](#8-capacity-configuration)
9. [GitHub Actions Setup](#9-github-actions-setup)
10. [Manual Execution](#10-manual-execution)
11. [Dry-Run Mode](#11-dry-run-mode)
12. [AI Configuration](#12-ai-configuration)
13. [Security](#13-security)
14. [Failure Recovery](#14-failure-recovery)
15. [Testing](#15-testing)
16. [Troubleshooting](#16-troubleshooting)
17. [Free-Tier Limitations](#17-free-tier-limitations)

---

## 1. Project Purpose

This system solves a real problem: **Supabase free-tier projects have a 500 MB database limit** (and even paid tiers have soft limits per compute instance). When your database approaches capacity, this automation:

1. Detects when capacity hits 90%
2. Moves the oldest records to MongoDB
3. Verifies each record was copied correctly
4. Deletes only verified records from Supabase
5. Continues until capacity drops to 50%
6. Stops and waits for the next scheduled run

The entire system runs as a **serverless batch job** in GitHub Actions — no always-on PC, no Docker, no paid infrastructure.

---

## 2. Architecture

```
GitHub Actions (scheduled every 15 min)
        │
        ▼
┌─────────────────────────┐
│  1. Check Supabase DB   │  ← pg_database_size(current_database())
│     capacity             │
└────────┬────────────────┘
         │
    ┌────▼────┐
    │ >= 90%? │──── NO ──→ Exit (nothing to do)
    └────┬────┘
         │ YES
         ▼
┌─────────────────────────┐
│  2. Read config/tables  │  ← JSON-driven table list
│     Filter enabled       │
└────────┬────────────────┘
         │
         ▼
┌─────────────────────────┐
│  3. For each table:     │
│     Fetch batch (500)    │  ← ORDER BY created_at ASC
│     Build MongoDB doc    │  ← Add _migration metadata
│     Upsert to MongoDB    │  ← Idempotent (update_one + upsert)
│     Verify document      │  ← SHA-256 hash comparison
│     Delete from Supabase │  ← ONLY if verified
│     Log to MongoDB       │
└────────┬────────────────┘
         │
    ┌────▼──────────┐
    │ Capacity <=50%│──── YES → Stop
    └────┬──────────┘
         │ NO
         ▼
      Continue loop
```

### Key Design Principles

- **Zero data loss**: Never delete from Supabase unless MongoDB verification passes
- **Idempotent**: Safe to retry — upserts prevent duplicates
- **Portable**: Core logic is Python, not YAML — runnable from any scheduler
- **Configurable**: Thresholds, batch sizes, tables — all via config
- **AI-optional**: Works completely without AI; AI is informational only

---

## 3. Repository Structure

```
supabase-mongodb-migrator/
│
├── .github/
│   └── workflows/
│       └── migration.yml          # GitHub Actions workflow
│
├── src/
│   ├── __init__.py
│   ├── config.py                  # Environment & table configuration
│   ├── capacity.py                # Supabase capacity monitoring
│   ├── supabase_client.py         # Supabase PostgreSQL data access
│   ├── mongodb_client.py          # MongoDB upsert, verify, log
│   ├── verification.py            # Data integrity verification
│   ├── migration.py               # Core migration engine
│   ├── ai_processor.py            # Optional AI analysis
│   ├── logger.py                  # Structured logging
│   └── main.py                    # Entry point
│
├── config/
│   └── tables.json                # Table mapping configuration
│
├── tests/
│   ├── conftest.py                # Shared fixtures
│   ├── test_capacity.py           # Capacity threshold tests
│   ├── test_migration.py          # Migration flow tests
│   ├── test_verification.py       # Integrity verification tests
│   └── test_idempotency.py        # Idempotency tests
│
├── .env.example                   # Environment variable template
├── .gitignore
├── requirements.txt
└── README.md
```

---

## 4. Supabase Setup

### 4.1 Find Your Database Connection Details

1. Go to your Supabase project dashboard
2. Navigate to **Settings → Database → Connection string**
3. Under **Direct connection**, find:
   - **Host** (e.g., `db.xxxxx.supabase.co`)
   - **Port** (default: `5432`)
   - **Database name** (default: `postgres`)
   - **User** (default: `postgres`)
   - **Password**

> **Note:** The migration uses direct PostgreSQL connections (not the REST API) because we need `pg_database_size()` and `DELETE` capabilities.

### 4.2 Determine Your Database Size Limit

Your maximum database size depends on your compute tier:

| Compute Tier | Max DB Size | `SUPABASE_MAX_DB_SIZE_BYTES` |
|---|---|---|
| Nano (Free) | 500 MB | `524288000` |
| Micro | 10 GB | `10737418240` |
| Small | 50 GB | `53687091200` |
| Medium | 100 GB | `107374182400` |
| Large | 200 GB | `214748364800` |
| XL | 500 GB | `536870912000` |
| 2XL | 1 TB | `1099511627776` |

Find your compute tier in the Supabase dashboard under **Settings → Infrastructure → Compute**.

### 4.3 How Capacity Is Monitored

The system connects to your Supabase PostgreSQL database and runs:

```sql
SELECT pg_database_size(current_database());
```

This returns the **database size in bytes** — the actual PostgreSQL data size, not file storage or bandwidth. The percentage is then:

```
usage_pct = (pg_database_size / SUPABASE_MAX_DB_SIZE_BYTES) × 100
```

**Important limitation:** Supabase does not expose a "percentage used" API. The `pg_database_size()` function is the most reliable metric. It measures the logical database size including indexes and TOAST data.

---

## 5. MongoDB Setup

### 5.1 Free Options

- **MongoDB Atlas Free Tier (M0):** 512 MB storage — [mongodb.com/atlas](https://www.mongodb.com/atlas/database)
- **Self-hosted MongoDB:** Any MongoDB instance you control

### 5.2 Create a Database

1. Create a MongoDB Atlas cluster (or use your self-hosted instance)
2. Create a database user with **readWrite** permissions
3. Note the connection string (e.g., `mongodb+srv://user:pass@cluster.mongodb.net/`)

### 5.3 Recommended Database Name

Use a dedicated database for migration (e.g., `supabase_migration`). This limits the blast radius and keeps migration data separate from application data.

### 5.4 MongoDB Capacity Safety

Before deleting any Supabase record, the system checks MongoDB's available capacity. If the destination is full, migration stops immediately and all Supabase records are preserved.

---

## 6. GitHub Secrets Setup

Go to your GitHub repository → **Settings → Secrets and variables → Actions → New repository secret**.

### Required Secrets

| Secret Name | Description | Example |
|---|---|---|
| `SUPABASE_URL` | Supabase project URL | `https://xxxxx.supabase.co` |
| `SUPABASE_KEY` | Supabase anon/service key | `eyJhbGci...` |
| `SUPABASE_DB_HOST` | PostgreSQL host from Supabase dashboard | `db.xxxxx.supabase.co` |
| `SUPABASE_DB_PORT` | PostgreSQL port | `5432` |
| `SUPABASE_DB_NAME` | Database name | `postgres` |
| `SUPABASE_DB_USER` | Database user | `postgres` |
| `SUPABASE_DB_PASSWORD` | Database password | `your-password` |
| `SUPABASE_MAX_DB_SIZE_BYTES` | Max DB size in bytes for your tier | `524288000` |
| `MONGODB_URI` | MongoDB connection string | `mongodb+srv://...` |
| `MONGODB_DATABASE` | MongoDB database name | `supabase_migration` |

### Optional Secrets

| Secret Name | Default | Description |
|---|---|---|
| `START_THRESHOLD` | `90` | Start migration at this usage % |
| `TARGET_THRESHOLD` | `50` | Stop migration at this usage % |
| `BATCH_SIZE` | `500` | Records per batch |
| `MAX_RETRIES` | `3` | Retry attempts per operation |
| `RETRY_DELAY_SECONDS` | `10` | Base delay (exponential backoff) |
| `MIGRATION_ENABLED` | `true` | Emergency stop (`false` = no deletes) |
| `DRY_RUN` | `false` | Test mode (`true` = no deletions) |
| `AI_ENABLED` | `false` | Enable optional AI analysis |
| `AI_PROVIDER` | | `openai`, `anthropic`, `gemini`, `openrouter` |
| `AI_API_KEY` | | API key for AI provider |
| `AI_MODEL` | | Model name (provider-specific default used if empty) |

---

## 7. Table Configuration

Tables are configured in `config/tables.json`. **No code changes needed** to add or remove tables.

```json
{
  "tables": [
    {
      "supabase_table": "inventory",
      "mongodb_collection": "inventory",
      "primary_key": "id",
      "sort_column": "created_at",
      "enabled": true
    },
    {
      "supabase_table": "orders",
      "mongodb_collection": "orders",
      "primary_key": "id",
      "sort_column": "created_at",
      "enabled": true
    }
  ]
}
```

### Field Reference

| Field | Required | Default | Description |
|---|---|---|---|
| `supabase_table` | Yes | — | PostgreSQL table name in Supabase |
| `mongodb_collection` | Yes | — | Target MongoDB collection name |
| `primary_key` | No | `id` | Column used as unique record identifier |
| `sort_column` | No | `created_at` | Column to sort by (oldest first) |
| `enabled` | No | `true` | Set to `false` to skip this table |

### Adding a New Table

1. Add the table to Supabase
2. Add a new entry to `config/tables.json`
3. Commit and push — the next workflow run will include it

---

## 8. Capacity Configuration

### Thresholds

| Parameter | Default | Description |
|---|---|---|
| `START_THRESHOLD` | `90` | Migration begins when usage ≥ this % |
| `TARGET_THRESHOLD` | `50` | Migration stops when usage ≤ this % |
| `SUPABASE_MAX_DB_SIZE_BYTES` | `524288000` (500 MB) | Your tier's max DB size in bytes |

### How Capacity Is Calculated

```
usage_percentage = (pg_database_size(current_database()) / SUPABASE_MAX_DB_SIZE_BYTES) × 100
```

The system does **not** use a hardcoded number. It queries the actual PostgreSQL database size and divides by your tier's maximum.

### Custom Thresholds

Set via GitHub Secrets or environment variables:

```bash
START_THRESHOLD=85    # Start earlier
TARGET_THRESHOLD=40   # Stop later
```

---

## 9. GitHub Actions Setup

### 9.1 Workflow Overview

The workflow (`.github/workflows/migration.yml`) runs:

- **Automatically:** Every 15 minutes via cron
- **Manually:** Via `workflow_dispatch` with optional overrides

### 9.2 Schedule Configuration

Edit the cron expression in the workflow:

```yaml
on:
  schedule:
    - cron: "*/15 * * * *"   # Every 15 minutes (default)
    # - cron: "*/5 * * * *"  # Every 5 minutes
    # - cron: "0 * * * *"    # Every hour
```

**Note:** GitHub Actions cron minimum granularity is 5 minutes. The schedule is not guaranteed to run at exact times during high GitHub Actions load.

### 9.3 Concurrency Protection

The workflow uses `concurrency` groups to prevent overlapping migrations:

```yaml
concurrency:
  group: supabase-mongodb-migration
  cancel-in-progress: false
```

Only one migration job runs at a time. If a second run is triggered while the first is active, it queues until the first completes.

### 9.4 Manual Trigger

1. Go to **Actions** tab in your repository
2. Select **Supabase MongoDB Migration**
3. Click **Run workflow**
4. Optionally set:
   - **dry_run:** `true` to test without deletions
   - **migration_enabled:** `false` to run capacity check only

---

## 10. Manual Execution

### From Command Line

```bash
# Clone the repository
git clone https://github.com/your-org/supabase-mongodb-migrator.git
cd supabase-mongodb-migrator

# Create a virtual environment
python -m venv venv
source venv/bin/activate  # or venv\Scripts\activate on Windows

# Install dependencies
pip install -r requirements.txt

# Set environment variables (or copy .env.example to .env)
export SUPABASE_DB_HOST="db.xxxxx.supabase.co"
export SUPABASE_DB_PASSWORD="your-password"
export MONGODB_URI="mongodb+srv://..."
export MONGODB_DATABASE="supabase_migration"

# Run the migration
python -m src.main
```

### Exit Codes

| Code | Meaning |
|---|---|
| `0` | Success (may mean "no migration needed") |
| `1` | Unrecoverable error |

---

## 11. Dry-Run Mode

Dry-run mode lets you test the entire pipeline without touching your Supabase data.

### Enable

```bash
export DRY_RUN=true
python -m src.main
```

Or via GitHub Actions:
- Trigger manually with `dry_run: true`

### What Dry-Run Does

✅ Checks Supabase capacity
✅ Reads table configurations
✅ Fetches record batches
✅ Builds MongoDB documents
✅ Upserts to MongoDB (optional, controlled by code)
✅ Verifies documents in MongoDB
❌ **Does NOT delete any Supabase records**

Output includes:

```
DRY RUN — NO DATA DELETED
```

---

## 12. AI Configuration

AI is **completely optional**. The migration works without it.

### Supported Providers

| Provider | `AI_PROVIDER` | Default Model | Free Tier |
|---|---|---|---|
| OpenAI | `openai` | `gpt-4o-mini` | Pay-per-use |
| Anthropic | `anthropic` | `claude-3-haiku-20240307` | Pay-per-use |
| Google Gemini | `gemini` | `gemini-1.5-flash` | Free tier available |
| OpenRouter | `openrouter` | `gpt-4o-mini` | Various free models |

### Setup

```bash
export AI_ENABLED=true
export AI_PROVIDER=gemini
export AI_API_KEY=your-api-key
export AI_MODEL=gemini-1.5-flash  # optional
```

### What AI Does

After each successful migration, the AI receives a **safe, redacted** version of the migrated record and returns a structured analysis:

```json
{
  "action": "detect_anomaly",
  "priority": "medium",
  "reason": "Unexpected inventory quantity.",
  "recommendation": "Review inventory record.",
  "confidence": 0.92
}
```

### AI Safety Guarantees

- AI **never** deletes Supabase records
- AI **never** executes SQL or shell commands
- AI **never** receives credentials or connection strings
- AI **never** modifies migration state
- AI output is logged for review only

---

## 13. Security

### Secrets Protection

- All credentials stored in GitHub Secrets (never in code)
- `.env` files are gitignored
- No secrets printed to GitHub Actions logs
- Sensitive fields redacted before AI calls

### Database Permissions (Principle of Least Privilege)

**Supabase user:** Should only have:
- `SELECT` on migration tables
- `DELETE` on migration tables
- `CONNECT` to the database

**MongoDB user:** Should only have:
- `readWrite` on the migration database
- No admin access

### GitHub Actions Security

- Secrets are masked in logs (GitHub automatically masks values)
- The workflow uses `timeout-minutes: 30` to prevent runaway jobs
- Concurrency controls prevent race conditions

---

## 14. Failure Recovery

### Scenario: MongoDB Upsert Fails

```
Supabase record: KEPT (not deleted)
Next run: Retry upsert (idempotent)
```

### Scenario: MongoDB Verification Fails

```
Supabase record: KEPT (not deleted)
MongoDB document: May exist with incorrect data
Next run: Upsert overwrites, verification re-checks
```

### Scenario: Supabase Deletion Fails

```
MongoDB document: EXISTS (verified)
Supabase record: KEPT
Next run: Upsert is idempotent (no duplicate), retry deletion
```

### Scenario: Both Fail

```
Supabase record: KEPT
MongoDB document: May or may not exist
Next run: Idempotent upsert + verification + retry
```

### Scenario: AI Fails

```
Migration: CONTINUES normally
AI status: logged as "failed"
No impact on data integrity
```

### Emergency Stop

Set `MIGRATION_ENABLED=false` in GitHub Secrets. The next run will:

- Still check capacity (read-only)
- NOT delete any records
- Log "MIGRATION DISABLED"

### Manual Recovery

If you need to manually intervene:

1. Check `migration_logs` collection in MongoDB for the status of each record
2. If a record is in MongoDB but not deleted from Supabase — it's safe, the next run will retry
3. If you need to roll back a migration, copy records from MongoDB back to Supabase

---

## 15. Testing

### Run All Tests

```bash
pip install -r requirements.txt
pytest tests/ -v
```

### Test Coverage

| Test | Scenario | Expected Behavior |
|---|---|---|
| Test 1 | Usage = 89% | No migration |
| Test 2 | Usage = 90% | Migration starts |
| Test 3 | MongoDB succeeds | Verify → Delete Supabase |
| Test 4 | MongoDB fails | Keep Supabase |
| Test 5 | Verification fails | Keep Supabase |
| Test 6 | Supabase delete fails | Retry deletion |
| Test 7 | Two runs | No duplicate MongoDB docs |
| Test 8 | Capacity reaches 50% | Stop migration |
| Test 9 | MongoDB full | Stop, keep Supabase |
| Test 10 | AI fails | Migration continues |
| Test 11 | Dry-run | No Supabase deletions |
| Test 12 | Migration disabled | No destructive action |

### Test Structure

```
tests/
├── test_capacity.py        # Tests 1, 2, 8, 9
├── test_migration.py       # Tests 3, 4, 5, 6, 11, 12
├── test_verification.py    # Verification logic + hash integrity
└── test_idempotency.py     # Test 7 + general idempotency
```

---

## 16. Troubleshooting

### "SUPABASE_DB_HOST is not set"

- You need the **direct connection** host from Supabase dashboard → Settings → Database → Connection string → Direct connection

### "Could not determine database size"

- Check that `SUPABASE_DB_HOST`, `SUPABASE_DB_USER`, and `SUPABASE_DB_PASSWORD` are correct
- Ensure the database user has permission to run `pg_database_size()`
- Check that the Supabase project is not paused (free-tier projects pause after 1 week of inactivity)

### "No enabled tables in config"

- Check `config/tables.json` has at least one table with `"enabled": true`

### "Migration disabled via MIGRATION_ENABLED=false"

- Set `MIGRATION_ENABLED=true` in GitHub Secrets

### "Usage X% is below start threshold"

- This is normal! The system only migrates when capacity ≥ 90%
- To test, set `START_THRESHOLD` to a lower value (e.g., 0)

### "MongoDB connection failed"

- Verify `MONGODB_URI` format: `mongodb+srv://user:pass@cluster.mongodb.net/`
- Verify `MONGODB_DATABASE` is set
- Check MongoDB Atlas network access (IP whitelist)

### Tests fail with import errors

```bash
pip install -r requirements.txt
pytest tests/ -v
```

---

## 17. Free-Tier Limitations

### Supabase Free Tier

- **Database size:** 500 MB per project
- **Projects:** Maximum 2 active projects
- **Inactivity pause:** Projects pause after 1 week of no activity
- **No automatic backups**
- **Compute:** Shared CPU, up to 0.5 GB RAM

### MongoDB Atlas Free Tier (M0)

- **Storage:** 512 MB
- **RAM:** Shared
- **No backups**
- **Region availability:** Limited

### GitHub Actions Free Tier

- **Public repos:** Unlimited
- **Private repos:** 2,000 minutes/month (free plan)
- **Cron minimum:** 5 minutes (not guaranteed exact time)

### Important Caveats

1. **Supabase free-tier projects pause after 1 week of inactivity.** The migration workflow helps prevent this by running regularly.

2. **GitHub Actions cron is not precise.** During high load, scheduled runs may be delayed. This is acceptable for a batch process.

3. **MongoDB M0 free tier has 512 MB storage.** If your Supabase data exceeds this, you'll need a larger MongoDB tier or a self-hosted instance.

4. **Free-tier limits can change.** Monitor your Supabase and MongoDB dashboards. This system does not guarantee unlimited free usage.

5. **pg_database_size() includes indexes and TOAST data.** The reported size may be larger than the raw data you see in the dashboard.

---

## License

MIT

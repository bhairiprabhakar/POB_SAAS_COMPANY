"""Apply missing Phase 9 migration columns to ALL tenant databases."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from saas import config, db_utils

# Connect to platform DB to list all tenant databases
platform_pool = db_utils.make_pool(config.PLATFORM_DB_NAME, minconn=1, maxconn=1)
pc = platform_pool.get_conn()
pc.autocommit = True
cur = pc.cursor()
cur.execute("SELECT tenant_db_name FROM companies WHERE tenant_db_name IS NOT NULL")
tenant_dbs = [r[0] for r in cur.fetchall()]
pc.close()

print(f"Found {len(tenant_dbs)} tenant database(s): {tenant_dbs}")

COLS = [
    ("pob_activities", "verification_state", "TEXT DEFAULT 'draft'"),
    ("pob_activities", "last_verification_state", "TEXT"),
    ("pob_verifications", "pipeline_status", "TEXT DEFAULT 'pending_agent'"),
    ("pob_verifications", "ai_extraction", "JSONB"),
    ("pob_verifications", "normalized_data", "JSONB"),
    ("pob_verifications", "mapped_product_id", "INTEGER"),
    ("pob_verifications", "product_mapping_confidence", "NUMERIC DEFAULT 0"),
    ("pob_verifications", "duplicate_check_result", "JSONB"),
    ("pob_verifications", "completeness_score", "NUMERIC DEFAULT 0"),
    ("pob_verifications", "correction_count", "INTEGER DEFAULT 0"),
    ("ocr_extractions", "field_confidences", "JSONB"),
    ("ocr_extractions", "extraction_step", "TEXT"),
    ("campaigns", "auto_reject_confidence", "NUMERIC DEFAULT 0"),
    ("campaigns", "manual_review_threshold", "NUMERIC DEFAULT 0"),
    ("campaigns", "allow_manual_override", "BOOLEAN DEFAULT TRUE"),
]

for tenant_db in tenant_dbs:
    print(f"\n=== {tenant_db} ===")
    pool = db_utils.make_pool(tenant_db, minconn=1, maxconn=2)
    conn = pool.get_conn()
    c = conn.cursor()
    applied = 0
    for table, col, typedef in COLS:
        c.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name=%s AND column_name=%s",
            (table, col),
        )
        if not c.fetchone():
            sql = f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {typedef}"
            print(f"  ADD {table}.{col}")
            c.execute(sql)
            applied += 1
    if applied:
        conn.commit()
        print(f"  Applied {applied} column(s)")
    else:
        print("  All columns present")
    pool.close_all()

print("\nDone!")

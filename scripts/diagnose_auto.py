"""Diagnose invoice automation for DRL12345 + check ALL missing columns."""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from saas import config, db_utils

platform_pool = db_utils.make_pool(config.PLATFORM_DB_NAME, minconn=1, maxconn=1)
pc = platform_pool.get_conn()
pc.autocommit = True
cur = pc.cursor()
cur.execute("SELECT tenant_db_name FROM companies WHERE code='DRL12345'")
row = cur.fetchone()
tenant_db = row[0]
print(f"Tenant DB: {tenant_db}")
pc.close()
platform_pool.close_all()

pool = db_utils.make_pool(tenant_db, minconn=1, maxconn=2)
conn = pool.get_conn()
c = conn.cursor()

# Check ALL expected columns in campaigns
print("\n=== CAMPAIGNS TABLE COLUMNS ===")
c.execute("SELECT column_name FROM information_schema.columns WHERE table_name='campaigns' ORDER BY ordinal_position")
camp_cols = [r[0] for r in c.fetchall()]
expected_camp = ['pob_required', 'auto_reject_confidence', 'manual_review_threshold', 'allow_manual_override']
for col in expected_camp:
    status = "PRESENT" if col in camp_cols else "MISSING"
    print(f"  {col}: {status}")

# Check ALL expected columns in pob_activities
print("\n=== pob_activities COLUMNS ===")
c.execute("SELECT column_name FROM information_schema.columns WHERE table_name='pob_activities' ORDER BY ordinal_position")
pa_cols = [r[0] for r in c.fetchall()]
expected_pa = ['verification_state', 'last_verification_state']
for col in expected_pa:
    status = "PRESENT" if col in pa_cols else "MISSING"
    print(f"  {col}: {status}")

# Check ALL expected columns in pob_verifications
print("\n=== pob_verifications COLUMNS ===")
c.execute("SELECT column_name FROM information_schema.columns WHERE table_name='pob_verifications' ORDER BY ordinal_position")
pv_cols = [r[0] for r in c.fetchall()]
expected_pv = ['pipeline_status', 'ai_extraction', 'normalized_data', 'mapped_product_id',
               'product_mapping_confidence', 'duplicate_check_result', 'completeness_score', 'correction_count']
for col in expected_pv:
    status = "PRESENT" if col in pv_cols else "MISSING"
    print(f"  {col}: {status}")

# Check ALL expected columns in ocr_extractions
print("\n=== ocr_extractions COLUMNS ===")
c.execute("SELECT column_name FROM information_schema.columns WHERE table_name='ocr_extractions' ORDER BY ordinal_position")
oe_cols = [r[0] for r in c.fetchall()]
expected_oe = ['field_confidences', 'extraction_step']
for col in expected_oe:
    status = "PRESENT" if col in oe_cols else "MISSING"
    print(f"  {col}: {status}")

# Apply missing columns
print("\n=== APPLYING MISSING COLUMNS ===")
ALL_COLS = [
    ("campaigns", "pob_required", "BOOLEAN DEFAULT TRUE"),
    ("campaigns", "auto_reject_confidence", "NUMERIC DEFAULT 0"),
    ("campaigns", "manual_review_threshold", "NUMERIC DEFAULT 0"),
    ("campaigns", "allow_manual_override", "BOOLEAN DEFAULT TRUE"),
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
]
applied = 0
for table, col, typedef in ALL_COLS:
    c.execute("SELECT column_name FROM information_schema.columns WHERE table_name=%s AND column_name=%s", (table, col))
    if not c.fetchone():
        sql = f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {typedef}"
        print(f"  ADD {table}.{col}")
        c.execute(sql)
        applied += 1
if applied:
    conn.commit()
    print(f"  Applied {applied} column(s)")
else:
    print("  All columns already present")

# Now run the diagnosis
print("\n=== CAMPAIGNS ===")
c.execute("SELECT id, name, auto_verify, auto_verify_confidence, approval_workflow_id FROM campaigns WHERE active=TRUE")
for r in c.fetchall():
    print(f"  Campaign #{r[0]}: name={r[1]}, auto_verify={r[2]}, auto_verify_conf={r[3]}, workflow={r[4]}")

print("\n=== LATEST POBs (last 10) ===")
c.execute("""SELECT pa.id, pa.campaign_id, pa.status, pa.verification_state, pa.invoice_number,
                    pa.invoice_date, pa.auto_verified, pa.confidence, pa.product_id, pa.quantity,
                    pa.pob_amount, pa.invoice_amount, pa.content_hash IS NOT NULL, pa.created_at
             FROM pob_activities pa ORDER BY pa.id DESC LIMIT 10""")
for r in c.fetchall():
    print(f"  POB #{r[0]}: camp={r[1]}, status={r[2]}, v_state={r[3]}, "
          f"inv_no={r[4]}, inv_date={r[5]}, auto_v={r[6]}, conf={r[7]}, "
          f"prod={r[8]}, qty={r[9]}, pob_amt={r[10]}, inv_amt={r[11]}, "
          f"hash={r[12]}, created={r[13]}")

print("\n=== LATEST VERIFICATIONS (last 10) ===")
c.execute("""SELECT pv.id, pv.pob_id, pv.status, pv.pipeline_status, pv.auto_approved,
                    pv.confidence, pv.ai_extraction IS NOT NULL as has_ai, pv.created_at
             FROM pob_verifications pv ORDER BY pv.id DESC LIMIT 10""")
for r in c.fetchall():
    print(f"  Verif #{r[0]}: pob={r[1]}, status={r[2]}, pipeline={r[3]}, "
          f"auto_approved={r[4]}, conf={r[5]}, has_ai={r[6]}, created={r[7]}")

print("\n=== LATEST OCR (last 10) ===")
c.execute("""SELECT oe.id, oe.pob_id, oe.engine, oe.confidence, oe.auto_approved,
                    substring(coalesce(jsonb_pretty(oe.fields), '') for 300) as fields_preview
             FROM ocr_extractions oe ORDER BY oe.id DESC LIMIT 10""")
for r in c.fetchall():
    print(f"  OCR #{r[0]}: pob={r[1]}, eng={r[2]}, conf={r[3]}, auto={r[4]}, fields={r[5]}")

print("\n=== VERIFICATION HISTORY (last 10) ===")
c.execute("""SELECT vh.id, vh.pob_id, vh.action, vh.reason, vh.created_at
             FROM verification_history vh ORDER BY vh.id DESC LIMIT 10""")
for r in c.fetchall():
    print(f"  #{r[0]}: pob={r[1]}, action={r[2]}, reason={str(r[3])[:120]}, created={r[4]}")

pool.close_all()

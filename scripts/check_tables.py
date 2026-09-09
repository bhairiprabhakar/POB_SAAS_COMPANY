"""Check if new tables exist in DEMO1234 tenant DB."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from saas import config, db_utils

pool = db_utils.make_pool("DEMO1234", minconn=1, maxconn=2)
conn = pool.get_conn()
c = conn.cursor()
tables = ["product_aliases", "campaign_verification_rules", "verification_corrections", "verification_pipeline_steps"]
for t in tables:
    c.execute("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name=%s)", (t,))
    exists = c.fetchone()[0]
    status = "EXISTS" if exists else "MISSING"
    print(f"{t}: {status}")
pool.close_all()

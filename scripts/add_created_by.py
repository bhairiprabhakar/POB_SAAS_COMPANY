"""Add created_by column to chemists in all tenant DBs."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from saas import config, db_utils

platform_pool = db_utils.make_pool(config.PLATFORM_DB_NAME, minconn=1, maxconn=1)
pc = platform_pool.get_conn()
pc.autocommit = True
cur = pc.cursor()
cur.execute("SELECT tenant_db_name FROM companies WHERE status='active'")
tenant_dbs = [r[0] for r in cur.fetchall()]
pc.close()
platform_pool.close_all()

print(f"Processing {len(tenant_dbs)} tenant database(s)...")
for tenant_db in tenant_dbs:
    pool = db_utils.make_pool(tenant_db, minconn=1, maxconn=2)
    conn = pool.get_conn()
    c = conn.cursor()
    c.execute("SELECT column_name FROM information_schema.columns WHERE table_name='chemists' AND column_name='created_by'")
    if not c.fetchone():
        c.execute("ALTER TABLE chemists ADD COLUMN IF NOT EXISTS created_by INTEGER")
        c.execute("""UPDATE chemists c SET created_by = sub.uid
            FROM (SELECT DISTINCT ON (chemist_id) chemist_id, user_id AS uid
                  FROM pob_activities ORDER BY chemist_id, id) sub
            WHERE c.id = sub.chemist_id AND c.created_by IS NULL""")
        conn.commit()
        print(f"  {tenant_db}: added created_by")
    else:
        print(f"  {tenant_db}: already has created_by")
    pool.close_all()
print("Done!")

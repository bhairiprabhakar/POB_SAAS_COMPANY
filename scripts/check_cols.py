"""Quick column check for pob_verifications and ocr_extractions."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from saas import db_utils

pool = db_utils.make_pool('pob_cmp_0016', minconn=1, maxconn=1)
conn = pool.get_conn()
c = conn.cursor()

c.execute("SELECT column_name FROM information_schema.columns WHERE table_name='pob_verifications' ORDER BY ordinal_position")
print("pob_verifications cols:", [r[0] for r in c.fetchall()])

c.execute("SELECT column_name FROM information_schema.columns WHERE table_name='ocr_extractions' ORDER BY ordinal_position")
print("ocr_extractions cols:", [r[0] for r in c.fetchall()])

c.execute("SELECT column_name FROM information_schema.columns WHERE table_name='pob_activities' ORDER BY ordinal_position")
print("pob_activities cols:", [r[0] for r in c.fetchall()])

pool.close_all()

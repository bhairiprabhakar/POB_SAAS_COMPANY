"""Reset stuck POBs for re-upload."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from saas import db_utils

pool = db_utils.make_pool('pob_cmp_0016', minconn=1, maxconn=2)
conn = pool.get_conn()
c = conn.cursor()

c.execute("SELECT id, status, verification_state, invoice_number FROM pob_activities WHERE id IN (6,7)")
for r in c.fetchall():
    print(f"POB #{r[0]}: status={r[1]}, v_state={r[2]}, inv_no={r[3]}")

c.execute("SELECT id, pob_id, status, pipeline_status FROM pob_verifications WHERE pob_id IN (6,7)")
for r in c.fetchall():
    print(f"Verif #{r[0]}: pob={r[1]}, status={r[2]}, pipeline={r[3]}")

c.execute("""UPDATE pob_activities SET status='submitted', verification_state='draft',
    invoice_number=NULL, invoice_date=NULL, invoice_path=NULL, content_hash=NULL,
    invoice_number_norm=NULL, proof_submitted_at=NULL, auto_verified=FALSE, confidence=0,
    current_verification_id=NULL
    WHERE id IN (6,7)""")
print(f"Reset {c.rowcount} POBs to submitted")

c.execute("DELETE FROM pob_verifications WHERE pob_id IN (6,7)")
print(f"Deleted {c.rowcount} verification records")
c.execute("DELETE FROM verification_history WHERE pob_id IN (6,7)")
print(f"Deleted {c.rowcount} history records")
c.execute("DELETE FROM ocr_extractions WHERE pob_id IN (6,7)")
print(f"Deleted {c.rowcount} OCR records")
c.execute("DELETE FROM pob_approvals WHERE pob_id IN (6,7)")
print(f"Deleted {c.rowcount} approval records")

conn.commit()
print("\nDone! POBs reset - user can re-upload.")
pool.close_all()

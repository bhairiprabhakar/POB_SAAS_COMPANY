"""
Seed a demo tenant so the platform can be explored immediately.

Creates a company (default code DEMO1234), provisions its database, seeds
brands / campaigns / products / chemists and a small user roster
(MR, ASM, verifier, finance) wired into the default hierarchy and roles.

Run:  python scripts/seed_demo.py            (creates DEMO1234)
      python scripts/seed_demo.py --code DEMO5678 --password Admin@123 --reset
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.security import hash_pw
from saas import platform_db, pools, provision
from saas.db_utils import get_conn


def _resolve_id(c, table, value):
    c.execute(f"SELECT id FROM {table} WHERE name=%s", (value,))
    row = c.fetchone()
    return row[0] if row else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", default="DEMO1234")
    ap.add_argument("--password", default="Admin@123")
    ap.add_argument("--reset", action="store_true", help="drop + recreate if the code already exists")
    args = ap.parse_args()
    code = args.code.upper()

    conn = platform_db.get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM companies WHERE code=%s", (code,))
    existing = c.fetchone()
    if existing:
        if not args.reset:
            print(f"[skip] company {code} already exists")
            conn.close()
            sys.exit(0)
        c.execute("SELECT tenant_db_name FROM companies WHERE code=%s", (code,))
        row = c.fetchone()
        if row and row[0]:
            provision.deprovision_tenant(existing[0], row[0])
        c.execute("DELETE FROM company_subscriptions WHERE company_id=%s", (existing[0],))
        c.execute("DELETE FROM companies WHERE code=%s", (code,))
        conn.commit()
        print(f"[reset] dropped previous {code}")

    c.execute(
        """INSERT INTO companies (name, code, gst, pan, address, contact_person, contact_email,
           contact_mobile, status, user_limit, storage_limit_gb)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'inactive',200,50) RETURNING id""",
        ("Demo Pharma Ltd", code, "27AABCD1234F1Z5", "ABCDE1234F", "123 Business Park, Mumbai",
         "Demo Contact", "admin@demopharma.in", "9876500000"),
    )
    cid = c.fetchone()[0]
    conn.commit()
    conn.close()

    tenant_db = provision.provision_tenant(
        cid, "company_admin", args.password,
        admin_email="admin@demopharma.in", admin_full_name="Company Administrator",
    )
    print(f"[ok] provisioned {tenant_db} for {code}")

    conn = platform_db.get_db()
    c = conn.cursor()
    c.execute("UPDATE companies SET tenant_db_name=%s, status='active', provisioned_at=CURRENT_TIMESTAMP WHERE id=%s",
              (tenant_db, cid))
    conn.commit()
    conn.close()
    pools.get_tenant_pool(tenant_db)
    print(f"[ok] company row updated to active")

    t = get_conn(tenant_db)
    cur = t.cursor()

    # brands
    brands = ["NovaLife", "CuraMed", "Vitalis"]
    for b in brands:
        cur.execute("INSERT INTO brands (name, code, description) VALUES (%s,%s,%s)",
                    (b, b[:4].upper(), f"{b} therapeutic range"))

    # campaigns (each with scheme type from the gratification catalog)
    scheme = [("Summer POB 2026", "cashback", True),
              ("Derma Launch Drive", "physical_gift", True),
              ("Cardio Care Scheme", "voucher", True)]
    for name, st, active in scheme:
        cur.execute(
            """INSERT INTO campaigns (name, brand_id, division, start_date, end_date,
               active, status, scheme_type, invoice_verification_required, description)
               VALUES (%s,%s,%s,current_date,current_date+90,%s,'active',%s,TRUE,%s) RETURNING id""",
            (name, _resolve_id(cur, "brands", brands[scheme.index((name, st, active))]),
             "Domestic Sales", active, st, f"Demo {st} scheme"),
        )

    # products per campaign
    cur.execute("SELECT id, name FROM campaigns ORDER BY id")
    campaigns = cur.fetchall()
    product_names = [("Tab Panacea 20mg", "TAB-PNC-20"), ("Syr Relieve 100ml", "SYR-REL-100"),
                     ("Gel DermaCare 50g", "GEL-DRM-50")]
    for i, (cid, cname) in enumerate(campaigns):
        pname, sku = product_names[i % len(product_names)]
        cur.execute(
            """INSERT INTO products (brand_id, sku, name, strength, pack, ptr, mrp,
               min_quantity, min_pob, max_pob, scheme_eligibility) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE) RETURNING id""",
            (_resolve_id(cur, "brands", brands[i % len(brands)]), sku, pname,
             "20 mg", "10x10", 120.0, 145.0, 1, 500.0, 50000.0),
        )
        pid = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO campaign_products (campaign_id, product_id, sort_order) VALUES (%s,%s,0)",
            (cid, pid),
        )

    # chemists
    chemists = [("Sunrise Pharmacy", "Andheri", "MH", "9820000001"),
                ("MedPlus Chemist", "Bandra", "MH", "9820000002"),
                ("Green Cross Medico", "Powai", "MH", "9820000003"),
                ("Apollo Pharmacy", "Colaba", "MH", "9820000004")]
    for name, city, state, mobile in chemists:
        cur.execute(
            "INSERT INTO chemists (name, shop_name, gst, mobile, address, city, district, state, pin, category, status) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active')",
            (name, name, f"27AABC{city[:4].upper()}1234", mobile, f"{city} main road", city, city, state,
             "400000", "retail"),
        )

    # users: MRs reporting up through ASM -> RSM -> NSM -> HO
    cur.execute("SELECT id FROM hierarchy_levels WHERE name='MR'")
    mr_lvl = cur.fetchone()[0]
    cur.execute("SELECT id FROM hierarchy_levels WHERE name='ASM'")
    asm_lvl = cur.fetchone()[0]
    cur.execute("SELECT id FROM roles WHERE name='mr'")
    mr_role = cur.fetchone()[0]
    cur.execute("SELECT id FROM roles WHERE name='asm'")
    asm_role = cur.fetchone()[0]
    cur.execute("SELECT id FROM roles WHERE name='verifier'")
    ver_role = cur.fetchone()[0]
    cur.execute("SELECT id FROM roles WHERE name='finance'")
    fin_role = cur.fetchone()[0]

    cur.execute("SELECT id, full_name FROM users WHERE role_id=%s", (asm_role,))
    asm = cur.fetchone()
    asm_id = asm[0] if asm else None
    if not asm_id:
        cur.execute("INSERT INTO users (username, password, full_name, hierarchy_level_id, role_id, email, mobile) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    ("asm.rahul", hash_pw("Demo@123"), "Rahul Sharma", asm_lvl, asm_role, "rahul@demopharma.in", "9830000001"))
        asm_id = cur.fetchone()[0]

    mrs = [("mr.amit", "Amit Kumar", "9830000002"), ("mr.pooja", "Pooja Patel", "9830000003"),
           ("mr.ravi", "Ravi Verma", "9830000004")]
    for username, full, mobile in mrs:
        cur.execute("INSERT INTO users (username, password, full_name, hierarchy_level_id, role_id, parent_id, email, mobile) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (username, hash_pw("Demo@123"), full, mr_lvl, mr_role, asm_id, f"{username}@demopharma.in", mobile))

    cur.execute("INSERT INTO users (username, password, full_name, role_id, email, mobile) VALUES (%s,%s,%s,%s,%s,%s)",
                ("verifier.kavita", hash_pw("Demo@123"), "Kavita Iyer", ver_role, "kavita@demopharma.in", "9830000005"))
    cur.execute("INSERT INTO users (username, password, full_name, role_id, email, mobile) VALUES (%s,%s,%s,%s,%s,%s)",
                ("finance.sunil", hash_pw("Demo@123"), "Sunil Rao", fin_role, "sunil@demopharma.in", "9830000006"))

    t.commit()
    t.close()

    print()
    print("Demo tenant ready.")
    print(f"  Login URL : http://localhost:8000/login")
    print(f"  Company   : {code}")
    print(f"  company_admin : {args.password}")
    print(f"  mr.amit / asm.rahul / verifier.kavita / finance.sunil : Demo@123")


if __name__ == "__main__":
    main()

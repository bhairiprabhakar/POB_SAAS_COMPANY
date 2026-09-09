"""
Per-company (tenant) database schema + seed data.

Every company database is created from this DDL at provisioning time, so all
tenants share an identical, up-to-date schema. Column notes:
  - No company_id columns anywhere: the database itself IS the tenant boundary.
  - Hierarchy is dynamic: hierarchy_levels is a self-referencing tree; a user
    belongs to one level (reporting position) and one role (permissions).
"""

TENANT_DDL = """
-- ── Dynamic hierarchy ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hierarchy_levels (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    label TEXT,
    rank INTEGER NOT NULL DEFAULT 0,
    parent_level_id INTEGER REFERENCES hierarchy_levels(id),
    active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ── RBAC ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS permissions (
    code TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    module TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS roles (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    is_system BOOLEAN DEFAULT FALSE,
    data_entry BOOLEAN DEFAULT FALSE,
    active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS role_permissions (
    role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    permission_code TEXT NOT NULL REFERENCES permissions(code) ON DELETE CASCADE,
    PRIMARY KEY (role_id, permission_code)
);

-- ── Divisions (created early: users & campaigns link to them) ──────────────
CREATE TABLE IF NOT EXISTS divisions (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    code TEXT,
    description TEXT,
    status TEXT DEFAULT 'active',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ── Users ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password TEXT NOT NULL,
    full_name TEXT NOT NULL,
    email TEXT,
    mobile TEXT,
    employee_id TEXT,
    hierarchy_level_id INTEGER REFERENCES hierarchy_levels(id),
    role_id INTEGER REFERENCES roles(id),
    parent_id INTEGER REFERENCES users(id),
    region TEXT,
    area TEXT,
    territory TEXT,
    state TEXT,
    division TEXT,
    division_id INTEGER REFERENCES divisions(id),
    status TEXT DEFAULT 'active',          -- active | inactive | left
    last_login TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS refresh_tokens (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT UNIQUE NOT NULL,
    scope TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    revoked BOOLEAN DEFAULT FALSE,
    revoked_at TIMESTAMP,
    family_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    -- SHA-256 hash of the raw token, not the raw token itself -- see
    -- saas/security.py issue_password_reset_token()/verify_password_reset_token().
    -- If the DB is ever compromised, stored rows alone can't be used to
    -- reset a password.
    token_hash TEXT UNIQUE NOT NULL,
    expires_at INTEGER NOT NULL,
    used BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ── Masters ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS brands (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    code TEXT,
    description TEXT,
    status TEXT DEFAULT 'active',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS campaigns (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    brand_id INTEGER REFERENCES brands(id),
    division_id INTEGER REFERENCES divisions(id),
    division TEXT,
    start_date DATE,
    end_date DATE,
    active BOOLEAN DEFAULT TRUE,
    status TEXT DEFAULT 'draft',           -- draft | active | completed | paused
    scheme_type TEXT DEFAULT 'others',     -- from gratification_types
    invoice_verification_required BOOLEAN DEFAULT TRUE,
    logo_path TEXT,
    banner_path TEXT,
    description TEXT,
    terms_conditions TEXT,
    created_by INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS products (
    id SERIAL PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    brand_id INTEGER REFERENCES brands(id),
    sku TEXT,
    name TEXT NOT NULL,
    strength TEXT,
    pack TEXT,
    ptr REAL DEFAULT 0,
    pts REAL DEFAULT 0,
    mrp REAL DEFAULT 0,
    min_quantity INTEGER DEFAULT 1,
    min_pob REAL DEFAULT 0,
    max_pob REAL,
    scheme_eligibility BOOLEAN DEFAULT TRUE,
    status TEXT DEFAULT 'active',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS chemists (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    shop_name TEXT,
    gst TEXT,
    dl_number TEXT,
    owner_name TEXT,
    mobile TEXT,
    alternate_mobile TEXT,
    email TEXT,
    address TEXT,
    city TEXT,
    district TEXT,
    state TEXT,
    pin TEXT,
    latitude REAL,
    longitude REAL,
    ocid TEXT,
    doctor_name TEXT,
    category TEXT,
    area TEXT,
    upi_id TEXT,
    division_id INTEGER REFERENCES divisions(id),
    status TEXT DEFAULT 'active',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ── Chemist follow-up visits / stock liquidation tracking ──────────────────
CREATE TABLE IF NOT EXISTS chemist_visits (
    id SERIAL PRIMARY KEY,
    chemist_id INTEGER NOT NULL REFERENCES chemists(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    campaign_id INTEGER REFERENCES campaigns(id),
    visit_date DATE NOT NULL DEFAULT CURRENT_DATE,
    opening_stock REAL DEFAULT 0,
    quantity_sold REAL DEFAULT 0,
    current_stock REAL DEFAULT 0,
    fresh_purchase REAL DEFAULT 0,
    remarks TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ── POB activities ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS pob_activities (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    chemist_id INTEGER NOT NULL REFERENCES chemists(id),
    quantity INTEGER DEFAULT 0,
    ptr REAL DEFAULT 0,
    mrp REAL DEFAULT 0,
    invoice_amount REAL DEFAULT 0,
    pob_amount REAL DEFAULT 0,
    remarks TEXT,
    invoice_path TEXT,
    invoice_original_name TEXT,
    invoice_number TEXT,
    invoice_date TEXT,
    content_hash TEXT,
    submission_group TEXT,
    status TEXT DEFAULT 'pending_verification',
    -- submitted (visit only, no invoice) | pending_verification | verified | rejected | duplicate | needs_review
    current_verification_id INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pob_verifications (
    id SERIAL PRIMARY KEY,
    pob_id INTEGER NOT NULL REFERENCES pob_activities(id) ON DELETE CASCADE,
    verifier_id INTEGER REFERENCES users(id),
    status TEXT DEFAULT 'pending',
    -- pending | approved | rejected | duplicate | needs_review
    reason TEXT,
    matched_fields JSONB DEFAULT '{}'::jsonb,
    duplicate_of INTEGER,
    started_at TIMESTAMP,
    verified_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS verification_history (
    id SERIAL PRIMARY KEY,
    pob_id INTEGER NOT NULL REFERENCES pob_activities(id) ON DELETE CASCADE,
    verifier_id INTEGER,
    action TEXT NOT NULL,
    reason TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ── Gratification ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gratification_types (
    id SERIAL PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT,
    active BOOLEAN DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS gifts (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    image_path TEXT,
    cost REAL DEFAULT 0,
    stock INTEGER DEFAULT 0,
    active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS gratifications (
    id SERIAL PRIMARY KEY,
    pob_id INTEGER NOT NULL REFERENCES pob_activities(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
    type_code TEXT NOT NULL,
    gift_id INTEGER REFERENCES gifts(id),
    scheme_value REAL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'eligible',
    -- physical_gift: eligible -> dispatched -> delivered -> acknowledged -> completed
    -- cashback/upi:  eligible -> approved -> paid -> completed
    -- voucher:       generated -> sent -> redeemed
    upi_id TEXT,
    payment_ref TEXT,
    paid_at TIMESTAMP,
    voucher_code TEXT,
    voucher_status TEXT,
    dispatch_status TEXT,
    delivery_status TEXT,
    photo_path TEXT,
    gps_lat REAL,
    gps_lng REAL,
    acknowledgement TEXT,
    eligible_at TIMESTAMP,
    dispatched_at TIMESTAMP,
    delivered_at TIMESTAMP,
    completed_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS gratification_events (
    id SERIAL PRIMARY KEY,
    gratification_id INTEGER NOT NULL REFERENCES gratifications(id) ON DELETE CASCADE,
    event TEXT NOT NULL,
    detail TEXT,
    actor_id INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ── Notifications ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS notification_templates (
    id SERIAL PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    channel TEXT NOT NULL DEFAULT 'inapp',  -- inapp | email | whatsapp | sms | push
    subject TEXT,
    body TEXT NOT NULL,
    variables TEXT[] DEFAULT '{}',
    active BOOLEAN DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS notifications (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    channel TEXT DEFAULT 'inapp',
    type TEXT,
    title TEXT,
    message TEXT,
    reference_type TEXT,
    reference_id INTEGER,
    is_read BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ── Reports / audit ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS report_schedules (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    report_type TEXT NOT NULL,
    format TEXT DEFAULT 'excel',
    cron TEXT,
    recipients TEXT DEFAULT '',
    enabled BOOLEAN DEFAULT TRUE,
    last_run TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id SERIAL PRIMARY KEY,
    user_id INTEGER,
    actor TEXT,
    action TEXT NOT NULL,
    entity_type TEXT,
    entity_id INTEGER,
    detail JSONB DEFAULT '{}'::jsonb,
    ip TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

# ── Seed data ───────────────────────────────────────────────────────────────

DEFAULT_HIERARCHY = [
    # (rank, name, label, parent_name)
    (7, "HO",  "Head Office",            None),
    (6, "NSM", "National Sales Manager", "HO"),
    (5, "ZSM", "Zonal Sales Manager",    "NSM"),
    (4, "SM",  "State Manager",          "ZSM"),
    (3, "RSM", "Regional Sales Manager", "SM"),
    (2, "ASM", "Area Sales Manager",     "RSM"),
    (1, "MR",  "Medical Representative", "ASM"),
]

# module -> [(code, label)]
PERMISSION_CATALOG = [
    ("company",      [("company.view", "View company settings"), ("company.manage", "Manage company settings")]),
    ("hierarchy",    [("hierarchy.view", "View hierarchy"), ("hierarchy.manage", "Manage hierarchy levels")]),
    ("user",         [("user.view", "View users"), ("user.manage", "Create/edit/delete users")]),
    ("brand",        [("brand.view", "View brands"), ("brand.manage", "Create/edit/delete brands")]),
    ("campaign",     [("campaign.view", "View campaigns"), ("campaign.manage", "Create/edit/delete campaigns")]),
    ("product",      [("product.view", "View products")]),
    ("chemist",      [("chemist.view", "View chemists"), ("chemist.manage", "Manage chemists")]),
    ("pob",          [("pob.submit", "Submit POB activities"), ("pob.view", "View POB activities"), ("pob.manage", "Manage POB activities")]),
    ("verification", [("verification.view", "View verification queue"), ("verification.approve", "Approve POBs"),
                      ("verification.reject", "Reject POBs"), ("verification.manage", "Manage verification")]),
    ("gratification",[("gratification.view", "View gratifications"), ("gratification.manage", "Manage gratifications"),
                      ("gratification.dispatch", "Dispatch physical gifts"), ("gratification.approve", "Approve cashback"),
                      ("gratification.pay", "Mark payments paid")]),
    ("report",       [("report.view", "View reports"), ("report.export", "Export reports"), ("report.schedule", "Schedule reports")]),
    ("notification", [("notification.view", "View notifications"), ("notification.manage", "Manage notifications")]),
    ("dashboard",    [("dashboard.view", "View dashboards")]),
    ("audit",        [("audit.view", "View audit logs")]),
    ("settings",     [("settings.manage", "Manage company settings")]),
    ("inventory",    [("inventory.view", "View inventory"), ("inventory.manage", "Manage warehouses & gifts"),
                      ("inventory.stock", "Adjust / transfer stock")]),
    ("payout",       [("payout.view", "View payout batches"), ("payout.run", "Run payout batches"),
                      ("payout.reconcile", "Reconcile payouts")]),
    ("security",     [("mfa.manage", "Manage own two-factor auth"),
                      ("apikey.view", "View API keys"), ("apikey.manage", "Create / revoke API keys"),
                      ("webhook.view", "View webhooks & deliveries"), ("webhook.manage", "Manage webhook endpoints"),
                      ("job.view", "View background jobs"), ("job.run", "Trigger / run background jobs")]),
    ("visit",        [("visit.view", "View chemist visits & follow-ups"),
                      ("visit.manage", "Record / edit chemist visits")]),
]

ALL_PERMISSIONS = [p for _, perms in PERMISSION_CATALOG for p in perms]

# role name -> list of permission codes
DEFAULT_ROLES = {
    "division_admin": [p[0] for p in ALL_PERMISSIONS],
    "campaignos_admin": [p[0] for p in ALL_PERMISSIONS],
    "ho":  ["dashboard.view", "user.view", "hierarchy.view", "campaign.view", "product.view",
            "brand.view", "chemist.view", "chemist.manage", "pob.view", "verification.view",
            "verification.approve", "verification.reject", "report.view", "report.export",
            "notification.view"],
    "nsm": ["dashboard.view", "user.view", "hierarchy.view", "campaign.view", "product.view",
            "brand.view", "chemist.view", "chemist.manage", "pob.view", "report.view",
            "report.export", "notification.view"],
    "zsm": ["dashboard.view", "user.view", "hierarchy.view", "campaign.view", "product.view",
            "brand.view", "chemist.view", "chemist.manage", "pob.view", "report.view",
            "report.export", "notification.view"],
    "sm":  ["dashboard.view", "user.view", "hierarchy.view", "campaign.view", "product.view",
            "brand.view", "chemist.view", "chemist.manage", "pob.view", "report.view",
            "report.export", "notification.view"],
    "rsm": ["dashboard.view", "hierarchy.view", "campaign.view", "product.view", "brand.view",
            "chemist.view", "chemist.manage", "pob.view", "report.view", "report.export",
            "notification.view"],
    "asm": ["dashboard.view", "hierarchy.view", "campaign.view", "product.view",
            "brand.view", "chemist.view", "chemist.manage", "pob.view", "verification.view",
            "verification.approve", "report.view", "notification.view"],
    "mr":  ["dashboard.view", "pob.submit", "campaign.view", "product.view", "chemist.view",
            "chemist.manage", "notification.view", "visit.view", "visit.manage"],
    "psr": ["dashboard.view", "pob.submit", "campaign.view", "product.view", "chemist.view",
            "chemist.manage", "notification.view", "visit.view", "visit.manage"],
    "verifier": ["dashboard.view", "verification.view", "verification.approve", "verification.reject",
                 "report.view", "notification.view", "pob.view"],
    "auditor": ["dashboard.view", "report.view", "report.export", "audit.view", "verification.view",
                "notification.view", "apikey.view", "webhook.view"],
    "finance": ["dashboard.view", "gratification.view", "gratification.approve", "gratification.pay",
                "payout.view", "payout.run", "payout.reconcile", "inventory.view",
                "report.view", "report.export", "notification.view"],
    "distributor": ["dashboard.view", "gratification.view", "notification.view"],
}

# Roles whose users submit / upload field data (see nav bar visibility).
# Super admin can flip roles.data_entry per role from the Roles page.
DATA_ENTRY_ROLES = {"mr", "psr"}

DEFAULT_GRATIFICATION_TYPES = [
    ("cashback", "Cashback"),
    ("upi", "UPI"),
    ("voucher", "Voucher"),
    ("gift", "Gift"),
    ("coupon", "Coupon"),
    ("points", "Points"),
    ("physical_gift", "Physical Gift"),
    ("others", "Others"),
]


def seed_tenant(conn):
    """Insert baseline hierarchy, permissions, roles and gratification types
    into a freshly-provisioned tenant database."""
    c = conn.cursor()

    # permissions
    for module, perms in PERMISSION_CATALOG:
        for code, label in perms:
            c.execute(
                "INSERT INTO permissions (code, label, module) VALUES (%s,%s,%s) ON CONFLICT (code) DO NOTHING",
                (code, label, module),
            )

    # default hierarchy (idempotent)
    inserted = {}
    for rank, name, label, parent in DEFAULT_HIERARCHY:
        c.execute(
            "INSERT INTO hierarchy_levels (name, label, rank, parent_level_id) "
            "VALUES (%s, %s, %s, NULL) ON CONFLICT (name) DO NOTHING RETURNING id",
            (name, label, rank),
        )
        row = c.fetchone()
        if row:
            inserted[name] = row[0]
        else:
            c.execute("SELECT id FROM hierarchy_levels WHERE name=%s", (name,))
            inserted[name] = c.fetchone()[0]
    for _, name, _, parent in DEFAULT_HIERARCHY:
        if parent:
            c.execute(
                "UPDATE hierarchy_levels SET parent_level_id=%s WHERE name=%s",
                (inserted[parent], name),
            )

    # default roles
    role_ids = {}
    for name, perms in DEFAULT_ROLES.items():
        c.execute(
            "INSERT INTO roles (name, description, is_system, data_entry) VALUES (%s, %s, TRUE, %s) "
            "ON CONFLICT (name) DO NOTHING RETURNING id",
            (name, f"System role: {name}", name in DATA_ENTRY_ROLES),
        )
        row = c.fetchone()
        if row:
            role_ids[name] = row[0]
        else:
            c.execute("SELECT id FROM roles WHERE name=%s", (name,))
            role_ids[name] = c.fetchone()[0]
    for name, perms in DEFAULT_ROLES.items():
        for perm in perms:
            c.execute(
                "INSERT INTO role_permissions (role_id, permission_code) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (role_ids[name], perm),
            )

    # gratification types
    for code, name in DEFAULT_GRATIFICATION_TYPES:
        c.execute(
            "INSERT INTO gratification_types (code, name) VALUES (%s,%s) ON CONFLICT (code) DO NOTHING",
            (code, name),
        )

    # default notification templates
    templates = [
        ("pob.submitted", "inapp", "POB Submitted", "Your POB for campaign {campaign} ({invoice_amount}) has been submitted for verification.", ["campaign", "invoice_amount"]),
        ("pob.approved", "inapp", "POB Approved", "Your POB {pob_id} has been approved. You are eligible for umbrella distribution of the {campaign} scheme. Gratification is being processed.", ["pob_id", "campaign"]),
        ("pob.rejected", "inapp", "POB Rejected", "Your POB {pob_id} was rejected: {reason}", ["pob_id", "reason"]),
        ("gift.ready", "inapp", "Gift Ready", "Your gift {gift} is ready for dispatch.", ["gift"]),
        ("gift.delivered", "inapp", "Gift Delivered", "Your gift {gift} has been delivered.", ["gift"]),
        ("cashback.paid", "inapp", "Cashback Paid", "Cashback of {amount} has been paid to your UPI {upi}.", ["amount", "upi"]),
        ("voucher.issued", "inapp", "Voucher Issued", "Voucher {code} worth {amount} has been issued to you.", ["code", "amount"]),
        ("followup.due", "inapp", "Follow-up Due", "You have {count} POBs pending verification.", ["count"]),
        ("visit.due", "inapp", "Follow-up Due", "You have {count} chemist(s) due for 15-day follow-up visit and stock liquidation check.", ["count"]),
        ("pob.proof.due", "inapp", "Invoice Proof Due",
         "The campaign {campaign} has ended. {count} submitted POB(s) still have no invoice proof — please upload the invoice so the POBs can be verified.", ["campaign", "count"]),
    ]
    for code, channel, subject, body, variables in templates:
        c.execute(
            """INSERT INTO notification_templates (code, channel, subject, body, variables)
               VALUES (%s,%s,%s,%s,%s) ON CONFLICT (code) DO NOTHING""",
            (code, channel, subject, body, variables),
        )

    conn.commit()


def ensure_role_id(conn, role_name: str) -> int | None:
    c = conn.cursor()
    c.execute("SELECT id FROM roles WHERE name=%s", (role_name,))
    row = c.fetchone()
    return row[0] if row else None


# ─────────────────────────────────────────────────────────────────────────────
# Enterprise patches (applied to ALL tenants via migrations -- idempotent).
# New tenants run this right after TENANT_DDL at provisioning.
# ─────────────────────────────────────────────────────────────────────────────

TENANT_PATCHES = r"""
-- ── Security hardening (refresh-token reuse detection + hashed reset tokens) ─
-- family_id groups every rotation of one refresh token lineage so reuse of
-- an already-rotated token can be detected and the whole family revoked.
ALTER TABLE refresh_tokens ADD COLUMN IF NOT EXISTS family_id TEXT;

-- Existing password_reset_tokens rows (if any) stored the raw token in a
-- column named `token`. Any outstanding tokens are invalidated by this
-- migration (they can no longer be looked up under the old plaintext
-- scheme) -- callers must request a fresh reset link, which is the correct
-- outcome after a hashing-scheme change.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='password_reset_tokens' AND column_name='token'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='password_reset_tokens' AND column_name='token_hash'
    ) THEN
        ALTER TABLE password_reset_tokens RENAME COLUMN token TO token_hash;
        UPDATE password_reset_tokens SET used=TRUE;  -- invalidate: raw values, not hashes
    END IF;
END $$;

-- ── Data-entry roles: field (MR/PSR) submit & upload; managers review ───────
-- Added first so later INSERTs referencing the column succeed on old tenants.
ALTER TABLE roles ADD COLUMN IF NOT EXISTS data_entry BOOLEAN DEFAULT FALSE;

-- ── Division-based administration (2.6.4) ───────────────────────────────────
-- Every company runs on divisions; the company-wide admin role becomes a
-- division admin bound to one division via users.division_id. Added early so
-- later INSERTs referencing these columns succeed on old tenants.
CREATE TABLE IF NOT EXISTS divisions (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    code TEXT,
    description TEXT,
    status TEXT DEFAULT 'active',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
ALTER TABLE users ADD COLUMN IF NOT EXISTS division_id INTEGER REFERENCES divisions(id);
ALTER TABLE chemists ADD COLUMN IF NOT EXISTS division_id INTEGER REFERENCES divisions(id);
-- created_by is in the CREATE TABLE but had no patch, so tenants provisioned
-- before it was added never received it.
ALTER TABLE chemists ADD COLUMN IF NOT EXISTS created_by INTEGER;

-- Division -> brands -> campaigns. Brands were company-wide; they now belong to
-- a division so the campaign builder can offer only that division's brands.
ALTER TABLE brands ADD COLUMN IF NOT EXISTS division_id INTEGER REFERENCES divisions(id);
CREATE INDEX IF NOT EXISTS idx_brands_division ON brands (division_id);

-- Stable URL segment for the per-division login link, set by the super admin.
-- Kept separate from `name` so renaming a division never breaks a printed link.
ALTER TABLE divisions ADD COLUMN IF NOT EXISTS slug TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_divisions_slug ON divisions (lower(slug)) WHERE slug IS NOT NULL;

-- Rename the legacy company-wide admin role (same role_id -> perms preserved).
UPDATE roles SET name='division_admin'
WHERE name='company_admin' AND NOT EXISTS (SELECT 1 FROM roles WHERE name='division_admin');
INSERT INTO roles (name, description, is_system, data_entry) VALUES
    ('division_admin', 'System role: Division Admin (all-company or single division)', TRUE, FALSE)
ON CONFLICT (name) DO NOTHING;

-- ── Phase 1: per-tenant configuration ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS company_settings (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_by INTEGER,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ── Phase 2: workflow definitions (configurable approval chains) ───────────
CREATE TABLE IF NOT EXISTS workflow_definitions (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    steps JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- steps: [{"order":1,"action":"approve","role":"asm","notify":true}, ...]
    active BOOLEAN DEFAULT TRUE,
    created_by INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ── Phase 2: gratification rule engine ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS gratification_rules (
    id SERIAL PRIMARY KEY,
    campaign_id INTEGER REFERENCES campaigns(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    priority INTEGER DEFAULT 0,
    conditions JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- conditions: [{"field":"invoice_amount","op":">=","value":5000}, ...]
    -- supported fields: invoice_amount, quantity, ptr, mrp, pob_amount, product_count
    -- supported ops: >=, >, <=, <, ==, contains, in
    then_action TEXT NOT NULL DEFAULT 'gift',   -- gift|cashback|voucher|points|coupon
    value REAL DEFAULT 0,
    gift_id INTEGER REFERENCES gifts(id),
    active BOOLEAN DEFAULT TRUE,
    created_by INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ── Phase 2: campaign builder config columns ───────────────────────────────
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS upload_roles TEXT DEFAULT '';
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS approval_workflow_id INTEGER;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS payout_cycle TEXT DEFAULT 'instant';  -- instant|weekly|monthly
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS payout_weekday INTEGER;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS payout_month_day INTEGER;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS auto_verify BOOLEAN DEFAULT FALSE;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS auto_verify_confidence REAL DEFAULT 0.95;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS pob_required BOOLEAN DEFAULT TRUE;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS notification_rules JSONB DEFAULT '{}'::jsonb;

-- ── Invoice-proof acceptance: campaign period + flexible grace ──────────────
-- period_type: none | monthly | quarterly (invoice copies accepted within the
-- campaign window; monthly/quarterly marks the invoicing cadence). Invoice
-- proof is rejected when the invoice date falls outside
-- [start_date - pre_grace_days, end_date + grace_months months + grace_days].
-- pre_grace_days covers pre-launch invoices for new campaigns; grace_months +
-- grace_days extends the acceptance window after the campaign ends (the
-- months selector the campaign owner picks when configuring dates).
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS period_type TEXT DEFAULT 'none';
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS grace_days INTEGER DEFAULT 15;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS grace_months INTEGER DEFAULT 0;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS pre_grace_days INTEGER DEFAULT 0;

-- Multi-brand support: a campaign may target one brand (brand_id) or several.
-- brand_ids holds the full set as a comma-separated list; brand_id stays the
-- primary brand so existing joins/reports keep working.
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS brand_ids TEXT DEFAULT '';

-- Normalized invoice number (case/punctuation-insensitive) so duplicate
-- invoice numbers for the same chemist can be rejected reliably.
ALTER TABLE pob_activities ADD COLUMN IF NOT EXISTS invoice_number_norm TEXT;
UPDATE pob_activities
   SET invoice_number_norm = upper(regexp_replace(coalesce(invoice_number,''), '[^A-Za-z0-9]', '', 'g'))
 WHERE invoice_number IS NOT NULL AND invoice_number <> '';

-- When the invoice proof was attached (single-shot /pob/submit or the later
-- /pob/invoice-proof upload). NULL until proof arrives. proof_lag_days is
-- computed as this timestamp minus pob_activities.created_at.
ALTER TABLE pob_activities ADD COLUMN IF NOT EXISTS proof_submitted_at TIMESTAMP;

-- ── Phase 2/5: product master enrichment ───────────────────────────────────
ALTER TABLE products ADD COLUMN IF NOT EXISTS category TEXT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS gst REAL DEFAULT 0;
ALTER TABLE products ADD COLUMN IF NOT EXISTS division TEXT;

-- ── Phase 3: inventory (warehouses / gift stock) ───────────────────────────
CREATE TABLE IF NOT EXISTS warehouses (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    code TEXT,
    location TEXT,
    contact TEXT,
    active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
ALTER TABLE gifts ADD COLUMN IF NOT EXISTS sku TEXT;
ALTER TABLE gifts ADD COLUMN IF NOT EXISTS category TEXT;
CREATE TABLE IF NOT EXISTS gift_stock (
    id SERIAL PRIMARY KEY,
    gift_id INTEGER NOT NULL REFERENCES gifts(id) ON DELETE CASCADE,
    warehouse_id INTEGER REFERENCES warehouses(id) ON DELETE CASCADE,
    quantity INTEGER DEFAULT 0,
    reserved INTEGER DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (gift_id, warehouse_id)
);
CREATE TABLE IF NOT EXISTS gift_stock_movements (
    id SERIAL PRIMARY KEY,
    gift_id INTEGER NOT NULL REFERENCES gifts(id) ON DELETE CASCADE,
    warehouse_id INTEGER,
    movement TEXT NOT NULL,          -- inbound | allocation | dispatch | return | adjustment
    quantity INTEGER DEFAULT 0,
    reference_type TEXT,
    reference_id INTEGER,
    note TEXT,
    created_by INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ── Phase 3: payout batches (weekly/monthly/instant) + reconciliation ──────
ALTER TABLE gratifications ADD COLUMN IF NOT EXISTS warehouse_id INTEGER;
ALTER TABLE gratifications ADD COLUMN IF NOT EXISTS payout_cycle TEXT;
ALTER TABLE gratifications ADD COLUMN IF NOT EXISTS payout_batch_date DATE;
ALTER TABLE gratifications ADD COLUMN IF NOT EXISTS reconciled BOOLEAN DEFAULT FALSE;
ALTER TABLE gratifications ADD COLUMN IF NOT EXISTS reconciliation_ref TEXT;
CREATE TABLE IF NOT EXISTS payout_batches (
    id SERIAL PRIMARY KEY,
    cycle TEXT NOT NULL,             -- weekly|monthly|instant
    period_start DATE,
    period_end DATE,
    total_amount REAL DEFAULT 0,
    status TEXT DEFAULT 'open',      -- open|processing|paid|reconciled
    payment_ref TEXT,
    created_by INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS payout_batch_items (
    id SERIAL PRIMARY KEY,
    batch_id INTEGER NOT NULL REFERENCES payout_batches(id) ON DELETE CASCADE,
    gratification_id INTEGER NOT NULL REFERENCES gratifications(id) ON DELETE CASCADE,
    amount REAL DEFAULT 0,
    status TEXT DEFAULT 'pending'    -- pending|paid|failed|reconciled
);

-- ── Phase 4: OCR / AI extraction cache ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS ocr_extractions (
    id SERIAL PRIMARY KEY,
    pob_id INTEGER REFERENCES pob_activities(id) ON DELETE CASCADE,
    engine TEXT DEFAULT 'gemini',
    raw JSONB DEFAULT '{}'::jsonb,
    fields JSONB DEFAULT '{}'::jsonb,
    confidence REAL DEFAULT 0,
    auto_approved BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
ALTER TABLE pob_activities ADD COLUMN IF NOT EXISTS auto_verified BOOLEAN DEFAULT FALSE;
ALTER TABLE pob_activities ADD COLUMN IF NOT EXISTS confidence REAL DEFAULT 0;

-- ── Phase 8: OCR usage ledger (cost attribution) ───────────────────────────
-- One row per extraction attempt (Gemini calls are token-billed). The cost
-- statement hides `cost`/`currency` from company admins; the super admin
-- aggregates across tenants. Row is written+committed even when the POB
-- request later rolls back, so the ledger reflects real provider spend.
CREATE TABLE IF NOT EXISTS ocr_usage (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    engine TEXT NOT NULL DEFAULT 'gemini',
    cost REAL NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'USD',
    status TEXT NOT NULL DEFAULT 'success',   -- success | error
    invoice_number TEXT,
    filename TEXT,
    model_name TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_ocr_usage_created_at ON ocr_usage (created_at);
CREATE INDEX IF NOT EXISTS idx_ocr_usage_user ON ocr_usage (user_id);

-- ── Phase 5: audit enrichment ──────────────────────────────────────────────
ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS user_agent TEXT;
ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS gps_lat REAL;
ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS gps_lng REAL;
ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS photo_path TEXT;
ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS before_state JSONB DEFAULT '{}'::jsonb;
ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS after_state JSONB DEFAULT '{}'::jsonb;

-- ── Phase 6: security (MFA) + API platform (keys / webhooks) ───────────────
ALTER TABLE users ADD COLUMN IF NOT EXISTS mfa_secret TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS mfa_enabled BOOLEAN DEFAULT FALSE;
CREATE TABLE IF NOT EXISTS api_keys (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    key_hash TEXT NOT NULL,
    scope TEXT DEFAULT 'read',
    scopes TEXT[] DEFAULT '{}',
    active BOOLEAN DEFAULT TRUE,
    created_by INTEGER,
    last_used_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS webhooks (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    events TEXT[] DEFAULT '{}',
    secret TEXT,
    active BOOLEAN DEFAULT TRUE,
    created_by INTEGER,
    last_delivery_at TIMESTAMP,
    last_delivery_status TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id SERIAL PRIMARY KEY,
    webhook_id INTEGER NOT NULL REFERENCES webhooks(id) ON DELETE CASCADE,
    event TEXT NOT NULL,
    payload JSONB DEFAULT '{}'::jsonb,
    status TEXT DEFAULT 'pending',   -- pending|delivered|failed
    attempts INTEGER DEFAULT 0,
    http_status INTEGER,
    response TEXT,
    error TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    delivered_at TIMESTAMP
);
-- existing tenants may already have the Phase 6 tables from an earlier patch;
-- ensure the newer columns exist everywhere
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS scopes TEXT[] DEFAULT '{}';
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS created_by INTEGER;
ALTER TABLE webhooks ADD COLUMN IF NOT EXISTS created_by INTEGER;
ALTER TABLE webhooks ADD COLUMN IF NOT EXISTS last_delivery_at TIMESTAMP;
ALTER TABLE webhooks ADD COLUMN IF NOT EXISTS last_delivery_status TEXT;

-- ── Phase 7: background jobs ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS job_queue (
    id SERIAL PRIMARY KEY,
    job_type TEXT NOT NULL,
    payload JSONB DEFAULT '{}'::jsonb,
    status TEXT DEFAULT 'queued',    -- queued|running|done|failed
    attempts INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 5,
    error TEXT,
    run_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    finished_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
ALTER TABLE job_queue ADD COLUMN IF NOT EXISTS max_attempts INTEGER DEFAULT 5;

-- ── default settings row ───────────────────────────────────────────────────
INSERT INTO company_settings (key, value)
VALUES ('general', '{}'::jsonb)
ON CONFLICT (key) DO NOTHING;

-- ── Phase 2b: dynamic workflow approval tracking ───────────────────────────
CREATE TABLE IF NOT EXISTS pob_approvals (
    id SERIAL PRIMARY KEY,
    pob_id INTEGER NOT NULL REFERENCES pob_activities(id) ON DELETE CASCADE,
    step INTEGER NOT NULL DEFAULT 1,
    step_name TEXT,
    role_name TEXT,
    approver_id INTEGER,
    status TEXT DEFAULT 'pending',   -- pending|approved|rejected|skipped
    comment TEXT,
    decided_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
ALTER TABLE pob_activities ADD COLUMN IF NOT EXISTS workflow_id INTEGER;
ALTER TABLE pob_activities ADD COLUMN IF NOT EXISTS workflow_step INTEGER DEFAULT 0;
ALTER TABLE pob_activities ADD COLUMN IF NOT EXISTS workflow_done BOOLEAN DEFAULT FALSE;

-- ── Phase 3: permissions + role wiring (idempotent) ────────────────────────
INSERT INTO permissions (code, label, module) VALUES
    ('inventory.view',  'View inventory', 'inventory'),
    ('inventory.manage', 'Manage warehouses & gifts', 'inventory'),
    ('inventory.stock', 'Adjust / transfer stock', 'inventory'),
    ('payout.view', 'View payout batches', 'payout'),
    ('payout.run', 'Run payout batches', 'payout'),
    ('payout.reconcile', 'Reconcile payouts', 'payout')
ON CONFLICT (code) DO NOTHING;

INSERT INTO role_permissions (role_id, permission_code)
SELECT r.id, p.code FROM roles r, permissions p
WHERE r.name='division_admin'
  AND p.module IN ('inventory','payout')
ON CONFLICT DO NOTHING;

INSERT INTO role_permissions (role_id, permission_code)
SELECT r.id, p.code FROM roles r, permissions p
WHERE r.name='finance'
  AND p.code IN ('payout.view','payout.run','payout.reconcile','inventory.view')
ON CONFLICT DO NOTHING;

INSERT INTO role_permissions (role_id, permission_code)
SELECT r.id, p.code FROM roles r, permissions p
WHERE r.name='asm'
  AND p.code IN ('verification.view','verification.approve')
ON CONFLICT DO NOTHING;

-- ── Phase 6: security permissions + wiring (idempotent) ────────────────────
INSERT INTO permissions (code, label, module) VALUES
    ('mfa.manage', 'Manage own two-factor auth', 'security'),
    ('apikey.view', 'View API keys', 'security'),
    ('apikey.manage', 'Create / revoke API keys', 'security'),
    ('webhook.view', 'View webhooks & deliveries', 'security'),
    ('webhook.manage', 'Manage webhook endpoints', 'security')
ON CONFLICT (code) DO NOTHING;

INSERT INTO role_permissions (role_id, permission_code)
SELECT r.id, p.code FROM roles r, permissions p
WHERE r.name='division_admin' AND p.module='security'
ON CONFLICT DO NOTHING;

INSERT INTO role_permissions (role_id, permission_code)
SELECT r.id, p.code FROM roles r, permissions p
WHERE r.name='auditor'
  AND p.code IN ('apikey.view','webhook.view')
ON CONFLICT DO NOTHING;

-- ── Phase 7: job visibility for admins + scheduler user ────────────────────
INSERT INTO permissions (code, label, module) VALUES
    ('job.view', 'View background jobs', 'security'),
    ('job.run', 'Trigger / run background jobs', 'security')
ON CONFLICT (code) DO NOTHING;

INSERT INTO role_permissions (role_id, permission_code)
SELECT r.id, p.code FROM roles r, permissions p
WHERE r.name='division_admin' AND p.code IN ('job.view','job.run')
ON CONFLICT DO NOTHING;

-- ── Gap closure 2.5.0: SM level, PSR/SM/CampaignOS-Admin roles, visits ──────
ALTER TABLE chemists ADD COLUMN IF NOT EXISTS area TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS state TEXT;

CREATE TABLE IF NOT EXISTS chemist_visits (
    id SERIAL PRIMARY KEY,
    chemist_id INTEGER NOT NULL REFERENCES chemists(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    campaign_id INTEGER REFERENCES campaigns(id),
    visit_date DATE NOT NULL DEFAULT CURRENT_DATE,
    opening_stock REAL DEFAULT 0,
    quantity_sold REAL DEFAULT 0,
    current_stock REAL DEFAULT 0,
    fresh_purchase REAL DEFAULT 0,
    remarks TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- hierarchy: insert SM (State Manager) + re-rank so PSR→ASM→RSM→SM→ZSM→NSM→HO
INSERT INTO hierarchy_levels (name, label, rank)
SELECT 'SM', 'State Manager', 4
WHERE NOT EXISTS (SELECT 1 FROM hierarchy_levels WHERE name='SM');
UPDATE hierarchy_levels SET rank=7 WHERE name='HO';
UPDATE hierarchy_levels SET rank=6 WHERE name='NSM';
UPDATE hierarchy_levels SET rank=5 WHERE name='ZSM';
UPDATE hierarchy_levels SET rank=4 WHERE name='SM';
UPDATE hierarchy_levels SET rank=3 WHERE name='RSM';
UPDATE hierarchy_levels SET rank=2 WHERE name='ASM';
UPDATE hierarchy_levels SET rank=1 WHERE name='MR';
UPDATE hierarchy_levels SET parent_level_id=(SELECT id FROM hierarchy_levels WHERE name='ZSM') WHERE name='SM';
UPDATE hierarchy_levels SET parent_level_id=(SELECT id FROM hierarchy_levels WHERE name='SM') WHERE name='RSM';

-- visit permissions
INSERT INTO permissions (code, label, module) VALUES
    ('visit.view', 'View chemist visits & follow-ups', 'visit'),
    ('visit.manage', 'Record / edit chemist visits', 'visit')
ON CONFLICT (code) DO NOTHING;

INSERT INTO role_permissions (role_id, permission_code)
SELECT r.id, p.code FROM roles r, permissions p
WHERE r.name IN ('mr','psr','division_admin','campaignos_admin')
  AND p.code IN ('visit.view','visit.manage')
ON CONFLICT DO NOTHING;

INSERT INTO role_permissions (role_id, permission_code)
SELECT r.id, p.code FROM roles r, permissions p
WHERE r.name IN ('asm','rsm','sm','zsm','nsm','ho') AND p.code='visit.view'
ON CONFLICT DO NOTHING;

-- PSR / SM / CampaignOS-Admin roles
INSERT INTO roles (name, description, is_system, data_entry) VALUES
    ('psr', 'System role: PSR (Medical Representative)', TRUE, TRUE),
    ('sm', 'System role: SM (State Manager)', TRUE, FALSE),
    ('campaignos_admin', 'System role: CampaignOS Admin (full operations)', TRUE, FALSE)
ON CONFLICT (name) DO NOTHING;

INSERT INTO role_permissions (role_id, permission_code)
SELECT r.id, p.code FROM roles r, permissions p
WHERE r.name IN ('mr','psr') AND p.code IN
    ('dashboard.view','pob.submit','chemist.view','chemist.manage','notification.view','visit.view','visit.manage')
ON CONFLICT DO NOTHING;

INSERT INTO role_permissions (role_id, permission_code)
SELECT r.id, p.code FROM roles r, permissions p
WHERE r.name='sm' AND p.code IN
    ('dashboard.view','user.view','hierarchy.view','campaign.view','product.view','brand.view',
     'chemist.view','pob.view','report.view','report.export','notification.view')
ON CONFLICT DO NOTHING;

INSERT INTO role_permissions (role_id, permission_code)
SELECT r.id, p.code FROM roles r, permissions p
WHERE r.name='campaignos_admin'
ON CONFLICT DO NOTHING;

-- HO approves / rejects invoices (SOW: HO + CampaignOS Admin approve/reject)
INSERT INTO role_permissions (role_id, permission_code)
SELECT r.id, p.code FROM roles r, permissions p
WHERE r.name='ho' AND p.code IN ('verification.view','verification.approve','verification.reject')
ON CONFLICT DO NOTHING;

-- approval message now confirms umbrella distribution eligibility
INSERT INTO notification_templates (code, channel, subject, body, variables)
VALUES ('pob.approved', 'inapp', 'POB Approved',
        'Your POB {pob_id} has been approved. You are eligible for umbrella distribution of the {campaign} scheme. Gratification is being processed.',
        ARRAY['pob_id','campaign'])
ON CONFLICT (code) DO UPDATE SET body=EXCLUDED.body, variables=EXCLUDED.variables;

INSERT INTO notification_templates (code, channel, subject, body, variables)
VALUES ('visit.due', 'inapp', 'Follow-up Due',
        'You have {count} chemist(s) due for 15-day follow-up visit and stock liquidation check.',
        ARRAY['count'])
ON CONFLICT (code) DO NOTHING;

INSERT INTO notification_templates (code, channel, subject, body, variables)
VALUES ('pob.proof.due', 'inapp', 'Invoice Proof Due',
        'The campaign {campaign} has ended. {count} submitted POB(s) still have no invoice proof — please upload the invoice so the POBs can be verified.',
        ARRAY['campaign','count'])
ON CONFLICT (code) DO NOTHING;

-- ── Chemist registration: UPI / gratification number for cashback payouts ──
ALTER TABLE chemists ADD COLUMN IF NOT EXISTS upi_id TEXT;

-- ── Chemist registration page for field & manager roles ────────────────────
INSERT INTO role_permissions (role_id, permission_code)
SELECT r.id, p.code FROM roles r, permissions p
WHERE r.name IN ('ho','nsm','zsm','sm','rsm','asm') AND p.code='chemist.manage'
ON CONFLICT DO NOTHING;

-- ── Product pricing: PTS (Price to Stockist) alongside PTR ────────────────
ALTER TABLE products ADD COLUMN IF NOT EXISTS pts REAL DEFAULT 0;

-- ── POB submitters (MR/PSR) need to see active campaigns & products ────────
INSERT INTO role_permissions (role_id, permission_code)
SELECT r.id, p.code FROM roles r, permissions p
WHERE r.name IN ('mr','psr') AND p.code IN ('campaign.view','product.view')
ON CONFLICT DO NOTHING;

-- ── Two-phase POB: visit submission (no invoice) then invoice proof ────────
ALTER TABLE pob_activities ADD COLUMN IF NOT EXISTS submission_group TEXT;

-- ── Divisions: campaign linkage + legacy division text ─────────────────────
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS division_id INTEGER;
ALTER TABLE users ADD COLUMN IF NOT EXISTS division TEXT;

-- Link legacy division text on users to the divisions table.
UPDATE users u SET division_id=d.id
FROM divisions d
WHERE lower(d.name)=lower(u.division) AND u.division_id IS NULL;

-- ── Mark field roles as data-entry on existing tenants ──────────────────────
UPDATE roles SET data_entry=TRUE WHERE name IN ('mr','psr');

-- ── Phase 8: OCR usage ledger + statement permission (2.8.0) ────────────────
CREATE TABLE IF NOT EXISTS ocr_usage (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    engine TEXT NOT NULL DEFAULT 'gemini',
    cost REAL NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'USD',
    status TEXT NOT NULL DEFAULT 'success',   -- success | error
    invoice_number TEXT,
    filename TEXT,
    model_name TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_ocr_usage_created_at ON ocr_usage (created_at);
CREATE INDEX IF NOT EXISTS idx_ocr_usage_user ON ocr_usage (user_id);

-- OCR usage / Gemini cost statements are super-admin-managed (platform
-- console). Tenant roles no longer hold the statement permission.
DELETE FROM role_permissions
WHERE permission_code='ocr.usage.view'
  AND role_id IN (SELECT id FROM roles WHERE name IN ('division_admin','campaignos_admin'));
DELETE FROM permissions WHERE code='ocr.usage.view';

-- ═══════════════════════════════════════════════════════════════════════════
-- Phase 9: AI-First Verification Pipeline (schema v3.0.0)
-- ═══════════════════════════════════════════════════════════════════════════

-- ── Product alias mapping (AI-extracted text → canonical product) ─────────
CREATE TABLE IF NOT EXISTS product_aliases (
    id SERIAL PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    alias_text TEXT NOT NULL,
    match_type TEXT NOT NULL DEFAULT 'exact',   -- exact | fuzzy | regex
    confidence_weight REAL DEFAULT 1.0,
    active BOOLEAN DEFAULT TRUE,
    created_by INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_product_aliases_product ON product_aliases (product_id);
CREATE INDEX IF NOT EXISTS idx_product_aliases_text ON product_aliases (alias_text);

-- ── Campaign verification rules (separate from gratification rules) ──────
CREATE TABLE IF NOT EXISTS campaign_verification_rules (
    id SERIAL PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    rule_type TEXT NOT NULL,
    -- min_quantity | max_quantity | min_pob | max_pob | campaign_period
    -- chemist_match | ptr_match
    params JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- params: {"min":10} or {"max":50000} etc.
    action_on_fail TEXT NOT NULL DEFAULT 'flag',  -- flag | auto_reject | auto_review
    active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_cv_rules_campaign ON campaign_verification_rules (campaign_id);

-- ── Verification correction audit trail ──────────────────────────────────
CREATE TABLE IF NOT EXISTS verification_corrections (
    id SERIAL PRIMARY KEY,
    verification_id INTEGER NOT NULL REFERENCES pob_verifications(id) ON DELETE CASCADE,
    agent_id INTEGER NOT NULL REFERENCES users(id),
    field_name TEXT NOT NULL,
    original_value TEXT,
    corrected_value TEXT,
    reason TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_vcorr_verification ON verification_corrections (verification_id);

-- ── Verification pipeline step tracking ──────────────────────────────────
CREATE TABLE IF NOT EXISTS verification_pipeline_steps (
    id SERIAL PRIMARY KEY,
    verification_id INTEGER NOT NULL REFERENCES pob_verifications(id) ON DELETE CASCADE,
    step_name TEXT NOT NULL,
    step_status TEXT NOT NULL DEFAULT 'passed',  -- passed | failed | skipped
    detail JSONB DEFAULT '{}'::jsonb,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_vpipe_verification ON verification_pipeline_steps (verification_id);

-- ── Extended pob_verifications columns ───────────────────────────────────
ALTER TABLE pob_verifications ADD COLUMN IF NOT EXISTS pipeline_status TEXT DEFAULT 'not_started';
ALTER TABLE pob_verifications ADD COLUMN IF NOT EXISTS ai_extraction JSONB DEFAULT '{}'::jsonb;
ALTER TABLE pob_verifications ADD COLUMN IF NOT EXISTS normalized_data JSONB DEFAULT '{}'::jsonb;
ALTER TABLE pob_verifications ADD COLUMN IF NOT EXISTS mapped_product_id INTEGER;
ALTER TABLE pob_verifications ADD COLUMN IF NOT EXISTS product_mapping_confidence REAL DEFAULT 0;
ALTER TABLE pob_verifications ADD COLUMN IF NOT EXISTS duplicate_check_result JSONB DEFAULT '{}'::jsonb;
ALTER TABLE pob_verifications ADD COLUMN IF NOT EXISTS completeness_score REAL DEFAULT 0;
ALTER TABLE pob_verifications ADD COLUMN IF NOT EXISTS correction_count INTEGER DEFAULT 0;

-- ── Extended pob_activities columns ──────────────────────────────────────
ALTER TABLE pob_activities ADD COLUMN IF NOT EXISTS verification_state TEXT DEFAULT 'draft';
ALTER TABLE pob_activities ADD COLUMN IF NOT EXISTS last_verification_state TEXT;

-- ── Extended ocr_extractions columns ─────────────────────────────────────
ALTER TABLE ocr_extractions ADD COLUMN IF NOT EXISTS field_confidences JSONB DEFAULT '{}'::jsonb;
ALTER TABLE ocr_extractions ADD COLUMN IF NOT EXISTS extraction_step TEXT;

-- ── Extended campaigns columns ───────────────────────────────────────────
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS auto_reject_confidence REAL DEFAULT 0.3;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS manual_review_threshold REAL DEFAULT 0.5;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS allow_manual_override BOOLEAN DEFAULT TRUE;

-- ── Migrate existing data to new verification_state ──────────────────────
UPDATE pob_activities SET verification_state='submitted' WHERE status='pending_verification' AND verification_state='draft';
UPDATE pob_activities SET verification_state='approved' WHERE status='verified' AND verification_state='draft';
UPDATE pob_activities SET verification_state='rejected' WHERE status='rejected' AND verification_state='draft';
UPDATE pob_activities SET verification_state='manual_review' WHERE status='needs_review' AND verification_state='draft';
UPDATE pob_activities SET verification_state='submitted' WHERE status='submitted' AND verification_state='draft';

-- Migrate existing pob_verifications pipeline_status
UPDATE pob_verifications SET pipeline_status='pending_agent' WHERE status='pending' AND pipeline_status='not_started';
UPDATE pob_verifications SET pipeline_status='completed' WHERE status='approved' AND pipeline_status='not_started';
UPDATE pob_verifications SET pipeline_status='completed' WHERE status='rejected' AND pipeline_status='not_started';
UPDATE pob_verifications SET pipeline_status='completed' WHERE status='duplicate' AND pipeline_status='not_started';
UPDATE pob_verifications SET pipeline_status='pending_agent' WHERE status='needs_review' AND pipeline_status='not_started';
UPDATE pob_verifications SET pipeline_status='completed' WHERE status='superseded' AND pipeline_status='not_started';

-- ── Seed notification templates for new pipeline states ───────────────────
INSERT INTO notification_templates (code, channel, subject, body, variables)
VALUES
    ('pob.auto_approved', 'inapp', 'POB Auto-Approved',
     'Your POB #{pob_id} for campaign {campaign} has been auto-approved. Gratification is being processed.',
     ARRAY['pob_id','campaign']),
    ('pob.needs_review', 'inapp', 'POB Needs Review',
     'POB #{pob_id} from {mr_name} for campaign {campaign} needs manual review.',
     ARRAY['pob_id','mr_name','campaign']),
    ('pob.correction_required', 'inapp', 'POB Needs Correction',
     'Your POB #{pob_id} needs correction: {reason}. Please review and resubmit.',
     ARRAY['pob_id','reason']),
    ('pob.resubmitted', 'inapp', 'POB Resubmitted',
     'POB #{pob_id} has been resubmitted after correction and is pending review.',
     ARRAY['pob_id'])
ON CONFLICT (code) DO NOTHING;

-- ── Auto-populate product aliases from existing product names ─────────────
INSERT INTO product_aliases (product_id, alias_text, match_type, confidence_weight, active)
SELECT pr.id, lower(pr.name), 'exact', 1.0, TRUE
FROM products pr
WHERE pr.status = 'active'
  AND NOT EXISTS (
      SELECT 1 FROM product_aliases pa
      WHERE pa.product_id = pr.id AND pa.alias_text = lower(pr.name)
  );

-- ── Chemist created_by: track who registered each chemist ─────────────────
ALTER TABLE chemists ADD COLUMN IF NOT EXISTS created_by INTEGER;

-- Back-fill from POB activities (first user to submit a POB for this chemist)
UPDATE chemists c SET created_by = sub.uid
FROM (
  SELECT DISTINCT ON (chemist_id) chemist_id, user_id AS uid
  FROM pob_activities ORDER BY chemist_id, id
) sub
WHERE c.id = sub.chemist_id AND c.created_by IS NULL;

-- ── Drop campaign project_name (not used by this project) ─────────────────
ALTER TABLE campaigns DROP COLUMN IF EXISTS project_name;

-- ── Phase 10: Gemini token + model attribution on ocr_usage (3.4.0) ────────
-- Costing dashboard needs to report input/output tokens and the model that
-- served each invoice extraction, not just a lump-sum cost.
ALTER TABLE ocr_usage ADD COLUMN IF NOT EXISTS model_name TEXT;
ALTER TABLE ocr_usage ADD COLUMN IF NOT EXISTS input_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ocr_usage ADD COLUMN IF NOT EXISTS output_tokens INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_ocr_usage_user ON ocr_usage (user_id);
"""

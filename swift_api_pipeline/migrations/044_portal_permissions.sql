-- Migration 044: Portal Permission System
-- Multi-tenant ready: org_id on all tables for future customer access.
-- Phase 1 (internal): single org "ontel", superuser jamil.mendez@ontel.co.
--
-- Tables:
--   agent.organizations        — tenants (ontel now, customers later)
--   agent.roles                — role templates per org
--   agent.role_permissions     — what each role grants
--   agent.users                — portal users (linked to Supabase Auth)
--   agent.user_permission_overrides — per-user exceptions
--   agent.permission_audit_log — who changed what

BEGIN;

-- ============================================================
-- Organizations
-- ============================================================
CREATE TABLE IF NOT EXISTS agent.organizations (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    slug        TEXT NOT NULL UNIQUE,
    is_active   BOOLEAN NOT NULL DEFAULT true,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE agent.organizations IS 'Tenants — one per company/customer. Multi-tenant ready.';

-- Seed ontel as the first org
INSERT INTO agent.organizations (name, slug)
VALUES ('Ontel Tech-Ops', 'ontel')
ON CONFLICT (slug) DO NOTHING;

-- ============================================================
-- Roles (templates per org)
-- ============================================================
CREATE TABLE IF NOT EXISTS agent.roles (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      UUID NOT NULL REFERENCES agent.organizations(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    description TEXT,
    is_default  BOOLEAN NOT NULL DEFAULT false,
    created_by  UUID,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (org_id, name)
);

COMMENT ON TABLE agent.roles IS 'Role templates — each org defines its own roles.';

-- Seed default roles for ontel
INSERT INTO agent.roles (org_id, name, description, is_default)
SELECT o.id, r.name, r.description, r.is_default
FROM agent.organizations o
CROSS JOIN (VALUES
    ('Superuser',   'Full access. Cannot be locked out.',                            false),
    ('Admin',       'Full access within their org. Can manage users and roles.',     false),
    ('Manager',     'Access to dashboards, explorer, reports, DARA for assigned projects/teams.', false),
    ('Technician',  'Limited access — own data and assigned project dashboards.',    true),
    ('Viewer',      'Read-only dashboards and KPIs. No raw data or exports.',        false)
) AS r(name, description, is_default)
WHERE o.slug = 'ontel'
ON CONFLICT (org_id, name) DO NOTHING;

-- ============================================================
-- Role Permissions
-- ============================================================
CREATE TABLE IF NOT EXISTS agent.role_permissions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    role_id         UUID NOT NULL REFERENCES agent.roles(id) ON DELETE CASCADE,
    permission_type TEXT NOT NULL CHECK (permission_type IN ('page', 'data', 'action')),
    permission_key  TEXT NOT NULL,
    scope_value     TEXT,  -- NULL = unrestricted; for data type: 'TS16', 'Zeta', etc.
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (role_id, permission_type, permission_key, scope_value)
);

COMMENT ON TABLE agent.role_permissions IS 'What each role grants. permission_type: page|data|action. scope_value narrows data access.';
COMMENT ON COLUMN agent.role_permissions.permission_key IS 'page: view_home, view_explorer, view_dashboards, view_reports, view_dara, view_calendar, manage_admin. data: project, team, data_source, own_data_only. action: can_export, can_ask_dara, can_schedule, can_manage_users, can_manage_roles.';
COMMENT ON COLUMN agent.role_permissions.scope_value IS 'For data permissions: specific project (TS16), team (Zeta), or data source (asset_tasks). NULL = all.';

-- Seed permissions for default ontel roles
-- Superuser: everything
INSERT INTO agent.role_permissions (role_id, permission_type, permission_key)
SELECT r.id, p.permission_type, p.permission_key
FROM agent.roles r
JOIN agent.organizations o ON o.id = r.org_id
CROSS JOIN (VALUES
    ('page',   'view_home'),
    ('page',   'view_explorer'),
    ('page',   'view_dashboards'),
    ('page',   'view_reports'),
    ('page',   'view_dara'),
    ('page',   'view_calendar'),
    ('page',   'manage_admin'),
    ('data',   'all'),
    ('action', 'can_export'),
    ('action', 'can_ask_dara'),
    ('action', 'can_schedule'),
    ('action', 'can_manage_users'),
    ('action', 'can_manage_roles')
) AS p(permission_type, permission_key)
WHERE o.slug = 'ontel' AND r.name = 'Superuser'
ON CONFLICT DO NOTHING;

-- Admin: everything except manage_roles
INSERT INTO agent.role_permissions (role_id, permission_type, permission_key)
SELECT r.id, p.permission_type, p.permission_key
FROM agent.roles r
JOIN agent.organizations o ON o.id = r.org_id
CROSS JOIN (VALUES
    ('page',   'view_home'),
    ('page',   'view_explorer'),
    ('page',   'view_dashboards'),
    ('page',   'view_reports'),
    ('page',   'view_dara'),
    ('page',   'view_calendar'),
    ('page',   'manage_admin'),
    ('data',   'all'),
    ('action', 'can_export'),
    ('action', 'can_ask_dara'),
    ('action', 'can_schedule'),
    ('action', 'can_manage_users')
) AS p(permission_type, permission_key)
WHERE o.slug = 'ontel' AND r.name = 'Admin'
ON CONFLICT DO NOTHING;

-- Manager: dashboards, explorer, reports, DARA, calendar — data scoped by assignment
INSERT INTO agent.role_permissions (role_id, permission_type, permission_key)
SELECT r.id, p.permission_type, p.permission_key
FROM agent.roles r
JOIN agent.organizations o ON o.id = r.org_id
CROSS JOIN (VALUES
    ('page',   'view_home'),
    ('page',   'view_explorer'),
    ('page',   'view_dashboards'),
    ('page',   'view_reports'),
    ('page',   'view_dara'),
    ('page',   'view_calendar'),
    ('action', 'can_export'),
    ('action', 'can_ask_dara'),
    ('action', 'can_schedule')
) AS p(permission_type, permission_key)
WHERE o.slug = 'ontel' AND r.name = 'Manager'
ON CONFLICT DO NOTHING;

-- Technician: home + dashboards, own data only
INSERT INTO agent.role_permissions (role_id, permission_type, permission_key)
SELECT r.id, p.permission_type, p.permission_key
FROM agent.roles r
JOIN agent.organizations o ON o.id = r.org_id
CROSS JOIN (VALUES
    ('page',   'view_home'),
    ('page',   'view_dashboards'),
    ('data',   'own_data_only'),
    ('action', 'can_export')
) AS p(permission_type, permission_key)
WHERE o.slug = 'ontel' AND r.name = 'Technician'
ON CONFLICT DO NOTHING;

-- Viewer: home + dashboards only, no raw data, no actions
INSERT INTO agent.role_permissions (role_id, permission_type, permission_key)
SELECT r.id, p.permission_type, p.permission_key
FROM agent.roles r
JOIN agent.organizations o ON o.id = r.org_id
CROSS JOIN (VALUES
    ('page',   'view_home'),
    ('page',   'view_dashboards')
) AS p(permission_type, permission_key)
WHERE o.slug = 'ontel' AND r.name = 'Viewer'
ON CONFLICT DO NOTHING;

-- ============================================================
-- Users
-- ============================================================
CREATE TABLE IF NOT EXISTS agent.users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    auth_id         UUID UNIQUE,  -- links to Supabase auth.users.id
    email           TEXT NOT NULL,
    display_name    TEXT,
    org_id          UUID NOT NULL REFERENCES agent.organizations(id) ON DELETE CASCADE,
    role_id         UUID NOT NULL REFERENCES agent.roles(id),
    is_superuser    BOOLEAN NOT NULL DEFAULT false,
    is_active       BOOLEAN NOT NULL DEFAULT true,
    last_login_at   TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (org_id, email)
);

COMMENT ON TABLE agent.users IS 'Portal users. auth_id links to Supabase Auth. is_superuser overrides all permissions.';

CREATE INDEX IF NOT EXISTS idx_users_email ON agent.users(email);
CREATE INDEX IF NOT EXISTS idx_users_org ON agent.users(org_id);
CREATE INDEX IF NOT EXISTS idx_users_auth ON agent.users(auth_id) WHERE auth_id IS NOT NULL;

-- Seed superuser
INSERT INTO agent.users (email, display_name, org_id, role_id, is_superuser)
SELECT
    'jamil.mendez@ontel.co',
    'Jamil Mendez',
    o.id,
    r.id,
    true
FROM agent.organizations o
JOIN agent.roles r ON r.org_id = o.id AND r.name = 'Superuser'
WHERE o.slug = 'ontel'
ON CONFLICT (org_id, email) DO NOTHING;

-- ============================================================
-- User Permission Overrides
-- ============================================================
CREATE TABLE IF NOT EXISTS agent.user_permission_overrides (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES agent.users(id) ON DELETE CASCADE,
    permission_type TEXT NOT NULL CHECK (permission_type IN ('page', 'data', 'action')),
    permission_key  TEXT NOT NULL,
    scope_value     TEXT,
    grant_type      TEXT NOT NULL CHECK (grant_type IN ('allow', 'deny')),
    granted_by      UUID REFERENCES agent.users(id),
    granted_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, permission_type, permission_key, scope_value)
);

COMMENT ON TABLE agent.user_permission_overrides IS 'Per-user exceptions that override role permissions. allow adds access, deny removes it.';

CREATE INDEX IF NOT EXISTS idx_overrides_user ON agent.user_permission_overrides(user_id);

-- ============================================================
-- Permission Audit Log
-- ============================================================
CREATE TABLE IF NOT EXISTS agent.permission_audit_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_id        UUID REFERENCES agent.users(id),
    target_user_id  UUID REFERENCES agent.users(id),
    action          TEXT NOT NULL,
    details         JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE agent.permission_audit_log IS 'Tracks all permission changes — who did what to whom.';
COMMENT ON COLUMN agent.permission_audit_log.action IS 'Values: create_user, update_role, add_override, remove_override, deactivate_user, create_role, update_role_permissions';

CREATE INDEX IF NOT EXISTS idx_audit_actor ON agent.permission_audit_log(actor_id);
CREATE INDEX IF NOT EXISTS idx_audit_target ON agent.permission_audit_log(target_user_id);
CREATE INDEX IF NOT EXISTS idx_audit_created ON agent.permission_audit_log(created_at DESC);

-- ============================================================
-- Helper function: get resolved permissions for a user
-- Merges role permissions + user overrides (deny wins over allow)
-- ============================================================
CREATE OR REPLACE FUNCTION agent.get_user_permissions(p_user_id UUID)
RETURNS TABLE (
    permission_type TEXT,
    permission_key  TEXT,
    scope_value     TEXT,
    source          TEXT  -- 'role', 'override_allow', 'override_deny'
) LANGUAGE sql STABLE AS $$
    -- Start with role permissions
    SELECT
        rp.permission_type,
        rp.permission_key,
        rp.scope_value,
        'role'::TEXT AS source
    FROM agent.users u
    JOIN agent.role_permissions rp ON rp.role_id = u.role_id
    WHERE u.id = p_user_id
      AND u.is_active = true
      -- Exclude any role permission that has a deny override
      AND NOT EXISTS (
          SELECT 1 FROM agent.user_permission_overrides uo
          WHERE uo.user_id = p_user_id
            AND uo.permission_type = rp.permission_type
            AND uo.permission_key = rp.permission_key
            AND COALESCE(uo.scope_value, '') = COALESCE(rp.scope_value, '')
            AND uo.grant_type = 'deny'
      )

    UNION ALL

    -- Add override allows (permissions beyond the role)
    SELECT
        uo.permission_type,
        uo.permission_key,
        uo.scope_value,
        'override_allow'::TEXT AS source
    FROM agent.user_permission_overrides uo
    JOIN agent.users u ON u.id = uo.user_id
    WHERE uo.user_id = p_user_id
      AND uo.grant_type = 'allow'
      AND u.is_active = true
$$;

COMMENT ON FUNCTION agent.get_user_permissions IS 'Returns resolved permissions for a user: role grants minus deny overrides plus allow overrides.';

-- ============================================================
-- Helper function: check if user has a specific permission
-- ============================================================
CREATE OR REPLACE FUNCTION agent.user_has_permission(
    p_user_id       UUID,
    p_type          TEXT,
    p_key           TEXT,
    p_scope         TEXT DEFAULT NULL
) RETURNS BOOLEAN LANGUAGE sql STABLE AS $$
    SELECT EXISTS (
        -- Superusers always have access
        SELECT 1 FROM agent.users WHERE id = p_user_id AND is_superuser = true AND is_active = true
    ) OR EXISTS (
        SELECT 1 FROM agent.get_user_permissions(p_user_id) gup
        WHERE gup.permission_type = p_type
          AND gup.permission_key = p_key
          AND (p_scope IS NULL OR gup.scope_value IS NULL OR gup.scope_value = p_scope)
    )
$$;

COMMENT ON FUNCTION agent.user_has_permission IS 'Check if a user has a specific permission. Superusers always return true.';

COMMIT;

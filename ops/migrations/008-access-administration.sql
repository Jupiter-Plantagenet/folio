CREATE TABLE IF NOT EXISTS folio_workspace_access (
  workspace uuid PRIMARY KEY,
  settings_version bigint NOT NULL DEFAULT 1 CHECK (settings_version > 0),
  auth_epoch bigint NOT NULL DEFAULT 1 CHECK (auth_epoch > 0),
  policy_version bigint NOT NULL DEFAULT 1 CHECK (policy_version > 0)
);
INSERT INTO folio_workspace_access(workspace)
VALUES ('11111111-1111-4111-8111-111111111111') ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS folio_access_requests (
  workspace uuid NOT NULL,
  id uuid NOT NULL,
  requested_by text NOT NULL REFERENCES users(username),
  target_username text NOT NULL REFERENCES users(username),
  action text NOT NULL CHECK (action IN ('grant_role','revoke_role','activate','deactivate')),
  role text CHECK (role IS NULL OR role IN ('reviewer','approver','viewer','auditor','admin')),
  reason text NOT NULL CHECK (length(reason) BETWEEN 1 AND 1000),
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','denied')),
  version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  reviewed_by text REFERENCES users(username),
  reviewed_at timestamptz,
  review_reason text,
  PRIMARY KEY(workspace,id),
  CHECK ((action IN ('grant_role','revoke_role') AND role IS NOT NULL)
      OR (action IN ('activate','deactivate') AND role IS NULL)),
  CHECK ((status='pending' AND reviewed_by IS NULL AND reviewed_at IS NULL)
      OR (status<>'pending' AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS folio_access_requests_pending
  ON folio_access_requests(workspace,created_at,id) WHERE status='pending';
CREATE INDEX IF NOT EXISTS folio_commands_actor_key
  ON commands(workspace,actor,key);

ALTER TABLE folio_workspace_access ENABLE ROW LEVEL SECURITY;
ALTER TABLE folio_workspace_access FORCE ROW LEVEL SECURITY;
DO $policy$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_policies WHERE schemaname='public' AND tablename='folio_workspace_access' AND policyname='folio_workspace') THEN
    CREATE POLICY folio_workspace ON folio_workspace_access FOR ALL TO PUBLIC
      USING (workspace='11111111-1111-4111-8111-111111111111'::uuid)
      WITH CHECK (workspace='11111111-1111-4111-8111-111111111111'::uuid);
  END IF;
END $policy$;

-- The access API must never write public.users directly.  The fixed definer
-- capability below is owned by the nonsuperuser/nobypass migration database
-- owner.  Runtime callers prove the raw cookie secret and CSRF token; caller
-- supplied usernames and role arrays are never treated as authority.
CREATE SCHEMA IF NOT EXISTS folio_access_admin_v1 AUTHORIZATION CURRENT_USER;
REVOKE ALL ON SCHEMA folio_access_admin_v1 FROM PUBLIC;
DO $access_owner$ DECLARE flags record; owner_oid oid; BEGIN
  SELECT rolsuper,rolbypassrls INTO flags
    FROM pg_catalog.pg_roles WHERE rolname=current_user;
  IF NOT FOUND OR flags.rolsuper OR flags.rolbypassrls THEN
    RAISE EXCEPTION 'UNSAFE_ACCESS_ADMIN_FUNCTION_OWNER';
  END IF;
  SELECT nspowner INTO owner_oid FROM pg_catalog.pg_namespace
    WHERE nspname='folio_access_admin_v1';
  IF owner_oid IS DISTINCT FROM (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_user) THEN
    RAISE EXCEPTION 'ACCESS_ADMIN_SCHEMA_OWNER_MISMATCH';
  END IF;
END $access_owner$;

CREATE OR REPLACE FUNCTION folio_access_admin_v1.authenticate(
  p_session_secret text, p_csrf text
) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET lock_timeout = '5s'
AS $auth$
DECLARE actor text; actor_roles jsonb; session_csrf text;
BEGIN
  SELECT u.username,u.roles,s.csrf INTO actor,actor_roles,session_csrf
    FROM public.sessions AS s JOIN public.users AS u ON u.username=s.username
    WHERE s.token=pg_catalog.encode(
      pg_catalog.sha256(pg_catalog.convert_to(p_session_secret,'UTF8')),'hex'
    ) AND s.expires>pg_catalog.clock_timestamp() AND u.active
    FOR SHARE OF s,u;
  IF NOT FOUND THEN RETURN pg_catalog.jsonb_build_object('error','SESSION_REQUIRED','status',401); END IF;
  IF p_csrf IS NULL OR p_csrf IS DISTINCT FROM session_csrf THEN
    RETURN pg_catalog.jsonb_build_object('error','CSRF_DENIED','status',403);
  END IF;
  RETURN pg_catalog.jsonb_build_object('username',actor,'roles',actor_roles);
END $auth$;
REVOKE ALL ON FUNCTION folio_access_admin_v1.authenticate(text,text) FROM PUBLIC;

CREATE OR REPLACE FUNCTION folio_access_admin_v1.authorize_mutation(
  p_session_secret text, p_csrf text, p_workspace uuid,
  p_operation text, p_request_id uuid
) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET lock_timeout = '5s'
AS $authorize$
DECLARE auth jsonb; actor text; actor_roles jsonb; requester text; target text;
        requested_action text; requested_role text;
BEGIN
  IF p_workspace IS DISTINCT FROM '11111111-1111-4111-8111-111111111111'::uuid THEN
    RETURN pg_catalog.jsonb_build_object('error','NOT_FOUND_OR_FORBIDDEN','status',404);
  END IF;
  PERFORM 1 FROM public.folio_workspace_access WHERE workspace=p_workspace FOR UPDATE;
  IF NOT FOUND THEN RETURN pg_catalog.jsonb_build_object('error','ACCESS_SCHEMA_REQUIRED','status',503); END IF;
  auth := folio_access_admin_v1.authenticate(p_session_secret,p_csrf);
  IF auth ? 'error' THEN RETURN auth; END IF;
  actor := auth->>'username'; actor_roles := auth->'roles';
  IF p_operation='create' THEN
    IF NOT (actor_roles ? 'admin') THEN RETURN pg_catalog.jsonb_build_object('error','PERMISSION_DENIED','status',403); END IF;
  ELSIF p_operation='review' THEN
    SELECT r.requested_by,r.target_username,r.action,r.role
      INTO requester,target,requested_action,requested_role
      FROM public.folio_access_requests AS r
      WHERE r.workspace=p_workspace AND r.id=p_request_id;
    IF NOT FOUND THEN RETURN pg_catalog.jsonb_build_object('error','ACCESS_REQUEST_NOT_FOUND','status',404); END IF;
    IF actor=requester OR actor=target THEN
      RETURN pg_catalog.jsonb_build_object('error','INDEPENDENT_REVIEW_REQUIRED','status',403);
    END IF;
    IF requested_action='grant_role' AND requested_role='approver' THEN
      IF NOT (actor_roles ? 'approver') THEN RETURN pg_catalog.jsonb_build_object('error','PERMISSION_DENIED','status',403); END IF;
    ELSIF NOT (actor_roles ? 'admin') THEN
      RETURN pg_catalog.jsonb_build_object('error','PERMISSION_DENIED','status',403);
    END IF;
  ELSE
    RETURN pg_catalog.jsonb_build_object('error','INVALID_ACCESS_OPERATION','status',422);
  END IF;
  RETURN auth;
END $authorize$;
REVOKE ALL ON FUNCTION folio_access_admin_v1.authorize_mutation(text,text,uuid,text,uuid) FROM PUBLIC;

CREATE OR REPLACE FUNCTION folio_access_admin_v1.apply_mutation(
  p_session_secret text, p_csrf text, p_workspace uuid,
  p_operation text, p_request_id uuid, p_payload jsonb, p_if_match text
) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET lock_timeout = '5s'
AS $apply$
DECLARE auth jsonb; actor text; actor_roles jsonb; current_settings_version bigint;
        target text; target_active boolean; target_roles jsonb; next_active boolean;
        next_roles jsonb; action text; granted_role text; reason text; decision text;
        review_text text; requester text; req_target text; req_action text; req_role text;
        req_reason text; req_status text; req_version bigint; req_created timestamptz;
        req_reviewed_by text; req_reviewed_at timestamptz; req_review_reason text;
        other_admins bigint; result jsonb;
BEGIN
  IF p_workspace IS DISTINCT FROM '11111111-1111-4111-8111-111111111111'::uuid THEN
    RETURN pg_catalog.jsonb_build_object('error','NOT_FOUND_OR_FORBIDDEN','status',404);
  END IF;
  SELECT access.settings_version INTO current_settings_version
    FROM public.folio_workspace_access AS access
    WHERE access.workspace=p_workspace FOR UPDATE;
  IF NOT FOUND THEN RETURN pg_catalog.jsonb_build_object('error','ACCESS_SCHEMA_REQUIRED','status',503); END IF;
  auth := folio_access_admin_v1.authenticate(p_session_secret,p_csrf);
  IF auth ? 'error' THEN RETURN auth; END IF;
  actor := auth->>'username'; actor_roles := auth->'roles';

  IF p_operation='create' THEN
    IF NOT (actor_roles ? 'admin') THEN RETURN pg_catalog.jsonb_build_object('error','PERMISSION_DENIED','status',403); END IF;
    IF pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object' THEN RETURN pg_catalog.jsonb_build_object('error','INVALID_ACCESS_REQUEST','status',422); END IF;
    target := p_payload->>'target_username'; action := p_payload->>'action';
    granted_role := p_payload->>'role'; reason := pg_catalog.btrim(p_payload->>'reason');
    IF target IS NULL OR target='' OR target=actor THEN RETURN pg_catalog.jsonb_build_object('error','SELF_ADMINISTRATION_DENIED','status',403); END IF;
    IF action IS NULL OR action NOT IN ('grant_role','revoke_role','activate','deactivate') THEN RETURN pg_catalog.jsonb_build_object('error','INVALID_ACCESS_ACTION','status',422); END IF;
    IF (action IN ('grant_role','revoke_role') AND (granted_role IS NULL OR granted_role NOT IN ('reviewer','approver','viewer','auditor','admin')))
       OR (action IN ('activate','deactivate') AND granted_role IS NOT NULL) THEN
      RETURN pg_catalog.jsonb_build_object('error','INVALID_ROLE_ACTION','status',422);
    END IF;
    IF reason IS NULL OR pg_catalog.length(reason)<1 OR pg_catalog.length(reason)>1000 THEN RETURN pg_catalog.jsonb_build_object('error','REASON_REQUIRED','status',422); END IF;
    IF p_if_match IS DISTINCT FROM ('"access-v'||current_settings_version||'"') THEN RETURN pg_catalog.jsonb_build_object('error','STALE_VERSION','status',412); END IF;
    SELECT u.active,u.roles INTO target_active,target_roles FROM public.users AS u WHERE u.username=target FOR UPDATE;
    IF NOT FOUND THEN RETURN pg_catalog.jsonb_build_object('error','ACCOUNT_NOT_FOUND','status',404); END IF;
    IF action='grant_role' AND target_roles ? granted_role THEN RETURN pg_catalog.jsonb_build_object('error','ROLE_ALREADY_ASSIGNED','status',409); END IF;
    IF action='revoke_role' AND NOT (target_roles ? granted_role) THEN RETURN pg_catalog.jsonb_build_object('error','ROLE_NOT_ASSIGNED','status',409); END IF;
    IF action='activate' AND target_active THEN RETURN pg_catalog.jsonb_build_object('error','ACCOUNT_ALREADY_ACTIVE','status',409); END IF;
    IF action='deactivate' AND NOT target_active THEN RETURN pg_catalog.jsonb_build_object('error','ACCOUNT_ALREADY_INACTIVE','status',409); END IF;
    INSERT INTO public.folio_access_requests AS inserted(workspace,id,requested_by,target_username,action,role,reason)
      VALUES(p_workspace,p_request_id,actor,target,action,granted_role,reason)
      RETURNING inserted.requested_by,inserted.target_username,inserted.action,inserted.role,inserted.reason,inserted.status,inserted.version,inserted.created_at
      INTO requester,req_target,req_action,req_role,req_reason,req_status,req_version,req_created;
    UPDATE public.folio_workspace_access SET settings_version=settings_version+1 WHERE workspace=p_workspace;
    INSERT INTO public.events(workspace,case_id,kind,actor,body)
      VALUES(p_workspace,NULL,'DOC-access-requested',actor,pg_catalog.jsonb_build_object(
        'request_id',p_request_id::text,'target_username',target,'action',action,'role',granted_role,'reason',reason));
    RETURN pg_catalog.jsonb_build_object('id',p_request_id::text,'requested_by',requester,'target_username',req_target,
      'action',req_action,'role',req_role,'reason',req_reason,'status',req_status,'version',req_version,
      'created_at',req_created,'reviewed_by',NULL,'reviewed_at',NULL,'review_reason',NULL);
  ELSIF p_operation='review' THEN
    SELECT r.requested_by,r.target_username,r.action,r.role,r.reason,r.status,r.version,r.created_at
      INTO requester,req_target,req_action,req_role,req_reason,req_status,req_version,req_created
      FROM public.folio_access_requests AS r WHERE r.workspace=p_workspace AND r.id=p_request_id FOR UPDATE OF r;
    IF NOT FOUND THEN RETURN pg_catalog.jsonb_build_object('error','ACCESS_REQUEST_NOT_FOUND','status',404); END IF;
    IF p_if_match IS DISTINCT FROM ('"access-request-v'||req_version||'"') THEN RETURN pg_catalog.jsonb_build_object('error','STALE_VERSION','status',412); END IF;
    IF req_status<>'pending' THEN RETURN pg_catalog.jsonb_build_object('error','ACCESS_REQUEST_ALREADY_RESOLVED','status',409); END IF;
    IF actor=requester OR actor=req_target THEN RETURN pg_catalog.jsonb_build_object('error','INDEPENDENT_REVIEW_REQUIRED','status',403); END IF;
    IF req_action='grant_role' AND req_role='approver' THEN
      IF NOT (actor_roles ? 'approver') THEN RETURN pg_catalog.jsonb_build_object('error','PERMISSION_DENIED','status',403); END IF;
    ELSIF NOT (actor_roles ? 'admin') THEN
      RETURN pg_catalog.jsonb_build_object('error','PERMISSION_DENIED','status',403);
    END IF;
    decision := p_payload->>'decision'; review_text := pg_catalog.btrim(p_payload->>'reason');
    IF decision IS NULL OR decision NOT IN ('approve','deny') THEN RETURN pg_catalog.jsonb_build_object('error','INVALID_ACCESS_DECISION','status',422); END IF;
    IF review_text IS NULL OR pg_catalog.length(review_text)<1 OR pg_catalog.length(review_text)>1000 THEN RETURN pg_catalog.jsonb_build_object('error','REASON_REQUIRED','status',422); END IF;
    IF decision='approve' THEN
      SELECT active,roles INTO target_active,target_roles FROM public.users WHERE username=req_target FOR UPDATE;
      IF NOT FOUND THEN RETURN pg_catalog.jsonb_build_object('error','ACCOUNT_NOT_FOUND','status',404); END IF;
      next_active := target_active; next_roles := target_roles;
      IF req_action='grant_role' THEN
        IF next_roles ? req_role THEN RETURN pg_catalog.jsonb_build_object('error','ROLE_ALREADY_ASSIGNED','status',409); END IF;
        SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(value) ORDER BY value), '[]'::jsonb)
          INTO next_roles
          FROM pg_catalog.jsonb_array_elements_text(next_roles || pg_catalog.jsonb_build_array(req_role)) AS roles(value);
      ELSIF req_action='revoke_role' THEN
        IF NOT (next_roles ? req_role) THEN RETURN pg_catalog.jsonb_build_object('error','ROLE_NOT_ASSIGNED','status',409); END IF;
        SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(value) ORDER BY value), '[]'::jsonb) INTO next_roles
          FROM pg_catalog.jsonb_array_elements_text(next_roles) AS roles(value) WHERE value <> req_role;
      ELSIF req_action='activate' THEN
        IF target_active THEN RETURN pg_catalog.jsonb_build_object('error','ACCOUNT_ALREADY_ACTIVE','status',409); END IF;
        next_active := true;
      ELSIF req_action='deactivate' THEN
        IF NOT target_active THEN RETURN pg_catalog.jsonb_build_object('error','ACCOUNT_ALREADY_INACTIVE','status',409); END IF;
        next_active := false;
      END IF;
      PERFORM 1 FROM public.users WHERE active AND roles ? 'admin' FOR UPDATE;
      SELECT pg_catalog.count(*) INTO other_admins FROM public.users
        WHERE username<>req_target AND active AND roles ? 'admin';
      IF next_active AND next_roles ? 'admin' THEN other_admins := other_admins+1; END IF;
      IF other_admins<1 THEN RETURN pg_catalog.jsonb_build_object('error','LAST_ADMIN_PROTECTED','status',422); END IF;
      UPDATE public.users SET roles=next_roles,active=next_active WHERE username=req_target;
      DELETE FROM public.sessions WHERE username=req_target;
      UPDATE public.folio_workspace_access SET settings_version=settings_version+1,auth_epoch=auth_epoch+1 WHERE workspace=p_workspace;
    ELSE
      UPDATE public.folio_workspace_access SET settings_version=settings_version+1 WHERE workspace=p_workspace;
    END IF;
    UPDATE public.folio_access_requests SET status=CASE WHEN decision='approve' THEN 'approved' ELSE 'denied' END,
      version=version+1,reviewed_by=actor,reviewed_at=pg_catalog.clock_timestamp(),review_reason=review_text
      WHERE workspace=p_workspace AND id=p_request_id
      RETURNING status,version,reviewed_by,reviewed_at,review_reason
      INTO req_status,req_version,req_reviewed_by,req_reviewed_at,req_review_reason;
    INSERT INTO public.events(workspace,case_id,kind,actor,body)
      VALUES(p_workspace,NULL,'DOC-access-reviewed',actor,pg_catalog.jsonb_build_object(
        'request_id',p_request_id::text,'requester',(SELECT requested_by FROM public.folio_access_requests WHERE workspace=p_workspace AND id=p_request_id),
        'target_username',req_target,'action',req_action,'role',req_role,'decision',decision,'reason',review_text));
    RETURN pg_catalog.jsonb_build_object('id',p_request_id::text,'requested_by',(SELECT requested_by FROM public.folio_access_requests WHERE workspace=p_workspace AND id=p_request_id),
      'target_username',req_target,'action',req_action,'role',req_role,'reason',req_reason,'status',req_status,'version',req_version,
      'created_at',req_created,'reviewed_by',req_reviewed_by,'reviewed_at',req_reviewed_at,'review_reason',req_review_reason);
  END IF;
  RETURN pg_catalog.jsonb_build_object('error','INVALID_ACCESS_OPERATION','status',422);
END $apply$;
REVOKE ALL ON FUNCTION folio_access_admin_v1.apply_mutation(text,text,uuid,text,uuid,jsonb,text) FROM PUBLIC;

ALTER TABLE folio_access_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE folio_access_requests FORCE ROW LEVEL SECURITY;
DO $policy$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_policies WHERE schemaname='public' AND tablename='folio_access_requests' AND policyname='folio_workspace') THEN
    CREATE POLICY folio_workspace ON folio_access_requests FOR ALL TO PUBLIC
      USING (workspace='11111111-1111-4111-8111-111111111111'::uuid)
      WITH CHECK (workspace='11111111-1111-4111-8111-111111111111'::uuid);
  END IF;
END $policy$;

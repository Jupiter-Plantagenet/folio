-- Additive only. Apply as migration owner on an isolated/restored copy first.

CREATE TABLE IF NOT EXISTS export_jobs(
 id uuid PRIMARY KEY, workspace uuid NOT NULL, case_id uuid NOT NULL,
 actor text NOT NULL, snapshot jsonb NOT NULL,
 snapshot_sha256 text NOT NULL CHECK(length(snapshot_sha256)=64),
 state text NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','leased','ready','exhausted','tombstoned')),
 attempt uuid, fence bigint NOT NULL DEFAULT 0, tries integer NOT NULL DEFAULT 0,
 lease_until timestamptz, artifact_ref text, artifact_sha256 text, artifact_bytes integer,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 expires_at timestamptz NOT NULL DEFAULT clock_timestamp()+interval '365 days',
 deleted_at timestamptz,
 FOREIGN KEY(workspace,case_id) REFERENCES cases(workspace,id),
 CHECK(octet_length(snapshot::text)<=524288),
 CHECK(artifact_bytes IS NULL OR artifact_bytes BETWEEN 1 AND 262144),
 CHECK(state!='ready' OR (artifact_ref IS NOT NULL AND artifact_sha256 IS NOT NULL AND artifact_bytes IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS export_jobs_pending ON export_jobs(state,lease_until);
ALTER TABLE export_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE export_jobs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS folio_workspace ON export_jobs;
CREATE POLICY folio_workspace ON export_jobs TO PUBLIC
 USING(workspace='11111111-1111-4111-8111-111111111111'::uuid)
 WITH CHECK(workspace='11111111-1111-4111-8111-111111111111'::uuid);


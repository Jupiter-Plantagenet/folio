-- Folio migration 005 (round 6, DOC-T-LOAD read latency). EXPAND-ONLY.
-- Adds indexes only: no table, column, constraint, policy, grant or data change.
-- App N runs on schema N-1 (startup does not require 005) and N-1 runs on N.
-- Rollback is not needed for correctness; DROP INDEX is safe if ever required.

-- Queue list (GET /cases): only imported packets are listed. Without this partial index
-- every list request detoasted every case body to test the two keys.
CREATE INDEX IF NOT EXISTS cases_queue_listing ON cases(workspace, id)
  WHERE (body ? 'sample_digest' OR body ? 'intake_digest');

-- Case detail (GET /cases/{id}) joins proposals, jobs and analysis runs by case.
CREATE INDEX IF NOT EXISTS proposals_case_id ON proposals(case_id);
CREATE INDEX IF NOT EXISTS jobs_proposal ON jobs(proposal);
CREATE INDEX IF NOT EXISTS analysis_runs_case ON analysis_runs(workspace, case_id);

CREATE TABLE IF NOT EXISTS payables(workspace uuid NOT NULL,id uuid NOT NULL,body jsonb NOT NULL,PRIMARY KEY(workspace,id));
        CREATE TABLE IF NOT EXISTS authority(workspace uuid NOT NULL,operation_key uuid NOT NULL,digest text NOT NULL,fence bigint NOT NULL,grant_body jsonb,expires timestamptz,PRIMARY KEY(workspace,operation_key));
        CREATE TABLE IF NOT EXISTS operations(workspace uuid NOT NULL,operation_key uuid NOT NULL,target_id uuid NOT NULL,body jsonb NOT NULL,PRIMARY KEY(workspace,operation_key),FOREIGN KEY(workspace,target_id) REFERENCES payables(workspace,id));
        CREATE TABLE IF NOT EXISTS target_sessions(token text PRIMARY KEY,expires timestamptz NOT NULL);
        GRANT USAGE ON SCHEMA public TO folio_verifier;
        GRANT SELECT ON payables,operations,authority TO folio_verifier;
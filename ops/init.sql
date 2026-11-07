CREATE ROLE folio_workbench NOLOGIN;
CREATE ROLE folio_target NOLOGIN;
CREATE ROLE folio_verifier NOLOGIN;
CREATE DATABASE folio_workbench OWNER folio_workbench;
CREATE DATABASE folio_target OWNER folio_target;
GRANT CONNECT ON DATABASE folio_target TO folio_verifier;

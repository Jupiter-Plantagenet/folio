-- Fixed read/row-lock capability only. The migration owner must be trusted and
-- distinct from every runtime grantee. No auth writes or runtime users UPDATE.
CREATE OR REPLACE FUNCTION public.folio_export_lock_actor(session_token text)
RETURNS TABLE(username text, roles jsonb, csrf text)
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET lock_timeout = '5s'
AS $actor$
 SELECT u.username, u.roles, s.csrf
 FROM public.sessions AS s JOIN public.users AS u ON u.username = s.username
 WHERE s.token = session_token AND s.expires > pg_catalog.clock_timestamp() AND u.active
 FOR SHARE OF s, u
$actor$;
REVOKE ALL ON FUNCTION public.folio_export_lock_actor(text) FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

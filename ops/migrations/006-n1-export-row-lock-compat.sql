-- Narrow compatibility for the immutable N-1 exporter, which still issues
-- SELECT ... FOR SHARE directly against sessions and users. PostgreSQL requires
-- UPDATE privilege on each locked relation; the current application uses the
-- fixed 004 SECURITY DEFINER capability instead.
--
-- This column has no application meaning. Runtime roles receive UPDATE on this
-- column alone, solely to satisfy the legacy row-lock privilege check. Any DML
-- attempt against it is rejected, including a no-op update.
ALTER TABLE public.users
    ADD COLUMN IF NOT EXISTS n1_export_lock_capability boolean NOT NULL DEFAULT false;
REVOKE UPDATE (n1_export_lock_capability) ON public.users FROM PUBLIC;

CREATE OR REPLACE FUNCTION public.folio_reject_n1_export_lock_capability_update()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $guard$
BEGIN
    RAISE EXCEPTION 'N1_EXPORT_LOCK_CAPABILITY_IMMUTABLE'
        USING ERRCODE = '42501';
END;
$guard$;

REVOKE ALL ON FUNCTION public.folio_reject_n1_export_lock_capability_update() FROM PUBLIC;

DROP TRIGGER IF EXISTS folio_n1_export_lock_capability_immutable ON public.users;
CREATE TRIGGER folio_n1_export_lock_capability_immutable
    BEFORE UPDATE OF n1_export_lock_capability ON public.users
    FOR EACH ROW
    EXECUTE FUNCTION public.folio_reject_n1_export_lock_capability_update();

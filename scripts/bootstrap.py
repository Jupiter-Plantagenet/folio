"""Trusted local synthetic fixture/database bootstrap, not an application command."""

from folio.target import migrate as target_migrate
from folio.target import seed
from folio.workbench import TARGET, WORKSPACE, migrate

target_migrate()
migrate()
seed(WORKSPACE, TARGET)
print(
    "Workbench/target schemas initialized; authored target fixture inserted only if absent. Existing receipts and effects are never reset."
)

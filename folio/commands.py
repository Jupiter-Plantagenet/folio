"""Closed wire commands: validation is independent of model instructions."""

from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
PositiveInt = Annotated[int, Field(strict=True, ge=1, le=9223372036854775807)]
Quantity = Annotated[str, Field(pattern=r"^(0|[1-9]\d{0,11})(\.\d{1,6})?$")]
# The wire has no currency field: bounded lexical scale is checked here;
# the target validates the exact scale against its authoritative record.
Money = Annotated[str, Field(pattern=r"^(0|[1-9]\d{0,13})(\.\d{1,3})?$")]


def canonical_uuid(value):
    """Validate literal wire text; never turn an alias or malformed token into success."""
    if str(UUID(value)) != value:
        raise ValueError("CANONICAL_UUID_REQUIRED")
    return value


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ExactPatch(Closed):
    recognized_quantity: Quantity
    net_amount: Money
    tax_amount: Money
    gross_amount: Money
    status: Literal["partially_matched"]

    @model_validator(mode="after")
    def accounting(self):
        if Decimal(self.recognized_quantity) <= 0 or Decimal(self.net_amount) + Decimal(
            self.tax_amount
        ) != Decimal(self.gross_amount):
            raise ValueError("PATCH_ARITHMETIC")
        return self


class FenceCommand(Closed):
    operation_key: str
    intent_digest: Digest
    monotonic_fence: PositiveInt

    @field_validator("operation_key")
    @classmethod
    def uuid_value(cls, value):
        return canonical_uuid(value)


class GrantCommand(Closed):
    operation_key: str
    intent_digest: Digest
    target_id: str
    expected_revision: PositiveInt
    target_fence: PositiveInt
    exact_patch: ExactPatch
    approval: dict | None = None
    # Explicit renewal only: names the prior grant's JTI (blueprint section 7 renewal).
    renew_prior_jti: str | None = None

    @field_validator("operation_key", "target_id")
    @classmethod
    def uuid_value(cls, value):
        return canonical_uuid(value)

    @field_validator("renew_prior_jti")
    @classmethod
    def optional_uuid_value(cls, value):
        return None if value is None else canonical_uuid(value)


class MutationCommand(Closed):
    intent_digest: Digest
    grant_jti: str
    fence: PositiveInt
    exact_patch: ExactPatch

    @field_validator("grant_jti")
    @classmethod
    def uuid_value(cls, value):
        return canonical_uuid(value)

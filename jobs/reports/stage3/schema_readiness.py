from __future__ import annotations

from dataclasses import dataclass


REQUIRED_CLIENT_MIGRATION = "db/client_business/042_workflow_b_stage3_runtime_schema.sql"


@dataclass(frozen=True, slots=True)
class MissingSchemaObject:
    schema_name: str
    table_name: str
    object_type: str
    object_name: str

    @property
    def qualified_name(self) -> str:
        if self.object_type == "table":
            return f"{self.schema_name}.{self.table_name}"
        return f"{self.schema_name}.{self.table_name}.{self.object_name}"


class Stage3SchemaReadinessError(RuntimeError):
    """Non-retryable runtime failure requiring an admin-applied client migration."""

    def __init__(
        self,
        missing: list[MissingSchemaObject] | tuple[MissingSchemaObject, ...],
        *,
        required_migration: str = REQUIRED_CLIENT_MIGRATION,
    ):
        if not missing:
            raise ValueError("At least one missing schema object is required")
        self.missing = tuple(missing)
        self.required_migration = required_migration
        labels = ", ".join(
            f"{item.object_type} {item.qualified_name}" for item in self.missing
        )
        super().__init__(
            f"Workflow B Stage 3 schema is not ready: missing {labels}; "
            f"apply {self.required_migration} with the migration/admin role"
        )


class RuntimeSchemaMutationDisabledError(RuntimeError):
    """Raised when a removed runtime schema/grant bootstrap option is requested."""

    def __init__(self, option_name: str):
        self.option_name = option_name
        super().__init__(
            f"{option_name}=true is no longer supported during recurring Workflow B runtime; "
            f"apply {REQUIRED_CLIENT_MIGRATION} and controlled least-privilege grants before the job"
        )

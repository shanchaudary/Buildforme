"""Provider adapters (Stage 5 dry-run + Stage 6 live CLI)."""

from buildforme.adapters.dry_run import DryRunAdapter
from buildforme.adapters.registry import all_providers_have_adapters, get_adapter, list_live_adapter_ids

__all__ = [
    "DryRunAdapter",
    "get_adapter",
    "list_live_adapter_ids",
    "all_providers_have_adapters",
]

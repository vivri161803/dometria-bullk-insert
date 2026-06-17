"""Processing blocks for the Bulk Insert pipeline."""

from bulk_insert.blocks.contract import (
    ContractDeterminationError,
    determine_contract,
)

__all__ = [
    "determine_contract",
    "ContractDeterminationError",
]

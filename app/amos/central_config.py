"""Application routing configuration; credentials belong to the reusable client."""
from dataclasses import dataclass
from pathlib import Path
import tomllib

from amoscentral import ClientConfig, validate_address


@dataclass
class RoutingConfig:
    service: str
    data_source_ids: dict[str, str]
    service_version: str = "1.0.0"
    product: str = ""
    batch_header: str = "amc_correlation_id"
    snapshot_header: str = "amc_send_time"
    require_encryption: bool = True
    pending_batch_limit: int = 10
    pending_batch_hours: int = 168

    def __post_init__(self):
        validate_address(self.service)
        if set(self.data_source_ids) != {'parts', 'configurations', 'documents'} or len(set(self.data_source_ids.values())) != 3:
            raise ValueError('Configure three distinct dataSourceId values')
        if not all(isinstance(v, str) and v for v in self.data_source_ids.values()):
            raise ValueError('Empty dataSourceId')
        if not self.batch_header.startswith('amc_') or not self.snapshot_header.startswith('amc_'):
            raise ValueError('Batch and snapshot headers must be AMOScentral headers')
        if not 1 <= self.pending_batch_limit <= 100 or not 1 <= self.pending_batch_hours <= 8760:
            raise ValueError('Invalid pending-batch limits')


def load_config(path):
    with Path(path).open('rb') as stream:
        data = tomllib.load(stream)
    return ClientConfig(**data['amoscentral']), RoutingConfig(**data['routing'])

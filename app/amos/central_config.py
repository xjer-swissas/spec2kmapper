"""Application routing configuration; credentials belong to the reusable client."""
from dataclasses import dataclass, field
from pathlib import Path
import tomllib

from amoscentral import ClientConfig

EXPORT_SERVICE = "com.swiss-as.amos.aim.export_data_source"


@dataclass
class RoutingConfig:
    data_source_ids: dict[str, str] = field(default_factory=lambda: {
        'parts': 'parts', 'configurations': 'configurations', 'documents': 'documents'})
    service_version: str = "1.0"
    # Accepted for compatibility with old TOML files; these no longer gate imports.
    product: str = ""
    require_encryption: bool = False
    pending_batch_limit: int = 10
    pending_batch_hours: int = 168
    export_type_header: str = "amc_2k5mapper_product_header"

    def export_kind(self, headers):
        if not self.export_type_header:
            return None
        value = headers.get(self.export_type_header)
        kinds = {'part_definition': 'parts', 'configuration': 'configurations', 'documents': 'documents'}
        if value not in kinds:
            raise ValueError(f'Missing or unrecognized {self.export_type_header}; expected part_definition, configuration, or documents')
        return kinds[value]

    def __post_init__(self):
        if self.export_type_header and not self.export_type_header.startswith('amc_'):
            raise ValueError('Export type header must start with amc_')
        if not self.export_type_header and (set(self.data_source_ids) != {'parts', 'configurations', 'documents'} or len(set(self.data_source_ids.values())) != 3):
            raise ValueError('Configure three distinct dataSourceId values')
        if not self.export_type_header and not all(isinstance(v, str) and v for v in self.data_source_ids.values()):
            raise ValueError('Empty dataSourceId')


def load_config(path):
    with Path(path).open('rb') as stream:
        data = tomllib.load(stream)
    return ClientConfig(**data['amoscentral']), RoutingConfig(**data['routing'])

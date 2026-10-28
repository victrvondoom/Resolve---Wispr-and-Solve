"""Load and validate the user-requested 80-concept RESOLVE registry.

The registry records intended workspace routing. A PLANNED record is not a
claim that the original project has been researched or that an adapter exists.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


REGISTRY_PATH = Path(__file__).resolve().parents[1] / 'capabilities' / 'registry.json'
STATUSES = frozenset({
    'PLANNED', 'SCAFFOLDED', 'IMPLEMENTED', 'TESTED', 'VERIFIED',
    'EXTERNAL_DEPENDENCY_REQUIRED', 'UNSUPPORTED',
})
WORKSPACES = frozenset({
    'Computer Control', 'Skill Learning', 'Trust and Safety',
    'Multi-Agent Workforce', 'Development Operations', 'Business and GTM',
    'Commerce', 'Science and Engineering', 'Creative and Educational',
    'Real-World and Life Admin',
})
REQUIRED_FIELDS = frozenset({
    'name', 'source_hackathon', 'source_reference', 'original_purpose',
    'core_capabilities', 'resolve_workspace', 'shared_runtime_dependencies',
    'required_external_services', 'licensing_notes', 'implementation_status',
    'test_status', 'demo_status',
})


def load_registry(path: str | Path = REGISTRY_PATH) -> list[dict[str, Any]]:
    """Return validated concepts, refusing incomplete or falsely tested entries."""
    with Path(path).open('r', encoding='utf-8') as source:
        data = json.load(source)
    if not isinstance(data, list):
        raise ValueError('registry root must be a list')
    if len(data) != 80:
        raise ValueError(f'registry must have exactly 80 concepts; found {len(data)}')

    names: set[str] = set()
    counts = {'Iterate Computer Use Hackathon': 0, 'Iterate Agentic GTM Hackathon': 0}
    for index, row in enumerate(data):
        if not isinstance(row, dict):
            raise ValueError(f'concept {index} must be an object')
        missing = REQUIRED_FIELDS - row.keys()
        if missing:
            raise ValueError(f'concept {index} missing fields: {sorted(missing)}')
        name = row['name']
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f'concept {index} has invalid name')
        if name in names:
            raise ValueError(f'duplicate concept name: {name}')
        names.add(name)
        source = row['source_hackathon']
        if source not in counts:
            raise ValueError(f'{name}: unknown source hackathon')
        counts[source] += 1
        parsed = urlparse(row['source_reference']) if isinstance(row['source_reference'], str) else None
        if not parsed or parsed.scheme != 'https' or not parsed.netloc:
            raise ValueError(f'{name}: source_reference must be an HTTPS URL')
        if row['original_purpose'] is not None and not isinstance(row['original_purpose'], str):
            raise ValueError(f'{name}: original_purpose must be text or null')
        if row['resolve_workspace'] not in WORKSPACES:
            raise ValueError(f'{name}: unknown workspace')
        for field in ('core_capabilities', 'shared_runtime_dependencies'):
            if not isinstance(row[field], list) or not all(isinstance(item, str) for item in row[field]):
                raise ValueError(f'{name}: {field} must be a text list')
        services = row['required_external_services']
        if services is not None and (not isinstance(services, list) or not all(isinstance(item, str) for item in services)):
            raise ValueError(f'{name}: required_external_services must be a text list or null')
        if not isinstance(row['licensing_notes'], str) or not row['licensing_notes'].strip():
            raise ValueError(f'{name}: licensing_notes must be text')
        for field in ('implementation_status', 'test_status', 'demo_status'):
            if row[field] not in STATUSES:
                raise ValueError(f'{name}: invalid {field}')
        if row['implementation_status'] == 'PLANNED' and any(row[field] in {'IMPLEMENTED', 'TESTED', 'VERIFIED'} for field in ('test_status', 'demo_status')):
            raise ValueError(f'{name}: planned implementation cannot be marked tested or demonstrated')
    if counts != {'Iterate Computer Use Hackathon': 44, 'Iterate Agentic GTM Hackathon': 36}:
        raise ValueError(f'expected 44 computer-use and 36 GTM concepts; found {counts}')
    return data


def concepts_for_workspace(workspace: str, path: str | Path = REGISTRY_PATH) -> list[dict[str, Any]]:
    if workspace not in WORKSPACES:
        raise ValueError(f'unknown workspace: {workspace}')
    return [row for row in load_registry(path) if row['resolve_workspace'] == workspace]

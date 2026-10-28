"""G7 capability catalogue loader/validator (capabilities/catalog.yaml).

A TESTED entry must name a pytest node id whose file exists in tests/; nothing
in the catalogue may claim VERIFIED without Windows/human evidence elsewhere.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / 'capabilities' / 'catalog.yaml'
STATUSES = frozenset({'IDEA', 'SPECIFIED', 'BACKEND_IMPLEMENTED', 'WIRED', 'TESTED', 'VERIFIED',
                      'PROVIDER_REQUIRED', 'BLOCKED'})
SOURCES = {'computer_use': 44, 'agentic_gtm': 36}
REQUIRED = ('id', 'source_hackathon', 'name', 'original_idea', 'resolve_capability', 'workspace', 'adapter',
            'dependencies', 'status', 'acceptance_test', 'evidence', 'source_license_if_reused')


def load_catalog(path: str | Path | None = None) -> list[dict[str, Any]]:
    data = yaml.safe_load(Path(path or CATALOG_PATH).read_text(encoding='utf-8'))
    concepts = data.get('concepts') if isinstance(data, dict) else data
    if not isinstance(concepts, list):
        raise ValueError('catalog must contain a list of concepts')
    return concepts


def summary(entries: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    entries = load_catalog() if entries is None else entries
    return {'total': len(entries),
            'by_status': dict(sorted(Counter(e.get('status') for e in entries).items())),
            'by_source': dict(sorted(Counter(e.get('source_hackathon') for e in entries).items()))}


def test_file_of(node_id: str) -> Path:
    return ROOT / node_id.split('::', 1)[0]


def validate(entries: list[dict[str, Any]], root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    for i, e in enumerate(entries):
        label = e.get('id', f'#{i}') if isinstance(e, dict) else f'#{i}'
        if not isinstance(e, dict):
            errors.append(f'{label}: entry is not a mapping')
            continue
        for field in REQUIRED:
            if field not in e:
                errors.append(f'{label}: missing {field}')
        if e.get('id') in seen_ids:
            errors.append(f'{label}: duplicate id')
        seen_ids.add(e.get('id'))
        if e.get('name') in seen_names:
            errors.append(f'{label}: duplicate name')
        seen_names.add(e.get('name'))
        if e.get('source_hackathon') not in SOURCES:
            errors.append(f'{label}: invalid source_hackathon {e.get("source_hackathon")!r}')
        status = e.get('status')
        if status not in STATUSES:
            errors.append(f'{label}: invalid status {status!r}')
        if not isinstance(e.get('dependencies', []), list):
            errors.append(f'{label}: dependencies must be a list')
        if status in ('TESTED', 'VERIFIED'):
            node = str(e.get('acceptance_test') or '')
            if not node.startswith('tests/') or '::' not in node:
                errors.append(f'{label}: {status} requires a pytest node id under tests/')
            elif not (root / node.split('::', 1)[0]).is_file():
                errors.append(f'{label}: acceptance_test file missing: {node}')
        if status == 'BLOCKED' and not str(e.get('evidence') or '').strip():
            errors.append(f'{label}: BLOCKED requires evidence')
        if status == 'VERIFIED':
            errors.append(f'{label}: VERIFIED needs Windows/human evidence not available to the catalogue')
    counts = Counter(e.get('source_hackathon') for e in entries if isinstance(e, dict))
    for src, expected in SOURCES.items():
        if counts.get(src, 0) != expected:
            errors.append(f'{src}: expected {expected} concepts, found {counts.get(src, 0)}')
    return errors


if __name__ == '__main__':
    import json
    cat = load_catalog()
    print(json.dumps({'summary': summary(cat), 'errors': validate(cat)}, indent=2))

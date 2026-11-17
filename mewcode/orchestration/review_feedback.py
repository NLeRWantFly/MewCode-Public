"""Locate review findings in existing deliveries, never in permission scopes."""
from __future__ import annotations

import re
from pathlib import PurePosixPath


def issue_route(issue):
    """Unclassified/legacy prose is not proof that a producer can fix a finding."""
    route = issue.get('resolution', 'unclassified')
    if route == 'downstream' and issue.get('blocking') is False and issue.get('origin') == 'out_of_scope':
        return 'downstream'
    if issue.get('origin') == 'out_of_scope':
        return 'out_of_scope'
    if route in {'user_decision', 'capability_gap'}:
        return route
    if (route == 'producer_fix' and issue.get('blocking', True)
            and str(issue.get('acceptance_test') or '').strip()):
        return route
    return 'unclassified'


def _path(value):
    text = str(value or '').strip().replace('\\', '/')
    # Display-only suffixes may describe a declared file or cite its line.
    text = re.split(r'：|:\s+', text, maxsplit=1)[0]
    text = re.sub(r':\d+(?::\d+)?$', '', text).strip(' `')
    if not text or '..' in PurePosixPath(text).parts:
        return ''
    return str(PurePosixPath(text)).removeprefix('./')


def issue_owners(value, keys, issue):
    """Use explicit producer IDs or unambiguous artifact/declared output paths.

    A multi-file finding can belong to several producers. Broad write grants,
    prose mentions and directory prefixes do not establish ownership.
    """
    if issue.get('node_id') in keys:
        return [issue['node_id']]
    owners = set()
    for part in re.split(r'[;；\n]', str(issue.get('file') or '')):
        path = _path(part)
        if not path:
            continue
        matches = []
        for key in keys:
            artifacts = value.get('certificates', {}).get(key, {}).get('artifacts', [])
            paths = [row.get('path') for row in artifacts if row.get('kind') == 'file']
            paths += value['contracts'][key].get('deliverables', [])
            if any(_path(item) == path for item in paths):
                matches.append(key)
        if len(matches) == 1:
            owners.add(matches[0])
    return sorted(owners)

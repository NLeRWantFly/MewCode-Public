import copy
import json
import os
from pathlib import Path

import pytest

from mewcode.sandbox import workspace_metadata as metadata


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows handle metadata APIs are required")


def _known_snapshot():
    return {
        "schema": metadata.SCHEMA,
        "kind": "file",
        "capture_consistent": True,
        "attributes": 0x80,
        "created": 1,
        "written": 2,
        "security": {
            "owner_group_dacl": {"state": "captured", "sddl": "O:SYG:SYD:(A;;FA;;;SY)"},
            "sacl": {"state": "captured", "present": False, "null": False, "sddl": None},
        },
        "streams": {"state": "captured", "items": [
            {"name": "::$DATA", "size": 3, "allocation_size": 4096},
        ]},
        "extended_attributes": {"state": "captured", "size": 0},
    }


def test_known_inventory_is_accepted_and_access_time_is_not_compared():
    baseline = _known_snapshot()
    metadata.require_supported_metadata(baseline)
    changed_access_only = copy.deepcopy(baseline)
    changed_access_only["accessed"] = 777
    baseline["accessed"] = 123

    assert metadata.metadata_drift(baseline, changed_access_only) == ()


def test_metadata_only_changes_are_reported():
    baseline = _known_snapshot()
    current = copy.deepcopy(baseline)
    current["written"] += 100
    current["security"]["owner_group_dacl"]["sddl"] = "O:SYG:SYD:(A;;FR;;;SY)"
    current["streams"]["items"][0]["allocation_size"] += 4096
    current["extended_attributes"]["size"] = 8

    assert metadata.metadata_drift(baseline, current) == (
        "extended_attributes.size",
        "security.owner_group_dacl.sddl",
        "streams.items",
        "written",
    )


def test_unknown_sacl_is_not_equivalent_to_no_sacl():
    inventory = _known_snapshot()
    inventory["security"]["sacl"] = {
        "state": "unavailable", "present": None, "null": None, "sddl": None,
    }

    with pytest.raises(metadata.MetadataInventoryError, match="SACL state is unknown"):
        metadata.require_supported_metadata(inventory)
    assert "security.sacl.unavailable" in metadata.metadata_drift(inventory, inventory)


def test_real_file_inventory_is_json_safe_and_explicit_about_security(tmp_path: Path):
    sample = tmp_path / "inventory.tmp"
    sample.write_bytes(b"synthetic metadata fixture")

    inventory = metadata.capture_metadata(sample)

    assert inventory["schema"] == metadata.SCHEMA
    assert inventory["kind"] == "file"
    assert inventory["capture_consistent"] is True
    assert isinstance(inventory["attributes"], int)
    assert isinstance(inventory["created"], int)
    assert isinstance(inventory["written"], int)
    assert "accessed" not in inventory
    assert inventory["security"]["owner_group_dacl"]["state"] in {"captured", "unavailable"}
    assert inventory["security"]["sacl"]["state"] in {"captured", "unavailable"}
    assert inventory["streams"]["state"] in {"captured", "unavailable"}
    assert inventory["extended_attributes"]["state"] in {"captured", "unavailable"}
    json.dumps(inventory, sort_keys=True)

    if inventory["security"]["sacl"]["state"] == "unavailable":
        with pytest.raises(metadata.MetadataInventoryError, match="SACL state is unknown"):
            metadata.require_supported_metadata(inventory)
        assert "security.sacl.unavailable" in metadata.metadata_drift(inventory, inventory)


def test_real_file_metadata_only_write_time_drift_is_detected(tmp_path: Path):
    sample = tmp_path / "write-time.tmp"
    sample.write_bytes(b"unchanged content")
    before = metadata.capture_metadata(sample)
    stat_before = sample.stat()

    os.utime(sample, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns + 5_000_000_000))

    after = metadata.capture_metadata(sample)
    assert sample.read_bytes() == b"unchanged content"
    assert "written" in metadata.metadata_drift(before, after)


def test_real_directory_inventory_uses_a_bounded_handle_query(tmp_path: Path):
    directory = tmp_path / "directory.tmp"
    directory.mkdir()

    inventory = metadata.capture_metadata(directory)

    assert inventory["kind"] == "directory"
    assert inventory["capture_consistent"] is True
    assert len(inventory["streams"].get("items", [])) <= metadata.MAX_STREAMS
    json.dumps(inventory, sort_keys=True)

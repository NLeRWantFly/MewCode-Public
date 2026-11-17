import pytest

from mewcode.experts.runtime_reconciliation import reconcile_known_artifacts
from tests.test_repair_work_batch import fixture


@pytest.mark.asyncio
async def test_resume_reconciliation_reports_changed_known_source_without_importing_it(tmp_path):
    _, _, store, run_id, ids = await fixture(tmp_path)
    before = store.load(run_id)
    verified = reconcile_known_artifacts(before, tmp_path)
    assert verified["findings"] == []
    (tmp_path / "left.md").write_text("changed outside ledger", encoding="utf-8")
    after = reconcile_known_artifacts(before, tmp_path)
    assert after["findings"]
    assert {row["item_id"] for row in after["findings"]} == set(ids)
    assert all(row["state"] == "digest_changed" for row in after["findings"])
    assert after["unknown_files_imported"] == 0
    assert after["business_acceptance_changed"] is False
    assert store.load(run_id) == before  # The scanner is read-only.

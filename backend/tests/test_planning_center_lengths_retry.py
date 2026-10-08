from pathlib import Path

import pytest

from stagepilot.core.settings import MemoryCredentialStore, SettingsFileStore, SettingsService
from stagepilot.services.planning_center_lengths import PlanningCenterLengthService
from test_planning_center_lengths import Rig


async def fresh_update(rig: Rig, length: int) -> None:
    plan = rig.plan.model_copy(deep=True)
    plan.songs[0].duration_seconds = rig.remote["0"]
    await rig.update(plan, [length])


@pytest.mark.parametrize("failure", [None, "timeout", 500, 403])
async def test_multiple_updates_restart_restore_original(
    tmp_path: Path, failure: str | int | None
) -> None:
    path = tmp_path / "settings.json"
    rig = Rig(path=path, count=1)
    await fresh_update(rig, 101)
    if failure is not None:
        rig.failures["0"] = failure
        await fresh_update(rig, 102)
        assert rig.remote["0"] == 101
        rig.failures.clear()
    await fresh_update(rig, 102)
    assert rig.remote["0"] == 102
    records = rig.settings.snapshot().planning_center_length_undo
    assert len(records) == 1
    assert (records[0].old_length, records[0].new_length) == (100, 102)
    restarted = SettingsService(SettingsFileStore(path), MemoryCredentialStore(), environ={})
    restarted.load()
    rig.settings = restarted
    rig.service = PlanningCenterLengthService(
        restarted, rig.state, rig.dispatcher, client_factory=rig.client
    )
    await rig.service.restore()
    assert rig.remote["0"] == 100
    assert not rig.service.undo_available
    assert rig.service.result.reload == "verified"


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("retry", [False, True])
async def test_uncertain_second_write_reconciles_for_retry_or_restore(
    committed: bool, retry: bool
) -> None:
    rig = Rig(count=1)
    await fresh_update(rig, 101)
    rig.failures["0"] = "timeout"
    await fresh_update(rig, 102)
    if committed:
        # A timeout cannot tell us whether the server applied the request.
        rig.remote["0"] = 102
    rig.failures.clear()
    if retry:
        await fresh_update(rig, 103)
        assert rig.remote["0"] == 103
    await rig.service.restore()
    assert rig.remote["0"] == 100
    assert not rig.service.undo_available


@pytest.mark.parametrize("uncertain", [False, True])
async def test_newer_operator_edit_never_overwritten_even_with_fresh_preview(
    uncertain: bool,
) -> None:
    rig = Rig(count=1)
    await fresh_update(rig, 101)
    if uncertain:
        rig.failures["0"] = "timeout"
        await fresh_update(rig, 102)
        rig.failures.clear()
    rig.remote["0"] = 999
    before = len([r for r in rig.requests if r.method == "PATCH"])
    await fresh_update(rig, 103)
    assert "Someone changed" in rig.service.result.message
    assert len([r for r in rig.requests if r.method == "PATCH"]) == before
    await rig.service.restore()
    assert rig.remote["0"] == 999
    assert not rig.service.undo_available


async def test_conflict_after_second_preview_preserves_original_undo() -> None:
    rig = Rig(count=1)
    await fresh_update(rig, 101)
    plan = (await rig.state.snapshot()).plan
    assert plan is not None
    preview = rig.service.preview_plan(plan, [102], "scan")
    rig.remote["0"] = 999
    await rig.service.confirm(preview.token, "scan")
    record = rig.settings.snapshot().planning_center_length_undo[0]
    assert (record.old_length, record.new_length) == (100, 101)
    assert rig.remote["0"] == 999

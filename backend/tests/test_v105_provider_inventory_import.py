"""1.0.5: "Import Downloads Started Elsewhere" is the operator's per-provider choice.

The inventory scan adds an account download this installation never bound as
its own transfer only while that provider's option is on (the default, the
inherited behaviour). Off, such a download is left to whoever started it,
while this installation's own transfers on the same account keep being
reconciled from the same scan.
"""
import importlib
from types import SimpleNamespace

import pytest

from transfers.models import Ownership, ResourceState
from test_universal_lifecycle import core, submit  # noqa: F401  (fixture)

DEBRID = ("alldebrid", "debridlink", "premiumize", "realdebrid", "torbox")


@pytest.mark.asyncio
async def test_off_a_download_started_elsewhere_is_not_imported(core):  # noqa: F811
    core.provider.imports_account_inventory = False
    theirs = core.provider.parcel("theirs", state=ResourceState.AVAILABLE, ownership=Ownership.OBSERVED)
    core.provider.inventory_items = (theirs.observation,)
    result = await core.engine.reconcile_inventory()
    assert result == ()
    assert tuple(await core.repository.active()) == ()
    assert await core.repository.historical_binding(theirs.observation.resource.id) is None

    # Turned back on, the next scan imports it as before.
    core.provider.imports_account_inventory = True
    await core.engine.reconcile_inventory()
    [transfer] = await core.repository.active()
    [(resource, _state, _pending)] = await core.repository.resources(transfer.id)
    assert resource.id == theirs.observation.resource.id


@pytest.mark.asyncio
async def test_off_this_installations_own_transfer_is_still_reconciled(core):  # noqa: F811
    mine = core.provider.parcel("mine")
    core.provider.responses = [mine]
    transfer = await submit(core, payload="mine")
    await core.engine.tick()
    [(_resource, before, _pending)] = await core.repository.resources(transfer.id)
    assert before == ResourceState.PREPARING

    core.provider.imports_account_inventory = False
    ready = core.provider.parcel("mine", state=ResourceState.AVAILABLE)
    theirs = core.provider.parcel("theirs", state=ResourceState.AVAILABLE, ownership=Ownership.OBSERVED)
    core.provider.inventory_items = (ready.observation, theirs.observation)
    await core.engine.reconcile_inventory()

    assert [item.id for item in await core.repository.active()] == [transfer.id]
    [(resource, after, _pending)] = await core.repository.resources(transfer.id)
    assert resource.id == mine.observation.resource.id and after == ResourceState.AVAILABLE


@pytest.mark.asyncio
async def test_a_provider_that_declares_nothing_keeps_importing(core):  # noqa: F811
    assert not hasattr(core.provider, "imports_account_inventory")
    theirs = core.provider.parcel("theirs", state=ResourceState.AVAILABLE, ownership=Ownership.OBSERVED)
    core.provider.inventory_items = (theirs.observation,)
    await core.engine.reconcile_inventory()
    assert len(await core.repository.active()) == 1


@pytest.mark.parametrize("integration", DEBRID)
@pytest.mark.parametrize("chosen", (True, False))
def test_each_debrid_provider_is_built_with_the_operators_choice(integration, chosen):
    definition = importlib.import_module(f"providers.{integration}.definition")
    options_type = definition.definition.options_model
    assert options_type().import_account_downloads is True       # on by default
    options = options_type(api_key="key", import_account_downloads=chosen)
    provider = definition.definition.factory(options, SimpleNamespace(commands=None))
    assert provider.imports_account_inventory is chosen


@pytest.mark.asyncio
async def test_off_a_deleted_transfers_resource_still_listed_is_still_cleaned_again(tmp_path, monkeypatch):
    # The switch withholds only new imports: what a Delete of this
    # installation's own transfer authorized is still owed on every pass that
    # lists the resource again (the gate sits after the historical binding).
    from test_v113_terminal_resource_ownership import cleanups, inventory_imports, listing, with_backup
    from transfers.models import CleanupAuthority
    lab, backup = await with_backup(tmp_path, monkeypatch)
    for provider in lab.providers.values():
        provider.imports_account_inventory = False
    provider = lab.providers["parcel-a"]
    await lab.engine.delete(lab.transfer.id, remote=True)
    assert len(cleanups(provider)) == 1
    provider.inventory_items = (listing(lab.primary), listing(backup))
    lab.providers["parcel-b"].inventory_items = (listing(backup),)
    for _ in range(2):
        await lab.engine.reconcile_inventory()
        await lab.engine.cleanup_pending()
        assert await inventory_imports(lab) == []
    assert cleanups(provider) == [(lab.primary.id, CleanupAuthority.USER_REQUEST)] * 3

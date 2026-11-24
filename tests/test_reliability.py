"""Regression tests for account refresh and climate writes."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.daikinone.client.client import DaikinOne
from custom_components.daikinone.client.models import DaikinThermostatMode, DaikinUserCredentials
from tests.test_mapping import AIR_HANDLER_DATA, OUTDOOR_UNIT_DATA, _make_device


@pytest.mark.asyncio
async def test_bad_device_does_not_block_healthy_thermostat():
    client = DaikinOne(DaikinUserCredentials("fixture", "fixture"))
    device = _make_device()
    client._transport.request = AsyncMock(return_value=[{"id": "unsupported"}, device])

    await client.update()

    assert client.get_thermostat(device["id"]).online


@pytest.mark.asyncio
async def test_missing_device_is_removed_and_cache_is_per_client():
    credentials = DaikinUserCredentials("fixture", "fixture")
    first, second = DaikinOne(credentials), DaikinOne(credentials)
    device = _make_device()
    first._transport.request = AsyncMock(side_effect=[[device], []])
    second._transport.request = AsyncMock(return_value=[])

    await first.update()
    await second.update()
    assert not second.get_thermostats()
    await first.update()
    assert not first.get_thermostats()


@pytest.mark.asyncio
async def test_failed_write_resumes_updates():
    from custom_components.daikinone.entity import DaikinOneEntity

    entity = object.__new__(DaikinOneEntity)
    entity._device = SimpleNamespace(id="fixture")
    entity._data = SimpleNamespace(write_lock=lambda _: asyncio.Lock())
    with pytest.raises(RuntimeError, match="simulated"):
        await entity.update_state_optimistically(
            AsyncMock(side_effect=RuntimeError("simulated")), lambda _: None, lambda _: True
        )

    assert not entity._updates_paused


@pytest.mark.asyncio
async def test_mode_change_issues_one_write():
    from custom_components.daikinone.climate import DaikinOneThermostat

    write = AsyncMock()

    async def run_operation(*, operation, **kwargs):
        await operation()

    entity = SimpleNamespace(
        _device=SimpleNamespace(id="fixture"),
        _data=SimpleNamespace(daikin=SimpleNamespace(set_thermostat_mode=write)),
        update_state_optimistically=run_operation,
    )

    await DaikinOneThermostat.set_thermostat_mode(entity, DaikinThermostatMode.HEAT)

    write.assert_awaited_once_with("fixture", DaikinThermostatMode.HEAT)


async def test_refresh_failure_is_shared_without_repeating_request(monkeypatch):
    from custom_components.daikinone import DaikinOneData
    from custom_components.daikinone.exceptions import DaikinServiceException

    now = 1
    monkeypatch.setattr("custom_components.daikinone.monotonic", lambda: now)
    update = AsyncMock(side_effect=[DaikinServiceException("simulated outage", 503), None])
    data = DaikinOneData(None, None, SimpleNamespace(update=update))

    for _ in range(2):
        with pytest.raises(DaikinServiceException, match="simulated outage"):
            await data.update()
    update.assert_awaited_once()
    now = 31
    await data.update()
    assert update.await_count == 2


async def test_bad_optional_air_quality_preserves_climate():
    client = DaikinOne(DaikinUserCredentials("fixture", "fixture"))
    device = _make_device()
    device["data"]["aqOutdoorAvailable"] = 1
    device["data"]["aqOutdoorLevel"] = 255
    client._transport.request = AsyncMock(return_value=[device])

    await client.update()

    assert client.get_thermostat(device["id"]).air_quality_outdoor is None


async def test_partial_range_confirms_only_changed_setpoint():
    from custom_components.daikinone.climate import DaikinOneThermostat
    from custom_components.daikinone.utils import Temperature

    client = DaikinOne(DaikinUserCredentials("fixture", "fixture"))
    device = _make_device()
    client._transport.request = AsyncMock(return_value=[device])
    await client.update()
    thermostat = client.get_thermostat(device["id"])
    thermostat.set_point_cool = Temperature.from_celsius(25)
    write = AsyncMock()

    async def verify_operation(*, operation, optimistic_update, check):
        await operation()
        optimistic_update(thermostat)
        assert check(thermostat)

    entity = SimpleNamespace(
        async_get_device=AsyncMock(return_value=thermostat),
        _data=SimpleNamespace(daikin=SimpleNamespace(set_thermostat_home_set_points=write)),
        update_state_optimistically=verify_operation,
    )
    await DaikinOneThermostat.async_set_temperature(entity, target_temp_low=19)
    write.assert_awaited_once_with(
        device["id"], heat=Temperature.from_celsius(19), cool=None, override_schedule=thermostat.schedule.enabled
    )


async def test_entry_migration_keeps_existing_entity_ids():
    from unittest.mock import Mock

    from custom_components.daikinone import async_migrate_entry
    from custom_components.daikinone.const import CONF_OPTION_ENTITY_UID_SCHEMA_VERSION_KEY

    entry = SimpleNamespace(version=1, minor_version=1, data={"email": "fixture"})
    update = Mock()
    hass = SimpleNamespace(config_entries=SimpleNamespace(async_update_entry=update))

    assert await async_migrate_entry(hass, entry)
    update.assert_called_once_with(
        entry, data={"email": "fixture", CONF_OPTION_ENTITY_UID_SCHEMA_VERSION_KEY: 0}, minor_version=2
    )


async def test_diagnostic_summary_excludes_account_and_device_data():
    from custom_components.daikinone.diagnostics import async_get_config_entry_diagnostics

    client = DaikinOne(DaikinUserCredentials("fixture", "fixture"))
    client._transport.request = AsyncMock(return_value=[_make_device()])
    await client.update()
    entry = SimpleNamespace(runtime_data=SimpleNamespace(daikin=client))

    assert await async_get_config_entry_diagnostics(None, entry) == {
        "thermostat_count": 1,
        "online_thermostat_count": 1,
        "equipment_count": 0,
    }
    client._transport.request.assert_awaited_once()


async def test_reauth_replaces_password_and_preserves_account(monkeypatch):
    from unittest.mock import Mock

    from custom_components.daikinone.config_flow import DaikinOneConfigFlow

    flow = DaikinOneConfigFlow()
    entry = SimpleNamespace(data={"email": "fixture@example.com", "password": "old"})
    check = AsyncMock(return_value=None)
    reload_and_abort = Mock(return_value={"type": "abort"})
    monkeypatch.setattr(flow, "_get_reauth_entry", lambda: entry)
    monkeypatch.setattr(flow, "_check_account", check)
    monkeypatch.setattr(flow, "async_update_reload_and_abort", reload_and_abort)

    assert await flow.async_step_reauth_confirm({"password": "new"}) == {"type": "abort"}
    check.assert_awaited_once_with("fixture@example.com", "new")
    reload_and_abort.assert_called_once_with(entry, data_updates={"password": "new"})


@pytest.mark.parametrize(
    "equipment",
    [
        {**AIR_HANDLER_DATA, "ctAHSerialNoCharacter1_15": None},
        {**OUTDOOR_UNIT_DATA, "ctOutdoorCompressorRunTime": 10**30},
    ],
)
async def test_invalid_equipment_value_does_not_block_other_thermostats(equipment):
    client = DaikinOne(DaikinUserCredentials("fixture", "fixture"))
    healthy = _make_device()
    bad = {**_make_device(equipment), "id": "bad"}
    client._transport.request = AsyncMock(return_value=[bad, healthy])

    await client.update()

    assert set(client.get_thermostats()) == {healthy["id"]}


async def test_all_malformed_records_leave_previous_snapshot_intact():
    from custom_components.daikinone.exceptions import DaikinServiceException

    client = DaikinOne(DaikinUserCredentials("fixture", "fixture"))
    healthy = _make_device()
    client._transport.request = AsyncMock(side_effect=[[healthy], [{"id": "malformed"}]])

    await client.update()
    with pytest.raises(DaikinServiceException, match="no usable thermostat records"):
        await client.update()
    assert set(client.get_thermostats()) == {healthy["id"]}


async def test_empty_startup_retries_instead_of_loading_empty_platforms(monkeypatch):
    from homeassistant.exceptions import ConfigEntryNotReady

    from custom_components.daikinone import async_setup_entry

    client = DaikinOne(DaikinUserCredentials("fixture", "fixture"))
    client._transport.request = AsyncMock(return_value=[])
    monkeypatch.setattr("custom_components.daikinone.DaikinOne", lambda *_: client)
    monkeypatch.setattr("custom_components.daikinone.async_get_clientsession", lambda _: None)
    forward = AsyncMock()
    hass = SimpleNamespace(config_entries=SimpleNamespace(async_forward_entry_setups=forward))
    entry = SimpleNamespace(data={"email": "fixture", "password": "fixture"})

    with pytest.raises(ConfigEntryNotReady, match="no thermostats"):
        await async_setup_entry(hass, entry)
    forward.assert_not_awaited()

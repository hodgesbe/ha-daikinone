"""DaikinOne orchestration: holds transport + thermostat cache, exposes the public API."""

import copy
import logging
from typing import Any

from aiohttp import ClientSession

from custom_components.daikinone.client.mapping import map_thermostat
from custom_components.daikinone.client.models import (
    DaikinThermostat,
    DaikinThermostatFanMode,
    DaikinThermostatFanSpeed,
    DaikinThermostatMode,
    DaikinUserCredentials,
)
from custom_components.daikinone.client.transport import DaikinTransport
from custom_components.daikinone.client.wire import (
    DAIKIN_API_URL_DEVICE_DATA,
    DaikinDeviceDataResponse,
)
from custom_components.daikinone.exceptions import DaikinServiceException
from custom_components.daikinone.utils import Temperature

log = logging.getLogger(__name__)


class DaikinOne:
    """Manages connection to Daikin API and fetching device data."""

    def __init__(self, creds: DaikinUserCredentials, session: ClientSession | None = None) -> None:
        self.__thermostats: dict[str, DaikinThermostat] = {}
        self._transport = DaikinTransport(creds, session)

    async def login(self) -> bool:
        return await self._transport.login()

    async def get_all_raw_device_data(self) -> list[dict[str, Any]]:
        """Get raw device data"""
        return await self._transport.request(DAIKIN_API_URL_DEVICE_DATA)

    async def get_raw_device_data(self, device_id: str) -> dict[str, Any]:
        """Get raw device data"""
        return await self._transport.request(f"{DAIKIN_API_URL_DEVICE_DATA}/{device_id}")

    async def update(self) -> None:
        raw = await self._transport.request(DAIKIN_API_URL_DEVICE_DATA)
        if not isinstance(raw, list):
            raise DaikinServiceException("Unexpected thermostat list from Daikin")
        thermostats: dict[str, DaikinThermostat] = {}
        skipped = 0
        for device in raw:
            try:
                response = DaikinDeviceDataResponse.model_validate(device)
                if not response.online and not response.data:
                    continue
                thermostats[response.id] = map_thermostat(response)
            except AttributeError, KeyError, OverflowError, TypeError, ValueError:
                skipped += 1
        if skipped and not thermostats:
            raise DaikinServiceException("Daikin returned no usable thermostat records")
        if skipped:
            log.warning("Skipped %s unsupported or incomplete Daikin device records", skipped)
        self.__thermostats = thermostats
        log.debug("Cached %s thermostats", len(thermostats))

    def get_thermostat(self, thermostat_id: str) -> DaikinThermostat:
        return copy.deepcopy(self.__thermostats[thermostat_id])

    def get_thermostats(self) -> dict[str, DaikinThermostat]:
        return copy.deepcopy(self.__thermostats)

    async def set_thermostat_mode(self, thermostat_id: str, mode: DaikinThermostatMode) -> None:
        """Set thermostat mode"""
        await self._transport.request(
            url=f"{DAIKIN_API_URL_DEVICE_DATA}/{thermostat_id}",
            method="PUT",
            body={"mode": mode.value},
        )

    async def set_thermostat_home_set_points(
        self,
        thermostat_id: str,
        heat: Temperature | None = None,
        cool: Temperature | None = None,
        override_schedule: bool = False,
    ) -> None:
        """Set thermostat home set points"""
        if not heat and not cool:
            raise ValueError("At least one of heat or cool set points must be set")

        payload: dict[str, Any] = {}
        if heat:
            payload["hspHome"] = heat.celsius
        if cool:
            payload["cspHome"] = cool.celsius
        if override_schedule:
            payload["schedOverride"] = 1

        await self._transport.request(
            url=f"{DAIKIN_API_URL_DEVICE_DATA}/{thermostat_id}",
            method="PUT",
            body=payload,
        )

    async def set_thermostat_fan_mode(self, thermostat_id: str, fan_mode: DaikinThermostatFanMode) -> None:
        """Set thermostat fan mode"""
        await self._transport.request(
            url=f"{DAIKIN_API_URL_DEVICE_DATA}/{thermostat_id}",
            method="PUT",
            body={"fanCirculate": fan_mode.value},
        )

    async def set_thermostat_fan_speed(self, thermostat_id: str, fan_speed: DaikinThermostatFanSpeed) -> None:
        """Set thermostat fan speed"""
        await self._transport.request(
            url=f"{DAIKIN_API_URL_DEVICE_DATA}/{thermostat_id}",
            method="PUT",
            body={"fanCirculateSpeed": fan_speed.value},
        )

import asyncio
import logging
from collections.abc import Awaitable, Callable

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from custom_components.daikinone import DaikinOneData
from custom_components.daikinone.client.models import DaikinDevice, DaikinEquipment, DaikinThermostat
from custom_components.daikinone.const import DOMAIN, MANUFACTURER
from custom_components.daikinone.exceptions import DaikinAuthError, DaikinServiceException

log = logging.getLogger(__name__)


class DaikinOneEntity[D: DaikinDevice](Entity):
    _device: D
    _data: DaikinOneData

    _updates_paused: bool = False

    def __init__(self, data: DaikinOneData, device: D):
        self._data = data
        self._device = device
        self._attr_device_info = self.get_device_info()

    def get_device_info(self) -> DeviceInfo | None:
        """Return device information for this sensor."""

        info = DeviceInfo(
            identifiers={(DOMAIN, self._device.id)},
            name=self.device_name,
            manufacturer=MANUFACTURER,
            model=self._device.model,
            sw_version=self._device.firmware_version,
        )

        if self.device_parent is not None:
            info["via_device"] = (DOMAIN, self.device_parent)

        return info

    @property
    def device_name(self) -> str:
        """Return the name of the device."""
        match self._device:
            case DaikinThermostat():
                return f"{self._device.name} Thermostat"
            case DaikinEquipment():
                thermostat = self._data.daikin.get_thermostat(self._device.thermostat_id)
                return f"{thermostat.name} {self._device.name}"
            case _:
                raise ValueError("unexpected device type when generating device name")

    @property
    def device_parent(self) -> str | None:
        """Return the name of the device."""
        match self._device:
            case DaikinEquipment():
                thermostat = self._data.daikin.get_thermostat(self._device.thermostat_id)
                return thermostat.id
            case _:
                return None

    async def async_get_device(self) -> D:
        """Get the latest state of the sensor."""
        raise NotImplementedError("DaikinOneEntity subclass has not implemented async_get_device")

    def update_entity_attributes(self) -> None:
        """Update the entity attributes. When this is called, the device has already been updated."""
        raise NotImplementedError("DaikinOneEntity subclass has not implemented update_entity_attributes")

    async def async_update(self, no_throttle: bool = False) -> None:
        """Gets the latest state of the entity."""
        if self._updates_paused:
            return

        log.debug("Updating daikinone entity %s for device %s", self.unique_id, self._device.id)
        try:
            await self._data.update(no_throttle=no_throttle)
        except DaikinAuthError:
            self._data.entry.async_start_reauth(self.hass)
            self._attr_available = False
            return
        except DaikinServiceException as err:
            log.debug("Daikin refresh failed: %s", err)
            self._attr_available = False
            return
        try:
            self._device = await self.async_get_device()
        except KeyError:
            # Backing device missing from the cache this refresh (e.g. equipment skipped
            # due to garbage identity strings). Mark unavailable and retain the last-known
            # _device so attributes recover cleanly on the next successful refresh.
            log.debug("Device %s missing this refresh, marking entity unavailable", self._device.id)
            self._attr_available = False
            return

        if isinstance(self._device, DaikinThermostat):
            self._attr_available = self._device.online
        elif isinstance(self._device, DaikinEquipment):
            self._attr_available = self._data.daikin.get_thermostat(self._device.thermostat_id).online
        self.update_entity_attributes()

    async def update_state_optimistically(
        self,
        operation: Callable[[], Awaitable[None]],
        optimistic_update: Callable[[D], None],
        check: Callable[[D], bool],
    ) -> None:
        """
        Executes the given state update optimistically, then waits for the API to update the state as well. Regularly
        scheduled updates are paused while waiting to avoid overwriting the optimistic update with stale data. A full
        entity update is scheduled at the end regardless of whether updated remote state was found or not.
        """
        thermostat_id = self._device.thermostat_id if isinstance(self._device, DaikinEquipment) else self._device.id
        async with self._data.write_lock(thermostat_id):
            self._updates_paused = True
            try:
                await operation()
                optimistic_update(self._device)
                self.update_entity_attributes()
                self.async_write_ha_state()
                try:
                    async with asyncio.timeout(10):
                        await self._wait_for_updated_value(check)
                except TimeoutError:
                    log.debug("Timed out waiting for thermostat state confirmation")
            finally:
                self._updates_paused = False

            await self.async_update(no_throttle=True)
            self.async_write_ha_state()

    async def _wait_for_updated_value(self, check: Callable[[D], bool]) -> None:
        """Poll until the cloud confirms the write or the caller's deadline expires."""
        while True:
            await self._data.update(no_throttle=True)
            if check(await self.async_get_device()):
                return
            await asyncio.sleep(1)

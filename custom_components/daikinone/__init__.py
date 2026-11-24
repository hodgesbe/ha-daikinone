import asyncio
import logging
from dataclasses import dataclass, field
from time import monotonic

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.daikinone.client.client import DaikinOne
from custom_components.daikinone.client.models import DaikinUserCredentials
from custom_components.daikinone.const import (
    CONF_OPTION_ENTITY_UID_SCHEMA_VERSION_KEY,
    MIN_TIME_BETWEEN_UPDATES,
    PLATFORMS,
)
from custom_components.daikinone.exceptions import DaikinAuthError, DaikinServiceException

log = logging.getLogger(__name__)


@dataclass
class DaikinOneData:
    _hass: HomeAssistant
    entry: ConfigEntry
    daikin: DaikinOne
    _refresh_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _write_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    _next_refresh: float = 0.0
    _refresh_error: tuple[type[DaikinServiceException], str, int | None] | None = None

    def write_lock(self, thermostat_id: str) -> asyncio.Lock:
        return self._write_locks.setdefault(thermostat_id, asyncio.Lock())

    async def update(self, no_throttle: bool = False) -> None:
        """Refresh once per polling interval and share the result with all entities."""
        async with self._refresh_lock:
            if not no_throttle and monotonic() < self._next_refresh:
                if self._refresh_error is not None:
                    error_type, message, status = self._refresh_error
                    raise error_type(message, status)
                return
            try:
                await self.daikin.update()
            except DaikinServiceException as err:
                self._next_refresh = monotonic() + MIN_TIME_BETWEEN_UPDATES.total_seconds()
                self._refresh_error = (type(err), str(err), err.status)
                raise
            else:
                self._refresh_error = None
                self._next_refresh = monotonic() + MIN_TIME_BETWEEN_UPDATES.total_seconds()


type DaikinOneConfigEntry = ConfigEntry[DaikinOneData]


async def async_setup_entry(hass: HomeAssistant, entry: DaikinOneConfigEntry) -> bool:
    """Set up the Daikin account."""
    credentials = DaikinUserCredentials(entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD])
    data = DaikinOneData(hass, entry, DaikinOne(credentials, async_get_clientsession(hass)))
    try:
        await data.update()
    except DaikinAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except DaikinServiceException as err:
        raise ConfigEntryNotReady(str(err)) from err
    entry.runtime_data = data
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: DaikinOneConfigEntry) -> bool:
    """Unload this account's platforms."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old entry."""
    log.debug("Migrating from version %s.%s", entry.version, entry.minor_version)

    if entry.version > 1:
        log.error(
            "Incompatible config entry version %s.%s; restore a backup or re-add the integration",
            entry.version,
            entry.minor_version,
        )
        return False

    if entry.minor_version < 2:
        new = {**entry.data, CONF_OPTION_ENTITY_UID_SCHEMA_VERSION_KEY: 0}
        hass.config_entries.async_update_entry(entry, data=new, minor_version=2)

    log.info("Migration to version %s.%s successful", entry.version, entry.minor_version)

    return True

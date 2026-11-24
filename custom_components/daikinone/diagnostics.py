"""Privacy-preserving diagnostics for Daikin One."""

from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from custom_components.daikinone import DaikinOneConfigEntry
from custom_components.daikinone.const import DOMAIN


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: DaikinOneConfigEntry) -> dict[str, int]:
    thermostats = entry.runtime_data.daikin.get_thermostats().values()
    return {
        "thermostat_count": len(thermostats),
        "online_thermostat_count": sum(thermostat.online for thermostat in thermostats),
        "equipment_count": sum(len(thermostat.equipment) for thermostat in thermostats),
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant, config_entry: DaikinOneConfigEntry, device: DeviceEntry
) -> dict[str, bool | int]:
    device_id = next((identifier for domain, identifier in device.identifiers if domain == DOMAIN), None)
    thermostat = config_entry.runtime_data.daikin.get_thermostats().get(device_id) if device_id else None
    if thermostat is None:
        return {"thermostat_in_cache": False}
    return {
        "thermostat_in_cache": True,
        "online": thermostat.online,
        "equipment_count": len(thermostat.equipment),
        "indoor_air_quality_available": thermostat.air_quality_indoor is not None,
        "outdoor_air_quality_available": thermostat.air_quality_outdoor is not None,
    }

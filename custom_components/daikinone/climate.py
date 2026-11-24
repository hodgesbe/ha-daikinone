import logging
from enum import Enum

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityDescription,
)
from homeassistant.components.climate.const import (
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    FAN_OFF,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from custom_components.daikinone import DaikinOneConfigEntry, DaikinOneData
from custom_components.daikinone.client.models import (
    DaikinThermostat,
    DaikinThermostatCapability,
    DaikinThermostatFanMode,
    DaikinThermostatMode,
    DaikinThermostatStatus,
)
from custom_components.daikinone.entity import DaikinOneEntity
from custom_components.daikinone.utils import Temperature

log = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: DaikinOneConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Daikin One thermostats"""
    data: DaikinOneData = config_entry.runtime_data

    entities = [
        DaikinOneThermostat(
            ClimateEntityDescription(key=device.id, has_entity_name=True, name=None),
            data,
            device,
        )
        for device in data.daikin.get_thermostats().values()
    ]

    async_add_entities(entities, True)


class DaikinOneThermostatPresetMode(Enum):
    NONE = "none"
    EMERGENCY_HEAT = "emergency_heat"


class DaikinOneThermostatFanMode(Enum):
    OFF = FAN_OFF
    ALWAYS_ON = "always_on"
    SCHEDULED = "schedule"


class DaikinOneThermostatFanSpeed(Enum):
    LOW = 0
    MEDIUM = 1
    HIGH = 2


class DaikinOneThermostat(DaikinOneEntity[DaikinThermostat], ClimateEntity):
    """Thermostat entity for Daikin One"""

    def __init__(
        self,
        description: ClimateEntityDescription,
        data: DaikinOneData,
        thermostat: DaikinThermostat,
    ):
        super().__init__(data, thermostat)

        self.entity_description = description

        self._attr_translation_key = "daikinone_thermostat"
        self._attr_unique_id = f"{self._device.id}-climate"
        self._attr_native_temperature_unit = UnitOfTemperature.CELSIUS
        self._attr_supported_features = (
            ClimateEntityFeature.TURN_ON
            | ClimateEntityFeature.TURN_OFF
            | ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
            | ClimateEntityFeature.FAN_MODE
        )
        self._attr_hvac_modes = self.get_hvac_modes()
        self._attr_fan_modes = [m.value for m in DaikinOneThermostatFanMode]

        # These attributes must be initialized otherwise HA `CachedProperties` doesn't create a
        # backing prop. If they are not initialized, climate will error during setup because we support
        # TARGET_TEMPERATURE_RANGE and it tries to read them. These attributes are not initialized in
        # `ClimateEntity` like most others, and in a case where the thermostat is not set to auto,
        # they do not get set in async_update either.
        self._attr_native_target_temperature_low = None
        self._attr_native_target_temperature_high = None

        # Set up preset modes based on thermostat capabilities. The preset climate feature will only be
        # enabled if at least one preset is detected as supported.
        self._attr_preset_modes = [DaikinOneThermostatPresetMode.NONE.value]
        self._attr_preset_mode = None

        if DaikinThermostatCapability.EMERGENCY_HEAT in self._device.capabilities:
            self._attr_supported_features |= ClimateEntityFeature.PRESET_MODE
            self._attr_preset_modes += [DaikinOneThermostatPresetMode.EMERGENCY_HEAT.value]

    def get_hvac_modes(self) -> list[HVACMode]:
        modes: list[HVACMode] = []

        if (
            DaikinThermostatCapability.HEAT in self._device.capabilities
            and DaikinThermostatCapability.COOL in self._device.capabilities
        ):
            modes.append(HVACMode.HEAT_COOL)

        if DaikinThermostatCapability.HEAT in self._device.capabilities:
            modes.append(HVACMode.HEAT)
        if DaikinThermostatCapability.COOL in self._device.capabilities:
            modes.append(HVACMode.COOL)

        modes.append(HVACMode.OFF)

        return modes

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new target hvac mode."""
        target_mode: DaikinThermostatMode
        match hvac_mode:
            case HVACMode.HEAT_COOL:
                target_mode = DaikinThermostatMode.AUTO
            case HVACMode.HEAT:
                target_mode = DaikinThermostatMode.HEAT
            case HVACMode.COOL:
                target_mode = DaikinThermostatMode.COOL
            case HVACMode.OFF:
                target_mode = DaikinThermostatMode.OFF
            case _:
                raise ValueError(f"Attempted to set unsupported HVAC mode: {hvac_mode}")

        await self.set_thermostat_mode(target_mode)

    async def set_thermostat_mode(self, target_mode: DaikinThermostatMode) -> None:
        log.debug("Setting thermostat mode to %s", target_mode)

        # update thermostat mode optimistically
        def update(t: DaikinThermostat):
            t.mode = target_mode

        await self.update_state_optimistically(
            operation=lambda: self._data.daikin.set_thermostat_mode(self._device.id, target_mode),
            optimistic_update=update,
            check=lambda t: t.mode == target_mode,
        )

    async def async_set_preset_mode(self, preset_mode: str):
        """Set new target preset mode."""
        match preset_mode:
            case DaikinOneThermostatPresetMode.EMERGENCY_HEAT.value:
                await self.set_thermostat_mode(DaikinThermostatMode.AUX_HEAT)

            case DaikinOneThermostatPresetMode.NONE.value:
                match self._device.mode:
                    # turning off emergency heat should set the thermostat mode to heat
                    case DaikinThermostatMode.AUX_HEAT:
                        await self.set_thermostat_mode(DaikinThermostatMode.HEAT)

                    # any other thermostat mode should already be "none", and if its not,
                    # we don't need to do anything
                    case _:
                        pass

            case _:
                raise ValueError(f"Attempted to set unsupported preset mode: {preset_mode}")

    async def async_set_temperature(self, **kwargs: float) -> None:
        """Set a single setpoint or update the automatic mode range."""
        heat: Temperature | None = None
        cool: Temperature | None = None

        async def operation() -> None:
            nonlocal heat, cool
            try:
                thermostat = await self.async_get_device()
            except KeyError:
                raise ServiceValidationError("Daikin thermostat is unavailable") from None
            if not thermostat.online:
                raise ServiceValidationError("Daikin thermostat is offline")

            low = kwargs.get(ATTR_TARGET_TEMP_LOW)
            high = kwargs.get(ATTR_TARGET_TEMP_HIGH)
            target = kwargs.get(ATTR_TEMPERATURE)
            if low is not None or high is not None:
                heat = Temperature.from_celsius(low) if low is not None else None
                cool = Temperature.from_celsius(high) if high is not None else None
                effective_heat = heat if heat is not None else thermostat.set_point_heat
                effective_cool = cool if cool is not None else thermostat.set_point_cool
                if (
                    effective_heat is not None
                    and effective_cool is not None
                    and effective_heat.celsius >= effective_cool.celsius
                ):
                    raise ServiceValidationError("Heating setpoint must be below the cooling setpoint")
            elif target is not None:
                match thermostat.mode:
                    case DaikinThermostatMode.HEAT | DaikinThermostatMode.AUX_HEAT:
                        heat = Temperature.from_celsius(target)
                    case DaikinThermostatMode.COOL:
                        cool = Temperature.from_celsius(target)
                    case _:
                        raise ServiceValidationError("A single setpoint requires heating or cooling mode")
            else:
                raise ServiceValidationError("At least one setpoint is required")

            await self._data.daikin.set_thermostat_home_set_points(
                thermostat.id, heat=heat, cool=cool, override_schedule=thermostat.schedule.enabled
            )

        def update(thermostat: DaikinThermostat) -> None:
            if heat is not None:
                thermostat.set_point_heat = heat
            if cool is not None:
                thermostat.set_point_cool = cool

        await self.update_state_optimistically(
            operation=operation,
            optimistic_update=update,
            check=lambda t: (heat is None or t.set_point_heat == heat) and (cool is None or t.set_point_cool == cool),
        )

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        target_fan_mode: DaikinThermostatFanMode
        match fan_mode:
            case DaikinOneThermostatFanMode.OFF.value:
                target_fan_mode = DaikinThermostatFanMode.OFF
            case DaikinOneThermostatFanMode.ALWAYS_ON.value:
                target_fan_mode = DaikinThermostatFanMode.ALWAYS_ON
            case DaikinOneThermostatFanMode.SCHEDULED.value:
                target_fan_mode = DaikinThermostatFanMode.SCHEDULED
            case _:
                raise ValueError(f"Attempted to set unsupported fan mode: {fan_mode}")

        # update fan mode optimistically
        def update(t: DaikinThermostat):
            t.fan_mode = target_fan_mode

        await self.update_state_optimistically(
            operation=lambda: self._data.daikin.set_thermostat_fan_mode(self._device.id, target_fan_mode),
            optimistic_update=update,
            check=lambda t: t.fan_mode == target_fan_mode,
        )

    async def async_get_device(self) -> DaikinThermostat:
        return self._data.daikin.get_thermostat(self._device.id)

    def update_entity_attributes(self) -> None:
        self._attr_available = self._device.online
        self._attr_native_current_temperature = (
            self._device.indoor_temperature.celsius if self._device.indoor_temperature else None
        )
        self._attr_current_humidity = self._device.indoor_humidity

        # hvac current mode and preset
        self._attr_preset_mode = DaikinOneThermostatPresetMode.NONE.value
        match self._device.mode:
            case DaikinThermostatMode.AUTO:
                self._attr_hvac_mode = HVACMode.HEAT_COOL
            case DaikinThermostatMode.HEAT:
                self._attr_hvac_mode = HVACMode.HEAT
            case DaikinThermostatMode.COOL:
                self._attr_hvac_mode = HVACMode.COOL
            case DaikinThermostatMode.AUX_HEAT:
                self._attr_hvac_mode = HVACMode.HEAT
                self._attr_preset_mode = DaikinOneThermostatPresetMode.EMERGENCY_HEAT.value
            case DaikinThermostatMode.OFF:
                self._attr_hvac_mode = HVACMode.OFF

        # hvac current action
        match self._device.status:
            case DaikinThermostatStatus.HEATING:
                self._attr_hvac_action = HVACAction.HEATING
            case DaikinThermostatStatus.COOLING:
                self._attr_hvac_action = HVACAction.COOLING
            case DaikinThermostatStatus.CIRCULATING_AIR:
                self._attr_hvac_action = HVACAction.FAN
            case DaikinThermostatStatus.DRYING:
                self._attr_hvac_action = HVACAction.DRYING
            case DaikinThermostatStatus.IDLE:
                self._attr_hvac_action = HVACAction.IDLE

        # target temperature

        # reset target temperature attributes first, single target temp takes precedence and can conflict with range
        self._attr_native_target_temperature = None
        self._attr_native_target_temperature_low = None
        self._attr_native_target_temperature_high = None

        match self._device.mode:
            case DaikinThermostatMode.HEAT | DaikinThermostatMode.AUX_HEAT:
                self._attr_native_target_temperature = (
                    self._device.set_point_heat.celsius if self._device.set_point_heat else None
                )
            case DaikinThermostatMode.COOL:
                self._attr_native_target_temperature = (
                    self._device.set_point_cool.celsius if self._device.set_point_cool else None
                )
            case DaikinThermostatMode.AUTO:
                self._attr_native_target_temperature_low = (
                    self._device.set_point_heat.celsius if self._device.set_point_heat else None
                )
                self._attr_native_target_temperature_high = (
                    self._device.set_point_cool.celsius if self._device.set_point_cool else None
                )
            case _:
                pass

        # temperature bounds
        heat_min = self._device.set_point_heat_min.celsius if self._device.set_point_heat_min else None
        cool_min = self._device.set_point_cool_min.celsius if self._device.set_point_cool_min else None
        heat_max = self._device.set_point_heat_max.celsius if self._device.set_point_heat_max else None
        cool_max = self._device.set_point_cool_max.celsius if self._device.set_point_cool_max else None

        # these should be the same but just in case, take the larger of the two for the min
        if heat_min is not None and cool_min is not None:
            self._attr_min_temp = max(heat_min, cool_min)
        # these should be the same but just in case, take the smaller of the two for the max
        if heat_max is not None and cool_max is not None:
            self._attr_max_temp = min(heat_max, cool_max)

        # fan settings
        match self._device.fan_mode:
            case DaikinThermostatFanMode.OFF:
                self._attr_fan_mode = DaikinOneThermostatFanMode.OFF.value
            case DaikinThermostatFanMode.ALWAYS_ON:
                self._attr_fan_mode = DaikinOneThermostatFanMode.ALWAYS_ON.value
            case DaikinThermostatFanMode.SCHEDULED:
                self._attr_fan_mode = DaikinOneThermostatFanMode.SCHEDULED.value

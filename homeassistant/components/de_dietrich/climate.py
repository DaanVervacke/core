"""Support for De Dietrich heating-circuit climate entities."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, cast, override

from diematic_modbus import Diematic, DiematicISystem, HeatingMode
from modbus_connection import ModbusError

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityDescription,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import DeDietrichConfigEntry, DeDietrichDataUpdateCoordinator
from .entity import DeDietrichEntity, DeDietrichEntityDescription

PARALLEL_UPDATES = 0

PRESET_AUTO = "auto"
PRESET_DAY = "day"
PRESET_NIGHT = "night"
PRESET_ANTIFREEZE = "antifreeze"

PRESET_MODES = [PRESET_AUTO, PRESET_DAY, PRESET_NIGHT, PRESET_ANTIFREEZE]

MODE_TO_PRESET: dict[HeatingMode, str] = {
    HeatingMode.AUTO: PRESET_AUTO,
    HeatingMode.TEMP_DAY: PRESET_DAY,
    HeatingMode.PERM_DAY: PRESET_DAY,
    HeatingMode.TEMP_NIGHT: PRESET_NIGHT,
    HeatingMode.PERM_NIGHT: PRESET_NIGHT,
    HeatingMode.ANTIFREEZE: PRESET_ANTIFREEZE,
    HeatingMode.HOLIDAY: PRESET_ANTIFREEZE,
}

PRESET_TO_MODE: dict[str, HeatingMode] = {
    PRESET_AUTO: HeatingMode.AUTO,
    PRESET_DAY: HeatingMode.PERM_DAY,
    PRESET_NIGHT: HeatingMode.PERM_NIGHT,
    PRESET_ANTIFREEZE: HeatingMode.ANTIFREEZE,
}

PRESET_TARGET_FIELD: dict[str, str] = {
    PRESET_DAY: "day_target",
    PRESET_NIGHT: "night_target",
    PRESET_ANTIFREEZE: "antifreeze_target",
}

_ISYSTEM_ANTIFREEZE_RANGE = (3.0, 20.0)
_ISYSTEM_ZONE_RANGE = (10.0, 30.0)
_BASE_RANGE = (5.0, 30.0)


class _Circuit(Protocol):
    """The subset of a circuit bundle's interface the climate entity needs. pump_on and permanent_derogation aren't universal across circuits, so those are read with getattr() instead."""

    room_temp: float | None
    calc_temp: float | None
    mode: HeatingMode | int | None
    day_target: float | None
    night_target: float | None
    antifreeze_target: float | None

    async def write(self, field: str, value: Any) -> None: ...


@dataclass(frozen=True, kw_only=True)
class DeDietrichClimateEntityDescription(
    DeDietrichEntityDescription, ClimateEntityDescription
):
    """Describe a De Dietrich heating-circuit climate entity."""

    circuit_fn: Callable[[Diematic | DiematicISystem], _Circuit]
    mode_setter_fn: Callable[
        [Diematic | DiematicISystem], Callable[[HeatingMode], Awaitable[None]]
    ]


CLIMATE_DESCRIPTIONS: tuple[DeDietrichClimateEntityDescription, ...] = (
    DeDietrichClimateEntityDescription(
        key="circuit_a",
        component="circuit_a",
        translation_key="circuit",
        circuit_fn=lambda device: device.circuit_a,
        mode_setter_fn=lambda device: device.set_circuit_a_mode,
    ),
    DeDietrichClimateEntityDescription(
        key="circuit_b",
        component="circuit_b",
        translation_key="circuit",
        circuit_fn=lambda device: device.circuit_b,
        mode_setter_fn=lambda device: device.set_circuit_b_mode,
    ),
    DeDietrichClimateEntityDescription(
        key="circuit_c",
        component="circuit_c",
        translation_key="circuit",
        circuit_fn=lambda device: cast(DiematicISystem, device).circuit_c,
        mode_setter_fn=lambda device: device.set_circuit_c_mode,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DeDietrichConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the De Dietrich climate platform."""
    coordinator = entry.runtime_data
    async_add_entities(
        DeDietrichClimate(coordinator, description)
        for description in CLIMATE_DESCRIPTIONS
        if coordinator.bundle_present(description.component)
    )


class DeDietrichClimate(DeDietrichEntity, ClimateEntity):
    """A heating circuit exposed as a climate entity."""

    _attr_hvac_mode = HVACMode.HEAT
    _attr_hvac_modes = [HVACMode.HEAT]
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 0.5
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.PRESET_MODE
    )
    _attr_preset_modes = PRESET_MODES
    _attr_translation_key = "circuit"
    entity_description: DeDietrichClimateEntityDescription

    def __init__(
        self,
        coordinator: DeDietrichDataUpdateCoordinator,
        entity_description: DeDietrichClimateEntityDescription,
    ) -> None:
        """Initialize the climate entity."""
        super().__init__(coordinator, entity_description)
        self._is_isystem = isinstance(coordinator.device, DiematicISystem)

    @property
    def _circuit(self) -> _Circuit:
        """Return the circuit bundle this entity reads from and writes to."""
        return self.entity_description.circuit_fn(self.coordinator.device)

    @property
    @override
    def preset_mode(self) -> str | None:
        """Return the current preset, or None for an unmapped raw mode."""
        mode = self._circuit.mode
        if isinstance(mode, HeatingMode):
            return MODE_TO_PRESET.get(mode)
        return None

    @property
    @override
    def current_temperature(self) -> float | None:
        """Return the circuit's room temperature."""
        return self._circuit.room_temp

    @property
    @override
    def target_temperature(self) -> float | None:
        """Return the preset-relative setpoint, or the calculated target in auto."""
        preset = self.preset_mode
        if preset is None or preset == PRESET_AUTO:
            return self._circuit.calc_temp
        return getattr(self._circuit, PRESET_TARGET_FIELD[preset])

    @property
    @override
    def min_temp(self) -> float:
        """Return the minimum settable temperature for the current preset."""
        return self._temperature_range()[0]

    @property
    @override
    def max_temp(self) -> float:
        """Return the maximum settable temperature for the current preset."""
        return self._temperature_range()[1]

    def _temperature_range(self) -> tuple[float, float]:
        """Return the (min, max) temperature range for the current layout and preset."""
        if not self._is_isystem:
            return _BASE_RANGE
        if self.preset_mode == PRESET_ANTIFREEZE:
            return _ISYSTEM_ANTIFREEZE_RANGE
        return _ISYSTEM_ZONE_RANGE

    @property
    @override
    def hvac_action(self) -> HVACAction | None:
        """Return whether the circuit's pump is running, when the circuit has one."""
        pump_on = getattr(self._circuit, "pump_on", None)
        if pump_on is None:
            return None
        return HVACAction.HEATING if pump_on else HVACAction.IDLE

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose the iSystem permanent-override flag, when the layout reports it."""
        derogation = getattr(self._circuit, "permanent_derogation", None)
        if derogation is None:
            return None
        return {"permanent_override": derogation}

    @override
    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Write the circuit mode register matching the requested preset."""
        mode = PRESET_TO_MODE.get(preset_mode)
        if mode is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="set_preset_mode_error",
            )
        setter = self.entity_description.mode_setter_fn(self.coordinator.device)
        try:
            await setter(mode)
        except ModbusError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="set_preset_mode_error",
            ) from err
        await self.coordinator.async_request_refresh()

    @override
    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Write the requested setpoint to the current preset's target register."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        preset = self.preset_mode
        if preset is None or preset == PRESET_AUTO:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="set_circuit_temperature_auto_error",
            )
        try:
            await self._circuit.write(PRESET_TARGET_FIELD[preset], temperature)
        except (ModbusError, ValueError) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="set_circuit_temperature_error",
            ) from err
        await self.coordinator.async_request_refresh()

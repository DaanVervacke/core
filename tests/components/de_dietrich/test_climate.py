"""Test the De Dietrich climate platform."""

from unittest.mock import patch

from diematic_modbus import HeatingMode
from modbus_connection import ModbusTimeoutError
from modbus_connection.mock import MockModbusConnection
import pytest

from homeassistant.components.climate import (
    ATTR_PRESET_MODE,
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_PRESET_MODE,
    SERVICE_SET_TEMPERATURE,
    HVACAction,
    HVACMode,
)
from homeassistant.components.de_dietrich.const import DEFAULT_UNIT_ID, DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from . import seed_boiler

from tests.common import MockConfigEntry

CIRCUIT_A_ENTITY_ID = "climate.de_dietrich_heating_circuit_a"


async def _setup(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_connection: MockModbusConnection,
) -> None:
    """Set up the config entry against the given mock connection."""
    mock_config_entry.add_to_hass(hass)
    with patch(
        "homeassistant.components.de_dietrich.async_get_unit",
        side_effect=lambda hass, entry, params, unit_id: mock_connection.for_unit(
            unit_id
        ),
    ):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)


async def test_circuit_entities_present_by_default(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_connection: MockModbusConnection,
    entity_registry: er.EntityRegistry,
) -> None:
    """Test circuit A and B climate entities exist and circuit C does not.

    The shared iSystem fixture only seeds live readings for circuits A and B;
    circuit C's registers are explicitly zapped here to the no-sensor code,
    matching how an uninstalled circuit reports on real hardware.
    """
    unit = mock_connection.for_unit(DEFAULT_UNIT_ID)
    for register in (618, 619):  # circuit_c room_temp, calc_temp
        unit.holding[register] = 0xFFFF
    await _setup(hass, mock_config_entry, mock_connection)

    for component in ("circuit_a", "circuit_b"):
        entity_id = entity_registry.async_get_entity_id(
            CLIMATE_DOMAIN, DOMAIN, f"{mock_config_entry.entry_id}_{component}"
        )
        assert entity_id is not None, f"Missing climate entity for {component}"

    circuit_c_entity_id = entity_registry.async_get_entity_id(
        CLIMATE_DOMAIN, DOMAIN, f"{mock_config_entry.entry_id}_circuit_c"
    )
    assert circuit_c_entity_id is None


async def test_climate_default_state(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
) -> None:
    """Test the default state exposed for a circuit's climate entity."""
    state = hass.states.get(CIRCUIT_A_ENTITY_ID)
    assert state is not None
    assert state.attributes["current_temperature"] == 21.0
    assert state.state == HVACMode.HEAT
    assert state.attributes["hvac_action"] == HVACAction.IDLE


async def test_set_preset_mode_writes_boiler(
    hass: HomeAssistant,
    mock_connection: MockModbusConnection,
    init_integration: MockConfigEntry,
) -> None:
    """Test set_preset_mode to day writes HeatingMode.PERM_DAY to the mode register."""
    unit = mock_connection.for_unit(DEFAULT_UNIT_ID)
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_PRESET_MODE,
        {ATTR_PRESET_MODE: "day"},
        target={ATTR_ENTITY_ID: CIRCUIT_A_ENTITY_ID},
        blocking=True,
    )
    # iSystem circuit A mode register 653, heating bits masked by 0x2F.
    assert (unit.holding[653] & 0x2F) == HeatingMode.PERM_DAY


async def test_set_preset_mode_unknown_raises(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
) -> None:
    """Test an unknown preset string raises HomeAssistantError."""
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_PRESET_MODE,
            {ATTR_PRESET_MODE: "bogus"},
            target={ATTR_ENTITY_ID: CIRCUIT_A_ENTITY_ID},
            blocking=True,
        )


async def test_set_preset_mode_translates_modbus_error(
    hass: HomeAssistant,
    mock_connection: MockModbusConnection,
    init_integration: MockConfigEntry,
) -> None:
    """Test a ModbusError from the mode setter surfaces as HomeAssistantError."""
    mock_connection.for_unit(DEFAULT_UNIT_ID).fail_write(
        653, ModbusTimeoutError("boom")
    )
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_PRESET_MODE,
            {ATTR_PRESET_MODE: "day"},
            target={ATTR_ENTITY_ID: CIRCUIT_A_ENTITY_ID},
            blocking=True,
        )


async def test_set_temperature_writes_day_target(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_connection: MockModbusConnection,
) -> None:
    """Test set_temperature in the day preset writes the day-target register."""
    unit = mock_connection.for_unit(DEFAULT_UNIT_ID)
    unit.holding[653] = HeatingMode.PERM_DAY
    await _setup(hass, mock_config_entry, mock_connection)

    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {ATTR_TEMPERATURE: 22.5},
        target={ATTR_ENTITY_ID: CIRCUIT_A_ENTITY_ID},
        blocking=True,
    )
    # iSystem circuit A day_target register 650, float10 scaled by 10.
    assert unit.holding[650] == 225


async def test_set_temperature_in_auto_raises(
    hass: HomeAssistant,
    mock_connection: MockModbusConnection,
    init_integration: MockConfigEntry,
) -> None:
    """Test set_temperature in the auto preset raises HomeAssistantError."""
    unit = mock_connection.for_unit(DEFAULT_UNIT_ID)
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            {ATTR_TEMPERATURE: 22.5},
            target={ATTR_ENTITY_ID: CIRCUIT_A_ENTITY_ID},
            blocking=True,
        )
    assert unit.holding.get(650) != 225


async def test_set_temperature_translates_modbus_error(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_connection: MockModbusConnection,
) -> None:
    """Test a ModbusError from the temperature write surfaces as HomeAssistantError."""
    unit = mock_connection.for_unit(DEFAULT_UNIT_ID)
    unit.holding[653] = HeatingMode.PERM_DAY
    unit.fail_write(650, ModbusTimeoutError("boom"))
    await _setup(hass, mock_config_entry, mock_connection)

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            {ATTR_TEMPERATURE: 22.5},
            target={ATTR_ENTITY_ID: CIRCUIT_A_ENTITY_ID},
            blocking=True,
        )


async def test_default_temperature_range(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
) -> None:
    """Test min/max/step on the default (iSystem, non-antifreeze) entity."""
    state = hass.states.get(CIRCUIT_A_ENTITY_ID)
    assert state is not None
    assert state.attributes["min_temp"] == 10.0
    assert state.attributes["max_temp"] == 30.0
    assert state.attributes["target_temp_step"] == 0.5


async def test_isystem_antifreeze_temperature_range(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_connection: MockModbusConnection,
) -> None:
    """Test min/max on the iSystem antifreeze preset are 3.0/20.0."""
    unit = mock_connection.for_unit(DEFAULT_UNIT_ID)
    unit.holding[653] = HeatingMode.ANTIFREEZE
    await _setup(hass, mock_config_entry, mock_connection)

    state = hass.states.get(CIRCUIT_A_ENTITY_ID)
    assert state is not None
    assert state.attributes["min_temp"] == 3.0
    assert state.attributes["max_temp"] == 20.0


async def test_circuit_a_skipped_when_absent(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_connection: MockModbusConnection,
    entity_registry: er.EntityRegistry,
) -> None:
    """Test no climate entity is created when circuit A has no live readings."""
    unit = mock_connection.for_unit(DEFAULT_UNIT_ID)
    for register in (614, 615, 621):  # circuit_a room_temp, calc_temp, supply_temp
        unit.holding[register] = 0xFFFF
    await _setup(hass, mock_config_entry, mock_connection)

    entity_id = entity_registry.async_get_entity_id(
        CLIMATE_DOMAIN, DOMAIN, f"{mock_config_entry.entry_id}_circuit_a"
    )
    assert entity_id is None


@pytest.mark.parametrize(
    "mode",
    [
        pytest.param(HeatingMode.PERM_DAY, id="day"),
        pytest.param(HeatingMode.ANTIFREEZE, id="antifreeze"),
    ],
)
async def test_base_layout_temperature_range(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_connection: MockModbusConnection,
    mode: HeatingMode,
) -> None:
    """Test min/max on a base-layout boiler are 5.0/30.0 regardless of preset."""
    unit = mock_connection.for_unit(DEFAULT_UNIT_ID)
    seed_boiler(unit)
    unit.holding[17] = mode  # circuit A mode register, base layout
    await _setup(hass, mock_config_entry, mock_connection)

    state = hass.states.get(CIRCUIT_A_ENTITY_ID)
    assert state is not None
    assert state.attributes["min_temp"] == 5.0
    assert state.attributes["max_temp"] == 30.0


async def test_base_layout_has_no_circuit_c(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_connection: MockModbusConnection,
    entity_registry: er.EntityRegistry,
) -> None:
    """Test a base-layout boiler never exposes a circuit C climate entity."""
    seed_boiler(mock_connection.for_unit(DEFAULT_UNIT_ID))
    await _setup(hass, mock_config_entry, mock_connection)

    entity_id = entity_registry.async_get_entity_id(
        CLIMATE_DOMAIN, DOMAIN, f"{mock_config_entry.entry_id}_circuit_c"
    )
    assert entity_id is None

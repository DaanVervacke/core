"""Test the ENGIE Belgium usage history import."""

from datetime import date, datetime
from functools import partial
from typing import Any
from unittest.mock import MagicMock

from aioengiebelgium import (
    EngieBeAuthenticationError,
    EngieBeCommunicationError,
    EngieBeError,
    MeteringServicePoint,
    ServicePointsResponse,
    UsageGranularity,
)
from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant.components.recorder.common import async_wait_recording_done
from syrupy.assertion import SnapshotAssertion

from homeassistant.components.engie_be.const import (
    CONF_HISTORY,
    DOMAIN,
    STATISTICS_SCAN_INTERVAL,
    HistoryDepth,
)
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    get_metadata,
    statistics_during_period,
)
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .conftest import (
    BAN,
    BRUSSELS_TIME_ZONE,
    DAILY_FROM,
    GAS_QUARTER_HOURLY_FROM,
    GAS_STATISTIC_ID,
    OFFTAKE_INJECTION_EAN,
    OFFTAKE_ONLY_EAN,
    OFFTAKE_STATISTIC_ID,
    SUBJECT,
    UsageScenario,
    build_config_entry,
    build_measured_point,
    build_usage_details,
)

GAS_COST_STATISTIC_ID = f"{DOMAIN}:{BAN}_gas_cost"
HIGH_LOAD_STATISTIC_ID = f"{DOMAIN}:{BAN}_electricity_offtake_high_load_hours"
TODAY = date(2026, 10, 3)


async def _setup_with_history(
    hass: HomeAssistant, depth: HistoryDepth = HistoryDepth.ALL
) -> MockConfigEntry:
    """Set up an entry that imports usage history and wait for the import."""
    entry = build_config_entry(history=depth)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)
    return entry


async def _run_scheduled_import(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Move to the next scheduled import and wait for it to be recorded."""
    freezer.tick(STATISTICS_SCAN_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)


async def _hourly_rows(hass: HomeAssistant) -> dict[str, list[dict[str, Any]]]:
    """Return every stored hourly row of the ENGIE Belgium statistics."""
    instance = get_instance(hass)
    metadata = await instance.async_add_executor_job(
        partial(get_metadata, hass, statistic_source=DOMAIN)
    )
    return await instance.async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2020, 1, 1, tzinfo=dt_util.UTC),
        None,
        set(metadata),
        "hour",
        None,
        {"state", "sum"},
    )


def _summary(
    rows: dict[str, list[dict[str, Any]]], statistic_ids: set[str] | None = None
) -> dict[str, list[str]]:
    """Return the rows as readable local start, state and sum lines."""
    return {
        statistic_id: [
            f"{dt_util.utc_from_timestamp(row['start']).astimezone(BRUSSELS_TIME_ZONE).isoformat()}"
            f" state={row['state']:.3f} sum={row['sum']:.3f}"
            for row in statistic_rows
        ]
        for statistic_id, statistic_rows in sorted(rows.items())
        if statistic_ids is None or statistic_id in statistic_ids
    }


def _sums_and_totals(
    rows: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, float], dict[str, float]]:
    """Return the last sum and the total of all states of every statistic."""
    return (
        {statistic_id: rows_[-1]["sum"] for statistic_id, rows_ in rows.items()},
        {
            statistic_id: pytest.approx(sum(row["state"] for row in rows_))
            for statistic_id, rows_ in rows.items()
        },
    )


def _use_scenario(client: MagicMock, scenario: UsageScenario) -> None:
    """Make the mocked API return the usage of the scenario."""
    client.async_get_usage_details.side_effect = partial(
        build_usage_details, scenario=scenario
    )


def _use_service_points(client: MagicMock, *points: MeteringServicePoint) -> None:
    """Make the mocked API return the given service points."""
    client.async_get_service_points.side_effect = None
    client.async_get_service_points.return_value = ServicePointsResponse(items=points)


@pytest.mark.usefixtures("frozen_afternoon", "mock_engie_client")
async def test_backfill(hass: HomeAssistant, snapshot: SnapshotAssertion) -> None:
    """Test the full history is imported with daily and hourly buckets."""
    await _setup_with_history(hass)

    metadata = await get_instance(hass).async_add_executor_job(
        partial(get_metadata, hass, statistic_source=DOMAIN)
    )
    assert {
        statistic_id: meta for statistic_id, (_, meta) in sorted(metadata.items())
    } == snapshot(name="metadata")
    assert _summary(await _hourly_rows(hass)) == snapshot(name="statistics")


@pytest.mark.parametrize(
    ("depth", "expected_start"),
    [
        pytest.param(HistoryDepth.MONTH, date(2026, 9, 2), id="month"),
        pytest.param(HistoryDepth.YEAR, date(2025, 10, 3), id="year"),
        pytest.param(HistoryDepth.ALL, date(2023, 10, 4), id="all"),
    ],
)
@pytest.mark.usefixtures("frozen_afternoon")
async def test_backfill_depth_without_data_availability(
    hass: HomeAssistant,
    mock_engie_client: MagicMock,
    depth: HistoryDepth,
    expected_start: date,
) -> None:
    """Test the chosen depth limits the backfill when availability is unknown."""
    client = mock_engie_client.return_value
    _use_service_points(client)

    await _setup_with_history(hass, depth)

    calls = client.async_get_usage_details.call_args_list
    assert calls[0].args[1] == expected_start
    assert {call.args[3] for call in calls} == {UsageGranularity.DAILY}


@pytest.mark.usefixtures("frozen_afternoon")
async def test_backfill_capped_by_data_availability(
    hass: HomeAssistant, mock_engie_client: MagicMock
) -> None:
    """Test the backfill starts at the first day with measured data."""
    await _setup_with_history(hass, HistoryDepth.YEAR)

    client = mock_engie_client.return_value
    client.async_get_usage_details.assert_any_call(
        BAN, DAILY_FROM, TODAY, UsageGranularity.DAILY
    )


@pytest.mark.parametrize(
    "points",
    [
        pytest.param(
            (
                MeteringServicePoint(ean=OFFTAKE_ONLY_EAN, division="GAS"),
                build_measured_point(
                    OFFTAKE_INJECTION_EAN, "ELECTRICITY", available=False
                ),
            ),
            id="unavailable_windows",
        ),
        pytest.param(
            (
                build_measured_point(OFFTAKE_ONLY_EAN, "GAS", GAS_QUARTER_HOURLY_FROM),
                build_measured_point(OFFTAKE_INJECTION_EAN, "ELECTRICITY"),
                build_measured_point(
                    "541448820000000003_ID1",
                    "ELECTRICITY",
                    datetime(2026, 9, 30, tzinfo=BRUSSELS_TIME_ZONE),
                    date(2026, 9, 10),
                ),
                build_measured_point("541448820000000004_ID1", "WATER"),
            ),
            id="duplicate_and_other_divisions",
        ),
        pytest.param(
            (
                build_measured_point(OFFTAKE_ONLY_EAN, "GAS", GAS_QUARTER_HOURLY_FROM),
                build_measured_point(OFFTAKE_INJECTION_EAN, "ELECTRICITY"),
                MeteringServicePoint(
                    ean="541448820000000003_ID1", division="ELECTRICITY"
                ),
            ),
            id="meter_without_hourly_data",
        ),
    ],
)
@pytest.mark.usefixtures("frozen_afternoon")
async def test_backfill_with_service_point_variations(
    hass: HomeAssistant,
    mock_engie_client: MagicMock,
    snapshot: SnapshotAssertion,
    points: tuple[MeteringServicePoint, ...],
) -> None:
    """Test the daily and hourly windows follow the reported data availability."""
    _use_service_points(mock_engie_client.return_value, *points)

    await _setup_with_history(hass)

    assert (
        _summary(await _hourly_rows(hass), {OFFTAKE_STATISTIC_ID, GAS_STATISTIC_ID})
        == snapshot
    )


@pytest.mark.usefixtures("frozen_afternoon")
async def test_backfill_skips_missing_values(
    hass: HomeAssistant,
    mock_engie_client: MagicMock,
    snapshot: SnapshotAssertion,
) -> None:
    """Test buckets without usage values are left out of the statistics."""
    _use_scenario(
        mock_engie_client.return_value,
        UsageScenario(sparse_days=frozenset({date(2026, 9, 22), date(2026, 9, 30)})),
    )

    await _setup_with_history(hass)

    assert (
        _summary(
            await _hourly_rows(hass),
            {OFFTAKE_STATISTIC_ID, GAS_STATISTIC_ID, HIGH_LOAD_STATISTIC_ID},
        )
        == snapshot
    )


@pytest.mark.usefixtures("frozen_afternoon")
async def test_backfill_nothing_to_import(
    hass: HomeAssistant, mock_engie_client: MagicMock
) -> None:
    """Test nothing is fetched when the measured data only starts today."""
    client = mock_engie_client.return_value
    _use_service_points(
        client,
        build_measured_point(OFFTAKE_INJECTION_EAN, "ELECTRICITY", daily_from=TODAY),
    )

    await _setup_with_history(hass)

    client.async_get_usage_details.assert_not_called()


@pytest.mark.usefixtures("frozen_afternoon")
async def test_incremental_import_applies_corrections(
    hass: HomeAssistant,
    mock_engie_client: MagicMock,
    freezer: FrozenDateTimeFactory,
    snapshot: SnapshotAssertion,
) -> None:
    """Test a later import re-imports recent days and keeps the sums consistent."""
    await _setup_with_history(hass)
    client = mock_engie_client.return_value
    _use_scenario(client, UsageScenario(offtake_offsets={date(2026, 9, 25): 5.0}))

    await _run_scheduled_import(hass, freezer)

    client.async_get_usage_details.assert_any_call(
        BAN, date(2026, 9, 2), date(2026, 10, 4), UsageGranularity.DAILY
    )
    rows = await _hourly_rows(hass)
    assert _summary(rows, {OFFTAKE_STATISTIC_ID}) == snapshot
    sums, totals = _sums_and_totals(rows)
    assert sums == totals


@pytest.mark.usefixtures("frozen_afternoon")
async def test_incremental_import_continues_new_points(
    hass: HomeAssistant,
    mock_engie_client: MagicMock,
    freezer: FrozenDateTimeFactory,
    snapshot: SnapshotAssertion,
) -> None:
    """Test points after the last stored row continue from its sum."""
    await _setup_with_history(hass)
    _use_scenario(
        mock_engie_client.return_value, UsageScenario(costs_from=date(2026, 10, 3))
    )

    await _run_scheduled_import(hass, freezer)

    assert _summary(await _hourly_rows(hass), {GAS_COST_STATISTIC_ID}) == snapshot


@pytest.mark.usefixtures("frozen_afternoon")
async def test_incremental_import_keeps_hourly_days(
    hass: HomeAssistant,
    mock_engie_client: MagicMock,
    freezer: FrozenDateTimeFactory,
    snapshot: SnapshotAssertion,
) -> None:
    """Test days stored hourly stay hourly when the hourly window starts later."""
    await _setup_with_history(hass)
    _use_service_points(
        mock_engie_client.return_value,
        build_measured_point(
            OFFTAKE_INJECTION_EAN,
            "ELECTRICITY",
            datetime(2026, 9, 30, tzinfo=BRUSSELS_TIME_ZONE),
        ),
    )

    await _run_scheduled_import(hass, freezer)

    rows = await _hourly_rows(hass)
    assert _summary(rows, {OFFTAKE_STATISTIC_ID}) == snapshot
    sums, totals = _sums_and_totals(rows)
    assert sums == totals


@pytest.mark.usefixtures("frozen_afternoon")
async def test_incremental_import_ignores_division_without_usage(
    hass: HomeAssistant,
    mock_engie_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test a meter without usage does not restart the backfill on every import."""
    client = mock_engie_client.return_value
    _use_scenario(client, UsageScenario(with_gas=False))
    await _setup_with_history(hass)
    client.async_get_usage_details.reset_mock()

    await _run_scheduled_import(hass, freezer)

    assert client.async_get_usage_details.call_args_list[0].args[1] == date(2026, 9, 2)


@pytest.mark.usefixtures("frozen_afternoon")
async def test_import_retried_after_failure(
    hass: HomeAssistant,
    mock_engie_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test a failed import is retried on the next scheduled import."""
    client = mock_engie_client.return_value
    client.async_get_usage_details.side_effect = EngieBeCommunicationError("boom")
    await _setup_with_history(hass)
    assert await _hourly_rows(hass) == {}

    _use_scenario(client, UsageScenario())
    await _run_scheduled_import(hass, freezer)

    rows = await _hourly_rows(hass)
    assert rows[OFFTAKE_STATISTIC_ID][-1]["sum"] == pytest.approx(338.0)


@pytest.mark.usefixtures("frozen_afternoon")
async def test_import_auth_error_starts_reauth(
    hass: HomeAssistant, mock_engie_client: MagicMock
) -> None:
    """Test an authentication error during the import starts a reauth flow."""
    mock_engie_client.return_value.async_get_usage_details.side_effect = (
        EngieBeAuthenticationError("expired")
    )

    entry = await _setup_with_history(hass)

    assert entry.state is ConfigEntryState.LOADED
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]


@pytest.mark.parametrize(
    "exception",
    [
        pytest.param(EngieBeCommunicationError("boom"), id="communication"),
        pytest.param(EngieBeError("boom"), id="other"),
    ],
)
@pytest.mark.usefixtures("frozen_afternoon")
async def test_import_error_keeps_entry_loaded(
    hass: HomeAssistant, mock_engie_client: MagicMock, exception: EngieBeError
) -> None:
    """Test a failed import stores nothing and leaves the entry loaded."""
    mock_engie_client.return_value.async_get_usage_details.side_effect = exception

    entry = await _setup_with_history(hass)

    assert entry.state is ConfigEntryState.LOADED
    assert await _hourly_rows(hass) == {}


@pytest.mark.usefixtures("frozen_afternoon")
async def test_no_scheduled_import_after_unload(
    hass: HomeAssistant,
    mock_engie_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test the scheduled import stops when the entry is unloaded."""
    entry = await _setup_with_history(hass)
    client = mock_engie_client.return_value
    assert await hass.config_entries.async_unload(entry.entry_id)
    client.async_get_usage_details.reset_mock()

    await _run_scheduled_import(hass, freezer)

    client.async_get_usage_details.assert_not_called()


@pytest.mark.usefixtures("frozen_afternoon")
async def test_no_import_without_history(
    hass: HomeAssistant,
    mock_engie_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test nothing is imported when the user declined the history import."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    mock_engie_client.return_value.async_get_usage_details.assert_not_called()


async def test_migrate_entry_without_history(
    hass: HomeAssistant, mock_engie_client: MagicMock
) -> None:
    """Test entries created before the history import keep importing nothing."""
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=SUBJECT, data=dict(build_config_entry().data)
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.minor_version == 2
    assert entry.options == {CONF_HISTORY: HistoryDepth.NONE}
    mock_engie_client.return_value.async_get_usage_details.assert_not_called()

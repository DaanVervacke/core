"""Test the ENGIE Belgium usage history import."""

from dataclasses import replace
from datetime import date, datetime
from typing import Any
from unittest.mock import MagicMock

from aioengiebelgium import (
    EngieBeAuthenticationError,
    EngieBeCommunicationError,
    ServicePointsResponse,
    UsageDetailsResponse,
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
    SUBJECT,
    build_config_entry,
    build_usage_details,
)

OFFTAKE_STATISTIC_ID = f"{DOMAIN}:{BAN}_electricity_offtake"
CORRECTED_DAY = date(2026, 9, 25)


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


async def _hourly_rows(hass: HomeAssistant) -> dict[str, list[dict[str, Any]]]:
    """Return every stored hourly row of the ENGIE Belgium statistics."""
    instance = get_instance(hass)
    metadata = await instance.async_add_executor_job(
        lambda: get_metadata(hass, statistic_source=DOMAIN)
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


def _summary(rows: dict[str, list[dict[str, Any]]]) -> dict[str, list[str]]:
    """Return the rows as readable local start, state and sum lines."""
    return {
        statistic_id: [
            f"{dt_util.utc_from_timestamp(row['start']).astimezone(BRUSSELS_TIME_ZONE).isoformat()}"
            f" state={row['state']:.3f} sum={row['sum']:.3f}"
            for row in statistic_rows
        ]
        for statistic_id, statistic_rows in sorted(rows.items())
    }


@pytest.mark.usefixtures("frozen_afternoon", "mock_engie_client")
async def test_backfill(hass: HomeAssistant, snapshot: SnapshotAssertion) -> None:
    """Test the full history is imported with daily and hourly buckets."""
    await _setup_with_history(hass)

    instance = get_instance(hass)
    metadata = await instance.async_add_executor_job(
        lambda: get_metadata(hass, statistic_source=DOMAIN)
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
    client.async_get_service_points.side_effect = None
    client.async_get_service_points.return_value = ServicePointsResponse()

    await _setup_with_history(hass, depth)

    calls = client.async_get_usage_details.call_args_list
    assert calls[0].args[1] == expected_start
    assert all(call.args[3] is UsageGranularity.DAILY for call in calls)


@pytest.mark.usefixtures("frozen_afternoon")
async def test_backfill_capped_by_data_availability(
    hass: HomeAssistant, mock_engie_client: MagicMock
) -> None:
    """Test the backfill starts at the first day with measured data."""
    await _setup_with_history(hass, HistoryDepth.YEAR)

    calls = mock_engie_client.return_value.async_get_usage_details.call_args_list
    assert calls[0].args[1] == DAILY_FROM


@pytest.mark.usefixtures("frozen_afternoon")
async def test_incremental_import_applies_corrections(
    hass: HomeAssistant,
    mock_engie_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test a later import re-imports recent days and keeps the sums consistent."""
    await _setup_with_history(hass)
    client = mock_engie_client.return_value

    def corrected_usage(
        ban: str, start_date: date, end_date: date, granularity: UsageGranularity
    ) -> UsageDetailsResponse:
        response = build_usage_details(ban, start_date, end_date, granularity)
        items = []
        for item in response.items:
            if item.start.astimezone(BRUSSELS_TIME_ZONE).date() == CORRECTED_DAY:
                assert item.energy is not None
                assert item.energy.offtake is not None
                item = replace(
                    item,
                    energy=replace(
                        item.energy,
                        offtake=replace(
                            item.energy.offtake,
                            kwh_sum=(item.energy.offtake.kwh_sum or 0) + 5,
                        ),
                    ),
                )
            items.append(item)
        return UsageDetailsResponse(items=tuple(items))

    client.async_get_usage_details.reset_mock()
    client.async_get_usage_details.side_effect = corrected_usage
    freezer.tick(STATISTICS_SCAN_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)

    first_call = client.async_get_usage_details.call_args_list[0]
    assert first_call.args[1] == date(2026, 9, 2)

    rows = await _hourly_rows(hass)
    corrected = next(
        row
        for row in rows[OFFTAKE_STATISTIC_ID]
        if dt_util.utc_from_timestamp(row["start"])
        .astimezone(BRUSSELS_TIME_ZONE)
        .date()
        == CORRECTED_DAY
    )
    assert corrected["state"] == pytest.approx(10 + CORRECTED_DAY.day + 5)
    for statistic_rows in rows.values():
        assert statistic_rows[-1]["sum"] == pytest.approx(
            sum(row["state"] for row in statistic_rows)
        )


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


@pytest.mark.usefixtures("frozen_afternoon")
async def test_import_error_keeps_entry_loaded(
    hass: HomeAssistant,
    mock_engie_client: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test a failed import is logged and leaves the entry loaded."""
    mock_engie_client.return_value.async_get_usage_details.side_effect = (
        EngieBeCommunicationError("boom")
    )

    entry = await _setup_with_history(hass)

    assert entry.state is ConfigEntryState.LOADED
    assert f"Error fetching {DOMAIN}_statistics_…0001 data" in caplog.text


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


@pytest.mark.usefixtures("mock_engie_client")
async def test_migrate_entry_without_history(hass: HomeAssistant) -> None:
    """Test entries created before the history import keep importing nothing."""
    data = dict(build_config_entry().data)
    del data[CONF_HISTORY]
    entry = MockConfigEntry(domain=DOMAIN, unique_id=SUBJECT, data=data)
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.minor_version == 2
    assert entry.data[CONF_HISTORY] == HistoryDepth.NONE

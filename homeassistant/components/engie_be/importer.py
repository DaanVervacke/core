"""Import ENGIE Belgium usage history into long-term statistics."""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from aioengiebelgium import (
    EngieBeClient,
    MeasuredDataWindow,
    MeteringServicePoint,
    UsageDirectionBreakdown,
    UsageGranularity,
    UsageItem,
)

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.const import CURRENCY_EURO, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util, slugify
from homeassistant.util.unit_conversion import EnergyConverter

from .const import BRUSSELS_TIME_ZONE, DOMAIN, LOGGER, HistoryDepth

ELECTRICITY = "ELECTRICITY"
GAS = "GAS"

_TOTAL_SLOT_CODE = "TOTAL_HOURS"
_ANCHOR_KEYS = {ELECTRICITY: "electricity_offtake", GAS: "gas"}
_DEPTHS = {
    HistoryDepth.MONTH: timedelta(days=31),
    HistoryDepth.YEAR: timedelta(days=365),
}

# Re-imported every run so ENGIE Belgium corrections replace earlier values.
CORRECTION_WINDOW = timedelta(days=30)
# Used when the service points do not report how far back data reaches.
FALLBACK_LOOKBACK = timedelta(days=3 * 365)
# The API documents no range limit, so stay conservative per request.
DAILY_CHUNK = timedelta(days=92)
HOURLY_CHUNK = timedelta(days=31)


@dataclass(frozen=True)
class MeterWindow:
    """The first days with daily and hourly measured data for one division."""

    daily_from: date | None
    hourly_from: date | None


@dataclass
class _Series:
    """Values of one statistic, keyed by the hour-aligned start of each bucket."""

    name: str
    is_cost: bool
    values: dict[datetime, float] = field(default_factory=dict)


def statistic_id(ban: str, key: str) -> str:
    """Return the external statistic id of one household statistic."""
    return f"{DOMAIN}:{slugify(f'{ban}_{key}')}"


def _local_date(moment: datetime) -> date:
    return moment.astimezone(BRUSSELS_TIME_ZONE).date()


def _first_full_day(moment: datetime) -> date:
    """Return the first Brussels day that starts at or after the moment."""
    local = moment.astimezone(BRUSSELS_TIME_ZONE)
    return local.date() if local.time() == time() else local.date() + timedelta(days=1)


def _window_start(window: MeasuredDataWindow | None) -> datetime | None:
    if window is None or not window.available:
        return None
    return window.start


def meter_windows(points: Iterable[MeteringServicePoint]) -> dict[str, MeterWindow]:
    """Return the measured data windows per division of the service points."""
    windows: dict[str, MeterWindow] = {}
    for point in points:
        if point.division not in _ANCHOR_KEYS:
            continue
        availability = point.data_availability
        daily = _window_start(availability.daily if availability else None)
        hourly = _window_start(availability.quarter_hourly if availability else None)
        window = MeterWindow(
            daily_from=None if daily is None else _local_date(daily),
            hourly_from=None if hourly is None else _first_full_day(hourly),
        )
        if (known := windows.get(point.division)) is not None:
            window = MeterWindow(
                daily_from=_earliest(known.daily_from, window.daily_from),
                hourly_from=_latest(known.hourly_from, window.hourly_from),
            )
        windows[point.division] = window
    return windows


def _earliest(first: date | None, second: date | None) -> date | None:
    if first is None or second is None:
        return first or second
    return min(first, second)


def _latest(first: date | None, second: date | None) -> date | None:
    if first is None or second is None:
        return first or second
    return max(first, second)


def backfill_start(window: MeterWindow, depth: HistoryDepth, today: date) -> date:
    """Return the first day to import for a division without statistics."""
    start = window.daily_from or today - FALLBACK_LOOKBACK
    if (lookback := _DEPTHS.get(depth)) is not None:
        start = max(start, today - lookback)
    return start


async def _async_fetch_usage(
    client: EngieBeClient,
    ban: str,
    start: date,
    end: date,
    granularity: UsageGranularity,
    chunk: timedelta,
) -> dict[datetime, UsageItem]:
    """Fetch complete, hour-aligned usage buckets between the days in chunks."""
    items: dict[datetime, UsageItem] = {}
    while start < end:
        chunk_end = min(start + chunk, end)
        response = await client.async_get_usage_details(
            ban, start, chunk_end, granularity
        )
        for item in response.items:
            start_utc = item.start.astimezone(dt_util.UTC)
            if item.partial_data or start_utc != start_utc.replace(
                minute=0, second=0, microsecond=0
            ):
                continue
            items[item.start] = item
        start = chunk_end
    return items


def _select(
    daily: dict[datetime, UsageItem],
    hourly: dict[datetime, UsageItem],
    hourly_from: date | None,
) -> list[UsageItem]:
    """Return daily buckets before the hourly window and hourly buckets inside it."""
    selected = [
        item
        for item in daily.values()
        if hourly_from is None or _local_date(item.start) < hourly_from
    ]
    if hourly_from is not None:
        selected.extend(
            item for item in hourly.values() if _local_date(item.start) >= hourly_from
        )
    return sorted(selected, key=lambda item: item.start)


class _SeriesBuilder:
    """Collect the values of every statistic of one household."""

    def __init__(self, household_name: str) -> None:
        self._household_name = household_name
        self.series: dict[str, _Series] = {}

    def add(
        self,
        key: str,
        label: str,
        start: datetime,
        value: float | None,
        *,
        is_cost: bool = False,
    ) -> None:
        if value is None:
            return
        series = self.series.setdefault(
            key, _Series(name=f"{self._household_name} {label}", is_cost=is_cost)
        )
        series.values[start] = value

    def add_energy(self, item: UsageItem) -> None:
        energy = item.energy
        if energy is None:
            return
        for direction, breakdown in (
            ("offtake", energy.offtake),
            ("injection", energy.injection),
        ):
            if breakdown is not None:
                self.add(
                    f"electricity_{direction}",
                    f"electricity {direction}",
                    item.start,
                    breakdown.kwh_sum,
                )

    def add_gas(self, item: UsageItem) -> None:
        if item.gas is not None:
            self.add("gas", "gas", item.start, item.gas.kwh)

    def add_daily_details(self, item: UsageItem) -> None:
        """Add the time-of-use split and the costs, which only daily buckets carry."""
        if item.energy is not None:
            for direction, breakdown in (
                ("offtake", item.energy.offtake),
                ("injection", item.energy.injection),
            ):
                self._add_slots(item.start, direction, breakdown)
        if item.costs is not None:
            if item.costs.offtake is not None:
                self.add(
                    "electricity_offtake_cost",
                    "electricity offtake cost",
                    item.start,
                    item.costs.offtake.amount_sum,
                    is_cost=True,
                )
            if item.costs.injection is not None:
                self.add(
                    "electricity_injection_compensation",
                    "electricity injection compensation",
                    item.start,
                    item.costs.injection.amount_sum,
                    is_cost=True,
                )
        if item.gas is not None:
            self.add("gas_cost", "gas cost", item.start, item.gas.cost, is_cost=True)

    def _add_slots(
        self, start: datetime, direction: str, breakdown: UsageDirectionBreakdown | None
    ) -> None:
        if breakdown is None:
            return
        for part in breakdown.supplier_parts:
            if part.slot_code == _TOTAL_SLOT_CODE:
                continue
            self.add(
                f"electricity_{direction}_{slugify(part.slot_code)}",
                f"electricity {direction} {part.slot_code.replace('_', ' ').lower()}",
                start,
                part.value,
            )


def _metadata(ban: str, key: str, series: _Series) -> StatisticMetaData:
    return StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=series.name,
        source=DOMAIN,
        statistic_id=statistic_id(ban, key),
        unit_class=None if series.is_cost else EnergyConverter.UNIT_CLASS,
        unit_of_measurement=(
            CURRENCY_EURO if series.is_cost else UnitOfEnergy.KILO_WATT_HOUR
        ),
    )


async def _async_last_start(hass: HomeAssistant, stat_id: str) -> datetime | None:
    last = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, stat_id, False, {"sum"}
    )
    if not (rows := last.get(stat_id)):
        return None
    return dt_util.utc_from_timestamp(rows[0]["start"])


async def _async_baseline(
    hass: HomeAssistant, stat_id: str, first_start: datetime
) -> float:
    """Return the stored sum just before the first bucket that is about to be written."""
    stored = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        first_start,
        None,
        {stat_id},
        "hour",
        None,
        {"sum", "state"},
    )
    if rows := stored.get(stat_id):
        return (rows[0].get("sum") or 0.0) - (rows[0].get("state") or 0.0)
    last = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, stat_id, False, {"sum"}
    )
    if rows := last.get(stat_id):
        return rows[0].get("sum") or 0.0
    return 0.0


async def async_import_household_statistics(
    hass: HomeAssistant,
    client: EngieBeClient,
    ban: str,
    household_name: str,
    depth: HistoryDepth,
) -> None:
    """Import the usage and costs of one household into external statistics."""
    today = dt_util.now(BRUSSELS_TIME_ZONE).date()
    service_points = await client.async_get_service_points(ban)
    windows = meter_windows(service_points.items) or {
        division: MeterWindow(daily_from=None, hourly_from=None)
        for division in _ANCHOR_KEYS
    }

    starts: list[date] = []
    for division, window in windows.items():
        last = await _async_last_start(hass, statistic_id(ban, _ANCHOR_KEYS[division]))
        starts.append(
            backfill_start(window, depth, today)
            if last is None
            else _local_date(last) - CORRECTION_WINDOW
        )
    start = min(starts)
    if start >= today:
        return

    daily = await _async_fetch_usage(
        client, ban, start, today, UsageGranularity.DAILY, DAILY_CHUNK
    )
    hourly: dict[datetime, UsageItem] = {}
    hourly_froms = [w.hourly_from for w in windows.values() if w.hourly_from]
    if hourly_froms and (hourly_start := max(start, min(hourly_froms))) < today:
        hourly = await _async_fetch_usage(
            client, ban, hourly_start, today, UsageGranularity.HOURLY, HOURLY_CHUNK
        )

    builder = _SeriesBuilder(household_name)
    no_window = MeterWindow(daily_from=None, hourly_from=None)
    for item in _select(daily, hourly, windows.get(ELECTRICITY, no_window).hourly_from):
        builder.add_energy(item)
    for item in _select(daily, hourly, windows.get(GAS, no_window).hourly_from):
        builder.add_gas(item)
    for item in sorted(daily.values(), key=lambda item: item.start):
        builder.add_daily_details(item)

    for key, series in builder.series.items():
        stat_id = statistic_id(ban, key)
        points = sorted(series.values.items())
        total = await _async_baseline(hass, stat_id, points[0][0])
        rows: list[StatisticData] = []
        for bucket_start, value in points:
            total += value
            rows.append(StatisticData(start=bucket_start, state=value, sum=total))
        async_add_external_statistics(hass, _metadata(ban, key, series), rows)
    LOGGER.debug(
        "Imported %s statistics for a household from %s", len(builder.series), start
    )

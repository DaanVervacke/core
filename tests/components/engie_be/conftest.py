"""Common fixtures for the ENGIE Belgium tests."""

from collections.abc import Generator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from aioengiebelgium import (
    AccountRelation,
    AuthFlow,
    BusinessAgreement,
    ConsumptionAddress,
    CustomerAccount,
    CustomerAccountRelations,
    DataAvailability,
    EanPrices,
    EnergyContract,
    EnergyContractsResponse,
    EngieBeEpexNotPublishedError,
    EpexGranularity,
    EpexPayload,
    EpexSlot,
    GasUsage,
    MeasuredDataWindow,
    MeteringServicePoint,
    PricePeriod,
    PriceSlot,
    PricesResponse,
    ProductConfiguration,
    ServicePoint,
    ServicePointsResponse,
    UsageDetailsResponse,
    UsageDirectionBreakdown,
    UsageElectricityBreakdown,
    UsageGranularity,
    UsageItem,
    UsageTouPart,
    bare_ean,
)
from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.typing import RecorderInstanceContextManager

from homeassistant.components.engie_be.const import (
    CONF_HISTORY,
    CONF_MFA_METHOD,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    HistoryDepth,
)
from homeassistant.components.recorder import Recorder
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_EMAIL
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util

BRUSSELS_TIME_ZONE = ZoneInfo("Europe/Brussels")
EMAIL = "user@example.com"
PASSWORD = "hunter2"
SUBJECT = "auth0|69f5b418f3be21cc2ede9c98"
BAN = "000000000001"
BAN_2 = "000000000002"
OFFTAKE_ONLY_EAN = "541448820000000001_ID1"
OFFTAKE_INJECTION_EAN = "541448820000000002_ID1"
_SERVICE_POINT_ENERGY_TYPES = {
    bare_ean(OFFTAKE_ONLY_EAN): "GAS",
    bare_ean(OFFTAKE_INJECTION_EAN): "ELECTRICITY",
}


def _business_agreement(ban: str, *, with_address: bool = True) -> BusinessAgreement:
    """Build a single active business agreement."""
    return BusinessAgreement(
        business_agreement_number=ban,
        active=True,
        consumption_address=ConsumptionAddress(
            street="Main street",
            house_number="1",
            postal_code="1000",
            city="Brussels",
        )
        if with_address
        else None,
    )


def build_relations(*bans: str, with_address: bool = True) -> CustomerAccountRelations:
    """Build a customer-account-relations response with the given active BANs."""
    bans = bans or (BAN,)
    return CustomerAccountRelations(
        accounts=(
            AccountRelation(
                id="account-1",
                admin=True,
                customer_account=CustomerAccount(
                    customer_account_number="can-1",
                    business_agreements=tuple(
                        _business_agreement(ban, with_address=with_address)
                        for ban in bans
                    ),
                ),
            ),
        )
    )


def build_prices(
    *,
    valid_from: date | None = date(2000, 1, 1),
    valid_to: date | None = date(2099, 12, 31),
) -> PricesResponse:
    """Build a prices response with an offtake-only EAN and a dual-direction EAN."""
    return PricesResponse(
        items=(
            EanPrices(
                ean=OFFTAKE_ONLY_EAN,
                periods=(
                    PricePeriod(
                        valid_from=valid_from,
                        valid_to=valid_to,
                        vat_tariff=6.0,
                        offtake=(
                            PriceSlot(
                                time_of_use_slot_code="TOTAL_HOURS",
                                price_value=0.123456,
                                price_value_excl_vat=0.116468,
                            ),
                        ),
                    ),
                ),
            ),
            EanPrices(
                ean=OFFTAKE_INJECTION_EAN,
                periods=(
                    PricePeriod(
                        valid_from=valid_from,
                        valid_to=valid_to,
                        vat_tariff=6.0,
                        offtake=(
                            PriceSlot(
                                time_of_use_slot_code="S_TOU1_OFFTAKE_PEAK",
                                price_value=0.18,
                                price_value_excl_vat=0.169811,
                            ),
                            PriceSlot(
                                time_of_use_slot_code="EN",
                                price_value=0.12,
                                price_value_excl_vat=0.113208,
                            ),
                            PriceSlot(
                                time_of_use_slot_code="S_TOU1_OFFTAKE_WEEKEND",
                                price_value=0.15,
                                price_value_excl_vat=0.141509,
                            ),
                        ),
                        injection=(
                            PriceSlot(
                                time_of_use_slot_code="S_TOU1_INJECTION_PEAK",
                                price_value=0.05,
                                price_value_excl_vat=0.047170,
                            ),
                        ),
                    ),
                ),
            ),
        )
    )


def build_service_point(ean: str) -> ServicePoint:
    """Build a service point response for a queried (possibly suffixed) EAN."""
    bare = bare_ean(ean)
    return ServicePoint(ean_energy_types={bare: _SERVICE_POINT_ENERGY_TYPES[bare]})


def build_contracts(*, dynamic: bool = False) -> EnergyContractsResponse:
    """Build an energy-contracts response with one active electricity contract."""
    return EnergyContractsResponse(
        items=(
            EnergyContract(
                business_agreement_number=BAN,
                service_point_number=bare_ean(OFFTAKE_INJECTION_EAN),
                division="ELECTRICITY",
                status="ACTIVE",
                product_configuration=ProductConfiguration(
                    energy_product="DYNAMIC" if dynamic else "FIXED"
                ),
            ),
        )
    )


DAILY_FROM = date(2026, 9, 20)
ELECTRICITY_QUARTER_HOURLY_FROM = datetime(2026, 9, 28, tzinfo=BRUSSELS_TIME_ZONE)
GAS_QUARTER_HOURLY_FROM = datetime(2026, 9, 29, 7, tzinfo=BRUSSELS_TIME_ZONE)
PARTIAL_DAY = date(2026, 10, 2)
OFFTAKE_STATISTIC_ID = f"{DOMAIN}:{BAN}_electricity_offtake"
GAS_STATISTIC_ID = f"{DOMAIN}:{BAN}_gas"


def build_measured_point(
    ean: str,
    division: str,
    quarter_hourly_from: datetime = ELECTRICITY_QUARTER_HOURLY_FROM,
    daily_from: date = DAILY_FROM,
    *,
    available: bool = True,
) -> MeteringServicePoint:
    """Build a metering service point with daily and quarter-hourly data."""
    return MeteringServicePoint(
        ean=bare_ean(ean),
        division=division,
        data_availability=DataAvailability(
            quarter_hourly=MeasuredDataWindow(
                available=available,
                start=quarter_hourly_from,
                end=datetime(2026, 10, 2, 23, 45, tzinfo=BRUSSELS_TIME_ZONE),
            ),
            daily=MeasuredDataWindow(
                available=available,
                start=datetime.combine(daily_from, time(), BRUSSELS_TIME_ZONE),
                end=datetime(2026, 10, 2, tzinfo=BRUSSELS_TIME_ZONE),
            ),
        ),
    )


def build_service_points(ban: str) -> ServicePointsResponse:
    """Build the metering service points of a dual-fuel household."""
    return ServicePointsResponse(
        items=(
            build_measured_point(OFFTAKE_ONLY_EAN, "GAS", GAS_QUARTER_HOURLY_FROM),
            build_measured_point(OFFTAKE_INJECTION_EAN, "ELECTRICITY"),
        )
    )


@dataclass(frozen=True, kw_only=True)
class UsageScenario:
    """Variations of the usage the mocked API returns."""

    offtake_offsets: Mapping[date, float] = field(default_factory=dict)
    costs_from: date = DAILY_FROM
    sparse_days: frozenset[date] = frozenset()
    with_gas: bool = True


DEFAULT_SCENARIO = UsageScenario()


def _daily_item(day: date, scenario: UsageScenario) -> UsageItem:
    """Build a daily usage bucket with a day/night split and costs."""
    start = datetime.combine(day, time(), BRUSSELS_TIME_ZONE)
    common = {
        "start": _api_timestamp(start.astimezone(UTC)),
        "end": _api_timestamp((start + timedelta(days=1)).astimezone(UTC)),
        "partial_data": day == PARTIAL_DAY,
    }
    if day in scenario.sparse_days:
        return UsageItem(
            **common,
            energy=UsageElectricityBreakdown(
                injection=UsageDirectionBreakdown(kwh_sum=None)
            ),
        )
    offtake = 10.0 + day.day + scenario.offtake_offsets.get(day, 0.0)
    costs = UsageElectricityBreakdown(
        offtake=UsageDirectionBreakdown(amount_sum=round(offtake * 0.3, 2)),
        injection=UsageDirectionBreakdown(amount_sum=0.1),
    )
    return UsageItem(
        **common,
        gas=GasUsage(kwh=20.0, cost=2.0 if day >= scenario.costs_from else None)
        if scenario.with_gas
        else None,
        energy=UsageElectricityBreakdown(
            offtake=UsageDirectionBreakdown(
                kwh_sum=offtake,
                supplier_parts=(
                    UsageTouPart(slot_code="HIGH_LOAD_HOURS", value=offtake * 0.6),
                    UsageTouPart(slot_code="LOW_LOAD_HOURS", value=offtake * 0.4),
                ),
            ),
            injection=UsageDirectionBreakdown(
                kwh_sum=2.0,
                supplier_parts=(UsageTouPart(slot_code="TOTAL_HOURS", value=2.0),),
            ),
        ),
        costs=costs if day >= scenario.costs_from else None,
    )


def _hourly_item(start: datetime, scenario: UsageScenario) -> UsageItem:
    """Build an hourly usage bucket without split or costs, like the API sends."""
    local = start.astimezone(BRUSSELS_TIME_ZONE)
    sparse = local.date() in scenario.sparse_days
    return UsageItem(
        start=_api_timestamp(start.astimezone(UTC)),
        end=_api_timestamp((start + timedelta(hours=1)).astimezone(UTC)),
        partial_data=local.date() == PARTIAL_DAY and local.hour >= 20,
        gas=GasUsage(kwh=1.0) if scenario.with_gas and not sparse else None,
        energy=None
        if sparse
        else UsageElectricityBreakdown(
            offtake=UsageDirectionBreakdown(kwh_sum=0.5),
            injection=UsageDirectionBreakdown(kwh_sum=0.1),
        ),
    )


def build_usage_details(
    ban: str,
    start_date: date,
    end_date: date,
    granularity: UsageGranularity,
    scenario: UsageScenario = DEFAULT_SCENARIO,
) -> UsageDetailsResponse:
    """Build the usage buckets from the start up to the end day within the data window."""
    first = datetime.combine(max(start_date, DAILY_FROM), time(), BRUSSELS_TIME_ZONE)
    end = datetime.combine(end_date, time(), BRUSSELS_TIME_ZONE)
    items: list[UsageItem] = []
    if granularity is UsageGranularity.DAILY:
        day = first.date()
        while day < end_date:
            items.append(_daily_item(day, scenario))
            day += timedelta(days=1)
        return UsageDetailsResponse(items=tuple(items))
    moment = max(first, ELECTRICITY_QUARTER_HOURLY_FROM).astimezone(UTC)
    while moment < end.astimezone(UTC):
        items.append(_hourly_item(moment, scenario))
        moment += timedelta(hours=1)
    return UsageDetailsResponse(items=tuple(items))


def _api_timestamp(moment: datetime) -> datetime:
    """Return a timestamp carrying the fixed Brussels offset the API would send."""
    wall = BRUSSELS_TIME_ZONE.fromutc(moment.replace(tzinfo=BRUSSELS_TIME_ZONE))
    return wall.replace(tzinfo=timezone(wall.utcoffset()))


def build_epex_payload(
    start: datetime,
    end: datetime,
    granularity: EpexGranularity = EpexGranularity.HOURLY,
) -> EpexPayload:
    """Build an EPEX payload of Brussels-offset slots covering the window with rising values."""
    step = timedelta(minutes=granularity.value)
    slots: list[EpexSlot] = []
    moment = start.astimezone(UTC)
    end_utc = end.astimezone(UTC)
    while moment < end_utc:
        local = moment.astimezone(BRUSSELS_TIME_ZONE)
        slots.append(
            EpexSlot(
                start=_api_timestamp(moment),
                end=_api_timestamp(moment + step),
                value_eur_per_kwh=round((local.hour + 1 + local.minute / 60) / 100, 6),
            )
        )
        moment = moment + step
    return EpexPayload(slots=tuple(slots), slot_duration=step)


def build_epex_payload_without_tomorrow(
    start: datetime,
    end: datetime,
    granularity: EpexGranularity = EpexGranularity.HOURLY,
) -> EpexPayload:
    """Raise for any window after today and build a payload otherwise."""
    if (
        start.astimezone(BRUSSELS_TIME_ZONE).date()
        > dt_util.now(BRUSSELS_TIME_ZONE).date()
    ):
        raise EngieBeEpexNotPublishedError("not published")
    return build_epex_payload(start, end, granularity)


def build_epex_payload_with_gap(
    start: datetime,
    end: datetime,
    granularity: EpexGranularity = EpexGranularity.HOURLY,
) -> EpexPayload:
    """Return an hourly payload without the slot covering the frozen afternoon."""
    payload = build_epex_payload(start, end, granularity)
    if granularity is not EpexGranularity.HOURLY:
        return payload
    gap_start = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
    return EpexPayload(
        slots=tuple(slot for slot in payload.slots if slot.start != gap_start),
        slot_duration=payload.slot_duration,
    )


def build_epex_payload_with_partial_tomorrow(
    start: datetime,
    end: datetime,
    granularity: EpexGranularity = EpexGranularity.HOURLY,
) -> EpexPayload:
    """Return one slot for tomorrow windows and a full payload otherwise."""
    if (
        start.astimezone(BRUSSELS_TIME_ZONE).date()
        > dt_util.now(BRUSSELS_TIME_ZONE).date()
    ):
        payload = build_epex_payload(start, end, granularity)
        return EpexPayload(slots=payload.slots[:1], slot_duration=payload.slot_duration)
    return build_epex_payload(start, end, granularity)


def build_epex_payload_with_negative_current(
    start: datetime,
    end: datetime,
    granularity: EpexGranularity = EpexGranularity.HOURLY,
) -> EpexPayload:
    """Build a payload whose slot covering the frozen afternoon is negative."""
    payload = build_epex_payload(start, end, granularity)
    afternoon = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
    return EpexPayload(
        slots=tuple(
            EpexSlot(
                start=slot.start,
                end=slot.end,
                value_eur_per_kwh=-0.05
                if slot.start <= afternoon < slot.end
                else slot.value_eur_per_kwh,
            )
            for slot in payload.slots
        ),
        slot_duration=payload.slot_duration,
    )


def build_config_entry(
    unique_id: str = SUBJECT,
    access_token: str = "access-token",
    history: HistoryDepth = HistoryDepth.NONE,
) -> MockConfigEntry:
    """Build a mock config entry for one ENGIE Belgium account."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=EMAIL,
        unique_id=unique_id,
        minor_version=2,
        data={
            CONF_EMAIL: EMAIL,
            CONF_MFA_METHOD: "sms",
            CONF_ACCESS_TOKEN: access_token,
            CONF_REFRESH_TOKEN: "refresh-token",
        },
        options={CONF_HISTORY: history.value},
    )


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a mock config entry."""
    return build_config_entry()


def configure_client(client: MagicMock, auth_flow: MagicMock) -> None:
    """Give a mocked ENGIE Belgium client the default responses."""
    client.async_get_customer_account_relations.return_value = build_relations()
    client.async_get_prices.return_value = build_prices()
    client.async_get_service_point.side_effect = build_service_point
    client.async_get_energy_contracts.return_value = build_contracts()
    client.async_get_epex_prices.side_effect = build_epex_payload
    client.async_start_authentication.return_value = auth_flow
    client.async_get_service_points.side_effect = build_service_points
    client.async_get_usage_details.side_effect = build_usage_details
    client.access_token = None
    client.refresh_token = None


@pytest.fixture
async def mock_recorder_before_hass(
    async_test_recorder: RecorderInstanceContextManager,
) -> None:
    """Set up the recorder before the hass fixture runs."""


@pytest.fixture
def mock_engie_client(
    recorder_mock: Recorder, mock_auth_flow: MagicMock
) -> Generator[MagicMock]:
    """Mock the EngieBeClient class constructed by the integration and config flow."""
    with (
        patch(
            "homeassistant.components.engie_be.EngieBeClient", autospec=True
        ) as mock_client_class,
        patch(
            "homeassistant.components.engie_be.config_flow.EngieBeClient",
            new=mock_client_class,
        ),
    ):
        configure_client(mock_client_class.return_value, mock_auth_flow)
        yield mock_client_class


@pytest.fixture
def mock_auth_flow() -> MagicMock:
    """Return a mock in-progress authentication flow."""
    auth_flow = MagicMock(spec=AuthFlow)
    auth_flow.async_submit_mfa = AsyncMock(
        return_value=("new-access-token", "new-refresh-token")
    )
    return auth_flow


@pytest.fixture
def mock_setup_entry(recorder_mock: Recorder) -> Generator[AsyncMock]:
    """Override async_setup_entry."""
    with patch(
        "homeassistant.components.engie_be.async_setup_entry", return_value=None
    ) as mock_setup_entry:
        yield mock_setup_entry


@pytest.fixture
def frozen_afternoon(freezer: FrozenDateTimeFactory) -> None:
    """Freeze time on a Brussels Saturday afternoon."""
    freezer.move_to(datetime(2026, 10, 3, 14, 0, tzinfo=BRUSSELS_TIME_ZONE))


async def setup_entry(hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
    """Set up a loaded ENGIE Belgium entry."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()


async def setup_dynamic_entry(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_engie_client: MagicMock,
) -> None:
    """Set up an entry whose household has a dynamic electricity tariff."""
    mock_engie_client.return_value.async_get_energy_contracts.return_value = (
        build_contracts(dynamic=True)
    )
    await setup_entry(hass, mock_config_entry)


def automation_calls(service_calls: list[ServiceCall]) -> list[ServiceCall]:
    """Return the recorded calls of the test automation action."""
    return [call for call in service_calls if call.domain == "test"]


def household_device_id(
    device_registry: dr.DeviceRegistry,
    mock_config_entry: MockConfigEntry,
    ban: str = BAN,
) -> str:
    """Return the device id of the household of the BAN."""
    device = device_registry.async_get_device_by_identifier(
        (DOMAIN, ban), mock_config_entry.entry_id
    )
    assert device is not None
    return device.id

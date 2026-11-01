"""Test the ENGIE Belgium config flow."""

import asyncio
from collections.abc import Callable
from typing import Any, NoReturn
from unittest.mock import AsyncMock, MagicMock, patch

from aioengiebelgium import (
    EngieBeAuthenticationError,
    EngieBeCommunicationError,
    EngieBeError,
    EngieBeMfaError,
    MfaMethod,
    UsageDetailsResponse,
)
import pytest
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.components.recorder.common import async_wait_recording_done

from homeassistant import config_entries
from homeassistant.components.engie_be.const import (
    CONF_HISTORY,
    CONF_MFA_METHOD,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    HistoryDepth,
)
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import get_last_statistics
from homeassistant.config_entries import ConfigEntryState, ConfigFlowResult
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .conftest import (
    EMAIL,
    OFFTAKE_STATISTIC_ID,
    PASSWORD,
    SUBJECT,
    build_config_entry,
    build_usage_details,
    setup_entry,
)

USER_INPUT = {
    CONF_EMAIL: EMAIL,
    CONF_PASSWORD: PASSWORD,
    CONF_MFA_METHOD: "sms",
}


async def _skip_history(
    hass: HomeAssistant, result: ConfigFlowResult
) -> ConfigFlowResult:
    """Decline the usage history import on the history step."""
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "history"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HISTORY: HistoryDepth.NONE}
    )


async def test_full_flow(
    hass: HomeAssistant,
    mock_engie_client: MagicMock,
    mock_setup_entry: AsyncMock,
) -> None:
    """Test the full user config flow."""
    mock_engie_client.return_value.subject = SUBJECT
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    sentinel_session = MagicMock()
    with patch(
        "homeassistant.components.engie_be.config_flow.async_create_clientsession",
        return_value=sentinel_session,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "mfa"

    call_kwargs = (
        mock_engie_client.return_value.async_start_authentication.call_args.kwargs
    )
    assert call_kwargs["auth_session"] is sentinel_session
    assert call_kwargs["auth_session"] is not async_get_clientsession(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "123456"}
    )
    result = await _skip_history(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == EMAIL
    assert result["data"][CONF_EMAIL] == EMAIL
    assert result["data"]["access_token"] == "new-access-token"
    assert result["data"]["refresh_token"] == "new-refresh-token"
    assert result["options"] == {CONF_HISTORY: HistoryDepth.NONE}
    assert CONF_PASSWORD not in result["data"]
    assert result["result"].unique_id == SUBJECT
    assert result["result"].minor_version == 2
    mock_engie_client.return_value.async_get_usage_details.assert_not_called()


@pytest.mark.parametrize(
    ("exception", "error"),
    [
        (EngieBeCommunicationError("boom"), "cannot_connect"),
        (EngieBeAuthenticationError("boom"), "invalid_auth"),
        (EngieBeError("boom"), "unknown"),
    ],
)
async def test_user_step_errors(
    hass: HomeAssistant,
    mock_engie_client: MagicMock,
    mock_setup_entry: AsyncMock,
    exception: Exception,
    error: str,
) -> None:
    """Test recoverable errors on the user step."""
    mock_engie_client.return_value.subject = SUBJECT
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    mock_engie_client.return_value.async_start_authentication.side_effect = exception
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": error}

    mock_engie_client.return_value.async_start_authentication.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "mfa"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "123456"}
    )
    result = await _skip_history(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_setup_entry.called


@pytest.mark.parametrize(
    ("exception", "error"),
    [
        (EngieBeMfaError("bad code"), "invalid_mfa_code"),
        (EngieBeAuthenticationError("boom"), "invalid_auth"),
        (EngieBeCommunicationError("boom"), "cannot_connect"),
        (EngieBeError("boom"), "unknown"),
    ],
)
async def test_mfa_submit_errors_recovery(
    hass: HomeAssistant,
    mock_engie_client: MagicMock,
    mock_auth_flow: MagicMock,
    mock_setup_entry: AsyncMock,
    exception: Exception,
    error: str,
) -> None:
    """Test recoverable errors on the MFA submit step."""
    mock_engie_client.return_value.subject = SUBJECT
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["step_id"] == "mfa"

    mock_auth_flow.async_submit_mfa.side_effect = exception
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "000000"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "mfa"
    assert result["errors"] == {"base": error}

    mock_auth_flow.async_submit_mfa.side_effect = None
    mock_auth_flow.async_submit_mfa.return_value = ("access", "refresh")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "123456"}
    )
    result = await _skip_history(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_setup_entry.called


async def test_already_configured(
    hass: HomeAssistant,
    mock_engie_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test aborting when the JWT subject is already configured."""
    mock_engie_client.return_value.subject = SUBJECT
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(mock_config_entry, unique_id=SUBJECT)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "mfa"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "123456"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_two_accounts_get_distinct_entries(
    hass: HomeAssistant,
    mock_engie_client: MagicMock,
    mock_setup_entry: AsyncMock,
) -> None:
    """Test that two distinct JWT subjects create two distinct entries."""
    other_subject = "auth0|7a3c9e21b48f5d0261af3d77"
    for subject in (SUBJECT, other_subject):
        mock_engie_client.return_value.subject = subject
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
        assert result["step_id"] == "mfa"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"code": "123456"}
        )
        result = await _skip_history(hass, result)
        assert result["type"] is FlowResultType.CREATE_ENTRY

    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 2
    assert {entry.unique_id for entry in entries} == {SUBJECT, other_subject}


async def test_jwt_subject_missing_shows_form_error(
    hass: HomeAssistant,
    mock_engie_client: MagicMock,
    mock_setup_entry: AsyncMock,
) -> None:
    """Test that a missing JWT subject shows a form error on the MFA step."""
    mock_engie_client.return_value.subject = None

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["step_id"] == "mfa"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "123456"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "mfa"
    assert result["errors"] == {"base": "invalid_auth"}

    mock_engie_client.return_value.subject = SUBJECT
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "123456"}
    )
    result = await _skip_history(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_setup_entry.called


@pytest.mark.usefixtures("mock_setup_entry")
async def test_reauth_flow(
    hass: HomeAssistant,
    mock_engie_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test the reauthentication flow."""
    mock_engie_client.return_value.subject = SUBJECT
    mock_config_entry.add_to_hass(hass)

    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["description_placeholders"][CONF_EMAIL] == EMAIL

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "new-password"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "mfa"
    mock_engie_client.return_value.async_start_authentication.assert_called_once()
    assert mock_engie_client.return_value.async_start_authentication.call_args.args[
        :3
    ] == (EMAIL, "new-password", MfaMethod.SMS)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "123456"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data == {
        CONF_EMAIL: EMAIL,
        CONF_MFA_METHOD: "sms",
        CONF_ACCESS_TOKEN: "new-access-token",
        CONF_REFRESH_TOKEN: "new-refresh-token",
    }


@pytest.mark.parametrize(
    ("exception", "error"),
    [
        pytest.param(
            EngieBeCommunicationError("boom"), "cannot_connect", id="cannot_connect"
        ),
        pytest.param(
            EngieBeAuthenticationError("boom"), "invalid_auth", id="invalid_auth"
        ),
        pytest.param(EngieBeError("boom"), "unknown", id="unknown"),
    ],
)
@pytest.mark.usefixtures("mock_setup_entry")
async def test_reauth_flow_errors(
    hass: HomeAssistant,
    mock_engie_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    exception: Exception,
    error: str,
) -> None:
    """Test recoverable errors in the reauthentication flow."""
    mock_engie_client.return_value.subject = SUBJECT
    mock_config_entry.add_to_hass(hass)

    result = await mock_config_entry.start_reauth_flow(hass)

    mock_engie_client.return_value.async_start_authentication.side_effect = exception
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "new-password"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": error}

    mock_engie_client.return_value.async_start_authentication.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "new-password"}
    )
    assert result["step_id"] == "mfa"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "123456"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_ACCESS_TOKEN] == "new-access-token"


async def test_reauth_flow_unique_id_mismatch(
    hass: HomeAssistant,
    mock_engie_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test reauthentication aborts when signing in to a different account."""
    mock_engie_client.return_value.subject = "auth0|7a3c9e21b48f5d0261af3d77"
    mock_config_entry.add_to_hass(hass)

    result = await mock_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "new-password"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "123456"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unique_id_mismatch"
    assert mock_config_entry.data[CONF_ACCESS_TOKEN] == "access-token"


async def _start_flow_to_history(hass: HomeAssistant) -> ConfigFlowResult:
    """Sign in and return the history step."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "123456"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "history"
    return result


def _fail_usage(*args: Any) -> NoReturn:
    """Fail a usage request like a connection error."""
    raise EngieBeCommunicationError("boom")


def _hold_usage(
    client: MagicMock,
    answer: Callable[..., UsageDetailsResponse] = build_usage_details,
) -> asyncio.Event:
    """Hold the usage requests until the returned event is set, then answer them."""
    release = asyncio.Event()

    async def held_usage(*args: Any) -> UsageDetailsResponse:
        await release.wait()
        return answer(*args)

    client.async_get_usage_details.side_effect = held_usage
    return release


async def _last_offtake_sum(hass: HomeAssistant) -> float | None:
    """Return the last stored sum of the electricity offtake statistic."""
    await async_wait_recording_done(hass)
    last = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, OFFTAKE_STATISTIC_ID, False, {"sum"}
    )
    return last[OFFTAKE_STATISTIC_ID][0]["sum"]


@pytest.mark.usefixtures("frozen_afternoon", "mock_setup_entry")
async def test_history_import(
    hass: HomeAssistant,
    mock_engie_client: MagicMock,
) -> None:
    """Test the usage history is imported before the entry is created."""
    client = mock_engie_client.return_value
    client.subject = SUBJECT
    release = _hold_usage(client)
    result = await _start_flow_to_history(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HISTORY: HistoryDepth.YEAR}
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["step_id"] == "import_history"
    assert result["progress_action"] == "import_history"

    release.set()
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == SUBJECT
    assert result["options"] == {CONF_HISTORY: HistoryDepth.YEAR}
    assert await _last_offtake_sum(hass) == pytest.approx(326.0)


@pytest.mark.usefixtures("frozen_afternoon", "mock_setup_entry")
async def test_history_import_stores_rotated_tokens(
    hass: HomeAssistant,
    mock_engie_client: MagicMock,
) -> None:
    """Test the entry gets the tokens the client rotated during the import."""
    client = mock_engie_client.return_value
    client.subject = SUBJECT
    result = await _start_flow_to_history(hass)
    client.access_token = "rotated-access-token"
    client.refresh_token = "rotated-refresh-token"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HISTORY: HistoryDepth.YEAR}
    )
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_configure(result["flow_id"])

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == SUBJECT
    assert result["data"][CONF_ACCESS_TOKEN] == "rotated-access-token"
    assert result["data"][CONF_REFRESH_TOKEN] == "rotated-refresh-token"


@pytest.mark.usefixtures("frozen_afternoon", "mock_setup_entry")
async def test_history_import_failure_still_creates_entry(
    hass: HomeAssistant,
    mock_engie_client: MagicMock,
) -> None:
    """Test a failed history import still creates the entry after confirmation."""
    client = mock_engie_client.return_value
    client.subject = SUBJECT
    release = _hold_usage(client, _fail_usage)
    result = await _start_flow_to_history(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HISTORY: HistoryDepth.ALL}
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS

    release.set()
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "import_failed"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == SUBJECT
    assert result["options"] == {CONF_HISTORY: HistoryDepth.ALL}


@pytest.mark.parametrize(
    "exception",
    [
        pytest.param(EngieBeCommunicationError("boom"), id="library_error"),
        pytest.param(ValueError("unexpected"), id="unexpected_error"),
    ],
)
@pytest.mark.usefixtures("frozen_afternoon", "mock_setup_entry")
async def test_history_import_failing_at_once(
    hass: HomeAssistant,
    mock_engie_client: MagicMock,
    exception: Exception,
) -> None:
    """Test an import that fails before it yields still asks for confirmation."""
    client = mock_engie_client.return_value
    client.subject = SUBJECT
    result = await _start_flow_to_history(hass)
    client.async_get_customer_account_relations.side_effect = exception

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HISTORY: HistoryDepth.ALL}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "import_failed"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == SUBJECT


@pytest.mark.usefixtures("frozen_afternoon")
async def test_options_flow_imports_history(
    hass: HomeAssistant,
    mock_engie_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test choosing a history depth in the options imports it right away."""
    await setup_entry(hass, mock_config_entry)
    release = _hold_usage(mock_engie_client.return_value)

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_HISTORY: HistoryDepth.YEAR}
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS

    release.set()
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done(wait_background_tasks=True)

    assert mock_config_entry.options == {CONF_HISTORY: HistoryDepth.YEAR}
    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert await _last_offtake_sum(hass) == pytest.approx(326.0)


@pytest.mark.usefixtures("frozen_afternoon")
async def test_options_flow_import_failure_still_saves(
    hass: HomeAssistant,
    mock_engie_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test a failed import in the options still saves the depth after confirmation."""
    await setup_entry(hass, mock_config_entry)
    release = _hold_usage(mock_engie_client.return_value, _fail_usage)

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_HISTORY: HistoryDepth.MONTH}
    )
    release.set()
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "import_failed"

    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_config_entry.options == {CONF_HISTORY: HistoryDepth.MONTH}


@pytest.mark.usefixtures("frozen_afternoon")
async def test_options_flow_stops_importing(
    hass: HomeAssistant,
    mock_engie_client: MagicMock,
) -> None:
    """Test choosing no history saves without importing."""
    entry = build_config_entry(history=HistoryDepth.YEAR)
    await setup_entry(hass, entry)
    client = mock_engie_client.return_value
    client.async_get_usage_details.reset_mock()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_HISTORY: HistoryDepth.NONE}
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_HISTORY: HistoryDepth.NONE}
    assert entry.state is ConfigEntryState.LOADED
    client.async_get_usage_details.assert_not_called()


async def test_options_flow_requires_loaded_entry(
    hass: HomeAssistant,
    mock_engie_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test the options abort while the entry is not loaded."""
    mock_engie_client.return_value.async_get_customer_account_relations.side_effect = (
        EngieBeCommunicationError("boom")
    )
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "entry_not_loaded"

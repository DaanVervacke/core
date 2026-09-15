"""Test the ENGIE Belgium config flow."""

from unittest.mock import AsyncMock, MagicMock, patch

from aioengiebelgium import (
    EngieBeAuthenticationError,
    EngieBeCommunicationError,
    EngieBeError,
    EngieBeMfaError,
)
import pytest

from homeassistant import config_entries
from homeassistant.components.engie_be.const import CONF_MFA_METHOD, DOMAIN
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .conftest import PASSWORD, USERNAME

from tests.common import MockConfigEntry

USER_INPUT = {
    CONF_USERNAME: USERNAME,
    CONF_PASSWORD: PASSWORD,
    CONF_MFA_METHOD: "sms",
}


async def test_full_flow(
    hass: HomeAssistant,
    mock_config_flow_client: MagicMock,
    mock_setup_entry: AsyncMock,
) -> None:
    """Test the full user config flow with a JWT subject as unique id."""
    subject = "auth0|alice@example.com"
    mock_config_flow_client.subject = subject

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

    call_kwargs = mock_config_flow_client.async_start_authentication.call_args.kwargs
    assert call_kwargs["auth_session"] is sentinel_session
    assert call_kwargs["auth_session"] is not async_get_clientsession(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "123456"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == USERNAME
    assert result["data"][CONF_USERNAME] == USERNAME
    assert result["data"]["access_token"] == "new-access-token"
    assert result["data"]["refresh_token"] == "new-refresh-token"
    assert CONF_PASSWORD not in result["data"]
    assert mock_setup_entry.called
    entry = mock_setup_entry.call_args.kwargs.get("entry") or mock_setup_entry.call_args.args[1]
    assert entry.unique_id == subject


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
    mock_config_flow_client: MagicMock,
    mock_setup_entry: AsyncMock,
    exception: Exception,
    error: str,
) -> None:
    """Test recoverable errors on the user step."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    mock_config_flow_client.async_start_authentication.side_effect = exception
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": error}

    mock_config_flow_client.async_start_authentication.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "mfa"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "123456"}
    )
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
    mock_config_flow_client: MagicMock,
    mock_auth_flow: MagicMock,
    mock_setup_entry: AsyncMock,
    exception: Exception,
    error: str,
) -> None:
    """Test recoverable errors on the MFA submit step."""
    mock_config_flow_client.subject = "auth0|test-account"

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
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_setup_entry.called


async def test_already_configured(
    hass: HomeAssistant,
    mock_config_flow_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A duplicate JWT subject aborts at the MFA step."""
    subject = "auth0|user@example.com"
    mock_config_entry.unique_id = subject
    mock_config_flow_client.subject = subject
    mock_config_entry.add_to_hass(hass)

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
    hass: HomeAssistant, mock_config_flow_client: MagicMock
) -> None:
    """Two distinct JWT subjects produce two distinct config entries."""
    mock_config_flow_client.subject = "auth0|alice@example.com"

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "123456"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    mock_config_flow_client.subject = "auth0|bob@example.com"
    result2 = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result2 = await hass.config_entries.flow.async_configure(
        result2["flow_id"], USER_INPUT
    )
    result2 = await hass.config_entries.flow.async_configure(
        result2["flow_id"], {"code": "654321"}
    )
    assert result2["type"] is FlowResultType.CREATE_ENTRY

    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 2
    assert {entry.unique_id for entry in entries} == {
        "auth0|alice@example.com",
        "auth0|bob@example.com",
    }


async def test_jwt_subject_missing_shows_form_error(
    hass: HomeAssistant, mock_config_flow_client: MagicMock
) -> None:
    """If the access token has no JWT sub, the MFA step shows a form error."""
    mock_config_flow_client.subject = None

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
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "mfa"
    assert result["errors"] == {"base": "invalid_auth"}

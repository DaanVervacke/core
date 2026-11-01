"""Config flow for the ENGIE Belgium integration."""

import asyncio
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, override

from aioengiebelgium import (
    AuthFlow,
    EngieBeAuthenticationError,
    EngieBeClient,
    EngieBeCommunicationError,
    EngieBeError,
    EngieBeMfaError,
    MfaMethod,
)
import probatio

from homeassistant.components.recorder import get_instance
from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .const import (
    CONF_HISTORY,
    CONF_MFA_METHOD,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    LOGGER,
    USER_MANAGEMENT_URL,
    HistoryDepth,
)
from .coordinator import active_agreements, household_name
from .importer import async_import_household_statistics

_MFA_METHOD_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=[method.value for method in MfaMethod],
        translation_key="mfa_method",
    )
)
_CODE_SCHEMA = probatio.Schema({probatio.Required("code"): str})
_HISTORY_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_HISTORY, default=HistoryDepth.YEAR.value): (
            selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[depth.value for depth in HistoryDepth],
                    translation_key=CONF_HISTORY,
                )
            )
        )
    }
)


class EngieBeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for ENGIE Belgium."""

    VERSION = 1
    MINOR_VERSION = 2

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._email: str | None = None
        self._password: str | None = None
        self._mfa_method: MfaMethod = MfaMethod.SMS
        self._auth_flow: AuthFlow | None = None
        self._client: EngieBeClient | None = None
        self._tokens: tuple[str, str] | None = None
        self._depth = HistoryDepth.NONE
        self._import_task: asyncio.Task[bool] | None = None
        self._import_failed_shown = False

    async def _async_start_authentication(
        self, user_input: dict[str, Any]
    ) -> dict[str, str]:
        """Start authentication with ENGIE Belgium and return any form errors."""
        self._email = user_input[CONF_EMAIL]
        self._password = user_input[CONF_PASSWORD]
        self._mfa_method = MfaMethod(user_input[CONF_MFA_METHOD])

        session = async_create_clientsession(self.hass)
        client = EngieBeClient(session=session)
        self._client = client
        try:
            self._auth_flow = await client.async_start_authentication(
                self._email,
                self._password,
                self._mfa_method,
                auth_session=session,
            )
        except EngieBeAuthenticationError:
            return {"base": "invalid_auth"}
        except EngieBeCommunicationError:
            return {"base": "cannot_connect"}
        except EngieBeError:
            return {"base": "unknown"}
        return {}

    async def _async_submit_mfa(
        self, code: str
    ) -> tuple[dict[str, str], tuple[str, str] | None]:
        """Submit the MFA code and return (errors, tokens)."""
        assert self._auth_flow is not None
        try:
            tokens = await self._auth_flow.async_submit_mfa(code)
        except EngieBeMfaError:
            return {"base": "invalid_mfa_code"}, None
        except EngieBeAuthenticationError:
            return {"base": "invalid_auth"}, None
        except EngieBeCommunicationError:
            return {"base": "cannot_connect"}, None
        except EngieBeError:
            return {"base": "unknown"}, None
        return {}, tokens

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            errors = await self._async_start_authentication(user_input)
            if not errors:
                return await self.async_step_mfa()

        return self.async_show_form(
            step_id="user",
            data_schema=probatio.Schema(
                {
                    probatio.Required(CONF_EMAIL): str,
                    probatio.Required(CONF_PASSWORD): str,
                    probatio.Required(
                        CONF_MFA_METHOD, default=MfaMethod.SMS.value
                    ): _MFA_METHOD_SELECTOR,
                }
            ),
            description_placeholders={"user_management_url": USER_MANAGEMENT_URL},
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle reauthentication when the stored tokens are rejected."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the password and restart the MFA login."""
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()

        if user_input is not None:
            errors = await self._async_start_authentication(
                {
                    CONF_EMAIL: reauth_entry.data[CONF_EMAIL],
                    CONF_PASSWORD: user_input[CONF_PASSWORD],
                    CONF_MFA_METHOD: reauth_entry.data[CONF_MFA_METHOD],
                }
            )
            if not errors:
                return await self.async_step_mfa()

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=probatio.Schema({probatio.Required(CONF_PASSWORD): str}),
            description_placeholders={CONF_EMAIL: reauth_entry.data[CONF_EMAIL]},
            errors=errors,
        )

    async def async_step_mfa(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the MFA code entry step."""
        errors: dict[str, str] = {}

        if user_input is not None and self._email is not None:
            errors, tokens = await self._async_submit_mfa(user_input["code"])
            if not errors and tokens is not None:
                access_token, refresh_token = tokens
                assert self._client is not None
                subject = self._client.subject
                if subject is None:
                    return self.async_show_form(
                        step_id="mfa",
                        data_schema=_CODE_SCHEMA,
                        errors={"base": "invalid_auth"},
                    )
                await self.async_set_unique_id(subject)
                if self.source == SOURCE_REAUTH:
                    self._abort_if_unique_id_mismatch()
                    return self.async_update_reload_and_abort(
                        self._get_reauth_entry(),
                        data_updates={
                            CONF_ACCESS_TOKEN: access_token,
                            CONF_REFRESH_TOKEN: refresh_token,
                        },
                    )
                self._abort_if_unique_id_configured()
                self._tokens = tokens
                return await self.async_step_history()

        return self.async_show_form(
            step_id="mfa", data_schema=_CODE_SCHEMA, errors=errors
        )

    async def async_step_history(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask how much usage history to import into the energy dashboard."""
        if user_input is None:
            return self.async_show_form(step_id="history", data_schema=_HISTORY_SCHEMA)
        self._depth = HistoryDepth(user_input[CONF_HISTORY])
        if self._depth is HistoryDepth.NONE:
            return self._async_create_account_entry()
        return await self.async_step_import_history()

    async def async_step_import_history(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Import the usage history while showing progress."""
        if self._import_task is None:
            self._import_task = self.hass.async_create_task(
                self._async_import_history(), "engie_be history import"
            )
        if not self._import_task.done():
            return self.async_show_progress(
                step_id="import_history",
                progress_action="import_history",
                progress_task=self._import_task,
            )
        if self._import_task.result():
            return self.async_show_progress_done(next_step_id="finish")
        return self.async_show_progress_done(next_step_id="import_failed")

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the entry after a successful history import."""
        return self._async_create_account_entry()

    async def async_step_import_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Tell the user the import is retried after setup, then create the entry."""
        # A progress task that finishes at once forwards the history step's input here.
        if not self._import_failed_shown:
            self._import_failed_shown = True
            return self.async_show_form(step_id="import_failed")
        return self._async_create_account_entry()

    async def _async_import_history(self) -> bool:
        """Import the usage history of every household and return whether it worked."""
        client = self._client
        if TYPE_CHECKING:
            assert client is not None
        try:
            relations = await client.async_get_customer_account_relations()
            for ban, agreement in active_agreements(relations).items():
                await async_import_household_statistics(
                    self.hass, client, ban, household_name(ban, agreement), self._depth
                )
        except EngieBeError as err:
            LOGGER.warning("Importing the usage history failed: %s", err)
            return False
        await get_instance(self.hass).async_block_till_done()
        return True

    def _async_create_account_entry(self) -> ConfigFlowResult:
        """Create the config entry with the tokens and the chosen history depth."""
        if TYPE_CHECKING:
            assert self._email is not None
            assert self._tokens is not None
        access_token, refresh_token = self._tokens
        return self.async_create_entry(
            title=self._email,
            data={
                CONF_EMAIL: self._email,
                CONF_MFA_METHOD: self._mfa_method.value,
                CONF_ACCESS_TOKEN: access_token,
                CONF_REFRESH_TOKEN: refresh_token,
                CONF_HISTORY: self._depth.value,
            },
        )

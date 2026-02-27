"""Config flow for Qinghua Gas integration."""
from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("card_id"): str,
        vol.Required("user_name"): str,
        vol.Required("now_price"): str,
        vol.Required("token_account"): str,
        vol.Required("token_current_month"): str,
        vol.Required("token_last_month"): str,
    }
)


async def validate_input(hass: HomeAssistant, data: dict) -> dict[str, any]:
    """Validate the user input allows us to connect."""
    return {"title": f"燃气卡 {data['card_id']} - {data['user_name']}"}


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Qinghua Gas."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
                return self.async_create_entry(title=info["title"], data=user_input)
            except Exception:  # pylint: disable=broad-except
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Create the options flow."""
        return OptionsFlowHandler(config_entry)


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle an options flow for Qinghua Gas."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options_schema = vol.Schema(
            {
                vol.Optional(
                    "card_id",
                    default=self.config_entry.data.get("card_id", ""),
                ): str,
                vol.Optional(
                    "user_name",
                    default=self.config_entry.data.get("user_name", ""),
                ): str,
                vol.Optional(
                    "now_price",
                    default=self.config_entry.data.get("now_price", ""),
                ): str,
                vol.Optional(
                    "token_account",
                    default=self.config_entry.data.get("token_account", ""),
                ): str,
                vol.Optional(
                    "token_current_month",
                    default=self.config_entry.data.get("token_current_month", ""),
                ): str,
                vol.Optional(
                    "token_last_month",
                    default=self.config_entry.data.get("token_last_month", ""),
                ): str,
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=options_schema,
        )

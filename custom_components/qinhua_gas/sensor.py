"""Sensor platform for Qinghua Gas integration."""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

import aiohttp
from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Qinghua Gas sensor platform."""
    card_id = entry.data["card_id"]
    user_name = entry.data["user_name"]
    now_price = entry.data["now_price"]

    # 获取token，从entry.data或entry.options中获取
    token_account = entry.data.get("token_account") or entry.options.get("token_account", "")
    token_current_month = entry.data.get("token_current_month") or entry.options.get("token_current_month", "")
    token_last_month = entry.data.get("token_last_month") or entry.options.get("token_last_month", "")

    entities = [
        QinghuaGasSensor(hass, entry, card_id, user_name, now_price, token_account, token_current_month, token_last_month),
    ]

    async_add_entities(entities)


class QinghuaGasSensor(SensorEntity):
    """Representation of a Qinghua Gas sensor."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        card_id: str,
        user_name: str,
        now_price: str,
        token_account: str,
        token_current_month: str,
        token_last_month: str,
    ) -> None:
        """Initialize the sensor."""
        self.hass = hass
        self.entry = entry
        self.card_id = card_id
        self.user_name = user_name
        self.now_price = now_price
        self.token_account = token_account
        self.token_current_month = token_current_month
        self.token_last_month = token_last_month
        self._attr_name = f"sensor.ranqi_{card_id}"
        self._attr_unique_id = f"{entry.entry_id}_gas"
        self._attr_native_value = None
        self._attr_native_unit_of_measurement = "元"
        self.scan_interval = timedelta(hours=2)  # 每2小时更新一次
        self.account_data = None
        self.current_month_data = None
        self.last_month_data = None

    async def async_update(self) -> None:
        """Fetch new state data for the sensor."""
        # 第一次请求 - 账户信息
        account_data = {
            "cardId": self.card_id,
            "userName": self.user_name,
            "nowPrice": self.now_price
        }
        url = "http://wkf.qhgas.com/rs/WX/meterRead"
        self.account_data = await self._make_request(account_data, self.token_account, url)

        # 第二次请求 - 本月用量
        current_month_data = {
            "f_card_id": self.card_id,
            "dateType": "本月",
            "groupname": "day"
        }
        url = "http://wkf.qhgas.com/rs/WX/getFulseAnalysis"
        self.current_month_data = await self._make_request(current_month_data, self.token_current_month, url)

        # 第三次请求 - 上月用量
        last_month_data = {
            "f_card_id": self.card_id,
            "dateType": "上月",
            "groupname": "day"
        }
        url = "http://wkf.qhgas.com/rs/WX/getFulseAnalysis"
        self.last_month_data = await self._make_request(last_month_data, self.token_last_month, url)

        # 解析账户信息，获取第一组数据的f_jval作为实体值
        if self.account_data and isinstance(self.account_data, list) and len(self.account_data) > 0:
            first_record = self.account_data[0]
            self._attr_native_value = first_record.get("f_jval")

    async def _make_request(self, data: dict, token: str, url: str) -> dict[str, Any] | None:
        """Make HTTP request with given data and token."""
        payload = {
            "data": data,
            "tokenS": token
        }

        # 转换为紧凑的JSON字符串（无空格、无缩进）
        json_payload = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    data=json_payload,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 NetType/WIFI MicroMessenger/7.0.20.1781(0x6700143B) WindowsWechat(0x63090b19) XWEB/14185 Flue",
                        "Accept": "application/json, text/plain, */*",
                        "X-Requested-With": "XMLHttpRequest",
                        "Origin": "http://wkf.qhgas.com",
                        "Accept-Language": "zh-CN,zh;q=0.9",
                    },
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    if response.status == 200:
                        return await response.json()
                    else:
                        response_text = await response.text()
                        _LOGGER.error(f"Request failed with status {response.status}")
                        _LOGGER.error(f"URL: {url}")
                        _LOGGER.error(f"Payload: {json_payload}")
                        _LOGGER.error(f"Response: {response_text}")
                        return None
        except aiohttp.ClientError as err:
            _LOGGER.error(f"Request error: {err}")
            _LOGGER.error(f"URL: {url}")
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional state attributes."""
        attributes = {}

        if self.account_data and isinstance(self.account_data, list) and len(self.account_data) > 0:
            first_record = self.account_data[0]
            attributes["更新日期"] = first_record.get("f_hand_date")
            attributes["累计用气"] = first_record.get("f_tablebase")
            attributes["单价"] = first_record.get("f_now_price")

        # 本月用气，如果为空则保留空json
        attributes["本月用气"] = self.current_month_data if self.current_month_data else {}

        # 上月用气，如果为空则保留空json
        attributes["上月用气"] = self.last_month_data if self.last_month_data else {}

        return attributes

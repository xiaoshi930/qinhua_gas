"""Sensor platform for Qinghua Gas integration."""
from __future__ import annotations

import asyncio
import json
import logging
import math
from datetime import datetime, timedelta
from typing import Any

import aiohttp
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN,
    CONF_IS_PREPAID,
    CONF_LADDER_LEVEL_1,
    CONF_LADDER_LEVEL_2,
    CONF_LADDER_PRICE_1,
    CONF_LADDER_PRICE_2,
    CONF_LADDER_PRICE_3,
    CONF_YEAR_LADDER_START,
)
from .statistics import (
    async_import_gas_cost_statistics,
    async_import_gas_statistics,
)
from .storage import QinhuaGasStorage

_LOGGER = logging.getLogger(__name__)

# 燃气阶梯默认值
DEFAULT_LADDER_LEVEL_1 = 480
DEFAULT_LADDER_LEVEL_2 = 660
DEFAULT_LADDER_PRICE_1 = 2.18
DEFAULT_LADDER_PRICE_2 = 2.62
DEFAULT_LADDER_PRICE_3 = 3.27
DEFAULT_YEAR_LADDER_START = "0101"


def build_device_info(card_id: str) -> DeviceInfo:
    """构造同卡号下多个实体共用的设备信息。

    同一卡号的实体必须使用**同一组 identifiers**，否则 HA 会为每个实体各建一个设备。
    """
    return DeviceInfo(
        identifiers={(DOMAIN, card_id)},
        name=f"秦华燃气 {card_id}",
        manufacturer="秦华燃气",
        model="燃气表",
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Qinghua Gas sensor platform."""
    config = entry.data

    # coordinator 在 __init__.py 中创建并完成首次刷新，这里直接复用
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    entities = [
        QinhuaGasSensor(coordinator, config),
        QinhuaGasTotalGasSensor(coordinator, config),
        QinhuaGasTotalCostSensor(coordinator, config),
    ]

    hass.data[DOMAIN][entry.entry_id]["entities"] = entities

    async_add_entities(entities, True)


class QinhuaGasCoordinator(DataUpdateCoordinator):
    """Coordinator for Qinghua Gas data."""

    def __init__(self, hass: HomeAssistant, config: dict):
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(hours=2),
        )
        self.config = config
        self.last_update_time = datetime.now()

        # 初始化持久化存储
        card_id = config.get("card_id", "default")
        self._storage = QinhuaGasStorage(hass, card_id)
        self.data = None

    async def async_load_storage(self) -> None:
        """Load persistent storage data asynchronously."""
        await self._storage.async_load()
        if self._storage.data.get("dayList"):
            self.data = dict(self._storage.data)

    async def _async_update_data(self):
        """Fetch data from API."""
        try:
            card_id = self.config["card_id"]
            user_name = self.config["user_name"]
            now_price = self.config["now_price"]
            token_account = self.config.get("token_account") or self.config.get("token_account", "")
            token_current_month = self.config.get("token_current_month") or self.config.get("token_current_month", "")
            token_last_month = self.config.get("token_last_month") or self.config.get("token_last_month", "")

            # 请求账户信息
            account_payload = {
                "cardId": card_id,
                "userName": user_name,
                "nowPrice": now_price,
            }
            account_data = await self._make_request(
                account_payload, token_account, "http://wkf.qhgas.com/rs/WX/meterRead"
            )

            # 请求本月用量
            current_month_payload = {
                "f_card_id": card_id,
                "dateType": "本月",
                "groupname": "day",
            }
            current_month_data = await self._make_request(
                current_month_payload, token_current_month, "http://wkf.qhgas.com/rs/WX/getFulseAnalysis"
            )

            # 请求上月用量
            last_month_payload = {
                "f_card_id": card_id,
                "dateType": "上月",
                "groupname": "day",
            }
            last_month_data = await self._make_request(
                last_month_payload, token_last_month, "http://wkf.qhgas.com/rs/WX/getFulseAnalysis"
            )

            if not account_data:
                _LOGGER.warning("未获取到账户数据，使用持久化数据")
                if self._storage.data.get("dayList"):
                    self.data = dict(self._storage.data)
                    # 接口不可用也要回填长期统计：能源面板的历史来自本地 dayList，
                    # 跟接口通不通没关系，否则接口抽风期间面板会一直缺历史
                    await self._import_statistics(card_id)
                    return self.data
                return {}

            # 处理数据
            processed = self._process_data(account_data, current_month_data, last_month_data)

            # 先更新到持久化存储，再读取到HA
            if processed:
                merged = await self.hass.async_add_executor_job(self._storage.update, processed)
                self.data = merged
            else:
                if self._storage.data.get("dayList"):
                    self.data = dict(self._storage.data)
                else:
                    self.data = {}

            self.last_update_time = datetime.now()

            # 回填 HA 长期统计，供能源面板「燃气消耗 / 成本跟踪」显示历史曲线
            await self._import_statistics(card_id)

            return self.data

        except Exception as ex:
            _LOGGER.error("更新燃气数据失败: %s", ex)
            raise UpdateFailed(f"Error updating gas data: {ex}")

    async def _import_statistics(self, card_id: str) -> None:
        """把历史日用气量 / 日燃气费回填到 HA 长期统计（能源面板依赖它）。

        单独抽成一个方法是为了让回填与**接口是否可用解耦**：只要 storage 里
        有 dayList 就尝试回填，避免接口抽风时能源面板一直没有历史。
        两份统计互不影响，各自失败只记 warning，不影响实体数据。
        """
        # 回填 HA 长期统计，供能源面板「燃气消耗」显示历史曲线
        try:
            await async_import_gas_statistics(self.hass, self._storage, card_id)
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.warning("导入燃气长期统计失败: %s", ex)

        # 回填「燃气费」统计，供能源面板「成本跟踪 → 统计」显示历史成本曲线
        try:
            await async_import_gas_cost_statistics(self.hass, self._storage, card_id)
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.warning("导入燃气费长期统计失败: %s", ex)

    async def _make_request(self, data: dict, token: str, url: str) -> dict[str, Any] | None:
        """Make HTTP request with given data and token."""
        payload = {
            "data": data,
            "tokenS": token,
        }
        json_payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

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
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as response:
                    if response.status == 200:
                        return await response.json()
                    else:
                        response_text = await response.text()
                        _LOGGER.error("请求失败 status=%s url=%s response=%s", response.status, url, response_text)
                        return None
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as err:
            # 超时（TimeoutError 的 str 为空）也在这里兜住，返回 None 让上层回退持久化数据
            _LOGGER.warning("请求失败或超时: %s url=%s", err or "Timeout", url)
            return None

    def _process_data(self, account_data, current_month_data, last_month_data):
        """Process raw API data into standardized format."""
        try:
            # 解析账户信息
            balance = 0
            date_str = ""
            consumer_name = ""
            if account_data and isinstance(account_data, list) and len(account_data) > 0:
                first_record = account_data[0]
                balance = float(first_record.get("f_jval", 0))
                date_str = first_record.get("f_hand_date", "")
                # 清理日期末尾的 .0 后缀（如 "2026-05-29 20:10:00.0"）
                if date_str and date_str.endswith(".0"):
                    date_str = date_str[:-2]
                consumer_name = self.config.get("user_name", "")

            # 合并本月和上月的日数据
            day_list_raw = []
            if current_month_data and isinstance(current_month_data, list):
                day_list_raw.extend(current_month_data)
            if last_month_data and isinstance(last_month_data, list):
                day_list_raw.extend(last_month_data)

            # 转换为标准日数据格式
            day_list = self._convert_day_list(day_list_raw)

            # 计算每日费用
            day_list = self._calculate_daily_cost(day_list)
            day_list.reverse()  # 改为最新日期在前

            # 处理月数据
            month_list = self._process_month_data(day_list)

            # 处理年数据
            year_list = self._process_year_data(month_list)

            return {
                "date": date_str,
                "balance": balance,
                "dayList": day_list,
                "monthList": month_list,
                "yearList": year_list,
                "consumer_name": consumer_name,
            }
        except Exception as ex:
            _LOGGER.error("处理燃气数据失败: %s", ex)
            return {}

    def _convert_day_list(self, raw_data: list) -> list:
        """Convert raw daily data to standard format.

        Handles various possible field names from the API.
        """
        result = []
        for item in raw_data:
            if not isinstance(item, dict):
                continue

            # 尝试多种日期字段名
            day = (
                item.get("f_date")
                or item.get("f_hand_date")
                or item.get("date")
                or item.get("day")
                or ""
            )
            if not day:
                continue

            # 格式化日期为 YYYY-MM-DD
            day = self._normalize_date(day)
            if not day:
                continue

            # 尝试多种用气量字段名
            day_ele_num = float(
                item.get("f_gas")
                or item.get("f_oughtamount")
                or item.get("f_consumption")
                or item.get("dayEleNum")
                or item.get("gas")
                or 0
            )

            result.append({
                "day": day,
                "dayEleNum": day_ele_num,
                "dayEleCost": 0,
            })

        return result

    def _normalize_date(self, date_str: str) -> str:
        """Normalize date string to YYYY-MM-DD format."""
        if not date_str:
            return ""
        date_str = str(date_str).strip()
        # 已经是标准格式
        if len(date_str) == 10 and date_str[4] == "-" and date_str[7] == "-":
            return date_str
        # YYYYMMDD 格式
        if len(date_str) == 8 and date_str.isdigit():
            return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
        # 尝试其他格式
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d", "%Y.%m.%d"):
            try:
                dt = datetime.strptime(date_str, fmt)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue
        return ""

    def _calculate_daily_cost(self, day_list: list) -> list:
        """Calculate daily gas cost based on year-ladder billing."""
        ladder_level_1 = self.config.get(CONF_LADDER_LEVEL_1, DEFAULT_LADDER_LEVEL_1)
        ladder_level_2 = self.config.get(CONF_LADDER_LEVEL_2, DEFAULT_LADDER_LEVEL_2)
        price_1 = self.config.get(CONF_LADDER_PRICE_1, DEFAULT_LADDER_PRICE_1)
        price_2 = self.config.get(CONF_LADDER_PRICE_2, DEFAULT_LADDER_PRICE_2)
        price_3 = self.config.get(CONF_LADDER_PRICE_3, DEFAULT_LADDER_PRICE_3)
        year_ladder_start = self.config.get(CONF_YEAR_LADDER_START, DEFAULT_YEAR_LADDER_START)

        for item in day_list:
            day_ele_num = item["dayEleNum"]
            current_day = item["day"]

            # 计算年阶梯
            current_year = int(current_day.split("-")[0])
            current_month = int(current_day.split("-")[1])
            current_day_int = int(current_day.split("-")[2])

            start_month = int(year_ladder_start[:2])
            start_day_int = int(year_ladder_start[2:])

            if (current_month < start_month) or (current_month == start_month and current_day_int < start_day_int):
                ladder_year = current_year - 1
            else:
                ladder_year = current_year

            year_ladder_start_date = f"{ladder_year}-{year_ladder_start[:2]}-{year_ladder_start[2:]}"

            # 计算年累计用气量
            year_accumulated = 0
            for data in day_list:
                if data["day"] >= year_ladder_start_date and data["day"] <= current_day:
                    year_accumulated += data["dayEleNum"]

            # 根据阶梯计算费用
            day_cost = self._calculate_ladder_cost(
                day_ele_num, year_accumulated,
                ladder_level_1, ladder_level_2,
                price_1, price_2, price_3,
            )
            item["dayEleCost"] = round(day_cost, 2)

        return day_list

    def _calculate_ladder_cost(
        self, day_ele_num, year_accumulated,
        ladder_level_1, ladder_level_2,
        price_1, price_2, price_3,
    ):
        """Calculate cost based on year-ladder billing."""
        if day_ele_num == 0:
            return 0

        if year_accumulated <= ladder_level_1:
            return day_ele_num * price_1
        elif year_accumulated <= ladder_level_2:
            if year_accumulated - day_ele_num <= ladder_level_1:
                first_part = ladder_level_1 - (year_accumulated - day_ele_num)
                second_part = day_ele_num - first_part
                return first_part * price_1 + second_part * price_2
            else:
                return day_ele_num * price_2
        else:
            if year_accumulated - day_ele_num <= ladder_level_1:
                first_part = ladder_level_1 - (year_accumulated - day_ele_num)
                remaining = day_ele_num - first_part
                if year_accumulated - day_ele_num + first_part + remaining <= ladder_level_2:
                    second_part = ladder_level_2 - (year_accumulated - day_ele_num + first_part)
                    third_part = remaining - second_part
                    return first_part * price_1 + second_part * price_2 + third_part * price_3
                else:
                    return day_ele_num * price_3
            elif year_accumulated - day_ele_num <= ladder_level_2:
                second_part = ladder_level_2 - (year_accumulated - day_ele_num)
                third_part = day_ele_num - second_part
                return second_part * price_2 + third_part * price_3
            else:
                return day_ele_num * price_3

    def _process_month_data(self, day_list: list) -> list:
        """Process monthly data from daily data."""
        try:
            month_map = {}
            for day_item in day_list:
                month_str = day_item["day"][:7]  # YYYY-MM
                if month_str not in month_map:
                    month_map[month_str] = {
                        "month": month_str,
                        "monthEleNum": 0,
                        "monthEleCost": 0,
                    }
                month_map[month_str]["monthEleNum"] += day_item.get("dayEleNum", 0)
                month_map[month_str]["monthEleCost"] += day_item.get("dayEleCost", 0)

            result = []
            for month_data in month_map.values():
                result.append({
                    "month": month_data["month"],
                    "monthEleNum": round(month_data["monthEleNum"], 2),
                    "monthEleCost": round(month_data["monthEleCost"], 2),
                })

            return sorted(result, key=lambda x: x["month"], reverse=True)
        except Exception as ex:
            _LOGGER.error("处理月数据失败: %s", ex)
            return []

    def _process_year_data(self, month_list: list) -> list:
        """Process yearly data from monthly data."""
        try:
            year_map = {}
            for month_data in month_list:
                year = month_data["month"].split("-")[0]
                if year not in year_map:
                    year_map[year] = {
                        "year": year,
                        "yearEleNum": 0,
                        "yearEleCost": 0,
                    }
                year_map[year]["yearEleNum"] += month_data.get("monthEleNum", 0)
                year_map[year]["yearEleCost"] += month_data.get("monthEleCost", 0)

            result = []
            for year_data in year_map.values():
                result.append({
                    "year": year_data["year"],
                    "yearEleNum": round(year_data["yearEleNum"], 2),
                    "yearEleCost": round(year_data["yearEleCost"], 2),
                })

            return sorted(result, key=lambda x: x["year"], reverse=True)
        except Exception as ex:
            _LOGGER.error("处理年数据失败: %s", ex)
            return []


class QinhuaGasSensor(SensorEntity):
    """Representation of a Qinghua Gas sensor."""

    def __init__(self, coordinator: QinhuaGasCoordinator, config: dict):
        """Initialize the sensor."""
        self.coordinator = coordinator
        self.config = config
        card_id = config.get("card_id", "")
        self._attr_unique_id = f"qinhua_gas_{card_id}"
        self._attr_name = f"秦华燃气 {card_id}"
        self._attr_icon = "mdi:fire"
        self._attr_native_unit_of_measurement = "元"
        self._card_id = card_id
        self._attr_device_info = build_device_info(card_id)

    @property
    def available(self):
        """Return if entity is available."""
        return self.coordinator.data is not None and bool(self.coordinator.data.get("dayList"))

    @property
    def native_value(self):
        """Return the state of the sensor."""
        if self.coordinator.data:
            return self.coordinator.data.get("balance", 0)
        return 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the state attributes."""
        attrs = {}

        if self.coordinator.data:
            day_list = self.coordinator.data.get("dayList", [])
            if day_list:
                sorted_days = sorted(day_list, key=lambda x: x["day"], reverse=True)
                recent_days = sorted_days[:7]

                if recent_days:
                    daily_costs = [day.get("dayEleCost", 0) for day in recent_days]
                    avg_daily_cost = sum(daily_costs) / len(daily_costs)

                    balance = self.coordinator.data.get("balance", 0)
                    if avg_daily_cost > 0:
                        estimated_days = balance / avg_daily_cost
                        try:
                            latest_day = sorted_days[0]["day"]
                            latest_date = datetime.strptime(latest_day, "%Y-%m-%d")
                            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                            days_since_latest = (today - latest_date).days
                            remaining_days = max(0, estimated_days - days_since_latest)

                            attrs["日均消费"] = round(avg_daily_cost, 2)
                            attrs["剩余天数"] = math.ceil(remaining_days)
                            attrs["预付费"] = "是" if self.config.get(CONF_IS_PREPAID, False) else "否"
                        except (ValueError, IndexError) as e:
                            _LOGGER.error("计算剩余天数时出错: %s", e)

            attrs.update({
                "date": self.coordinator.data.get("date", ""),
                "daylist": self.coordinator.data.get("dayList", []),
                "monthlist": self.coordinator.data.get("monthList", []),
                "yearlist": self.coordinator.data.get("yearList", []),
            })

        # 计费标准信息
        ladder_level_1 = self.config.get(CONF_LADDER_LEVEL_1, DEFAULT_LADDER_LEVEL_1)
        ladder_level_2 = self.config.get(CONF_LADDER_LEVEL_2, DEFAULT_LADDER_LEVEL_2)
        price_1 = self.config.get(CONF_LADDER_PRICE_1, DEFAULT_LADDER_PRICE_1)
        price_2 = self.config.get(CONF_LADDER_PRICE_2, DEFAULT_LADDER_PRICE_2)
        price_3 = self.config.get(CONF_LADDER_PRICE_3, DEFAULT_LADDER_PRICE_3)
        year_ladder_start = self.config.get(CONF_YEAR_LADDER_START, DEFAULT_YEAR_LADDER_START)

        billing_attrs = {"计费标准": "年阶梯"}

        # 获取当前阶梯档和累计用气量
        ladder_info = self._get_ladder_info()
        billing_attrs.update(ladder_info)

        billing_attrs["年阶梯第2档起始气量"] = ladder_level_1
        billing_attrs["年阶梯第3档起始气量"] = ladder_level_2
        billing_attrs["年阶梯第1档气价"] = price_1
        billing_attrs["年阶梯第2档气价"] = price_2
        billing_attrs["年阶梯第3档气价"] = price_3

        # 当前年阶梯日期范围
        current_date = datetime.now()
        current_year = current_date.year
        current_month = current_date.month
        current_day_int = current_date.day
        start_month = int(year_ladder_start[:2])
        start_day = int(year_ladder_start[2:])

        if (current_month < start_month) or (current_month == start_month and current_day_int < start_day):
            ladder_year = current_year - 1
        else:
            ladder_year = current_year

        year_ladder_start_date_formatted = f"{ladder_year}.{year_ladder_start[:2]}.{year_ladder_start[2:]}"
        start_date_next_year = datetime(ladder_year + 1, start_month, start_day)
        end_date = start_date_next_year - timedelta(days=1)
        year_ladder_end_date_formatted = f"{end_date.year}.{end_date.month:02d}.{end_date.day:02d}"

        billing_attrs["当前年阶梯起始日期"] = year_ladder_start_date_formatted
        billing_attrs["当前年阶梯结束日期"] = year_ladder_end_date_formatted

        attrs["计费标准"] = billing_attrs

        attrs["数据源"] = "秦华燃气"
        attrs["最后同步日期"] = self.coordinator.last_update_time.strftime("%Y-%m-%d %H:%M:%S")

        return attrs

    def _get_ladder_info(self):
        """获取当前阶梯档和累计用气量信息."""
        try:
            if not self.coordinator.data or not self.coordinator.data.get("dayList"):
                return {}

            day_list = self.coordinator.data.get("dayList", [])
            if not day_list:
                return {}

            sorted_days = sorted(day_list, key=lambda x: x["day"], reverse=True)
            latest_day_data = sorted_days[0]

            ladder_level_1 = self.config.get(CONF_LADDER_LEVEL_1, DEFAULT_LADDER_LEVEL_1)
            ladder_level_2 = self.config.get(CONF_LADDER_LEVEL_2, DEFAULT_LADDER_LEVEL_2)
            year_ladder_start = self.config.get(CONF_YEAR_LADDER_START, DEFAULT_YEAR_LADDER_START)

            current_day = latest_day_data["day"]
            current_year = int(current_day.split("-")[0])
            current_month = int(current_day.split("-")[1])
            current_day_int = int(current_day.split("-")[2])

            start_month = int(year_ladder_start[:2])
            start_day_int = int(year_ladder_start[2:])

            if (current_month < start_month) or (current_month == start_month and current_day_int < start_day_int):
                ladder_year = current_year - 1
            else:
                ladder_year = current_year

            year_ladder_start_date = f"{ladder_year}-{year_ladder_start[:2]}-{year_ladder_start[2:]}"

            year_accumulated = 0
            for data in day_list:
                if data["day"] >= year_ladder_start_date and data["day"] <= current_day:
                    year_accumulated += data["dayEleNum"]

            if year_accumulated <= ladder_level_1:
                current_ladder = "第1档"
            elif year_accumulated <= ladder_level_2:
                current_ladder = "第2档"
            else:
                current_ladder = "第3档"

            return {
                "当前年阶梯档": current_ladder,
                "年阶梯累计用气量": round(year_accumulated, 2),
            }
        except Exception as ex:
            _LOGGER.error("获取阶梯信息失败: %s", ex)
            return {}

    async def async_added_to_hass(self):
        """When entity is added to hass."""
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.async_add_listener(self.async_write_ha_state))


class QinhuaGasTotalGasSensor(SensorEntity):
    """累计用气量传感器，供 HA 能源面板的「燃气消耗」使用。

    取值 = 全部日用气量之和（storage 的 dayList 只增不删、不重算历史日，故单调不减）。

    刻意**不设置** state_class：statistics.py 已把同一份日用量回填为外部长期统计
    （statistic_id `qinhua_gas:total_gas_<card_id>`，含完整历史）。若本实体再带
    state_class，Recorder 会为它额外生成一份同源统计，两条会并列出现在能源面板的
    数据源下拉里、且显示名相同，极易被同时选中，导致用气量被算两遍。
    """

    _attr_device_class = SensorDeviceClass.GAS
    _attr_native_unit_of_measurement = UnitOfVolume.CUBIC_METERS
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: QinhuaGasCoordinator, config: dict):
        """Initialize the sensor."""
        self.coordinator = coordinator
        self.config = config
        card_id = config.get("card_id", "")
        self._card_id = card_id
        self._attr_unique_id = f"qinhua_gas_{card_id}_total_gas"
        self._attr_name = f"秦华燃气 {card_id} 累计用气"
        self._attr_icon = "mdi:fire"
        self._attr_device_info = build_device_info(card_id)
        if card_id:
            self.entity_id = f"sensor.qinhua_gas_{card_id}_total_gas"

    @property
    def available(self):
        """Return if entity is available."""
        return self.coordinator.data is not None and bool(
            self.coordinator.data.get("dayList")
        )

    @property
    def native_value(self):
        """Return lifetime cumulative gas usage in m³."""
        if not self.coordinator.data:
            return 0
        return round(
            sum(
                float(day.get("dayEleNum", 0) or 0)
                for day in self.coordinator.data.get("dayList", [])
            ),
            2,
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return state attributes."""
        data = self.coordinator.data or {}
        current_year = str(datetime.now().year)
        current_year_num = next(
            (
                item.get("yearEleNum", 0)
                for item in data.get("yearList", [])
                if item.get("year") == current_year
            ),
            0,
        )
        return {
            "当年用气": round(float(current_year_num or 0), 2),
            "数据源": "秦华燃气",
            "最后同步日期": self.coordinator.last_update_time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    async def async_added_to_hass(self):
        """When entity is added to hass."""
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.async_add_listener(self.async_write_ha_state))


class QinhuaGasTotalCostSensor(SensorEntity):
    """累计燃气费传感器，供 HA 能源面板的成本跟踪使用。

    取值 = 全部日费用（dayEleCost）之和。

    属性选择说明：
    - `device_class = MONETARY` + `state_class = TOTAL`：HA 的
      `DEVICE_CLASS_STATE_CLASSES[MONETARY]` 只允许 `total`。Recorder 会为它生成
      长期统计，于是它可以直接出现在「能源」面板 → 燃气 → 成本跟踪的
      「使用跟踪总成本的实体」（stat_cost）下拉里。
    - 必须是 `total` 而不是 `total_increasing`：阶梯气价参数可改、日费用会整段重算，
      累计值允许下降；`total_increasing` 遇到下降会被 recorder 当成换表重置，
      把整个数值记成一笔新消费。
    - 这条统计是**实体统计**（statistic_id 就是 `sensor.xxx`），与累计用气的外部
      统计（`qinhua_gas:total_gas_xxx`）互不相干，不会重复计量。
    """

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "元"
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: QinhuaGasCoordinator, config: dict):
        """Initialize the sensor."""
        self.coordinator = coordinator
        self.config = config
        card_id = config.get("card_id", "")
        self._card_id = card_id
        self._attr_unique_id = f"qinhua_gas_{card_id}_total_cost"
        self._attr_name = f"秦华燃气 {card_id} 累计燃气费"
        self._attr_icon = "mdi:cash-multiple"
        self._attr_device_info = build_device_info(card_id)
        if card_id:
            self.entity_id = f"sensor.qinhua_gas_{card_id}_total_cost"

    @property
    def available(self):
        """Return if entity is available."""
        return self.coordinator.data is not None and bool(
            self.coordinator.data.get("dayList")
        )

    @property
    def native_value(self):
        """Return lifetime cumulative gas cost in CNY."""
        if not self.coordinator.data:
            return 0
        return round(
            sum(
                float(day.get("dayEleCost", 0) or 0)
                for day in self.coordinator.data.get("dayList", [])
            ),
            2,
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return state attributes."""
        data = self.coordinator.data or {}
        current_year = str(datetime.now().year)
        current_year_cost = next(
            (
                item.get("yearEleCost", 0)
                for item in data.get("yearList", [])
                if item.get("year") == current_year
            ),
            0,
        )
        return {
            "当年燃气费": round(float(current_year_cost or 0), 2),
            "累计用气": round(
                sum(
                    float(day.get("dayEleNum", 0) or 0)
                    for day in data.get("dayList", [])
                ),
                2,
            ),
            "数据源": "秦华燃气",
            "最后同步日期": self.coordinator.last_update_time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    async def async_added_to_hass(self):
        """When entity is added to hass."""
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.async_add_listener(self.async_write_ha_state))

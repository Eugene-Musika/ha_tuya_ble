"""Verify that entity commands await datapoint writes and propagate failures."""

import asyncio
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock

from homeassistant.components.climate import ClimateEntityDescription
from homeassistant.components.climate.const import HVACMode, PRESET_AWAY, PRESET_NONE
from homeassistant.components.number import NumberEntityDescription
from homeassistant.components.select import SelectEntityDescription
from homeassistant.components.text import TextEntityDescription
from homeassistant.exceptions import HomeAssistantError

from custom_components.tuya_ble.climate import TuyaBLEClimate, TuyaBLEClimateMapping
from custom_components.tuya_ble.number import (
    TuyaBLENumber,
    TuyaBLENumberMapping,
    set_fingerbot_program_position,
    set_fingerbot_program_repeat_count,
)
from custom_components.tuya_ble.select import TuyaBLESelect, TuyaBLESelectMapping
from custom_components.tuya_ble.text import (
    TuyaBLEText,
    TuyaBLETextMapping,
    set_fingerbot_program,
)
from custom_components.tuya_ble.tuya_ble import TuyaBLEDataPointType


class EntityCommandTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.datapoint = SimpleNamespace(
            value=b"\x00\x02\x32\x01\x0a\x00\x05", set_value=AsyncMock()
        )
        self.datapoints = Mock()
        self.datapoints.get_or_create.return_value = self.datapoint
        self.datapoints.__getitem__ = Mock(return_value=self.datapoint)
        self.product = SimpleNamespace(fingerbot=SimpleNamespace(program=121))

    def entity(self, cls, mapping):
        entity = cls.__new__(cls)
        entity._device = SimpleNamespace(datapoints=self.datapoints)
        entity._product = self.product
        entity._mapping = mapping
        entity._hass = SimpleNamespace(create_task=Mock())
        return entity

    async def assert_write(self, command, expected):
        failure = RuntimeError("BLE write failed")
        self.datapoint.set_value.side_effect = failure
        with self.assertRaises(RuntimeError) as raised:
            await command()
        self.assertIs(raised.exception, failure)
        self.datapoint.set_value.assert_awaited_once_with(expected)

        self.datapoint.set_value.reset_mock(side_effect=True)
        await command()
        self.datapoint.set_value.assert_awaited_once_with(expected)

    async def test_number_scaled_write(self):
        entity = self.entity(
            TuyaBLENumber,
            TuyaBLENumberMapping(
                dp_id=1, description=NumberEntityDescription(key="number"), coefficient=10
            ),
        )
        await self.assert_write(lambda: entity.async_set_native_value(2.5), 25)
        self.datapoints.get_or_create.assert_called_with(
            1, TuyaBLEDataPointType.DT_VALUE, 25
        )

    async def test_number_custom_fingerbot_setters(self):
        for setter, value, expected in (
            (set_fingerbot_program_repeat_count, 7, b"\x00\x07\x32\x01\x0a\x00\x05"),
            (set_fingerbot_program_position, 65, bytearray(b"\x00\x02\x41\x01\x0a\x00\x05")),
        ):
            with self.subTest(setter=setter.__name__):
                entity = self.entity(
                    TuyaBLENumber,
                    TuyaBLENumberMapping(
                        dp_id=121,
                        description=NumberEntityDescription(key="program"),
                        setter=setter,
                    ),
                )
                self.datapoint.set_value.reset_mock(side_effect=True)
                await self.assert_write(lambda: entity.async_set_native_value(value), expected)

    async def test_select_write_and_invalid_option(self):
        entity = self.entity(
            TuyaBLESelect,
            TuyaBLESelectMapping(
                dp_id=2,
                description=SelectEntityDescription(key="select", options=["one", "two"]),
            ),
        )
        entity._attr_options = ["one", "two"]
        await self.assert_write(lambda: entity.async_select_option("two"), 1)
        self.datapoints.get_or_create.assert_called_with(
            2, TuyaBLEDataPointType.DT_ENUM, 1
        )
        self.datapoint.set_value.reset_mock()
        with self.assertRaises(HomeAssistantError):
            await entity.async_select_option("invalid")
        self.datapoint.set_value.assert_not_awaited()

    async def test_text_write(self):
        entity = self.entity(
            TuyaBLEText,
            TuyaBLETextMapping(dp_id=3, description=TextEntityDescription(key="text")),
        )
        await self.assert_write(lambda: entity.async_set_value("hello"), "hello")
        self.datapoints.get_or_create.assert_called_with(
            3, TuyaBLEDataPointType.DT_STRING, "hello"
        )

    async def test_text_custom_fingerbot_setter(self):
        entity = self.entity(
            TuyaBLEText,
            TuyaBLETextMapping(
                dp_id=121,
                description=TextEntityDescription(key="program"),
                setter=set_fingerbot_program,
            ),
        )
        await self.assert_write(
            lambda: entity.async_set_value("10/5;20"),
            bytearray(b"\x00\x02\x32\x02\x0a\x00\x05\x14\x00\x00"),
        )

    async def test_custom_setters_reject_unavailable_or_malformed_program(self):
        cases = (
            (TuyaBLENumber, TuyaBLENumberMapping, NumberEntityDescription, set_fingerbot_program_repeat_count, 7),
            (TuyaBLENumber, TuyaBLENumberMapping, NumberEntityDescription, set_fingerbot_program_position, 65),
            (TuyaBLEText, TuyaBLETextMapping, TextEntityDescription, set_fingerbot_program, "10/5;20"),
        )
        for cls, mapping_cls, description_cls, setter, value in cases:
            entity = self.entity(
                cls,
                mapping_cls(dp_id=121, description=description_cls(key="program"), setter=setter),
            )
            command = (
                entity.async_set_value if cls is TuyaBLEText else entity.async_set_native_value
            )
            for product, datapoint in (
                (SimpleNamespace(fingerbot=None), self.datapoint),
                (SimpleNamespace(fingerbot=SimpleNamespace(program=None)), self.datapoint),
                (self.product, None),
                (self.product, SimpleNamespace(value="not bytes")),
                (self.product, SimpleNamespace(value=b"\x00\x02\x32")),
                (self.product, SimpleNamespace(value=b"\x00\x02\x32\x02\x0a\x00\x05")),
            ):
                with self.subTest(setter=setter.__name__, datapoint=datapoint):
                    entity._product = product
                    self.datapoints.__getitem__.return_value = datapoint
                    with self.assertRaises(HomeAssistantError):
                        await command(value)
                    self.datapoint.set_value.assert_not_awaited()

    async def test_normal_commands_reject_unavailable_datapoint(self):
        self.datapoints.get_or_create.return_value = None
        number = self.entity(
            TuyaBLENumber,
            TuyaBLENumberMapping(dp_id=1, description=NumberEntityDescription(key="number")),
        )
        text = self.entity(
            TuyaBLEText,
            TuyaBLETextMapping(dp_id=2, description=TextEntityDescription(key="text")),
        )
        select = self.entity(
            TuyaBLESelect,
            TuyaBLESelectMapping(
                dp_id=3, description=SelectEntityDescription(key="select", options=["one"])
            ),
        )
        select._attr_options = ["one"]
        for command in (
            lambda: number.async_set_native_value(1),
            lambda: text.async_set_value("one"),
            lambda: select.async_select_option("one"),
        ):
            with self.subTest(command=command):
                with self.assertRaises(HomeAssistantError):
                    await command()

    async def test_climate_commands(self):
        mapping = TuyaBLEClimateMapping(
            description=ClimateEntityDescription(key="climate"),
            target_temperature_dp_id=1,
            target_temperature_coefficient=10,
            target_humidity_dp_id=4,
            target_humidity_coefficient=2,
            hvac_mode_dp_id=4,
            hvac_modes=[HVACMode.OFF, HVACMode.HEAT],
            preset_mode_dp_ids={PRESET_AWAY: 5, PRESET_NONE: 5},
        )
        entity = self.entity(TuyaBLEClimate, mapping)
        cases = (
            (lambda: entity.async_set_temperature(temperature=21.5), 215, 1, TuyaBLEDataPointType.DT_VALUE),
            (lambda: entity.async_set_humidity(40), 80, 4, TuyaBLEDataPointType.DT_VALUE),
            (lambda: entity.async_set_hvac_mode(HVACMode.HEAT), 1, 4, TuyaBLEDataPointType.DT_VALUE),
            (lambda: entity.async_set_preset_mode(PRESET_AWAY), True, 5, TuyaBLEDataPointType.DT_BOOL),
            (lambda: entity.async_set_preset_mode(PRESET_NONE), False, 5, TuyaBLEDataPointType.DT_BOOL),
        )
        for command, expected, dp_id, dp_type in cases:
            with self.subTest(dp_id=dp_id, expected=expected):
                self.datapoint.set_value.reset_mock(side_effect=True)
                await self.assert_write(command, expected)
                self.datapoints.get_or_create.assert_called_with(dp_id, dp_type, expected)

    async def test_climate_hvac_switch_and_multi_preset(self):
        mapping = TuyaBLEClimateMapping(
            description=ClimateEntityDescription(key="climate"),
            hvac_switch_dp_id=6,
            hvac_switch_mode=HVACMode.HEAT,
            preset_mode_dp_ids={"first": 7, "second": 8},
        )
        entity = self.entity(TuyaBLEClimate, mapping)
        for command, expected in (
            (lambda: entity.async_set_hvac_mode(HVACMode.HEAT), True),
            (lambda: entity.async_set_hvac_mode(HVACMode.OFF), False),
            (lambda: entity.async_set_preset_mode("second"), True),
        ):
            with self.subTest(expected=expected):
                self.datapoint.set_value.reset_mock(side_effect=True)
                await self.assert_write(command, expected)

    async def test_climate_rejects_unsupported_and_unconfigured_commands(self):
        mapping = TuyaBLEClimateMapping(description=ClimateEntityDescription(key="climate"))
        entity = self.entity(TuyaBLEClimate, mapping)
        for command in (
            lambda: entity.async_set_temperature(temperature=20),
            lambda: entity.async_set_humidity(40),
            lambda: entity.async_set_hvac_mode(HVACMode.HEAT),
            lambda: entity.async_set_preset_mode(PRESET_AWAY),
        ):
            with self.subTest(command=command):
                with self.assertRaises(HomeAssistantError):
                    await command()
        mapping.hvac_switch_dp_id = 1
        mapping.hvac_switch_mode = HVACMode.HEAT
        with self.assertRaises(HomeAssistantError):
            await entity.async_set_hvac_mode(HVACMode.COOL)
        mapping.hvac_mode_dp_id = 2
        mapping.hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
        with self.assertRaises(HomeAssistantError):
            await entity.async_set_hvac_mode(HVACMode.HEAT)
        mapping.preset_mode_dp_ids = {PRESET_AWAY: 0}
        with self.assertRaises(HomeAssistantError):
            await entity.async_set_preset_mode(PRESET_AWAY)
        mapping.preset_mode_dp_ids = {PRESET_AWAY: 5}
        with self.assertRaises(HomeAssistantError):
            await entity.async_set_preset_mode(PRESET_NONE)
        self.datapoints.get_or_create.assert_not_called()

    async def test_climate_rejects_unavailable_datapoints(self):
        mapping = TuyaBLEClimateMapping(
            description=ClimateEntityDescription(key="climate"),
            target_temperature_dp_id=1,
            target_humidity_dp_id=2,
            hvac_mode_dp_id=3,
            hvac_modes=[HVACMode.OFF, HVACMode.HEAT],
            hvac_switch_dp_id=4,
            hvac_switch_mode=HVACMode.HEAT,
            preset_mode_dp_ids={PRESET_AWAY: 5},
        )
        entity = self.entity(TuyaBLEClimate, mapping)
        self.datapoints.get_or_create.return_value = None
        for command in (
            lambda: entity.async_set_temperature(temperature=20),
            lambda: entity.async_set_humidity(40),
            lambda: entity.async_set_hvac_mode(HVACMode.HEAT),
            lambda: entity.async_set_preset_mode(PRESET_AWAY),
        ):
            with self.subTest(command=command):
                with self.assertRaises(HomeAssistantError):
                    await command()
        mapping.hvac_mode_dp_id = 0
        with self.assertRaises(HomeAssistantError):
            await entity.async_set_hvac_mode(HVACMode.HEAT)
        mapping.preset_mode_dp_ids = {"first": 5, "second": 6}
        self.datapoints.get_or_create.side_effect = [None, self.datapoint]
        with self.assertRaises(HomeAssistantError):
            await entity.async_set_preset_mode("second")
        self.datapoint.set_value.assert_not_awaited()

    async def test_command_waits_for_write_completion(self):
        started = asyncio.Event()
        finish = asyncio.Event()

        async def write(value):
            started.set()
            await finish.wait()

        self.datapoint.set_value.side_effect = write
        entity = self.entity(
            TuyaBLEText,
            TuyaBLETextMapping(dp_id=3, description=TextEntityDescription(key="text")),
        )
        task = asyncio.create_task(entity.async_set_value("hello"))
        await started.wait()
        self.assertFalse(task.done())
        finish.set()
        await task
        entity._hass.create_task.assert_not_called()

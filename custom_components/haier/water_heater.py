"""Support for water heaters (fixed: supports gas, electric, generic)."""
import logging

from homeassistant.components.water_heater import (
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    STATE_OFF,
    UnitOfTemperature,
    Platform,
    ATTR_TEMPERATURE,
)
from homeassistant.core import HomeAssistant

from . import async_register_entity
from .core.attribute import HaierAttribute
from .core.device import HaierDevice
from .entity import HaierAbstractEntity
from .helpers import try_read_as_bool

_LOGGER = logging.getLogger(__name__)

# 占位/无效的当前模式值
_INVALID_MODES = {'未设置', '无', '关', '关闭', '关机', 'off', 'OFF', 'none', 'None', ''}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
    await async_register_entity(
        hass,
        entry,
        async_add_entities,
        Platform.WATER_HEATER,
        lambda device, attribute: HaierWaterHeater(device, attribute)
    )


class HaierWaterHeater(HaierAbstractEntity, WaterHeaterEntity):

    def __init__(self, device: HaierDevice, attribute: HaierAttribute):
        super().__init__(device, attribute)
        self._attr_temperature_unit = UnitOfTemperature.CELSIUS

        ext = attribute.ext
        self._heater_type = ext.get('heater_type', 'gas')
        self._temp_key = ext.get('temp_key', 'targetTemp')
        self._current_temp_key = ext.get('current_temp_key', 'outWaterTemp')
        self._power_key = ext.get('power_key', 'onOffStatus')
        self._mode_key = ext.get('mode_key', None)
        # data -> desc（单向）
        self._mode_mapping = ext.get('mode_mapping', None) or {}
        # 已过滤无效项的有序模式列表
        self._mode_options = ext.get('mode_options', None)

        supported_features = WaterHeaterEntityFeature.TARGET_TEMPERATURE
        supported_features |= WaterHeaterEntityFeature.OPERATION_MODE
        self._attr_supported_features = supported_features

        self._build_operation_list()

    def _build_operation_list(self):
        modes = [STATE_OFF]

        if self._heater_type == 'gas':
            modes.append('gas')
        elif self._mode_options:
            for m in self._mode_options:
                if m not in modes and m not in _INVALID_MODES:
                    modes.append(m)
        else:
            modes.append('heat')

        self._attr_operation_list = modes

    @property
    def operation_list(self):
        return self._attr_operation_list

    def set_temperature(self, **kwargs) -> None:
        temperature = kwargs.get(ATTR_TEMPERATURE) or kwargs.get('temperature')
        if temperature is None:
            _LOGGER.warning('set_temperature called without temperature value')
            return
        _LOGGER.debug('Setting water heater temperature to %s via key %s', temperature, self._temp_key)
        self._send_command({self._temp_key: temperature})

    def _update_value(self):
        attrs = self._attributes_data

        if self._current_temp_key and self._current_temp_key in attrs:
            try:
                self._attr_current_temperature = float(attrs[self._current_temp_key])
            except (ValueError, TypeError):
                self._attr_current_temperature = None
        else:
            # 不在字段列表时，不再回退到目标温度（否则会显示"当前=目标"的假象）。
            # 而是遍历常见的出水/进水/内胆温度字段兜底。
            for fallback_key in ('outWaterTemp', 'innerWaterTemp', 'waterTemp',
                                  'actualTemp', 'realTemp'):
                if fallback_key in attrs:
                    try:
                        self._attr_current_temperature = float(attrs[fallback_key])
                        break
                    except (ValueError, TypeError):
                        continue
            else:
                self._attr_current_temperature = None

        if self._temp_key in attrs:
            try:
                self._attr_target_temperature = float(attrs[self._temp_key])
            except (ValueError, TypeError):
                pass

        power_on = True
        if self._power_key in attrs:
            power_on = try_read_as_bool(attrs[self._power_key])

        if not power_on:
            self._attr_current_operation = STATE_OFF
        else:
            if self._mode_key and self._mode_mapping and self._mode_key in attrs:
                mode_val = str(attrs[self._mode_key])
                current_mode = self._mode_mapping.get(mode_val)
                if current_mode and current_mode not in _INVALID_MODES:
                    self._attr_current_operation = current_mode
                else:
                    # 当前值是占位/未识别时，回退到列表里第一个可用模式
                    fallback = next((m for m in self._attr_operation_list if m != STATE_OFF),
                                   'heat' if self._heater_type != 'gas' else 'gas')
                    self._attr_current_operation = fallback
            else:
                self._attr_current_operation = 'gas' if self._heater_type == 'gas' else 'heat'

    def set_operation_mode(self, operation_mode):
        _LOGGER.debug('Setting water heater operation mode to: %s', operation_mode)
        commands = {}

        if operation_mode == STATE_OFF:
            commands[self._power_key] = False
            self._send_command(commands)
            return

        commands[self._power_key] = True

        if self._mode_mapping:
            # 找到对应数据值（单向 mapping data->desc，所以反查 desc->data）
            mode_value = None
            for data_val, desc in self._mode_mapping.items():
                if desc == operation_mode:
                    mode_value = data_val
                    break
            if mode_value is not None:
                try:
                    mode_value = int(mode_value)
                except (ValueError, TypeError):
                    pass
                commands[self._mode_key] = mode_value
                _LOGGER.debug('Sending mode command: %s = %s', self._mode_key, mode_value)

        self._send_command(commands)

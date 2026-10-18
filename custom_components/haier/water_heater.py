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

# 操作模式常量
MODE_OFF = 'off'
MODE_GAS = 'gas'
MODE_ECO = 'eco'
MODE_HEAT = 'heat'
MODE_PERFORMANCE = 'performance'


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

        # 从 ext 中读取配置
        ext = attribute.ext
        self._heater_type = ext.get('heater_type', 'gas')
        self._temp_key = ext.get('temp_key', 'targetTemp')
        self._current_temp_key = ext.get('current_temp_key', 'outWaterTemp')
        self._power_key = ext.get('power_key', 'onOffStatus')
        self._mode_key = ext.get('mode_key', None)
        self._mode_mapping = ext.get('mode_mapping', None)

        # 构建支持特性
        supported_features = WaterHeaterEntityFeature.TARGET_TEMPERATURE
        supported_features |= WaterHeaterEntityFeature.OPERATION_MODE

        # 如果有模式映射，支持 AWAY_MODE 映射为关机/节能
        if self._mode_mapping:
            supported_features |= WaterHeaterEntityFeature.AWAY_MODE

        self._attr_supported_features = supported_features

        # 构建操作模式列表
        self._build_operation_list()

    def _build_operation_list(self):
        """根据设备类型和模式映射构建可用的操作模式列表"""
        modes = [STATE_OFF]

        if self._heater_type == 'gas':
            # 燃气热水器：开机即为燃气加热
            modes.append('gas')
        elif self._mode_mapping:
            # 电热水器/通用：从设备的模式映射中获取可用模式
            added = set()
            for mode_data, mode_desc in self._mode_mapping.items():
                # 只加入描述性名称（data是数字，desc是中文名）
                if isinstance(mode_desc, str) and mode_desc not in added:
                    # 过滤掉明显不是工作模式的值（如"关闭"、"关"等会作为STATE_OFF处理）
                    if mode_desc not in ('关', '关闭', '关机', 'off', 'OFF'):
                        modes.append(mode_desc)
                        added.add(mode_desc)
        else:
            # 没有模式信息的简单热水器，至少提供加热模式
            modes.append('heat')

        self._attr_operation_list = modes

    @property
    def operation_list(self):
        """List of available operation modes."""
        return self._attr_operation_list

    def set_temperature(self, **kwargs) -> None:
        """设置目标温度"""
        temperature = kwargs.get(ATTR_TEMPERATURE) or kwargs.get('temperature')
        if temperature is None:
            _LOGGER.warning('set_temperature called without temperature value')
            return

        _LOGGER.debug('Setting water heater temperature to %s via key %s', temperature, self._temp_key)
        self._send_command({
            self._temp_key: temperature
        })

    def _update_value(self):
        """从设备属性数据更新实体状态"""
        attrs = self._attributes_data

        # 更新当前温度
        if self._current_temp_key and self._current_temp_key in attrs:
            try:
                self._attr_current_temperature = float(attrs[self._current_temp_key])
            except (ValueError, TypeError):
                self._attr_current_temperature = None
        else:
            # 没有当前温度字段时，目标温度作为回退（如issue #126的情况）
            if self._temp_key in attrs:
                try:
                    self._attr_current_temperature = float(attrs[self._temp_key])
                except (ValueError, TypeError):
                    self._attr_current_temperature = None

        # 更新目标温度
        if self._temp_key in attrs:
            try:
                self._attr_target_temperature = float(attrs[self._temp_key])
            except (ValueError, TypeError):
                pass

        # 更新开关机状态和运行模式
        power_on = True
        if self._power_key in attrs:
            power_on = try_read_as_bool(attrs[self._power_key])

        if not power_on:
            # 关机状态
            self._attr_current_operation = STATE_OFF
            self._attr_is_away_mode_on = True
        else:
            # 开机状态，判断具体模式
            self._attr_is_away_mode_on = False

            if self._mode_key and self._mode_mapping and self._mode_key in attrs:
                mode_val = str(attrs[self._mode_key])
                current_mode = self._mode_mapping.get(mode_val, None)
                if current_mode and isinstance(current_mode, str):
                    self._attr_current_operation = current_mode
                else:
                    # fallback
                    self._attr_current_operation = 'heat' if self._heater_type != 'gas' else 'gas'
            else:
                self._attr_current_operation = 'gas' if self._heater_type == 'gas' else 'heat'

    def turn_away_mode_on(self):
        """Turn away mode on (映射为关机或节能模式)"""
        self._send_command({
            self._power_key: False
        })

    def turn_away_mode_off(self):
        """Turn away mode off (映射为开机)"""
        self._send_command({
            self._power_key: True
        })

    def set_operation_mode(self, operation_mode):
        """Set operation mode - 修复：支持真正的模式切换而非仅开关机"""
        _LOGGER.debug('Setting water heater operation mode to: %s', operation_mode)

        commands = {}

        if operation_mode == STATE_OFF:
            commands[self._power_key] = False
            self._send_command(commands)
            return

        # 先确保开机
        commands[self._power_key] = True

        if operation_mode in ('gas', 'heat', 'performance'):
            # 简单开机即可的模式
            pass

        if self._mode_key and self._mode_mapping:
            # 有模式映射，查找对应的模式值并发送
            mode_value = self._mode_mapping.get(operation_mode)
            if mode_value is not None:
                try:
                    # 尝试转为数字（海尔API通常用数字表示模式）
                    mode_value = int(mode_value)
                except (ValueError, TypeError):
                    pass
                commands[self._mode_key] = mode_value
                _LOGGER.debug('Sending mode command: %s = %s', self._mode_key, mode_value)
            else:
                _LOGGER.warning('Mode "%s" not found in mode mapping: %s',
                              operation_mode, list(self._mode_mapping.keys()))

        self._send_command(commands)

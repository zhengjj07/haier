import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from . import async_register_entity
from .core.attribute import HaierAttribute
from .core.device import HaierDevice
from .entity import HaierAbstractEntity
from .helpers import try_read_as_bool

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
    await async_register_entity(
        hass,
        entry,
        async_add_entities,
        Platform.SWITCH,
        lambda device, attribute: HaierSwitch(device, attribute)
    )


class HaierSwitch(HaierAbstractEntity, SwitchEntity):

    def __init__(self, device: HaierDevice, attribute: HaierAttribute):
        super().__init__(device, attribute)
        # Record the "on value" and "off value" that this field actually
        # expects so we don't send True/False to a field that uses 1/0.
        self._on_value = attribute.ext.get('on_value', True)
        self._off_value = attribute.ext.get('off_value', False)

    def _update_value(self):
        data_key = self._attribute.ext.get('data_key', self._attribute.key)
        if data_key not in self._attributes_data:
            # 设备云端暂时没有下发这个字段的值（如四季温水/自动关机在部分机型
            # 上默认不推送 value），默认视为"关"且维持可用状态，避免整个实体
            # 显示为 unavailable、看起来像没生成。
            self._attr_is_on = False
            self._attr_available = True
            _LOGGER.debug(
                'entity [%s] attribute %s not in snapshot, default to OFF',
                self._attr_unique_id, data_key
            )
            return
        try:
            self._attr_is_on = try_read_as_bool(self._attributes_data[data_key])
        except (ValueError, TypeError):
            _LOGGER.exception('entity [{}] read value failed'.format(self._attr_unique_id))
            self._attr_is_on = False
            self._attr_available = True
            return
        self._attr_available = True

    def turn_on(self, **kwargs: Any) -> None:
        self._send_command({
            self._attribute.key: self._on_value
        })

    def turn_off(self, **kwargs: Any) -> None:
        self._send_command({
            self._attribute.key: self._off_value
        })

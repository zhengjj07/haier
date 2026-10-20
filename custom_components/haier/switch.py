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
        # 云端快照暂时未包含该字段时，用最近一次本地操作状态保持显示一致
        self._local_state = None

    def _update_value(self):
        data_key = self._attribute.ext.get('data_key', self._attribute.key)
        if data_key not in self._attributes_data:
            # 设备云端暂时没有下发这个字段的值，保留本地上一次操作状态，默认"关"
            self._attr_is_on = bool(self._local_state) if self._local_state is not None else False
            self._attr_available = True
            return
        try:
            self._attr_is_on = try_read_as_bool(self._attributes_data[data_key])
            self._local_state = self._attr_is_on
        except (ValueError, TypeError):
            _LOGGER.exception('entity [{}] read value failed'.format(self._attr_unique_id))
            self._attr_is_on = False
        self._attr_available = True

    def turn_on(self, **kwargs: Any) -> None:
        self._set_state(True, self._on_value)

    def turn_off(self, **kwargs: Any) -> None:
        self._set_state(False, self._off_value)

    def _set_state(self, target_state: bool, cmd_value) -> None:
        # 本地立即更新（乐观更新），点击后马上有视觉反馈
        self._attr_is_on = target_state
        self._local_state = target_state
        self.schedule_update_ha_state()
        self._send_command({
            self._attribute.key: cmd_value
        })

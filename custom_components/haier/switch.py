import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from . import async_register_entity
from .core.attribute import HaierAttribute, TRUE_VALUES, FALSE_VALUES
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

        ext = attribute.ext
        # data_key 用于读写设备数据，默认与实体 key 一致。
        # 热水器辅助功能实体的 key 带后缀，因此需要单独指定原始属性名。
        self._data_key = ext.get('data_key', attribute.key)
        self._on_value = ext.get('on_value', True)
        self._off_value = ext.get('off_value', False)

    def _update_value(self):
        raw = self._attributes_data[self._data_key]
        text = str(raw).strip().lower()

        # 云端布尔值可能是 true/false，也可能是 1/0 或 开/关
        if text in TRUE_VALUES:
            self._attr_is_on = True
            return

        if text in FALSE_VALUES:
            self._attr_is_on = False
            return

        try:
            self._attr_is_on = try_read_as_bool(raw)
        except ValueError:
            _LOGGER.exception('entity [{}] read value failed'.format(self._attr_unique_id))
            self._attr_available = False

    def turn_on(self, **kwargs: Any) -> None:
        self._send_command({
            self._data_key: self._on_value
        })

    def turn_off(self, **kwargs: Any) -> None:
        self._send_command({
            self._data_key: self._off_value
        })

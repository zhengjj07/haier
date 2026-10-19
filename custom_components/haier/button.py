import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from . import async_register_entity
from .core.attribute import HaierAttribute
from .core.device import HaierDevice
from .entity import HaierAbstractEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
    await async_register_entity(
        hass,
        entry,
        async_add_entities,
        Platform.BUTTON,
        lambda device, attribute: HaierButton(device, attribute)
    )


class HaierButton(HaierAbstractEntity, ButtonEntity):

    def __init__(self, device: HaierDevice, attribute: HaierAttribute):
        super().__init__(device, attribute)

    def _update_value(self):
        return None

    def press(self, **kwargs: Any) -> None:
        # 按钮实体有单独的 data_key，所以直接用真实字段名下发；
        # 若没有 data_key，则用 attribute.key 自身（保持原行为）。
        cmd_key = self._attribute.ext.get('data_key', self._attribute.key)
        cmd_val = self._attribute.ext['command_value']
        self._send_command({cmd_key: cmd_val})

import logging

from homeassistant.components.number import NumberEntity
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
        Platform.NUMBER,
        lambda device, attribute: HaierNumber(device, attribute)
    )


class HaierNumber(HaierAbstractEntity, NumberEntity):

    def __init__(self, device: HaierDevice, attribute: HaierAttribute):
        super().__init__(device, attribute)

    def _data_key(self):
        return self._attribute.ext.get('data_key', self._attribute.key)

    def _update_value(self):
        val = self._attributes_data.get(self._data_key())
        try:
            self._attr_native_value = float(val) if val is not None else None
        except (ValueError, TypeError):
            self._attr_native_value = val

    def set_native_value(self, value: float) -> None:
        # 保持与设备原始数据同类型（整数发整数）
        step = self._attribute.ext.get('native_step') or getattr(self, '_attr_native_step', None)
        if step and float(step).is_integer():
            value = int(round(value))
        self._send_command({
            self._attribute.key: value
        })

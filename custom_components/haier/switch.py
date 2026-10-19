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
        try:
            self._attr_is_on = try_read_as_bool(self._attributes_data[data_key])
        except (ValueError, KeyError):
            _LOGGER.exception('entity [{}] read value failed'.format(self._attr_unique_id))
            self._attr_available = False

    def turn_on(self, **kwargs: Any) -> None:
        self._send_command({
            self._attribute.key: self._on_value
        })

    def turn_off(self, **kwargs: Any) -> None:
        self._send_command({
            self._attribute.key: self._off_value
        })

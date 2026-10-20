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
        # Fallback 虚拟字段的候选列表与当前使用的字段名
        self._fallback_candidates = attribute.ext.get('fallback_candidates', None)
        self._fallback_current = attribute.ext.get('fallback_current', None)
        # 虚拟字段的本地状态（云端不推送该字段值时，用本地状态保持开关显示一致）
        self._virtual_local_state = None

    def _update_value(self):
        data_key = self._attribute.ext.get('data_key', self._attribute.key)
        # 首先尝试所有 fallback 候选字段（云端可能在回执中用任意一个名字回显）
        if self._fallback_candidates:
            for cand in self._fallback_candidates:
                if cand in self._attributes_data:
                    try:
                        self._attr_is_on = try_read_as_bool(self._attributes_data[cand])
                        self._fallback_current = cand  # 命中实际字段名，后续就用它
                        self._virtual_local_state = self._attr_is_on
                        self._attr_available = True
                        _LOGGER.info(
                            'fallback switch [%s] matched cloud field "%s" = %s',
                            self._attr_name, cand, self._attributes_data[cand]
                        )
                        return
                    except (ValueError, TypeError):
                        continue

        if data_key not in self._attributes_data:
            # 设备云端暂时没有下发这个字段的值（如虚拟的自动关机/四季温水）。
            # 若之前有过本地操作状态，保留本地状态；否则默认"关"。
            if self._virtual_local_state is not None:
                self._attr_is_on = self._virtual_local_state
            else:
                self._attr_is_on = False
            self._attr_available = True
            _LOGGER.debug(
                'entity [%s] attribute %s not in snapshot, keep local state=%s',
                self._attr_unique_id, data_key, self._attr_is_on
            )
            return
        try:
            self._attr_is_on = try_read_as_bool(self._attributes_data[data_key])
            self._virtual_local_state = self._attr_is_on
        except (ValueError, TypeError):
            _LOGGER.exception('entity [{}] read value failed'.format(self._attr_unique_id))
            self._attr_is_on = False
            self._attr_available = True
            return
        self._attr_available = True

    def turn_on(self, **kwargs: Any) -> None:
        self._send_with_log(self._on_value, True)

    def turn_off(self, **kwargs: Any) -> None:
        self._send_with_log(self._off_value, False)

    def _send_with_log(self, cmd_value, target_state: bool):
        """发送命令并打日志。对于 fallback 虚拟字段，会记录发送的字段名和值，
        方便通过日志确认云端是否接受、是否需要换字段名。
        同时在本地立即更新开关状态（乐观更新），让点击后立刻有视觉反馈。"""
        data_key = self._attribute.ext.get('data_key', self._attribute.key)
        self._attr_is_on = target_state
        self._virtual_local_state = target_state
        self.schedule_update_ha_state()
        self._send_command({
            self._attribute.key: cmd_value
        })
        if self._fallback_candidates:
            _LOGGER.warning(
                'Sending fallback command for [%s]: field="%s", value=%s (on=%s, off=%s). '
                '如果设备没反应，说明该字段名不被云端接受，维护者会尝试下一个候选字段。',
                self._attr_name, data_key, cmd_value, self._on_value, self._off_value
            )

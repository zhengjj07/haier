import json
import logging
from typing import List

from .attribute import HaierAttribute, V1SpecAttributeParser

_LOGGER = logging.getLogger(__name__)


class HaierDevice:
    _raw_data: dict
    _attributes: List[HaierAttribute]

    def __init__(self, client, raw: dict):
        self._client = client
        self._raw_data = raw
        self._attributes = []

    @property
    def id(self):
        return self._raw_data['deviceId']

    @property
    def name(self):
        return self._raw_data['deviceName'] if 'deviceName' in self._raw_data else self.id

    @property
    def type(self):
        return self._raw_data['deviceType'] if 'deviceType' in self._raw_data else None

    @property
    def product_code(self):
        return self._raw_data['productCodeT'] if 'productCodeT' in self._raw_data else None

    @property
    def product_name(self):
        return self._raw_data['productNameT'] if 'productNameT' in self._raw_data else None

    @property
    def wifi_type(self):
        return self._raw_data['wifiType']

    @property
    def attributes(self) -> List[HaierAttribute]:
        return self._attributes

    async def async_init(self):
        # 解析Attribute
        # noinspection PyBroadException
        try:
            parser = V1SpecAttributeParser()
            attributes = await self._client.get_digital_model_from_cache(self)

            # 便于排查某机型缺失/异常的实体：记录该设备全部属性的名称与描述
            _LOGGER.debug('Haier device %s attribute dump: %s', self.id,
                          [(a.get('name'), a.get('desc')) for a in attributes])

            for item in attributes:
                try:
                    attr = parser.parse_attribute(item)
                    if attr:
                        self._attributes.append(attr)
                except:
                    _LOGGER.exception("Haier device %s attribute %s parsing error occurred", self.id, item['name'])

            iter = parser.parse_global(attributes)
            if iter:
                for item in iter:
                    self._attributes.append(item)

            # 热水器辅助功能实体生成后，移除同一属性的只读副本，避免出现同名实体
            extra_keys = {attr.key for attr in self._attributes if attr.key.endswith('_extra')}
            if extra_keys:
                self._attributes = [
                    attr for attr in self._attributes
                    if attr.key.endswith('_extra') or attr.key + '_extra' not in extra_keys
                ]
        except Exception:
            _LOGGER.exception('Haier device %s init failed', self.id)

    def __str__(self) -> str:
        return json.dumps({
            'id': self.id,
            'name': self.name,
            'type': self.type,
            'product_code': self.product_code,
            'product_name': self.product_name,
            'wifi_type': self.wifi_type
        })

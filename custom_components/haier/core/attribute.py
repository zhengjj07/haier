import logging
from abc import abstractmethod, ABC
from typing import List

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.components.switch import SwitchDeviceClass
from homeassistant.const import Platform, UnitOfTemperature, PERCENTAGE, UnitOfVolume, UnitOfEnergy

from custom_components.haier.helpers import equals_ignore_case, contains_any_ignore_case

_LOGGER = logging.getLogger(__name__)

# 热水器辅助功能：按属性的中文描述(desc)匹配，避免依赖各机型并不统一的英文字段名。
# 每一项为 (在 HA 中展示的名称, 描述关键词元组)
WATER_HEATER_EXTRA_FEATURES = [
    ('四季温水', ('四季温水', '四季恒温', '四季温感')),
    ('智能抑菌', ('智能抑菌', '抑菌', '杀菌', '除菌', '灭菌', '抗菌')),
    ('自动关机', ('自动关机', '自动断电', '加热完成自动')),
    ('定时开关机', ('定时开关机', '定时开关', '定时开机', '定时关机', '定时')),
]

# 加热模式选项中需要过滤掉的占位项：数据里的"未设置"、纯数字等并不是可选的加热模式
MODE_PLACEHOLDER_VALUES = {
    '未设置', '未设定', '未选择', '不设置', '无', 'none', 'unset', 'null',
    'n/a', 'na', '--', '-', '', '关闭', '关机', '关', 'off', '停止'
}

# 布尔型属性在云端可能用 true/false、1/0，也可能用"开/关"，统一在此声明
TRUE_VALUES = ('true', '1', 'on', 'open', 'yes', '开', '开启', '打开', '启用')
FALSE_VALUES = ('false', '0', 'off', 'close', 'no', '关', '关闭', '停止', '禁用')


class HaierAttribute:

    def __init__(self, key: str, display_name: str, platform: Platform, options: dict = {}, ext: dict = {}):
        self._key = key
        self._display_name = display_name
        self._platform = platform
        self._options = options
        self._ext = ext

    @property
    def key(self) -> str:
        return self._key

    @property
    def display_name(self) -> str:
        return self._display_name

    @property
    def platform(self) -> Platform:
        return self._platform

    @property
    def options(self) -> dict:
        return self._options

    @property
    def ext(self) -> dict:
        return self._ext


class HaierAttributeParser(ABC):

    @abstractmethod
    def parse_attribute(self, attribute: dict) -> HaierAttribute:
        pass

    @abstractmethod
    def parse_global(self, attributes: List[dict]):
        pass

class V1SpecAttributeParser(HaierAttributeParser, ABC):

    def parse_attribute(self, attribute: dict) -> HaierAttribute:
        # 没有value的attribute后续无法正常使用，所以需要过滤掉
        if 'value' not in attribute:
            return None

        if not attribute['writable'] and attribute['readable']:
            return self._parse_as_sensor(attribute)

        if attribute['writable'] and equals_ignore_case(attribute['valueRange']['type'], 'STEP') and contains_any_ignore_case(attribute['valueRange']['dataStep']['dataType'], ['Integer', 'Double']):
            return self._parse_as_number(attribute)

        # One-way boolean commands (for example the refrigerator's forced
        # sterilization actions) are represented by a one-item LIST.  They are
        # not selects: the returned false value means "not currently firing"
        # and is not one of the selectable options.  Expose them as buttons so
        # HA never presents an invalid current option or a misleading toggle.
        if attribute['writable'] and V1SpecAttributeParser._is_one_way_boolean_command(attribute):
            return self._parse_as_button(attribute)

        # 一定要在select之前，不然会被select覆盖
        if attribute['writable'] and V1SpecAttributeParser._is_binary_attribute(attribute):
            return self._parse_as_switch(attribute)

        if attribute['writable'] and equals_ignore_case(attribute['valueRange']['type'], 'LIST'):
            return self._parse_as_select(attribute)

        return None

    def parse_global(self, attributes: List[dict]):
        all_attribute_keys = [attribute['name'] for attribute in attributes]
        attr_dict = {a['name']: a for a in attributes}

        # 空调特征字段
        climate_feature_fields = [
            ['targetTemperature', 'operationMode', 'windSpeed'],
            # https://github.com/banto6/haier/issues/53 有空调分左右风区
            ['targetTemperature', 'operationMode', 'windSpeedL', 'windSpeedR']
        ]
        for feature_fields in climate_feature_fields:
            if len(list(set(feature_fields) - set(all_attribute_keys))) == 0:
                yield self._parse_as_climate(attributes, feature_fields)
                break

        # 热水器识别（修复：扩展支持电热水器和更多型号）
        water_heater_parsed = False
        # 1. 完整燃气热水器：有出水温度、目标温度、累计用气量
        if 'outWaterTemp' in all_attribute_keys and 'targetTemp' in all_attribute_keys and 'totalUseGasL' in all_attribute_keys:
            yield self._parse_as_gas_water_heater(attributes)
            water_heater_parsed = True
        # 2. 降级燃气热水器（issue #126：无出水温度字段，如JSQ30-16R3BWU1）
        elif 'targetTemp' in all_attribute_keys and 'onOffStatus' in all_attribute_keys and 'totalUseGasL' in all_attribute_keys:
            yield self._parse_as_gas_water_heater(attributes, out_water_temp_optional=True)
            water_heater_parsed = True
        # 3. 电热水器：有目标温度 + 开关机状态 + 无累计用气量
        elif 'targetTemp' in all_attribute_keys and 'onOffStatus' in all_attribute_keys:
            # 查找加热模式字段
            mode_key = None
            for mk in ['heatMode', 'workMode', 'runningMode', 'heatingMode', 'mode', 'heatStatus', 'smartMode']:
                if mk in all_attribute_keys:
                    mode_key = mk
                    break
            yield self._parse_as_electric_water_heater(attributes, mode_key=mode_key)
            water_heater_parsed = True
        # 4. 目标温度用 targetTemperature 命名的热水器（部分新型号/空气能）
        elif 'targetTemperature' in all_attribute_keys and 'onOffStatus' in all_attribute_keys:
            mode_key = None
            for mk in ['heatMode', 'workMode', 'runningMode', 'heatingMode', 'mode', 'heatStatus']:
                if mk in all_attribute_keys:
                    mode_key = mk
                    break
            yield self._parse_as_generic_water_heater(attributes, temp_key='targetTemperature', mode_key=mode_key)
            water_heater_parsed = True

        # 热水器辅助功能：四季温水 / 智能抑菌 / 自动关机 / 定时开关机
        # 这些功能在部分机型上被云端标记为只读、或因为缺少 value 字段而在通用流程里被丢弃，
        # 这里按 desc 兜底补齐为可操作实体。
        if water_heater_parsed:
            for extra in self._parse_water_heater_extras(attributes):
                yield extra

        # 窗帘
        if 'openDegree' in all_attribute_keys:
            yield self._parse_as_cover(attributes)

        # 洗碗机/洗衣机：对只读LIST类型的洗涤模式属性，保留原Sensor作为状态读取，
        # 同时额外生成Select实体用于在HA中切换模式
        # 干衣机：drycycle(干燥程序)固件实际接受写入（含一键智烘），据此生成Select支持远程选程序
        _control_attrs = {'washprog', 'partitionwashstatus', 'runningmode', 'drycycle'}
        for attr in attributes:
            if attr['name'].lower() in _control_attrs \
                    and equals_ignore_case(attr['valueRange']['type'], 'LIST'):
                value_comparison_table = {}
                for item in attr['valueRange']['dataList']:
                    value_comparison_table[str(item['data'])] = item['desc']
                    value_comparison_table[str(item['desc'])] = item['data']
                yield HaierAttribute(
                    attr['name'] + '_sel',
                    attr['desc'],
                    Platform.SELECT,
                    {'options': [item['desc'] for item in attr['valueRange']['dataList']]},
                    {'value_comparison_table': value_comparison_table, 'data_key': attr['name']}
                )

    def _parse_water_heater_extras(self, attributes: List[dict]):
        """为热水器补齐辅助功能实体（四季温水/智能抑菌/自动关机/定时开关机）。

        匹配依据是属性的中文描述，因此不受各机型英文字段名不一致的影响。
        仅在该属性没有在通用流程中生成可控实体时才额外生成，避免出现重复实体。
        """
        for attribute in attributes:
            desc = str(attribute.get('desc', '') or '')

            feature_name = None
            for name, keywords in WATER_HEATER_EXTRA_FEATURES:
                if any(keyword in desc for keyword in keywords):
                    feature_name = name
                    break

            if feature_name is None:
                continue

            value_range = attribute.get('valueRange') or {}
            if not equals_ignore_case(value_range.get('type'), 'LIST'):
                continue

            existing = self.parse_attribute(attribute)
            if existing is not None and existing.platform in (
                    Platform.SWITCH, Platform.BUTTON, Platform.SELECT, Platform.NUMBER):
                # 通用流程已经产出了可操作实体，无需重复
                continue

            data_list = value_range.get('dataList') or []
            if len(data_list) == 0:
                continue

            key = attribute['name'] + '_extra'

            # 单项命令：只能触发，用按钮承载
            if len(data_list) == 1:
                yield HaierAttribute(
                    key,
                    feature_name,
                    Platform.BUTTON,
                    ext={'command_value': data_list[0]['data'], 'data_key': attribute['name']}
                )
                continue

            # 双项：开关型功能
            if len(data_list) == 2:
                on_value, off_value = V1SpecAttributeParser._extract_boolean_values(data_list)
                yield HaierAttribute(
                    key,
                    feature_name,
                    Platform.SWITCH,
                    {'device_class': SwitchDeviceClass.SWITCH},
                    ext={
                        'data_key': attribute['name'],
                        'on_value': on_value,
                        'off_value': off_value,
                    }
                )
                continue

            # 多项：选择型功能
            value_comparison_table = {}
            for item in data_list:
                value_comparison_table[str(item['data'])] = item['desc']
                value_comparison_table[str(item['desc'])] = item['data']

            yield HaierAttribute(
                key,
                feature_name,
                Platform.SELECT,
                {'options': [item['desc'] for item in data_list]},
                ext={'value_comparison_table': value_comparison_table, 'data_key': attribute['name']}
            )

    @staticmethod
    def _extract_mode_options(attribute) -> List[str]:
        """从LIST属性中提取有序、去重、已过滤占位项的选项描述列表。"""
        options = []
        seen = set()
        for item in attribute['valueRange']['dataList']:
            desc = item.get('desc', None)
            if desc is None:
                continue

            desc = str(desc).strip()
            if not desc or desc in seen:
                continue

            if V1SpecAttributeParser._is_mode_placeholder(desc):
                continue

            seen.add(desc)
            options.append(desc)

        return options

    @staticmethod
    def _is_mode_placeholder(desc: str) -> bool:
        """判断某个模式描述是否是占位项（如"未设置"、纯数字）。"""
        normalized = desc.strip().lower()
        return normalized in MODE_PLACEHOLDER_VALUES or normalized.isdigit()

    @staticmethod
    def _extract_boolean_values(data_list):
        """从双项LIST中判断哪一个 data 表示"开"、哪一个表示"关"。"""
        on_value = None
        off_value = None

        for item in data_list:
            raw = str(item.get('data', '')).strip().lower()
            desc = str(item.get('desc', '') or '').strip().lower()

            if on_value is None and (raw in TRUE_VALUES or desc in TRUE_VALUES):
                on_value = item['data']
            elif off_value is None and (raw in FALSE_VALUES or desc in FALSE_VALUES):
                off_value = item['data']

        if on_value is None:
            on_value = data_list[1]['data']

        if off_value is None:
            off_value = data_list[0]['data']

        return on_value, off_value

    @staticmethod
    def _parse_as_sensor(attribute):
        if V1SpecAttributeParser._is_binary_attribute(attribute):
            return HaierAttribute(attribute['name'], attribute['desc'], Platform.BINARY_SENSOR)

        options = {}
        ext = {}
        if equals_ignore_case(attribute['valueRange']['type'], 'LIST'):
            value_comparison_table = {}
            for item in attribute['valueRange']['dataList']:
                value_comparison_table[str(item['data'])] = item['desc']

            options['device_class'] = SensorDeviceClass.ENUM
            options['options'] = list(value_comparison_table.values())
            ext['value_comparison_table'] = value_comparison_table

        if equals_ignore_case(attribute['valueRange']['type'], 'STEP'):
            state_class, device_class, unit = V1SpecAttributeParser._guess_state_class_device_class_and_unit(attribute)
            if device_class:
                options['device_class'] = device_class

            if state_class:
                options['state_class'] = state_class

            if unit:
                options['native_unit_of_measurement'] = unit

        return HaierAttribute(attribute['name'], attribute['desc'], Platform.SENSOR, options, ext)

    @staticmethod
    def _parse_as_number(attribute):
        step = attribute['valueRange']['dataStep']
        options = {
            'native_min_value': float(step['minValue']),
            'native_max_value': float(step['maxValue']),
            'native_step': step['step']
        }

        _, _, unit = V1SpecAttributeParser._guess_state_class_device_class_and_unit(attribute)
        if unit:
            options['native_unit_of_measurement'] = unit

        return HaierAttribute(attribute['name'], attribute['desc'], Platform.NUMBER, options)

    @staticmethod
    def _parse_as_select(attribute):
        value_comparison_table = {}
        for item in attribute['valueRange']['dataList']:
            value_comparison_table[str(item['data'])] = item['desc']
            value_comparison_table[str(item['desc'])] = item['data']

        ext = {
            'value_comparison_table': value_comparison_table
        }

        options = {
            'options': [item['desc'] for item in attribute['valueRange']['dataList']]
        }

        return HaierAttribute(attribute['name'], attribute['desc'], Platform.SELECT, options, ext)

    @staticmethod
    def _parse_as_switch(attribute):
        options = {
            'device_class': SwitchDeviceClass.SWITCH
        }

        return HaierAttribute(attribute['name'], attribute['desc'], Platform.SWITCH, options)

    @staticmethod
    def _parse_as_button(attribute):
        item = attribute['valueRange']['dataList'][0]
        return HaierAttribute(
            attribute['name'],
            attribute['desc'],
            Platform.BUTTON,
            ext={'command_value': item['data']}
        )

    @staticmethod
    def _is_one_way_boolean_command(attribute):
        value_range = attribute.get('valueRange', {})
        data_list = value_range.get('dataList', [])
        return (
            equals_ignore_case(value_range.get('type'), 'LIST')
            and len(data_list) == 1
            and str(data_list[0].get('data')).lower() in ('true', 'false')
        )

    @staticmethod
    def _parse_as_climate(attributes: List[dict], feature_fields: List[str]):
        for attr in attributes:
            if attr['name'] == 'targetTemperature':
                target_temperature_attr = attr
                break
        else:
            raise RuntimeError('targetTemperature attr not found')

        options = {
            'min_temp': float(target_temperature_attr['valueRange']['dataStep']['minValue']),
            'max_temp': float(target_temperature_attr['valueRange']['dataStep']['maxValue']),
            'target_temperature_step': float(target_temperature_attr['valueRange']['dataStep']['step'])
        }

        ext = {
            'customize': True,
            # 是否存在多个风口
            'exist_multiple_vents': 'windSpeedL' in feature_fields
        }

        return HaierAttribute('climate', 'Climate', Platform.CLIMATE, options, ext)

    @staticmethod
    def _parse_as_gas_water_heater(attributes: List[dict], out_water_temp_optional: bool = False):
        for attr in attributes:
            if attr['name'] == 'targetTemp':
                target_temperature_attr = attr
                break
        else:
            raise RuntimeError('targetTemp attr not found')

        data_step = target_temperature_attr['valueRange']['dataStep']
        options = {
            'min_temp': float(data_step['minValue']),
            'max_temp': float(data_step['maxValue']),
            'target_temperature_step': float(data_step['step'])
        }

        ext = {
            'customize': True,
            'heater_type': 'gas',
            'temp_key': 'targetTemp',
            'current_temp_key': 'outWaterTemp' if not out_water_temp_optional else None,
            'power_key': 'onOffStatus',
        }

        return HaierAttribute('gas_water_heater', 'GasWaterHeater', Platform.WATER_HEATER, options, ext)

    @staticmethod
    def _parse_as_electric_water_heater(attributes: List[dict], mode_key: str = None):
        for attr in attributes:
            if attr['name'] == 'targetTemp':
                target_temperature_attr = attr
                break
        else:
            raise RuntimeError('targetTemp attr not found for electric water heater')

        data_step = target_temperature_attr['valueRange']['dataStep']
        options = {
            'min_temp': float(data_step['minValue']),
            'max_temp': float(data_step['maxValue']),
            'target_temperature_step': float(data_step['step'])
        }

        # 查找当前温度字段（电热水器常见：innerWaterTemp/waterTemp/inTemp）
        current_temp_key = None
        for ck in ['innerWaterTemp', 'waterTemp', 'inWaterTemp', 'currentTemp', 'actualTemp', 'temp']:
            if any(a['name'] == ck for a in attributes):
                current_temp_key = ck
                break

        # 构建模式映射表
        mode_mapping = None
        mode_options = None
        if mode_key:
            for attr in attributes:
                if attr['name'] == mode_key and 'valueRange' in attr and equals_ignore_case(attr['valueRange']['type'], 'LIST'):
                    mode_mapping = {}
                    for item in attr['valueRange']['dataList']:
                        mode_mapping[str(item['data'])] = item['desc']
                        mode_mapping[item['desc']] = item['data']
                    mode_options = V1SpecAttributeParser._extract_mode_options(attr)
                    break

        ext = {
            'customize': True,
            'heater_type': 'electric',
            'temp_key': 'targetTemp',
            'current_temp_key': current_temp_key,
            'power_key': 'onOffStatus',
            'mode_key': mode_key,
            'mode_mapping': mode_mapping,
            'mode_options': mode_options,
        }

        return HaierAttribute('electric_water_heater', 'ElectricWaterHeater', Platform.WATER_HEATER, options, ext)

    @staticmethod
    def _parse_as_generic_water_heater(attributes: List[dict], temp_key: str = 'targetTemperature', mode_key: str = None):
        for attr in attributes:
            if attr['name'] == temp_key:
                target_temperature_attr = attr
                break
        else:
            raise RuntimeError(f'{temp_key} attr not found for generic water heater')

        data_step = target_temperature_attr['valueRange']['dataStep']
        options = {
            'min_temp': float(data_step['minValue']),
            'max_temp': float(data_step['maxValue']),
            'target_temperature_step': float(data_step['step'])
        }

        current_temp_key = None
        for ck in ['currentTemp', 'inWaterTemp', 'waterTemp', 'innerWaterTemp', 'outWaterTemp', 'roomTemp']:
            if any(a['name'] == ck for a in attributes):
                current_temp_key = ck
                break

        mode_mapping = None
        mode_options = None
        if mode_key:
            for attr in attributes:
                if attr['name'] == mode_key and 'valueRange' in attr and equals_ignore_case(attr['valueRange']['type'], 'LIST'):
                    mode_mapping = {}
                    for item in attr['valueRange']['dataList']:
                        mode_mapping[str(item['data'])] = item['desc']
                        mode_mapping[item['desc']] = item['data']
                    mode_options = V1SpecAttributeParser._extract_mode_options(attr)
                    break

        ext = {
            'customize': True,
            'heater_type': 'generic',
            'temp_key': temp_key,
            'current_temp_key': current_temp_key,
            'power_key': 'onOffStatus',
            'mode_key': mode_key,
            'mode_mapping': mode_mapping,
            'mode_options': mode_options,
        }

        return HaierAttribute('generic_water_heater', 'WaterHeater', Platform.WATER_HEATER, options, ext)

    @staticmethod
    def _parse_as_cover(attributes: List[dict]):
        for attr in attributes:
            if attr["name"] == "openDegree":
                step = attr['valueRange']['dataStep']
        options = {
            'native_min_value': float(step['minValue']),
            'native_max_value': float(step['maxValue']),
            'native_step': step['step']
        }
        ext = {
            'customize': True,
        }
        return HaierAttribute('cover', 'Cover', Platform.COVER, options,ext)

    @staticmethod
    def _is_binary_attribute(attribute):
        valueRange = attribute['valueRange']

        return (equals_ignore_case(valueRange['type'], 'LIST')
                and len(valueRange['dataList']) == 2
                and contains_any_ignore_case(valueRange['dataList'][0]['data'], ['true', 'false'])
                and contains_any_ignore_case(valueRange['dataList'][1]['data'], ['true', 'false']))

    @staticmethod
    def _guess_state_class_device_class_and_unit(attribute) -> (str, str, str):
        """
        猜测 state class, device class和unit
        :return:
        """

        state_class = None

        if '累计' in attribute['desc']:
            state_class = SensorStateClass.TOTAL
        
        if '本次' in attribute['desc']:
            state_class = SensorStateClass.TOTAL_INCREASING

        if '温度' in attribute['desc']:
            return state_class, SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS

        if '湿度' in attribute['desc']:
            return state_class, SensorDeviceClass.HUMIDITY, PERCENTAGE

        if '用水量' in attribute['desc']:
            return state_class, SensorDeviceClass.WATER, UnitOfVolume.LITERS

        if '用气量' in attribute['desc']:
            return state_class, SensorDeviceClass.GAS, UnitOfVolume.LITERS

        if '用电量' in attribute['desc']:
            return state_class, SensorDeviceClass.ENERGY, UnitOfEnergy.KILO_WATT_HOUR

        return state_class, None, None

import logging
from abc import abstractmethod, ABC
from typing import List

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.components.switch import SwitchDeviceClass
from homeassistant.const import Platform, UnitOfTemperature, PERCENTAGE, UnitOfVolume, UnitOfEnergy

from custom_components.haier.helpers import equals_ignore_case, contains_any_ignore_case

_LOGGER = logging.getLogger(__name__)

# 描述里的无效/占位选项，模式列表里应当过滤掉
_INVALID_MODE_DESCS = ('未设置', '无', '关', '关闭', '关机', '停止', 'off', 'OFF', 'none', 'None', '')

# 热水器：按中文 desc 识别的特殊功能——即使被云端标为 writable=false，
# 实际控制命令仍可下发；默认流程会把它们当成只读 Sensor 丢掉，这里
# 统一强制升级为可操作实体。
_WATER_HEATER_BOOL_FEATURES = [
    ('season_temp', '四季温水', ['四季温水', '四季恒温', '四季温感']),
    ('auto_off', '自动关机', ['自动关机', '自动断电', '加热完成自动', '加热完成后自动']),
]

# 预约时段数量上限（覆盖预约1 ~ 预约N）
_MAX_RESERVATION_SLOTS = 4

# 关键词表：用于在 parse_attribute 阶段就识别出"虽然 writable=false
# 但后续会被热水器增强逻辑升级为可写实体"的字段，直接跳过只读副本，
# 避免 HA 里出现同名"双胞胎"（一个 switch + 一个 binary_sensor/sensor）。
_WH_FORCE_WRITABLE_BOOL_KWS = [
    '四季温水', '四季恒温', '四季温感',
    '自动关机', '自动断电', '加热完成自动',
    '智能抑菌', '抑菌', '杀菌', '除菌', '灭菌', '抗菌',
    '中温保温',
]

_WH_RESERVATION_PREFIXES = ('预约', '定时')
_WH_RESERVATION_KEYWORDS = ('启停', '开关', '循环', '重复', '小时', '分钟', '温度')


def _is_wh_reservation(desc: str) -> bool:
    """判断 desc 是否为热水器预约/定时字段（预约1启停、预约1循环、预约1时间小时 等）。"""
    if not desc:
        return False
    for p in _WH_RESERVATION_PREFIXES:
        if desc.startswith(p):
            for kw in _WH_RESERVATION_KEYWORDS:
                if kw in desc:
                    return True
    return False


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

        # —— 热水器增强字段黑名单：这些字段虽然被云端标记为 writable=false
        # 或为STEP只读，但实际可以下发命令，后面 parse_global 会专门为它们
        # 生成可写的 Switch/Number/Button 实体。这里跳过只读副本，避免 HA
        # 里出现同名但不可操作的"双胞胎"实体。
        if not attribute['writable'] and attribute['readable']:
            if self._is_water_heater_forced_writable(attribute):
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

        # 判断是不是热水器（用于后续增强实体判断）
        is_water_heater = any(key in all_attribute_keys for key in
                              ('targetTemp', 'targetTemperature')) and 'onOffStatus' in all_attribute_keys

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
        # 1. 完整燃气热水器：有出水温度、目标温度、累计用气量
        if 'outWaterTemp' in all_attribute_keys and 'targetTemp' in all_attribute_keys and 'totalUseGasL' in all_attribute_keys:
            yield self._parse_as_gas_water_heater(attributes)
        # 2. 降级燃气热水器（issue #126：无出水温度字段，如JSQ30-16R3BWU1）
        elif 'targetTemp' in all_attribute_keys and 'onOffStatus' in all_attribute_keys and 'totalUseGasL' in all_attribute_keys:
            yield self._parse_as_gas_water_heater(attributes, out_water_temp_optional=True)
        # 3. 电热水器：有目标温度 + 开关机状态 + 无累计用气量
        elif 'targetTemp' in all_attribute_keys and 'onOffStatus' in all_attribute_keys:
            # 查找加热模式字段
            mode_key = None
            for mk in ['heatMode', 'workMode', 'runningMode', 'heatingMode', 'mode', 'heatStatus', 'smartMode']:
                if mk in all_attribute_keys:
                    mode_key = mk
                    break
            yield self._parse_as_electric_water_heater(attributes, mode_key=mode_key)
        # 4. 目标温度用 targetTemperature 命名的热水器（部分新型号/空气能）
        elif 'targetTemperature' in all_attribute_keys and 'onOffStatus' in all_attribute_keys:
            mode_key = None
            for mk in ['heatMode', 'workMode', 'runningMode', 'heatingMode', 'mode', 'heatStatus']:
                if mk in all_attribute_keys:
                    mode_key = mk
                    break
            yield self._parse_as_generic_water_heater(attributes, temp_key='targetTemperature', mode_key=mode_key)

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

        # —— 热水器增强实体 ——
        # 基于中文 desc 匹配来强制生成可操作实体，覆盖：
        #   * 四季温水开关
        #   * 自动关机开关（加热完成后关机）
        #   * 智能抑菌按钮（单次触发）
        #   * 预约启停/循环开关、预约小时/分钟/温度Number
        # 这些字段在部分机型上被云端标记为 writable=false，默认流程会
        # 把它们当成 Sensor，但实际控制命令依然可以下发。
        if is_water_heater:
            yield from self._parse_water_heater_extra_entities(attributes)

    # ------------------------------------------------------------------
    # 热水器增强实体：通过中文 desc 识别，强制生成可控制的实体
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_water_heater_extra_entities(attributes: List[dict]):
        # 构建已解析实体名集合，用于去除重复生成（同名只读副本将由前端过滤或合并）
        yielded_keys = set()

        def _desc_contains(desc, keywords):
            """中文 desc 模糊匹配（desc 包含任意关键词即命中）"""
            if not desc:
                return False
            for kw in keywords:
                if kw in desc:
                    return True
            return False

        def _find_attr(keywords):
            for attr in attributes:
                if _desc_contains(attr.get('desc', ''), keywords):
                    return attr
            return None

        def _on_off_values(attr):
            """根据字段当前值/valueRange推断开/关的原始值。"""
            vr = attr.get('valueRange', {})
            raw = attr.get('value')
            # 有 dataList 的情况：取两项
            if equals_ignore_case(vr.get('type'), 'LIST') and 'dataList' in vr:
                items = vr['dataList']
                true_data, false_data = True, False
                for it in items:
                    d = str(it.get('data', ''))
                    desc = str(it.get('desc', ''))
                    if d.lower() in ('true', '1', '开', '开启') or '开' in desc or '启用' in desc:
                        true_data = it['data']
                    if d.lower() in ('false', '0', '关', '关闭') or '关' in desc or '禁用' in desc:
                        false_data = it['data']
                return true_data, false_data
            # 否则根据当前值类型推断
            if isinstance(raw, bool):
                return True, False
            if isinstance(raw, int):
                return 1, 0
            if isinstance(raw, str):
                if raw in ('1', '0'):
                    return '1', '0'
                if raw.lower() in ('true', 'false'):
                    return 'true', 'false'
            return True, False

        def _yield_switch(attr, entity_key, display_name):
            on_v, off_v = _on_off_values(attr)
            yield HaierAttribute(
                entity_key,
                display_name,
                Platform.SWITCH,
                {'device_class': SwitchDeviceClass.SWITCH},
                {
                    # 直接用属性的原始 name 做数据 key（entity_key 是我们内部唯一标识）
                    'data_key': attr['name'],
                    'on_value': on_v,
                    'off_value': off_v,
                }
            )
            yielded_keys.add(entity_key)

        # ---- 1. 四季温水 / 自动关机 等二元开关 ----
        for entity_suffix, display_name, keywords in _WATER_HEATER_BOOL_FEATURES:
            attr = _find_attr(keywords)
            if not attr:
                _LOGGER.debug('water heater feature %s not found (keywords=%s)', display_name, keywords)
                continue
            entity_key = attr['name']  # 用原始字段名，方便和默认解析流程对齐
            # 跳过已经作为正常switch解析的（智能抑菌会走正常路径）
            if attr.get('writable') and V1SpecAttributeParser._is_binary_attribute(attr):
                continue
            yield from _yield_switch(attr, entity_key, display_name)

        # ---- 2. 智能抑菌：若被云端标记为只读LIST，按按钮生成（单次触发） ----
        sterilization = _find_attr(['智能抑菌', '抑菌', '杀菌', '除菌', '灭菌', '抗菌'])
        if sterilization is not None and not (
                sterilization.get('writable')
                and V1SpecAttributeParser._is_binary_attribute(sterilization)):
            # 判断是不是"单次触发"（dataList只有1项或 desc 里含"一次"）
            desc = sterilization.get('desc', '')
            vr = sterilization.get('valueRange', {})
            is_one_shot = ('一次' in desc) or (
                    equals_ignore_case(vr.get('type'), 'LIST') and len(vr.get('dataList', [])) <= 1)
            if is_one_shot:
                cmd_value = True
                if equals_ignore_case(vr.get('type'), 'LIST') and vr.get('dataList'):
                    cmd_value = vr['dataList'][0].get('data', True)
                yield HaierAttribute(
                    sterilization['name'],
                    sterilization.get('desc', '智能抑菌'),
                    Platform.BUTTON,
                    ext={'command_value': cmd_value, 'data_key': sterilization['name']}
                )
                yielded_keys.add(sterilization['name'])
            else:
                yield from _yield_switch(sterilization, sterilization['name'],
                                         sterilization.get('desc', '智能抑菌'))

        # ---- 3. 预约/定时开关机：预约{N}启停、循环、小时、分钟、温度 ----
        # 海尔电热水器的定时是靠"预约1/预约2"实现的，每个预约包含：
        #   预约1启停 (LIST 开/关)
        #   预约1循环 (LIST 开/关：是否每天重复)
        #   预约1时间小时 (STEP 0-23)
        #   预约1时间分钟 (STEP 0-59)
        #   预约1温度 (STEP 35-75，部分机型)
        for i in range(1, _MAX_RESERVATION_SLOTS + 1):
            prefixes = [f'预约{i}', f'预约{i}号', f'定时{i}', f'定时{i}号']
            slot_attrs = {}
            for attr in attributes:
                desc = attr.get('desc', '')
                for p in prefixes:
                    if desc.startswith(p):
                        slot_attrs[desc] = attr
                        break

            if not slot_attrs:
                continue

            # 启停开关
            for desc, attr in slot_attrs.items():
                if '启停' in desc or '开关' in desc:
                    yield from _yield_switch(attr, attr['name'], f'预约{i}开关')
                elif '循环' in desc or '重复' in desc:
                    yield from _yield_switch(attr, attr['name'], f'预约{i}每日循环')
                elif '小时' in desc:
                    # 小时 Number：0-23
                    step = attr.get('valueRange', {}).get('dataStep', {})
                    yield HaierAttribute(
                        attr['name'],
                        f'预约{i}小时',
                        Platform.NUMBER,
                        {
                            'native_min_value': 0.0,
                            'native_max_value': 23.0,
                            'native_step': 1.0,
                            'native_unit_of_measurement': '时',
                        },
                        {'data_key': attr['name']}
                    )
                elif '分钟' in desc:
                    yield HaierAttribute(
                        attr['name'],
                        f'预约{i}分钟',
                        Platform.NUMBER,
                        {
                            'native_min_value': 0.0,
                            'native_max_value': 59.0,
                            'native_step': 1.0,
                            'native_unit_of_measurement': '分',
                        },
                        {'data_key': attr['name']}
                    )
                elif '温度' in desc:
                    step = attr.get('valueRange', {}).get('dataStep', {})
                    yield HaierAttribute(
                        attr['name'],
                        f'预约{i}温度',
                        Platform.NUMBER,
                        {
                            'native_min_value': float(step.get('minValue', 35)),
                            'native_max_value': float(step.get('maxValue', 75)),
                            'native_step': float(step.get('step', 1)),
                            'native_unit_of_measurement': UnitOfTemperature.CELSIUS,
                        },
                        {'data_key': attr['name']}
                    )

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

        # 推断 on/off 的原始值类型
        on_v, off_v = True, False
        vr = attribute.get('valueRange', {})
        if equals_ignore_case(vr.get('type'), 'LIST') and 'dataList' in vr:
            for it in vr['dataList']:
                d = it.get('data')
                desc = str(it.get('desc', ''))
                if str(d).lower() in ('true', '1') or '开' in desc:
                    on_v = d
                if str(d).lower() in ('false', '0') or '关' in desc:
                    off_v = d
        raw = attribute.get('value')
        if isinstance(raw, int):
            on_v, off_v = 1, 0
        elif isinstance(raw, str):
            if raw in ('1', '0'):
                on_v, off_v = '1', '0'

        ext = {'on_value': on_v, 'off_value': off_v}
        return HaierAttribute(attribute['name'], attribute['desc'], Platform.SWITCH, options, ext)

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
    def _is_water_heater_forced_writable(attribute):
        """判断一个 writable=false 的字段是否会被热水器增强逻辑升级为可写实体。

        用于在 parse_attribute 阶段直接跳过只读副本，避免出现 switch+binary_sensor 同名双胞胎。
        """
        desc = attribute.get('desc', '')
        if not desc:
            return False
        for kw in _WH_FORCE_WRITABLE_BOOL_KWS:
            if kw in desc:
                return True
        if _is_wh_reservation(desc):
            return True
        return False

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

        # 构建模式映射表（只保留 数字→中文 方向，避免双向字典污染操作列表）
        mode_mapping = None
        mode_options = None
        if mode_key:
            for attr in attributes:
                if attr['name'] == mode_key and 'valueRange' in attr and equals_ignore_case(attr['valueRange']['type'], 'LIST'):
                    mode_mapping = {}
                    ordered_options = []
                    seen = set()
                    for item in attr['valueRange']['dataList']:
                        data_val = item['data']
                        desc = item['desc']
                        # 过滤掉无效占位选项
                        if desc in _INVALID_MODE_DESCS or str(data_val) in _INVALID_MODE_DESCS:
                            continue
                        mode_mapping[str(data_val)] = desc
                        if desc not in seen:
                            ordered_options.append(desc)
                            seen.add(desc)
                    mode_options = ordered_options
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

        return HaierAttribute('electric_water_heater', 'WaterHeater', Platform.WATER_HEATER, options, ext)

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
                    ordered_options = []
                    seen = set()
                    for item in attr['valueRange']['dataList']:
                        data_val = item['data']
                        desc = item['desc']
                        if desc in _INVALID_MODE_DESCS or str(data_val) in _INVALID_MODE_DESCS:
                            continue
                        mode_mapping[str(data_val)] = desc
                        if desc not in seen:
                            ordered_options.append(desc)
                            seen.add(desc)
                    mode_options = ordered_options
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
        return HaierAttribute('cover', 'Cover', Platform.COVER, options, ext)

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

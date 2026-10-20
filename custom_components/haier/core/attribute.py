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
# 每项格式：(内部key后缀, 显示名, [中文desc关键词...], [英文名name关键词...])
_WATER_HEATER_BOOL_FEATURES = [
    # 用户确认：机器上的"中温保温"字段（heatKeepingStatus / desc=中温保温功能状态）
    # 实际就是海尔智家 APP 里的"四季温水"，开这个开关热水器就显示四季温水。
    # 因此把「四季温水」与「中温保温」合并为同一个功能，统一显示为"四季温水"，
    # 不再为四季温水单独生成额外的虚拟开关（否则会出现一个多余的、点不动的"四季温水"）。
    ('season_temp', '四季温水',
     ['四季温水', '四季恒温', '四季温感', '恒温保温', '智能保温', '中温保温', '中温恒温'],
     ['seasontemp', 'season_temp', 'fourseason', 'constanttemp',
      'midtempkeep', 'mid_temp_keep', 'mediumtempkeep', 'insulation']),
    ('sterilization', '智能抑菌',
     ['智能抑菌', '抑菌', '杀菌', '除菌', '灭菌', '抗菌'],
     ['sterilization', 'sterilize', 'bacteriostasis', 'antibacterial', 'steril']),
]

# 预约时段数量上限（覆盖预约1 ~ 预约N）
_MAX_RESERVATION_SLOTS = 4

# 关键词表：用于在 parse_attribute 阶段就识别出"虽然 writable=false
# 但后续会被热水器增强逻辑升级为可写实体"的字段，直接跳过只读副本，
# 避免 HA 里出现同名"双胞胎"（一个 switch + 一个 binary_sensor/sensor）。
_WH_FORCE_WRITABLE_BOOL_KWS = [
    # 四季温水（本机实际字段为 heatKeepingStatus，desc=中温保温功能状态）
    '四季温水', '四季恒温', '四季温感', '恒温保温', '智能保温',
    # 智能抑菌
    '智能抑菌', '抑菌', '杀菌', '除菌', '灭菌', '抗菌',
    # 中温保温
    '中温保温', '中温恒温',
]

# 英文名（name 字段）关键词，用于部分机型 desc 为英文/通用命名时也能识别
_WH_FORCE_WRITABLE_EN_KWS = [
    'seasontemp', 'season_temp', 'fourseason', 'constanttemp', 'midtempkeep',
    'sterilization', 'sterilize', 'bacteriostasis', 'antibacterial', 'steril',
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
    # 处理"定时开"/"定时关"这种没有数字的变体
    if ('定时' in desc or '预约' in desc) and any(kw in desc for kw in _WH_RESERVATION_KEYWORDS):
        return True
    return False


def _attr_name_matches(name: str, en_keywords) -> bool:
    """按英文名模糊匹配（不区分大小写，去除下划线）。"""
    if not name:
        return False
    n = name.lower().replace('_', '')
    for kw in en_keywords:
        if kw.lower().replace('_', '') in n:
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
        # —— 热水器增强字段黑名单（必须放在 'value' not in attribute 判断之前）：
        # 有些字段（如四季温水/中温保温）在部分机型上云端根本不下发 value 字段，
        # 只有描述，此时后面的增强逻辑（parse_global）会为它们单独生成可操作实体。
        # 这里直接跳过默认的只读副本，避免 HA 里出现残缺的 binary_sensor。
        if self._is_water_heater_forced_writable(attribute):
            return None

        # 没有value的attribute后续无法正常使用，所以需要过滤掉
        if 'value' not in attribute:
            return None

        # 只读属性 → 按 sensor / binary_sensor 处理
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

        # —— 热水器全属性 dump（DEBUG 级别，默认不输出）：
        # 仅在把 haier 的日志级别手动设为 debug 时才会打印该热水器数字模型里
        # 每个属性的 name / desc / writable / readable / value / rangeType，
        # 用于排查"某个功能为何没有出现"。默认运行日志不再输出任何调试内容。
        if is_water_heater:
            device_id_marker = ''
            for a in attributes:
                if a.get('deviceId'):
                    device_id_marker = a['deviceId']
                    break
            _LOGGER.debug('=== Haier water heater attribute dump (device=%s) ===', device_id_marker)
            for a in attributes:
                name = a.get('name', '')
                desc = a.get('desc', '')
                writable = a.get('writable')
                readable = a.get('readable')
                value = a.get('value', '<no value>')
                vr = a.get('valueRange', {})
                vr_type = vr.get('type', '') if vr else ''
                _LOGGER.debug(
                    '  attr: name=%-32s desc=%-24s writable=%s readable=%s value=%s rangeType=%s',
                    name, desc, writable, readable, value, vr_type
                )
            _LOGGER.debug('=== End attribute dump ===')

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
                if 'dataList' not in attr.get('valueRange', {}):

                    items = []

                else:

                    items = attr['valueRange'].get('dataList', [])

                for item in items:
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
        #   * 四季温水开关（本机实际字段 heatKeepingStatus）
        #   * 智能抑菌按钮/开关
        #   * 预约启停开关、预约小时/分钟/温度 Number
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

        def _find_attr(desc_keywords, en_keywords=None):
            """按中文 desc 模糊匹配，或按英文名模糊匹配（en_keywords）。"""
            for attr in attributes:
                if _desc_contains(attr.get('desc', ''), desc_keywords):
                    return attr
            if en_keywords:
                for attr in attributes:
                    if _attr_name_matches(attr.get('name', ''), en_keywords):
                        return attr
            return None

        def _on_off_values(attr):
            """根据字段当前值/valueRange推断开/关的原始值。"""
            vr = attr.get('valueRange', {})
            raw = attr.get('value')
            # 有 dataList 的情况：取两项
            if equals_ignore_case(vr.get('type'), 'LIST') and 'dataList' in vr:
                items = vr['dataList']
                true_data, false_data = 1, 0
                for it in items:
                    d = it.get('data')
                    desc = str(it.get('desc', ''))
                    d_str = str(d)
                    if d_str.lower() in ('true', '1', '开', '开启') or '开' in desc or '启用' in desc:
                        true_data = d
                    if d_str.lower() in ('false', '0', '关', '关闭') or '关' in desc or '禁用' in desc:
                        false_data = d
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
            # 字段没有 value（如部分机型的四季温水/中温保温），也没有 dataList，
            # 海尔电热水器的二元功能普遍用 1/0 控制，默认用 1/0 比 True/False 更兼容。
            return 1, 0

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

        # ---- 1. 四季温水 / 中温保温 / 智能抑菌 等二元开关 ----
        # 这些功能在部分机型上被云端标记为 writable=false，默认解析流程会把它们
        # 当成只读 Sensor，甚至因为没有 value 字段而直接丢弃；但实际控制命令
        # 依然可以下发，因此这里统一强制升级为可操作的开关实体。
        for _suffix, display_name, desc_keywords, en_keywords in _WATER_HEATER_BOOL_FEATURES:
            attr = _find_attr(desc_keywords, en_keywords)
            if not attr:
                # 该机型不具备此功能，静默跳过（不输出任何日志）
                continue
            entity_key = attr['name']  # 用原始字段名，方便和默认解析流程对齐
            # 跳过已经作为正常switch解析的（可写且已被默认流程识别为bool）
            if attr.get('writable') and V1SpecAttributeParser._is_binary_attribute(attr):
                continue
            # 避免重复生成（智能抑菌可能已被下面按钮或switch处理过）
            if entity_key in yielded_keys:
                continue
            yield from _yield_switch(attr, entity_key, display_name)

        # ---- 2. 智能抑菌：若未通过上面的开关生成，检查是否为单次触发按钮 ----
        sterilization = _find_attr(['智能抑菌', '抑菌', '杀菌', '除菌', '灭菌', '抗菌'],
                                   ['sterilization', 'sterilize', 'bacteriostasis', 'antibacterial', 'steril'])
        if sterilization is not None and sterilization['name'] not in yielded_keys:
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
        # 部分机型还可能用英文名：timer*, reserve*, appoint*, schedule*, timed*
        for i in range(1, _MAX_RESERVATION_SLOTS + 1):
            prefixes = [f'预约{i}', f'预约{i}号', f'定时{i}', f'定时{i}号']
            # 英文名前缀（timer1/reserve1/appoint1/schedule1/timing1/timed1）
            en_prefixes = [f'timer{i}', f'reserve{i}', f'reservation{i}',
                           f'appoint{i}', f'schedule{i}', f'timing{i}', f'timed{i}']
            slot_attrs = {}
            for attr in attributes:
                desc = attr.get('desc', '')
                name = attr.get('name', '').lower().replace('_', '')
                # 中文前缀匹配
                matched = False
                for p in prefixes:
                    if desc.startswith(p):
                        slot_attrs[desc] = attr
                        matched = True
                        break
                if matched:
                    continue
                # 英文名前缀匹配（不区分大小写/下划线）
                for ep in en_prefixes:
                    ep_clean = ep.lower().replace('_', '')
                    if name.startswith(ep_clean):
                        slot_attrs[desc or name] = attr
                        break

            if not slot_attrs:
                continue

            # 启停开关
            for desc, attr in slot_attrs.items():
                if '启停' in desc or '开关' in desc:
                    sw_name = '预约开关' if i == 1 else f'预约{i}开关'
                    yield from _yield_switch(attr, attr['name'], sw_name)
                elif '循环' in desc or '重复' in desc:
                    # 用户反馈：设备端的"预约循环"开关永远保持开启、点击无法关闭，
                    # 海尔智家 APP 的预约功能里也没有对应设置项，属于无效实体，
                    # 因此不再为它生成任何 HA 实体。
                    continue
                elif '小时' in desc:
                    # 小时 Number：0-23
                    num_name = '预约时间（时）' if i == 1 else f'预约{i}时间（时）'
                    yield HaierAttribute(
                        attr['name'],
                        num_name,
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
                    num_name = '预约时间（分）' if i == 1 else f'预约{i}时间（分）'
                    yield HaierAttribute(
                        attr['name'],
                        num_name,
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
                    num_name = '预约温度' if i == 1 else f'预约{i}温度'
                    yield HaierAttribute(
                        attr['name'],
                        num_name,
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
            dl = attribute['valueRange'].get('dataList', [])

            for item in dl:
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
        dl = attribute['valueRange'].get('dataList', [])

        for item in dl:
            value_comparison_table[str(item['data'])] = item['desc']
            value_comparison_table[str(item['desc'])] = item['data']

        ext = {
            'value_comparison_table': value_comparison_table
        }

        dl = attribute['valueRange'].get('dataList', [])
        options = {
            'options': [item['desc'] for item in dl]
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
        dl = attribute.get('valueRange', {}).get('dataList', [])
        cmd = dl[0]['data'] if dl else True
        return HaierAttribute(
            attribute['name'],
            attribute['desc'],
            Platform.BUTTON,
            ext={'command_value': cmd}
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
        name = attribute.get('name', '')
        if desc:
            for kw in _WH_FORCE_WRITABLE_BOOL_KWS:
                if kw in desc:
                    return True
        if name:
            if _attr_name_matches(name, _WH_FORCE_WRITABLE_EN_KWS):
                return True
        if desc and _is_wh_reservation(desc):
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

        vr = target_temperature_attr.get('valueRange', {})
        data_step = vr.get('dataStep', {'minValue': 35, 'maxValue': 75, 'step': 1})
        options = {
            'min_temp': float(data_step.get('minValue', 35)),
            'max_temp': float(data_step.get('maxValue', 75)),
            'target_temperature_step': float(data_step.get('step', 1))
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

        vr = target_temperature_attr.get('valueRange', {})
        data_step = vr.get('dataStep', {'minValue': 35, 'maxValue': 75, 'step': 1})
        options = {
            'min_temp': float(data_step.get('minValue', 35)),
            'max_temp': float(data_step.get('maxValue', 75)),
            'target_temperature_step': float(data_step.get('step', 1))
        }

        # 查找当前温度字段（海尔电热水器最常见字段名：currentTemperature）
        current_temp_key = None
        for ck in ['currentTemperature', 'currentTemp', 'innerWaterTemp', 'waterTemp',
                   'inWaterTemp', 'actualTemp', 'realTemp', 'inTemp', 'temp']:
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
                    if 'dataList' not in attr.get('valueRange', {}):

                        items = []

                    else:

                        items = attr['valueRange'].get('dataList', [])

                    for item in items:
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

        vr = target_temperature_attr.get('valueRange', {})
        data_step = vr.get('dataStep', {'minValue': 35, 'maxValue': 75, 'step': 1})
        options = {
            'min_temp': float(data_step.get('minValue', 35)),
            'max_temp': float(data_step.get('maxValue', 75)),
            'target_temperature_step': float(data_step.get('step', 1))
        }

        current_temp_key = None
        # currentTemperature 是海尔电热水器最常见的"当前温度"字段名，必须放第一位
        for ck in ['currentTemperature', 'currentTemp', 'inWaterTemp', 'waterTemp',
                   'innerWaterTemp', 'outWaterTemp', 'roomTemp', 'actualTemp', 'realTemp']:
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
                    if 'dataList' not in attr.get('valueRange', {}):

                        items = []

                    else:

                        items = attr['valueRange'].get('dataList', [])

                    for item in items:
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
        valueRange = attribute.get('valueRange', {})
        if not equals_ignore_case(valueRange.get('type'), 'LIST'):
            return False
        data_list = valueRange.get('dataList', [])
        if len(data_list) != 2:
            return False
        # 识别二元字段：true/false、1/0、开/关 三种形式都算
        values = []
        for item in data_list:
            d = item.get('data')
            desc = str(item.get('desc', ''))
            d_str = str(d).lower()
            if d_str in ('true', 'false', '1', '0'):
                values.append(d)
            elif '开' in desc or '关' in desc or '启用' in desc or '禁用' in desc:
                values.append(d)
        # 只要能提取到两个值（通常是true/false 或 1/0 或 开/关）就算二元
        if len(values) == 2:
            return True
        # 兼容原逻辑：只要两个值都包含true/false字符串
        return (contains_any_ignore_case(data_list[0]['data'], ['true', 'false'])
                and contains_any_ignore_case(data_list[1]['data'], ['true', 'false']))

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

"""Readable archive conditions from saved metadata only; no defaults or sample reads."""
from __future__ import annotations

import math


MISSING = "—"


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def _first_text(*values):
    return next((text for value in values if (text := _text(value))), None)


def _number(value, unit):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return f"{number:.12g} {unit}" if math.isfinite(number) else None


def _group(parameters, manifest, name):
    # A stored null explicitly disables a group; do not revive an older fallback.
    if name in parameters:
        return _mapping(parameters[name])
    fallback = _mapping(manifest.get(name))
    # Acquisition manifests wrap the original temperature configuration together
    # with runtime status; old CLI records may have no separate desktop context.
    if name == "temperature_sweep" and isinstance(fallback.get("config"), dict):
        return fallback["config"]
    return fallback


def archive_conditions(row, context=None, manifest=None):
    """Return display/search fields without changing the archive's raw metadata.

    ``row`` can be an index row or a details result. The latter already contains
    ``context`` and a filtered ``metadata`` manifest. Explicit context parameters
    take priority; run-directory names are never treated as sample identifiers.
    """
    row = _mapping(row)
    context = _mapping(row.get("context") if context is None else context)
    manifest = _mapping(row.get("metadata") if manifest is None else manifest)
    parameters = _mapping(context.get("parameters"))
    temperature = _group(parameters, manifest, "temperature_sweep")
    liquid = _group(parameters, manifest, "liquid_temperature")

    sample = _first_text(parameters.get("sample_name"), parameters.get("label"),
                         temperature.get("condition_label"), liquid.get("sample_id"),
                         manifest.get("sample_name"), manifest.get("label"),
                         manifest.get("initial_label"), manifest.get("material_input"))
    record_label = _first_text(parameters.get("label"), manifest.get("label"),
                              manifest.get("initial_label"), manifest.get("material_input"))
    sensor = _first_text(parameters.get("sensor"), context.get("sensor"), manifest.get("sensor"), row.get("sensor"))
    notes = _first_text(parameters.get("notes"), parameters.get("experiment_notes"), parameters.get("description"),
                       context.get("notes"), manifest.get("notes"), manifest.get("experiment_notes"), manifest.get("description"))
    details = []
    search = []

    def field(title, value):
        details.append(f"{title}：{value or MISSING}")
        if value:
            search.append(value)

    field("样品／条件名称", sample)
    field("记录标签", record_label)
    field("传感器", sensor)
    field("实验说明", notes)
    condition = _first_text(parameters.get("condition"), context.get("condition"))
    if condition:
        field("实验条件", {"material":"材料／样品", "finger":"手指"}.get(condition, condition))
        search.append(condition)

    summary = []
    if temperature:
        mode = _text(temperature.get("mode"))
        mode_name = {"manual": "手填参考温度", "continuous": "连续电压", "platform": "平台参考与电压"}.get(mode, mode)
        reference = _number(temperature.get("reference_temperature_c"), "°C")
        channel = _text(temperature.get("channel"))
        field("温度记录", mode_name)
        field("温度条件名称", _text(temperature.get("condition_label")))
        field("参考温度", reference)
        if mode == "manual":
            summary.append(f"手填参考 {reference or MISSING}")
        else:
            field("温度通道", channel)
            field("温度电压单位", "V" if mode in ("continuous", "platform") else None)
            summary.append(" · ".join(value for value in (mode_name, channel, "V" if mode in ("continuous", "platform") else None) if value) or MISSING)
            if reference:
                summary.append(f"参考 {reference}")
        calibration = _mapping(temperature.get("calibration"))
        if calibration:
            field("温度换算配置编号", _text(calibration.get("id")))
            field("换算斜率", _number(calibration.get("slope_c_per_v"), "°C/V"))
            field("换算截距", _number(calibration.get("intercept_c"), "°C"))
            low, high = (_number(calibration.get(key), "V") for key in ("voltage_min", "voltage_max"))
            field("换算有效电压范围", f"{low or MISSING} 至 {high or MISSING}")
        elif mode in ("continuous", "platform"):
            field("温度换算配置", None)
    else:
        field("温度记录", None)

    if liquid:
        liquid_name = _text(liquid.get("liquid"))
        volume = _number(liquid.get("volume_ml"), "mL")
        reference = _number(liquid.get("temperature_c"), "°C")
        distance = _number(liquid.get("distance_mm"), "mm")
        summary.append(" · ".join(value for value in (
            "液体 " + (liquid_name or MISSING), volume,
            "起始参考 " + reference if reference else None,
            "距离 " + distance if distance else None) if value))
        for key, title in (("liquid", "液体"), ("sample_id", "液体样品编号"), ("container", "容器"),
                           ("geometry", "几何／放置条件"), ("thermometer", "参考温度计"), ("repeat_id", "重复编号")):
            field(title, _text(liquid.get(key)))
        field("液体体积", volume)
        field("目标温度", _number(liquid.get("target_temperature_c"), "°C"))
        field("起始实测参考温度", reference)
        field("起始参考距离", distance)
        if liquid.get("temperature_source"):
            field("液体温度来源", _text(liquid.get("temperature_source")))
    else:
        field("液体条件", None)

    # Serial archives have their own manifest, not desktop_context parameters.
    protocol = _text(manifest.get("protocol"))
    if protocol:
        field("串口协议", protocol)
        channels = manifest.get("channels")
        channel_names = [_text(value) for value in channels] if isinstance(channels, list) else []
        channel_text = " / ".join(value for value in channel_names if value) or _text(manifest.get("pressure_channel"))
        field("记录通道", channel_text)
        unit = "V（原始电压／ΔV）" if protocol == "pressure_v2" else "V" if protocol in ("temperature_v1", "pressure_v1") else None
        field("串口测量单位", unit)
        serial_name = {"temperature_v1": "温度串口", "pressure_v1": "压力串口", "pressure_v2": "压力串口"}.get(protocol, protocol)
        summary.append(" · ".join(value for value in (serial_name, channel_text, unit) if value))
        for key, title in (("temperature_calibration", "温度标定方式"), ("pressure_calibration", "压力标定方式")):
            settings = _mapping(manifest.get(key))
            if settings:
                field(title, _text(settings.get("mode")))

    return {"sample_name": sample or MISSING, "condition_summary": "；".join(summary) or MISSING,
            "condition_details": "\n".join(details), "condition_search": "\n".join(search)}

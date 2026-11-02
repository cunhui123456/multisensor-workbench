"""纯配置与标定换算；导入/验证均不连接设备。"""

import math

from sspp_experiments.temperature_pressure.acquisition import CaptureConfig

SCHEMA = "sspp.temperature_sweep.v1"
DEFAULT_START_HZ = 5e9
DEFAULT_STOP_HZ = 5.3e9
DEFAULT_POINTS = 20


def _number(value, name):
    if isinstance(value, bool):
        raise ValueError(f"{name}必须为有限数")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name}必须为有限数") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name}必须为有限数")
    return result


def validate_config(value):
    if not isinstance(value, dict):
        raise ValueError("温度扫频配置必须为对象")
    result = {"schema": SCHEMA, "mode":value.get("mode","platform"), "protocol": value.get("protocol", "temperature_v1"),
              "port": value.get("port", ""), "channel": value.get("channel", "S6"),
              "baudrate": value.get("baudrate", 9600),
              "condition_label": value.get("condition_label", ""),
              "reference_temperature_c": value.get("reference_temperature_c"),
              "calibration": value.get("calibration"),
              "freshness_s": _number(value.get("freshness_s", 3.0), "样本有效时限"),
              "preparation": value.get("preparation")}
    if result["mode"] not in ("platform","manual","continuous"):
        raise ValueError("请选择手填温度或连续变温扫频")
    if result["mode"] == "manual":
        if result["reference_temperature_c"] is None:
            raise ValueError("请填写本次平台实测温度")
        result["reference_temperature_c"] = _number(result["reference_temperature_c"],"平台温度")
        result["calibration"] = None
        if not isinstance(result["condition_label"],str) or not result["condition_label"].strip():
            raise ValueError("请填写温度条件名称")
        return result
    if result["mode"] == "continuous":
        result["reference_temperature_c"] = None
    if result["protocol"] != "temperature_v1" or result["channel"] not in ("S6", "S7"):
        raise ValueError("温度扫频需要温度 v1 固件及 S6 或 S7 实际通道")
    if not .1 <= result["freshness_s"] <= 10:
        raise ValueError("样本有效时限必须在 0.1–10 秒之间")
    CaptureConfig(result["port"], result["protocol"], result["condition_label"],
                  channel=result["channel"], baudrate=result["baudrate"],
                  preparation=result["preparation"]).validate()
    if result["reference_temperature_c"] is not None:
        result["reference_temperature_c"] = _number(result["reference_temperature_c"], "参考温度")
    calibration = result["calibration"]
    if calibration is not None:
        if not isinstance(calibration, dict):
            raise ValueError("标定必须为对象或空值")
        ident = calibration.get("id")
        if not isinstance(ident, str) or not ident.strip() or len(ident) > 120:
            raise ValueError("请填写可追溯的标定编号（最多 120 字）")
        calibration = {"id": ident.strip(), **{
            key: _number(calibration.get(key), key) for key in
            ("slope_c_per_v", "intercept_c", "voltage_min", "voltage_max")}}
        if calibration["slope_c_per_v"] == 0:
            raise ValueError("标定斜率不能为零")
        if not 0 <= calibration["voltage_min"] < calibration["voltage_max"] <= 5.1:
            raise ValueError("标定有效电压范围须在 0–5.1 V 内且下限小于上限")
        result["calibration"] = calibration
    return result


def calibrated_temperature(voltage_v, calibration):
    """始终保留原始电压；未标定和越界返回 None，绝不外推。"""
    voltage = _number(voltage_v, "传感器电压")
    if calibration is None:
        return None, "uncalibrated"
    if not calibration["voltage_min"] <= voltage <= calibration["voltage_max"]:
        return None, "outside_calibration_range"
    value = calibration["slope_c_per_v"] * voltage + calibration["intercept_c"]
    if not math.isfinite(value):
        return None, "nonfinite_calibration_result"
    return value, "calibrated"

"""升温响应派生量；不从未标定电压推断实际温度。"""
import math
from collections import deque


def validate_ramp(value):
    if not isinstance(value, dict):
        raise ValueError("请填写温度标定条件")
    start = value.get("start_temperature_c")
    if isinstance(start, bool) or not isinstance(start, (int, float)) or not math.isfinite(start) or not -20 <= start < 90:
        raise ValueError("请填写参考仪器实测室温（-20 至 90 ℃之间）")
    if value.get("target_temperature_c") != 90:
        raise ValueError("本实验终温为 90 ℃")
    profile = value.get("heating_profile", "operator_reported_linear")
    if profile not in ("operator_reported_linear", "stepped_plateaus"):
        raise ValueError("升温方式无效")
    return {"start_temperature_c": float(start), "target_temperature_c": 90.,
            "heating_profile": profile, "temperature_source": "external_reference"}


class VoltageTrend:
    """每通道以首个实收样本为基线；最近10秒至少5秒跨度才计算变化速率。"""
    def __init__(self):
        self.channels = {}

    def add(self, row):
        key = row["channel"]
        t, v = float(row["host_elapsed_s"]), float(row["voltage_v"])
        state = self.channels.get(key)
        if state is None or state["segment"] != row["time_segment"]:
            state = {"segment": row["time_segment"], "baseline": v, "window": deque()}
            self.channels[key] = state
        window = state["window"]
        window.append((t, v))
        while window and t - window[0][0] > 10:
            window.popleft()
        rate = None
        if len(window) >= 2 and t - window[0][0] >= 5:
            mt = sum(x for x,y in window) / len(window)
            mv = sum(y for x,y in window) / len(window)
            denom = sum((x-mt)**2 for x,y in window)
            if denom > 0:
                rate = 60 * sum((x-mt)*(y-mv) for x,y in window) / denom
        return {"delta_voltage_v": v - state["baseline"], "voltage_rate_v_per_min": rate}

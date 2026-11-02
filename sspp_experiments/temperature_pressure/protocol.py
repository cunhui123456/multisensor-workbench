"""两份已有 Arduino Sketch 的只读文本协议；保留错误与时钟分段。"""

import math

PROTOCOLS = {"pressure_v1": "压力原程序 · 时间/电压", "temperature_v1": "温度检查 v1 · S6/S7 ADC/电压"}
TEMPERATURE_HEADER = "t6_ms,S6_adc,S6_V,t7_ms,S7_adc,S7_V"
CSV_FIELDS = ["sample_index", "line_number", "channel", "mcu_time_ms", "mcu_elapsed_s",
              "host_time_utc", "host_elapsed_s", "adc", "voltage_v", "time_segment", "event"]


class LineParser:
    """feed 返回 (rows, events)。任何截断/畸形行都不补造样本。"""

    def __init__(self, protocol, pressure_channel="A2", max_line_bytes=16384):
        if protocol not in PROTOCOLS:
            raise ValueError("请选择已支持的 Arduino 串口协议")
        self.protocol = protocol
        self.pressure_channel = pressure_channel
        self.max_line_bytes = max_line_bytes
        self.buffer = bytearray()
        self.discarding = False
        self.line_number = 0
        self.sample_index = 0
        self.segment = 0
        self.clocks = {}

    def _event(self, kind, text, host_time_utc, host_elapsed_s):
        return {"kind": kind, "line_number": self.line_number, "text": text,
                "host_time_utc": host_time_utc, "host_elapsed_s": host_elapsed_s,
                "time_segment": self.segment}

    def feed(self, data, *, host_time_utc, host_elapsed_s):
        rows, events = [], []
        for byte in data:
            if byte == 10:
                self.line_number += 1
                if not self.discarding:
                    parsed, messages = self._line(bytes(self.buffer).rstrip(b"\r"), host_time_utc, host_elapsed_s)
                    rows.extend(parsed)
                    events.extend(messages)
                self.buffer.clear()
                self.discarding = False
            elif not self.discarding:
                self.buffer.append(byte)
                if len(self.buffer) > self.max_line_bytes:
                    events.append(self._event("line_too_long", "超长串口行已保留于 raw_serial.bin，未解析", host_time_utc, host_elapsed_s))
                    self.buffer.clear()
                    self.discarding = True
        return rows, events

    def finish(self, *, host_time_utc, host_elapsed_s):
        if not self.buffer and not self.discarding:
            return []
        self.line_number += 1
        self.buffer.clear()
        self.discarding = False
        return [self._event("truncated_line", "结束时存在未完成行，原始字节保留，未解析", host_time_utc, host_elapsed_s)]

    @staticmethod
    def _time(value):
        if not value.isdecimal():
            raise ValueError("MCU 时间不是整数")
        result = int(value)
        if not 0 <= result < 2**32:
            raise ValueError("MCU 时间超出 millis() 范围")
        return result

    @staticmethod
    def _number(value, low, high):
        number = float(value)
        if not math.isfinite(number) or not low <= number <= high:
            raise ValueError("ADC/电压超出协议范围或不是有限数")
        return number

    def _clock(self, channel, value, host_time_utc, host_elapsed_s):
        messages, marker = [], ""
        old = self.clocks.get(channel)
        if old is not None and value < old["last"]:
            if old["last"] - value > 2**31:
                old["wraps"] += 1
                marker = "mcu_wrap"
            else:
                self.segment += 1
                self.clocks.clear()
                old = None
                marker = "mcu_reset"
            messages.append(self._event(marker, f"{channel} MCU 时间下降；已标记{'回绕' if marker == 'mcu_wrap' else '新时间段'}", host_time_utc, host_elapsed_s))
        if old is not None and value == old["last"]:
            marker = "duplicate_mcu_time"
            messages.append(self._event(marker, f"{channel} MCU 时间重复，保留实收样本", host_time_utc, host_elapsed_s))
        if old is None:
            old = {"origin": value, "last": value, "wraps": 0}
        old["last"] = value
        self.clocks[channel] = old
        return (value + old["wraps"] * 2**32 - old["origin"]) / 1000, marker, messages

    def _line(self, raw, host_time_utc, host_elapsed_s):
        try:
            text = raw.decode("utf-8", errors="strict").strip()
        except UnicodeDecodeError:
            return [], [self._event("invalid_encoding", "不是 UTF-8 文本，原始字节保留", host_time_utc, host_elapsed_s)]
        if not text:
            return [], []
        # Nano 打开串口可能先吐出旧半行，再在复位时输出启动横幅。
        # 仅识别精确固件签名；残片不解释为测量，原始字节仍完整保存。
        signature = "# Temperature_Channel_Check v1"
        banner_at = text.find(signature) if self.protocol == "temperature_v1" else -1
        if banner_at > 0:
            kind = "startup_fragment" if host_elapsed_s <= 3.0 else "interrupted_line"
            fragment = self._event(kind, "固件重启横幅前的未完成行已排除: " + text[:banner_at][:500],
                                   host_time_utc, host_elapsed_s)
            rows, messages = self._line(text[banner_at:].encode("utf-8"), host_time_utc, host_elapsed_s)
            return rows, [fragment, *messages]
        if text.startswith("#"):
            kind = "firmware_error" if "ERROR" in text.upper() else "firmware_status"
            messages = [self._event(kind, text, host_time_utc, host_elapsed_s)]
            if self.protocol == "temperature_v1" and text.startswith("# Temperature_Channel_Check v1") and self.sample_index:
                # 固件重启横幅比 millis 的下降更明确；即使新值已经较大也分段。
                self.segment += 1
                self.clocks.clear()
                messages.append(self._event("mcu_reset", "再次收到温度固件启动横幅，开始新时间段", host_time_utc, host_elapsed_s))
            return [], messages
        if self.protocol == "temperature_v1" and text == TEMPERATURE_HEADER:
            return [], [self._event("protocol_header", text, host_time_utc, host_elapsed_s)]
        try:
            values = [part.strip() for part in text.split(",")]
            if self.protocol == "pressure_v1":
                if len(values) != 2:
                    raise ValueError("压力协议需要两列")
                samples = [(self.pressure_channel, self._time(values[0]), "", self._number(values[1], 0, 5.1))]
            else:
                if len(values) != 6:
                    raise ValueError("温度协议需要六列")
                samples = [("S6", self._time(values[0]), self._number(values[1], 0, 1023), self._number(values[2], 0, 5.1)),
                           ("S7", self._time(values[3]), self._number(values[4], 0, 1023), self._number(values[5], 0, 5.1))]
                if samples[1][1] < samples[0][1] and samples[0][1] - samples[1][1] < 2**31:
                    raise ValueError("温度行的顺序采样时间倒置")
        except (ValueError, OverflowError) as exc:
            return [], [self._event("malformed_line", str(exc) + ": " + text[:500], host_time_utc, host_elapsed_s)]
        rows, events = [], []
        for channel, mcu_time, adc, voltage in samples:
            elapsed, marker, messages = self._clock(channel, mcu_time, host_time_utc, host_elapsed_s)
            events.extend(messages)
            self.sample_index += 1
            rows.append({"sample_index": self.sample_index, "line_number": self.line_number, "channel": channel,
                         "mcu_time_ms": mcu_time, "mcu_elapsed_s": elapsed, "host_time_utc": host_time_utc,
                         "host_elapsed_s": host_elapsed_s, "adc": adc, "voltage_v": voltage,
                         "time_segment": self.segment, "event": marker})
        return rows, events

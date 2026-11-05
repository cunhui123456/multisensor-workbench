"""Pressure v2 selection handshake. Only the owning worker calls write()."""
import re
import secrets
import time

CHANNELS = tuple(f"S{i}" for i in range(1, 6))
BANNER = "# Pressure_Diagnostic v2"


def open_transport(factory, port, baudrate, protocol):
    from .serial_io import PosixSerialReader
    if protocol == "pressure_v2" and factory is PosixSerialReader:
        return factory(port, baudrate, writable=True)
    return factory(port, baudrate)


class PressureControl:
    def __init__(self, channel, emit, *, token=None):
        if channel not in CHANNELS:
            raise ValueError("请选择压力硬件通道 S1–S5")
        self.channel = channel
        self.token = token or secrets.randbelow(2**32 - 1) + 1
        self.used_tokens = {self.token}
        self.emit = emit
        self.confirmed = False
        self.ever_confirmed = False
        self.history = []
        self.sent = False
        self.pending = None
        self.pending_since = None
        self.command_acked = False
        self.last_send = None
        self.last_row = {}
        self.error = ""
        self.ack = None

    def snapshot(self):
        return {"requested_channel": self.channel, "confirmed_channel": self.channel if self.confirmed else None,
                "selection_token": self.token, "pending": self.pending or (None if self.confirmed else "select"),
                "error": self.error, "selection_ack": self.ack, "selection_history": list(self.history),
                **{k: self.last_row.get(k) for k in ("baseline_v", "delta_voltage_v", "zero_id", "zero_count", "baseline_ms")}}

    def _write(self, transport, command):
        data = (command + "\n").encode("ascii")
        self.emit({"kind": "pressure_command_requested", "text": command})
        transport.write(data)
        self.emit({"kind": "pressure_command_sent", "text": command})

    def select(self, transport):
        if self.sent:
            # Retrying the same token could accept an earlier ACK while another
            # SELECT is still queued (and clears the baseline a second time).
            token = self.token
            while token in self.used_tokens:
                token = secrets.randbelow(2**32 - 1) + 1
            self.token = token
            self.used_tokens.add(token)
        self._write(transport, f"SELECT {self.channel} {self.token}")
        self.sent = True
        self.last_send = time.monotonic()

    def select_channel(self, transport, channel):
        if channel not in CHANNELS or not self.confirmed or self.pending:
            raise ValueError("请选择 S1–S5，并等待当前操作完成")
        self.channel = channel
        self.error = ""
        self.confirmed = False
        self.pending = "select"
        self.pending_since = time.monotonic()
        self.last_row = {}
        self.select(transport)

    def command(self, transport, operation):
        if operation not in ("zero", "clear") or not self.confirmed or self.pending:
            raise ValueError("压力通道尚未确认或有操作进行中")
        self.error = ""
        self.pending = operation
        self.pending_since = time.monotonic()
        self.command_acked = False
        self._write(transport, f"{'Z' if operation == 'zero' else 'C'} {self.token}")

    def tick(self, transport):
        if self.sent and not self.confirmed and time.monotonic() - self.last_send > 1.0:
            self.select(transport)
        if self.pending and time.monotonic() - self.pending_since > 10:
            self.fail("压力选通／置零／清除未确认，已停止采集")

    def fail(self, message):
        self.error = message
        raise RuntimeError(message)

    def consume(self, rows, events, transport):
        accepted = []
        # Events/rows from a single read must be handled in wire order: no old
        # frames may become valid because an ACK appears later in the same chunk.
        ordered = [(v["line_number"], 0, v) for v in events] + [(v["line_number"], 1, v) for v in rows]
        for _, row_kind, item in sorted(ordered, key=lambda v: (v[0], v[1])):
            if not row_kind:
                kind, text = item["kind"], item["text"]
                if kind in ("firmware_error", "mcu_reset", "malformed_line", "invalid_encoding", "line_too_long", "interrupted_line"):
                    if self.confirmed or kind == "firmware_error" or (self.ever_confirmed and kind == "mcu_reset"):
                        self.fail("压力数据／通道状态失效：" + text)
                if text.startswith(BANNER + ";") or text == BANNER:
                    if self.ever_confirmed:
                        self.fail("压力单片机已复位，需重新验证通道")
                    self.select(transport)
                if text.startswith("# SELECT "):
                    match = re.fullmatch(r"# SELECT token=(\d+) channel=(S[1-5]) mask=0x([0-9a-fA-F]{2}) adc=A2 verified=1", text)
                    if not match:
                        self.fail("选通确认格式无效")
                    token, channel, mask = int(match[1]), match[2], int(match[3], 16)
                    if token != self.token:
                        if self.confirmed:
                            self.fail("压力通道被其他命令改变")
                        continue
                    if channel != self.channel or mask != 1 << (int(channel[1]) - 1):
                        self.fail("单片机确认的压力通道与请求不一致")
                    if self.confirmed:
                        self.fail("采集中再次选通会改变基线，请重新开始记录")
                    self.confirmed = True
                    self.ever_confirmed = True
                    if self.pending == "select":
                        self.pending = None
                        self.pending_since = None
                    self.ack = text
                    self.history.append({"channel": channel, "selection_token": token,
                                         **{k: item[k] for k in ("line_number", "host_time_utc", "host_elapsed_s")}})
                if text.startswith("# COMMAND rejected="):
                    self.fail("压力控制命令被拒绝：" + text)
                if text.startswith("# COMMAND token="):
                    if not self.confirmed:
                        # An old connection can leave a late zero/clear ACK ahead
                        # of the new selection. It has no authority before ACK.
                        continue
                    fields = dict(re.findall(r"(\w+)=([^ ]+)", text))
                    if int(fields.get("token", "0")) != self.token or fields.get("channel") != self.channel:
                        self.fail("压力控制确认的会话／通道不一致")
                    if not self.pending or fields.get("op") != self.pending or fields.get("accepted") != "1":
                        self.fail("压力控制命令被拒绝或收到非预期操作")
                    self.command_acked = True
                continue
            if not self.sent:
                self.select(transport)
            if not self.confirmed:
                continue
            if item["channel"] != self.channel or item["selection_token"] != self.token:
                self.fail("实收压力样本的通道／会话令牌与选通确认不一致")
            if item.get("event") in ("mcu_reset", "duplicate_mcu_time"):
                self.fail("压力 MCU 时序异常")
            if self.pending and self.command_acked:
                done = (self.pending == "zero" and item["zero_count"] == 40 and item["baseline_v"] != "") or (self.pending == "clear" and item["zero_id"] == 0)
                if done:
                    self.pending = None
                    self.pending_since = None
            self.last_row = item
            accepted.append(item)
        return accepted

# 当前 SSPP 实验软件模块；源自已验证实现，老师原件保留在 historical/teacher_20260908/。
"""Local runner for the teacher's unmodified SSPP sweep modules.

plan: validate configuration without opening hardware.
probe: initialize the explicitly selected device, but do not stream.
acquire: sweep until configured rounds finish or the operator stops; retain point/transport safeguards.
"""
from __future__ import annotations
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from queue import Queue, Empty, Full
import signal
import sys
import threading
import time
import traceback
from sspp_lab.realtime import RealtimePublisher
from sspp_lab.power import IdleSleepGuard

ROOT = Path(__file__).resolve().parents[2]
ORIGINALS = tuple('historical/teacher_20260908/'+n for n in ('main.py', 'uhd_lib.py', 'thread_works.py', 'do_sweep.py', 'on_time_draw.py'))


# 对文件原始字节做 SHA-256；用于 FPGA 校验和实验溯源，不使用文件修改时间判断版本。
def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# 为 JSON 转换 NumPy 数组/标量和 Path；其他对象显式失败，避免含糊序列化。
def plain(value):
    import numpy as np
    if isinstance(value, np.ndarray): return value.tolist()
    if isinstance(value, np.generic): return value.item()
    if isinstance(value, Path): return str(value)
    raise TypeError(type(value).__name__)


# 写可复读的 UTF-8 JSON；allow_nan=False 防止把非有限数值当正常数据保存。
def dump(path, data):
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False, default=plain, allow_nan=False)+'\n')


# 解析运行模式与实验参数；默认值只定义配置，不会在解析时发射。
def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['plan', 'probe', 'acquire'])
    p.add_argument('--device-config', type=Path, default=ROOT/'local_device.json')
    p.add_argument('--label', default='empty_baseline')
    p.add_argument('--headless', action='store_true')
    p.add_argument('--output-root', type=Path, default=ROOT/'Data'/'runs')
    p.add_argument('--repeat', type=int, default=3)
    p.add_argument('--max-seconds', type=float, default=None, help='Deprecated compatibility option; ignored. Sweeps have no total time limit.')
    p.add_argument('--point-timeout', type=float, default=8)
    p.add_argument('--tx-gain', type=float, default=20)
    p.add_argument('--rx-gain', type=float, default=30)
    p.add_argument('--freq-start', type=float, default=2.4e9)
    p.add_argument('--freq-stop', type=float, default=3.0e9)
    p.add_argument('--points', type=int, default=20)
    p.add_argument('--rate', type=float, default=250000., help='Requested complex sample rate, S/s')
    p.add_argument('--amplitude', type=float, default=.3, help='Digital TX amplitude')
    p.add_argument('--tone-start', type=float, default=2000., help='First baseband tone, Hz')
    p.add_argument('--tone-step', type=float, default=1000., help='Tone increment per carrier point, Hz')
    p.add_argument('--temperature-config', help='Optional JSON configuration for joint temperature-voltage capture')
    opts=p.parse_args()
    if opts.max_seconds is not None:
        print('Deprecated --max-seconds ignored: sweep has no total time limit.', flush=True)
    opts.max_seconds = None
    return opts


# 核对设备字段、镜像哈希和范围，构造 SDR/Sweep 参数对象；不实例化 MultiUSRP。
def make_config(opts):
    from sspp_lab.hardware import SDRConfig
    from sspp_lab.sweep_logic import SweepConfig
    import numpy as np
    # 设备配置含本地序列号；不要把原配置或含标识的 manifest 直接公开分享。
    device = json.loads(opts.device_config.read_text())
    for key in ('serial', 'fpga', 'fpga_sha256', 'channel'):
        if key not in device: raise ValueError(f'Missing local device field: {key}')
    image = Path(device['fpga']).expanduser().resolve()
    if sha(image) != device['fpga_sha256']: raise ValueError('FPGA SHA-256 does not match the reviewed device profile')
    if ',' in str(image) or ',' in device['serial']: raise ValueError('Invalid device address component')
    if device['channel'] not in (0, 1): raise ValueError('B210 channel must be 0 or 1')
    if not (1 <= opts.repeat <= 100 and 2 <= opts.points <= 1000): raise ValueError('Invalid repeat/point count')
    if not (1 <= opts.point_timeout <= 30): raise ValueError('Point timeout must be within 1–30 seconds')
    if not (70e6 <= opts.freq_start < opts.freq_stop <= 6e9): raise ValueError('Sweep range must be within 70 MHz–6 GHz')
    if not all(np.isfinite(v) for v in [opts.tx_gain, opts.rx_gain, opts.freq_start, opts.freq_stop, opts.point_timeout]): raise ValueError('Parameters must be finite')
    if not (0 <= opts.tx_gain <= 30 and 0 <= opts.rx_gain <= 76): raise ValueError('Gain outside local runner bounds')
    if not (200000 <= opts.rate <= 2000000 and 0 < opts.amplitude <= .3): raise ValueError('Rate/amplitude outside current experiment bounds')
    if not (0 < opts.tone_start and 0 <= opts.tone_step): raise ValueError('Invalid baseband tone settings')
    config = SDRConfig(device=f'type=b200,serial={device["serial"]},fpga={image}',
                       channel=device['channel'], freq=opts.freq_start,
                       tx_gain=opts.tx_gain, rx_gain=opts.rx_gain, rate=opts.rate, amp=opts.amplitude)
    config.validate()
    sweep = SweepConfig(freq_start=opts.freq_start, freq_stop=opts.freq_stop,
                        num_points=opts.points, sweep_repeat=opts.repeat,
                        tone_start=opts.tone_start, tone_step=opts.tone_step, skip_invalid_points=True)
    sweep.validate(config.rate)
    return device, config, sweep


# 沿全局已发送样点编号合成单音，处理部分发送；停止/异常后也尝试发送 EOB。
def tx_loop(stream, args, stop, telemetry, failures):
    import uhd
    import numpy as np
    md = uhd.types.TXMetadata()
    md.start_of_burst, md.end_of_burst, md.has_time_spec = True, False, False
    index = 0
    telemetry['tx_end_of_burst_sent'] = False
    try:
        while not stop.is_set():
            fs = args.tx_rate_actual or args.rate
            n = np.arange(index, index+10000, dtype=np.float64)
            buff = (args.amp*np.exp(2j*np.pi*args.tone*n/fs)).astype(np.complex64)
            sent = 0
            # send() 可能返回短块，循环只补发尚未发送的切片，避免遗漏或重复样本。
            while sent < len(buff) and not stop.is_set():
                count = stream.send(buff[sent:], md, timeout=0.5)
                if count <= 0: raise RuntimeError('TX send returned zero samples')
                if count < len(buff)-sent: telemetry['tx_partial_sends'] += 1
                sent += count
                # 立即记账，后续 send 超时也保留已成功提交的部分样本。
                telemetry['tx_samples'] = index + sent
                md.start_of_burst = False
            index += sent
    except Exception as exc:
        failures.append(f'TX: {exc}')
        stop.set()
    finally:
        # 无论异常还是正常退出，tx_loop 的 finally 都尝试发送结束标记 EOB。
        md.start_of_burst, md.end_of_burst = False, True
        try:
            count = stream.send(np.zeros(1, dtype=np.complex64), md, timeout=0.5)
            if count != 1:
                raise RuntimeError(f'TX EOB send accepted {count} of 1 sample; stop unconfirmed')
            telemetry['tx_end_of_burst_sent'] = True
        except Exception as exc:
            failures.append(f'TX shutdown: {exc}')


def report_stop_status(telemetry, failures):
    """Report shutdown evidence without equating process exit with RF stopped."""
    confirmed = all(telemetry.get(key) is True for key in
                    ('tx_end_of_burst_sent', 'rx_stop_command_sent', 'workers_stopped'))
    telemetry['rf_stopped_confirmed'] = confirmed
    tx_stalled = any('TX send returned zero samples' in str(error) for error in failures)
    control_stalled = any('wait_for_ack' in str(error) or 'accum_timeout' in str(error)
                          for error in failures)
    diagnosis = None
    if tx_stalled and control_stalled:
        diagnosis = 'usrp_transport_control_unresponsive'
        telemetry['failure_diagnosis'] = diagnosis
        message = ('USRP 数据发送和设备控制通信均无响应；检查 USB 连接、集线器与供电，'
                   '重新连接 B210 后重新初始化。具体物理原因尚未确定；这不是弱信号质量超时。')
        if message not in failures:
            failures.append(message)
    event = {'event': 'STOP_STATUS', 'rf_stopped_confirmed': confirmed,
             'tx_end_of_burst_sent': telemetry.get('tx_end_of_burst_sent', False),
             'rx_stop_command_sent': telemetry.get('rx_stop_command_sent', False),
             'workers_stopped': telemetry.get('workers_stopped', False),
             'failure_diagnosis': diagnosis,
             'rf_start_attempted': telemetry.get('rf_start_attempted')}
    print(json.dumps(event, ensure_ascii=False), flush=True)
    if telemetry.get('rf_start_attempted') is False:
        print('RF streams were not started; saving diagnostic results.', flush=True)
    else:
        print('RX/TX stopped; saving results.' if confirmed else
              'RX/TX stop unconfirmed; saving partial results.', flush=True)
    return event


# 复制每帧有效 IQ 并赋连续软件索引；队列满即失败，不丢帧拼接出假连续数据。
def rx_loop(stream, args, stop, queue, telemetry, failures):
    import uhd
    import numpy as np
    md = uhd.types.RXMetadata()
    buff = np.zeros((1, args.nsamps), dtype=np.complex64)
    index = 0
    while not stop.is_set():
        try:
            count = stream.recv(buff, md, timeout=0.5)
            if stop.is_set(): break
            if md.error_code == uhd.types.RXMetadataErrorCode.timeout:
                telemetry['rx_timeouts'] += 1
                continue
            if md.error_code != uhd.types.RXMetadataErrorCode.none:
                raise RuntimeError(md.strerror())
            if count <= 0: continue
            values = buff[0, :count].copy()
            peak = float(max(np.max(np.abs(values.real)), np.max(np.abs(values.imag))))
            telemetry['rx_peak_component'] = max(telemetry['rx_peak_component'], peak)
            telemetry['rx_near_fullscale_components'] += int(np.count_nonzero(np.abs(values.real)>=.98)+np.count_nonzero(np.abs(values.imag)>=.98))
            # 这里不使用老师丢旧帧策略；Full 会进入失败处理，保护点内帧序列。
            queue.put_nowait((index, values))
            index += count
            telemetry['rx_samples'] = index
        except Full:
            failures.append('RX queue overflow; aborting instead of stitching discontinuous IQ')
            stop.set()
        except Exception as exc:
            failures.append(f'RX: {exc}')
            stop.set()


# 扫频子类记录真实 IQ、每次质量判定窗和最终合格测量，不更改历史老师归档。
def make_runner(args, cfg, manager, queue, stop, drawer, output, opts, failures, publisher=None, companion=None):
    import numpy as np
    from sspp_lab.sweep_logic import SweepRunner, PointQualityTimeout
    # 每个频点保存控制器实际取出的全部帧，包括 VERIFY 和 MEASURE 的输入。
    class RecordedSweep(SweepRunner):
        def on_round_started(self, repeat):
            if companion:companion.begin_round(repeat)

        def on_round_finished(self, repeat, complete):
            if companion:
                invalid=sum(q.get('measurement_status')=='invalid' for q in self.partial_result['all_quality_records'][-1])
                companion.finish_round(repeat,complete,invalid_point_count=invalid)

        def invalid_quality_details(self):
            return {'raw_iq_file':f'iq_point_{self.point_number:03d}.npz', 'last_attempt':self.last_quality_record}

        def on_invalid_measurement(self, sweep_idx, point_idx, freq, tone, quality):
            self.point_info['measurement_status']='invalid'
            self.point_info['reason_code']=quality['reason_code']
            temperature=(companion.record_point(repeat=sweep_idx+1,point=point_idx+1,carrier_hz=freq,tone_hz=tone,
                         amplitude_dB=None,point_started_monotonic_s=self.point_started_monotonic_s,
                         measurement_status='invalid',invalid_reason=quality['reason']) if companion else None)
            record={'event':'MEASUREMENT','repeat':sweep_idx+1,'point':point_idx+1,'carrier_hz':float(freq),
                    'tone_hz':float(tone),'amplitude_dB':None,'measured_tone_hz':None,
                    'measurement_status':'invalid','sweep_quality':quality,
                    **({'temperature_sweep':temperature} if companion else {})}
            with (output/'sweep_invalid_points.jsonl').open('a',encoding='utf-8') as handle:
                handle.write(json.dumps(record,ensure_ascii=False,allow_nan=False)+'\n')
            print(json.dumps(record,ensure_ascii=False,allow_nan=False),flush=True)

        def on_measurement(self, sweep_idx, point_idx, freq, tone, measured, amplitude):
            # 结构化事件供网页读取；输出不改变测量计算或原始 IQ 留存。
            temperature = (companion.record_point(repeat=sweep_idx+1, point=point_idx+1,
                           carrier_hz=freq, tone_hz=tone, amplitude_dB=amplitude,
                           point_started_monotonic_s=self.point_started_monotonic_s,
                           measurement_quality=self.last_measurement_quality)
                           if companion else None)
            print(json.dumps({'event':'MEASUREMENT','repeat':sweep_idx+1,'point':point_idx+1,
                              'carrier_hz':float(freq),'tone_hz':float(tone),
                              'measured_tone_hz':float(measured),'amplitude_dB':float(amplitude),
                              'sweep_quality':self.last_measurement_quality,
                              **({'temperature_sweep':temperature} if companion else {})},allow_nan=False),flush=True)
        # 维护分点缓存、下一帧索引、截止时间和原始文件清单。
        def __init__(self):
            super().__init__(args, cfg, manager, queue, stop, drawer)
            self.frames = []
            self.indices = []
            self.point_number = 0
            self.point_info = None
            self.expected_index = None
            self.deadline = 0
            self.raw_files = []
            self.point_started_monotonic_s = None
            self.quality_attempt_count = 0
            self.rejected_measurement_count = 0
            self.last_quality_rejection = None
            self.last_quality_record = None
            self.point_attempts = 0
            self.last_frame_at = time.monotonic()
        def on_quality_attempt(self, record):
            record['raw_iq_file'] = f'iq_point_{self.point_number:03d}.npz'
            record['rx_software_sample_start'] = int(self.indices[0] + record['window_start']) if self.indices else None
            self.quality_attempt_count += 1
            self.point_attempts += 1
            self.last_quality_record = dict(record)
            if not record['passed']:
                self.last_quality_rejection = record['rejection_reasons']
                if record['state'] == 'MEASURE': self.rejected_measurement_count += 1
            with (output/'sweep_quality.jsonl').open('a',encoding='utf-8') as handle:
                handle.write(json.dumps(record,ensure_ascii=False,allow_nan=False)+'\n')
        def point_deadline_expired(self):
            return time.monotonic() > self.deadline
        # 将前一频点缓存压缩保存为 NPZ 并登记哈希；这不是全会话连续 IQ。
        def flush_point(self):
            if self.point_info is not None and self.frames:
                target = output/f'iq_point_{self.point_number:03d}.npz'
                np.savez_compressed(target, iq=np.concatenate(self.frames),
                    frame_start_indices=np.asarray(self.indices, dtype=np.int64),
                    frame_lengths=np.asarray([len(x) for x in self.frames]),
                    rx_rate=args.rx_rate_actual,
                    point_info=json.dumps(self.point_info))
                self.raw_files.append({'file':target.name,'sha256':sha(target), **self.point_info})
            self.frames, self.indices = [], []
        # 先保存旧点，再调用父类切频；每个新点单独重设连续性基准与超时。
        def _set_freq_and_tone(self, freq, tone):
            self.flush_point()
            super()._set_freq_and_tone(freq, tone)
            self.point_started_monotonic_s = time.monotonic()
            self.point_number += 1
            self.expected_index = None
            self.deadline = time.monotonic()+opts.point_timeout
            self.point_info = {'point_number':self.point_number,'requested_carrier_hz':float(freq),
                              'tone_hz':float(tone),'tx_actual_carrier_hz':float(manager.usrp.get_tx_freq(args.channel)),
                              'rx_actual_carrier_hz':float(manager.usrp.get_rx_freq(args.channel))}
            self.last_quality_rejection = None
            self.last_quality_record = None
            self.point_attempts = 0
            self.last_frame_at = time.monotonic()
        # 检查同频点帧索引必须紧接；软件索引检查不替代硬件时间戳核验。
        def _fetch_available_frames(self):
            if failures: raise RuntimeError(failures[-1])
            frames = []
            while True:
                try: index, values = queue.get_nowait()
                except Empty: break
                if self.expected_index is not None and index != self.expected_index:
                    raise RuntimeError('Discontinuous queued IQ within a frequency point')
                # 跨频点清空队列是有意跳过过渡段；连续性判定只在当前频点内有效。
                self.expected_index = index+len(values)
                frames.append(values)
                self.frames.append(values)
                self.indices.append(index)
            if frames:self.last_frame_at=time.monotonic()
            if time.monotonic()-self.last_frame_at>3:
                raise RuntimeError('RX data stalled for more than 3 seconds; not treated as a weak-signal point')
            if time.monotonic()>self.deadline:
                if not self.point_attempts:raise RuntimeError('Point deadline reached without sufficient real IQ for validation')
                raise PointQualityTimeout(f'Point {self.point_number}: no quality-approved measurement within {opts.point_timeout}s; last rejection={self.last_quality_rejection}')
            if frames and publisher is not None:
                # Preview the current point's real frame before NPZ persistence.
                # Software sample indexes are not hardware continuity evidence.
                start = self.indices[-1]
                publisher.preview(frames[-1], rate=args.rx_rate_actual,
                                  carrier_hz=self.point_info['rx_actual_carrier_hz'],
                                  tone_hz=self.point_info['tone_hz'],
                                  t_s=(start+(len(frames[-1])-1)/2)/args.rx_rate_actual,
                                  sample_start=start, mode='sweep')
            return frames, frames[-1] if frames else None
    return RecordedSweep()


# 导出逐点精确表、按载波分组的 dB 均值/样本 SD 和停止后的曲线。
def save_outputs(result, output, cfg):
    import numpy as np
    import pandas as pd
    import matplotlib.pyplot as plt
    rows = []
    for k, amps in enumerate(result['all_amp_records']):
        for i, amp in enumerate(amps):
            accepted = result.get('all_quality_records', [])
            quality = accepted[k][i] if k < len(accepted) and i < len(accepted[k]) else {}
            rows.append({'repeat':k+1, 'point':i+1,
                'measurement_status':quality.get('measurement_status','valid'),
                'invalid_reason':quality.get('reason',''),
                'manual_temperature_c':result.get('manual_temperature_c'),
                'requested_carrier_GHz':float(result['all_freq_records'][k][i]),
                'expected_tone_Hz':float(result['all_expected_tone_records'][k][i]),
                'measured_tone_Hz':float(result['all_measured_tone_records'][k][i]) if result['all_measured_tone_records'][k][i] is not None else None,
                'peak_amplitude_dBFS_est':float(amp) if amp is not None else None,
                'algorithm_version':result.get('algorithm_version', 'legacy_fft_peak'),
                'amplitude_estimator':quality.get('amplitude_estimator'),
                'amplitude_frequency_Hz':quality.get('amplitude_frequency_hz'),
                'accepted_window_start':quality.get('window_start'),
                'accepted_window_count':quality.get('window_count'),
                'accepted_window_duration_s':quality.get('window_duration_s'),
                'adaptive_level_index':quality.get('adaptive_level_index'),
                'accepted_raw_iq_file':quality.get('raw_iq_file'),
                'quality_attempt_index':quality.get('quality_attempt_index'),
                'target_snr_db':quality.get('target_snr_db'),
                'previous_tone_margin_db':quality.get('previous_tone_margin_db'),
                'previous_tone_present':quality.get('previous_tone_present'),
                'previous_tone_snr_db':quality.get('previous_tone_snr_db'),
                'previous_tone_gate_status':quality.get('previous_tone_gate_status')})
    df = pd.DataFrame(rows)
    if not df.empty:
        for column in ("measured_tone_Hz","peak_amplitude_dBFS_est"):
            df[column]=pd.to_numeric(df[column],errors="coerce")
    if df.empty: return {'counts':[], 'complete':False, 'measurement_tone_failures':0}
    df.to_csv(output/'measurements.csv',index=False,float_format='%.12g')
    # dB 域均值/SD 保留相对比较含义，不等价于先平均线性功率再取对数。
    summary = df.groupby('requested_carrier_GHz',sort=True)['peak_amplitude_dBFS_est'].agg(['count','mean','std']).reset_index()
    with pd.ExcelWriter(output/'sweep_summary.xlsx',engine='openpyxl') as writer:
        df.to_excel(writer,index=False,sheet_name='Measurements')
        summary.to_excel(writer,index=False,sheet_name='Across_states_not_repeats' if cfg.continuous else 'Mean_SD')
        pd.DataFrame([asdict(cfg)]).to_excel(writer,index=False,sheet_name='Sweep_Config')
        for sheet in writer.sheets.values():
            for col in sheet.columns:
                sheet.column_dimensions[col[0].column_letter].width=min(48,max(len(str(c.value or '')) for c in col)+2)
    fig, ax = plt.subplots(figsize=(9,5),layout='constrained')
    plot_df=df[df['repeat'].isin(sorted(df['repeat'].unique())[-32:])] if cfg.continuous else df
    for repeat, part in plot_df.groupby('repeat'):
        ax.plot(part.requested_carrier_GHz,part.peak_amplitude_dBFS_est,'o-',label=f'Run {repeat}')
    ax.set(xlabel='Requested carrier frequency (GHz)',ylabel='Expected-tone coherent amplitude (dBFS estimate)',title='SSPP empty-baseline sweep' if cfg.save_filename=='empty_baseline' else f'SSPP sweep: {cfg.save_filename}')
    ax.grid(alpha=.3);ax.legend()
    fig.savefig(output/'sweep_summary.png',dpi=180)
    fig.savefig(output/'sweep_summary.pdf')
    plt.close(fig)
    counts = [len(v) for v in result['all_amp_records']]
    valid_counts=[sum(v is not None for v in values) for values in result['all_amp_records']]
    invalid_count=sum(counts)-sum(valid_counts)
    df[df['measurement_status']=='invalid'].to_csv(output/'invalid_points.csv',index=False)
    scan_completed=any(n==cfg.num_points for n in counts) if cfg.continuous else len(counts)==cfg.sweep_repeat and all(n==cfg.num_points for n in counts)
    errs = int(np.count_nonzero(np.abs(df.measured_tone_Hz-df.expected_tone_Hz)>=cfg.verify_tolerance_hz))
    return {'counts':counts,'valid_counts':valid_counts,'invalid_count':invalid_count,'scan_completed':scan_completed,
            'complete':scan_completed and invalid_count==0,
            'measurement_tone_failures':errs,'amplitude_range_dBFS_est':[float(df.peak_amplitude_dBFS_est.min()),float(df.peak_amplitude_dBFS_est.max())] if df.peak_amplitude_dBFS_est.notna().any() else None,
            'max_repeat_sd_dB':float(summary['std'].max()) if not cfg.continuous and summary['std'].notna().any() else None}


# 根据 plan/probe/acquire 分支执行；采集结果导出位于停止硬件之后。
def main():
    opts = parse_args()
    temperature_config = None
    if getattr(opts, 'temperature_config', None):
        from sspp_experiments.temperature_sweep.metadata import validate_config
        temperature_config = validate_config(json.loads(opts.temperature_config))
    publisher = RealtimePublisher()
    os.environ.setdefault('MPLCONFIGDIR', str(ROOT/'.cache'/'matplotlib'))
    import matplotlib
    if opts.headless: matplotlib.use('Agg')
    from sspp_lab.hardware import USRPManager
    from sspp_lab.display import OnTimeDraw
    from sspp_lab.sweep_logic import sweep_algorithm_policy
    device, args, cfg = make_config(opts)
    cfg.continuous = bool(temperature_config and temperature_config['mode']=='continuous')
    print(f'Profile: {opts.freq_start/1e9:g}-{opts.freq_stop/1e9:g} GHz; {opts.points} points x {opts.repeat}; channel {args.channel}; TX {args.tx_gain:g} dB / RX {args.rx_gain:g} dB', flush=True)
    print('Sweep has no total time limit; '+('operator stop ends continuous acquisition.' if cfg.continuous else 'configured rounds determine completion.'), flush=True)
    if opts.command == 'plan':
        print('Configuration and FPGA hash validated; hardware unopened.');return 0
    manager = USRPManager(args)
    if opts.command == 'probe':
        manager.configure();print('Configured; RX/TX streams were not started.');return 0
    label = ''.join(c if c.isalnum() or c in '_-' else '_' for c in opts.label).strip('_')
    if not label: raise ValueError('Nonempty label required')
    output = opts.output_root/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
    output.mkdir(parents=True,exist_ok=False)
    print('OUTPUT_DIR='+str(output),flush=True)
    cfg.save_dir, cfg.save_filename = str(output), label
    # 保存请求/实际参数、版本与原始文件哈希；注释修改也会改变源码哈希。
    manifest = {'status':'initializing','label':label,'started_utc':datetime.now(timezone.utc).isoformat(),
       'source_hashes':{n:sha(ROOT/n) for n in ORIGINALS},'runner_sha256':sha(__file__),
       'current_source_hashes':{n:sha(ROOT/n) for n in ['sspp_lab/hardware.py','sspp_experiments/material_sweep/logic.py','sspp_experiments/material_sweep/acquisition.py','sspp_lab/realtime.py','sspp_lab/power.py']},
       'requested_sdr':asdict(args),'sweep':asdict(cfg),'device_profile':device,
       'environment':{'python':sys.version,'executable':sys.executable},
       'measurement':'Teacher FFT peak estimator; relative digital amplitude, not calibrated S21/dBm; no KNN',
       'raw_scope':'Frames fetched after per-point settle/queue clear, including verification and measurement; not a continuous all-session hardware-time recording'}
    manifest['time_policy'] = {'total_time_limit_seconds': None,
        'completion': 'operator_stop' if cfg.continuous else 'configured_rounds',
        'point_timeout_seconds': opts.point_timeout}
    manifest['sweep_algorithm'] = sweep_algorithm_policy(cfg)
    manifest['measurement'] = 'Quality-gated exact expected-tone Hann coefficient; measured_tone_Hz is target-band peak diagnostic; peak_amplitude_dBFS_est is retained as a legacy column name; relative digital amplitude, not calibrated S21/dBm'
    if temperature_config:
        manifest['experiment'] = 'temperature_sweep'
        manifest['temperature_sweep'] = {'config': temperature_config, 'status': 'initializing'}
        manifest['current_source_hashes'].update({str(path.relative_to(ROOT)): sha(path)
            for path in (ROOT/'sspp_experiments'/'temperature_sweep').glob('*.py')})
        manifest['current_source_hashes'].update({str(path.relative_to(ROOT)): sha(path)
            for path in (ROOT/'sspp_experiments'/'temperature_pressure').glob('*.py')})
    dump(output/'manifest.json',manifest)
    stop = threading.Event();failures=[]
    telemetry={'tx_samples':0,'rx_samples':0,'tx_partial_sends':0,'rx_timeouts':0,
               'rx_peak_component':0.0,'rx_near_fullscale_components':0,'tx_end_of_burst_sent':False,
               'rf_start_attempted':False,'rx_stop_command_sent':False,'workers_stopped':False}
    # 将信号记录为失败原因并请求线程停止；信号回调不直接处理硬件。
    def interrupt(signum, frame):
        if cfg.continuous:
            telemetry['stop_reason']='operator_stop'
        else:
            failures.append(f'Interrupted by signal {signum}')
        stop.set()
    signal.signal(signal.SIGINT,interrupt);signal.signal(signal.SIGTERM,interrupt)
    # 无扫频总时限；保留每点截止、传输故障、操作者停止与父进程初始化保护。
    threads=[];runner=None;drawer=None;result=None;streaming=False;companion=None
    wake_guard = IdleSleepGuard()
    publisher.start()
    try:
        # 先于串口/USRP初始化建立进程级防空闲睡眠保护；无整场时限。
        wake_guard.start()
        manifest['idle_sleep_guard'] = wake_guard.snapshot()
        print(json.dumps({'event': 'POWER_STATUS', 'operation': 'started',
                          **manifest['idle_sleep_guard']}, ensure_ascii=False), flush=True)
        if manifest['idle_sleep_guard']['active']:
            print('采集期间防止系统空闲睡眠；停止后自动释放，显示器仍可休眠。', flush=True)
        dump(output/'manifest.json', manifest)
        if temperature_config and temperature_config["mode"] != "manual":
            from sspp_experiments.temperature_sweep.acquisition import TemperatureSweepCapture
            companion = TemperatureSweepCapture(output, temperature_config, stop, failures)
            companion.start()
        if stop.is_set(): raise RuntimeError('Stopped before RF initialization')
        manager.configure()
        if stop.is_set(): raise RuntimeError('Stopped during RF initialization; streams not started')
        print(json.dumps({'event':'HARDWARE_READY'},ensure_ascii=False),flush=True)
        manifest['actual_sdr']=asdict(args)
        manifest['actual_hardware']={'master_clock_rate':manager.usrp.get_master_clock_rate(),
            'tx_gain':manager.usrp.get_tx_gain(args.channel),'rx_gain':manager.usrp.get_rx_gain(args.channel),
            'tx_antenna':manager.usrp.get_tx_antenna(args.channel),'rx_antenna':manager.usrp.get_rx_antenna(args.channel)}
        manifest['tx_frontend']=manager.usrp.get_tx_subdev_name(args.channel)
        manifest['rx_frontend']=manager.usrp.get_rx_subdev_name(args.channel)
        tx,rx=manager.create_streamers()
        # 有界队列隔离接收与处理速率；满队列显式报错，不无限占用内存。
        queue=Queue(maxsize=256)
        rt=threading.Thread(target=rx_loop,args=(rx,args,stop,queue,telemetry,failures),daemon=True)
        tt=threading.Thread(target=tx_loop,args=(tx,args,stop,telemetry,failures),daemon=True)
        if companion: companion.ensure_fresh()
        if stop.is_set(): raise RuntimeError('Stopped before starting RF streams')
        telemetry['rf_start_attempted']=True
        manager.start_rx_stream();streaming=True;rt.start();threads.append(rt)
        try: queue.get(timeout=8)
        except Empty: raise RuntimeError('No first RX frame; TX not started')
        if failures: raise RuntimeError(failures[-1])
        tt.start();threads.append(tt);time.sleep(.3)
        print(json.dumps({'event':'ACQUIRING'},ensure_ascii=False),flush=True)
        drawer=None if opts.headless else OnTimeDraw(args)
        runner=make_runner(args,cfg,manager,queue,stop,drawer,output,opts,failures,publisher,companion)
        manifest['status']='running';dump(output/'manifest.json',manifest)
        result=runner.run(enable_plot=not opts.headless)
    except Exception as exc:
        failures.append(f'{type(exc).__name__}: {exc}')
        traceback.print_exc(file=sys.stdout)
    finally:
        stop.set()
        if streaming:
            try: manager.stop_rx_stream();telemetry['rx_stop_command_sent']=True
            except Exception as exc: failures.append(f'RX shutdown: {exc}')
        for thread in threads: thread.join(timeout=3)
        telemetry['workers_stopped']=all(not t.is_alive() for t in threads)
        if not telemetry['workers_stopped']: failures.append('Worker did not stop in time')
        if drawer: drawer.close()
        if runner:
            try: runner.flush_point();manifest['raw_files']=runner.raw_files
            except Exception as exc: failures.append(f'Raw saving: {exc}')
            if result is None: result = getattr(runner, 'partial_result', None)
            quality_log = output/'sweep_quality.jsonl'
            if quality_log.is_file():
                manifest['sweep_quality_file'] = {'path':quality_log.name,'sha256':sha(quality_log),
                                                'bytes':quality_log.stat().st_size}
            manifest['sweep_quality_attempts'] = {'total':runner.quality_attempt_count,
                                                 'rejected_measurements':runner.rejected_measurement_count}
        if companion:
            try: manifest['temperature_sweep'] = companion.close()
            except Exception as exc:
                failures.append(f'Temperature capture shutdown: {exc}')
                manifest['temperature_sweep']['status'] = 'failed_or_incomplete'
        # RF与串口收尾后释放；-w同时保证采集进程意外退出时不会遗留保护。
        try: wake_guard.close()
        except Exception as exc: failures.append(f'Idle sleep guard shutdown: {exc}')
        manifest['idle_sleep_guard'] = wake_guard.snapshot()
        print(json.dumps({'event': 'POWER_STATUS', 'operation': 'released',
                          **manifest['idle_sleep_guard']}, ensure_ascii=False), flush=True)
        telemetry['realtime_display']=publisher.stats()
        report_stop_status(telemetry, failures)
    # 只有停止/等待线程后才导出结果；部分测量保留但不标记 complete。
    quality={'complete':False,'counts':[]}
    try:
        if result:
            if temperature_config and temperature_config['mode']=='manual':result['manual_temperature_c']=temperature_config['reference_temperature_c']
            dump(output/'sweep_result.json',result)
            quality=save_outputs(result,output,cfg)
    except Exception as exc: failures.append(f'Result saving: {exc}');traceback.print_exc(file=sys.stdout)
    if quality.get('measurement_tone_failures',0): failures.append('Measured tone outside configured tolerance')
    if companion and companion.point_count != sum(quality.get('counts', [])):
        failures.append('RF/temperature point count mismatch; inspect retained partial data')
    if temperature_config and temperature_config['mode']=='manual':
        manifest['temperature_sweep']={'config':temperature_config,'status':'complete' if quality.get('scan_completed',quality['complete']) and not failures else 'failed_or_incomplete',
                                      'temperature_source':'manual_reference','reference_temperature_c':temperature_config['reference_temperature_c']}
    if telemetry['rx_near_fullscale_components']: failures.append('Near-fullscale IQ observed; inspect clipping')
    if not sum(quality.get('valid_counts',quality.get('counts',[]))):failures.append('No quality-approved RF measurements; missing points are not valid data')
    status=('complete' if quality['complete'] else 'complete_with_gaps') if quality.get('scan_completed',quality['complete']) and not failures else 'failed_or_incomplete'
    manifest['warnings']=[f"{quality.get('invalid_count',0)} frequency points could not be measured within the unchanged point deadline; inspect invalid_points.csv"] if quality.get('invalid_count') else []
    manifest.update({'status':status,
        'ended_utc':datetime.now(timezone.utc).isoformat(),'telemetry':telemetry,'quality':quality,'errors':failures})
    dump(output/'manifest.json',manifest)
    print(json.dumps({'status':manifest['status'],'output':str(output),'quality':quality,'errors':failures},ensure_ascii=False),flush=True)
    publisher.close()
    telemetry['realtime_display']=publisher.stats()
    dump(output/'manifest.json',manifest)
    return 0 if manifest['status'] in ('complete','complete_with_gaps') else 2

if __name__=='__main__':
    raise SystemExit(main())

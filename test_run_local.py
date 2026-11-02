# 离线回归测试：用合成 IQ 和虚拟 streamer 验证 FFT、部分发送、间断与导出。
# 会导入 UHD Python 类型，但不会实例化 MultiUSRP 或启动真实设备。
"""Offline checks; never open a USRP or transmit."""
import os
os.environ.setdefault('MPLBACKEND','Agg')
import tempfile
import threading
import time
import unittest
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
import numpy as np
import pandas as pd
from sspp_lab.sweep_logic import SweepConfig, PointQualityTimeout, fft_peak
from sspp_lab.hardware import SDRConfig
import run_local

# 覆盖现有扫频算法与数据导出契约；不运行真实 RF 实验。
class RunnerTests(unittest.TestCase):
    # 遍历 2–13 kHz 单音和 20/80 周期窗，检查原估计器误差与幅值范围。
    def test_original_fft_recovers_all_tones_both_stages(self):
        for tone in range(2000,14000,1000):
            for cycles in (20,80):
                n=np.arange(int(cycles*250000/tone))
                iq=.1*np.exp(2j*np.pi*tone*n/250000)
                peak,amp=fft_peak(iq,250000,expected_freq=tone)
                self.assertLess(abs(peak-tone),200)
                self.assertTrue(-22<amp<-19.9)

    # 虚拟 streamer 每次最多接收 3000 点，迫使线程分批补发。
    def test_partial_tx_and_end_of_burst(self):
        stop=threading.Event()
        # 用软件模拟短发送和 EOB，避免测试依赖 B210。
        class Stream:
            total=0
            ended=False
            # 累计有效发送样本，达到一块长度后停止并记录结束标记。
            def send(self, data, md, timeout):
                if md.end_of_burst:
                    self.ended=True
                    return len(data)
                n=min(3000,len(data))
                self.total+=n
                if self.total==10000: stop.set()
                return n
        stream=Stream()
        telemetry={'tx_partial_sends':0}
        failures=[]
        run_local.tx_loop(stream,SDRConfig(),stop,telemetry,failures)
        self.assertEqual(stream.total,10000)
        self.assertEqual(telemetry['tx_partial_sends'],3)
        self.assertTrue(stream.ended)
        self.assertEqual(failures,[])

    # 构造缺失一采样点和过期截止时间，两种情况都必须中止。
    def test_gap_and_point_timeout_abort(self):
        args=SDRConfig(rx_rate_actual=250000)
        q=Queue()
        with tempfile.TemporaryDirectory() as folder:
            runner=run_local.make_runner(args,SweepConfig(),None,q,threading.Event(),None,Path(folder),SimpleNamespace(point_timeout=1),[])
            runner.deadline=time.monotonic()+1
            q.put((0,np.ones(4,dtype=np.complex64)))
            q.put((5,np.ones(4,dtype=np.complex64)))
            with self.assertRaisesRegex(RuntimeError,'Discontinuous'): runner._fetch_available_frames()
            runner.deadline=time.monotonic()-1
            # 没有验证窗口不能视为弱信号缺测，须中止真实采集。
            with self.assertRaisesRegex(RuntimeError, 'without sufficient real IQ'):
                runner._fetch_available_frames()
            runner.point_attempts = 1
            with self.assertRaises(PointQualityTimeout):
                runner._fetch_available_frames()

    # 合成 3×12 记录核对导出数量、GHz 精度和图文件实际生成。
    def test_export_has_36_precise_frequency_rows(self):
        cfg=SweepConfig()
        freqs=np.linspace(cfg.freq_start,cfg.freq_stop,12)
        tones=np.arange(2000,14000,1000)
        result={'freq_list':freqs,'all_freq_records':[freqs/1e9]*3,
                'all_amp_records':[np.arange(12)-50.0+i*.01 for i in range(3)],
                'all_expected_tone_records':[tones]*3,'all_measured_tone_records':[tones]*3}
        with tempfile.TemporaryDirectory() as folder:
            out=Path(folder)
            quality=run_local.save_outputs(result,out,cfg)
            self.assertTrue(quality['complete'])
            self.assertEqual(quality['counts'],[12,12,12])
            data=pd.read_excel(out/'sweep_summary.xlsx',sheet_name='Measurements')
            self.assertEqual(len(data),36)
            self.assertAlmostEqual(data.requested_carrier_GHz.iloc[1],freqs[1]/1e9,places=12)
            self.assertTrue((out/'sweep_summary.png').stat().st_size>1000)

if __name__=='__main__': unittest.main()

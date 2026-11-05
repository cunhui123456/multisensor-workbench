"""实验服务与真实采集子进程之间的边界；本模块不初始化 UHD。"""
from collections import deque, OrderedDict
from copy import deepcopy
from datetime import datetime, timezone
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
import uuid

from sspp_experiments.material_sweep.budget import budget_info

ROOT=Path(__file__).resolve().parent.parent
DEFAULTS={'mode':'sweep','condition':'empty','label':'empty_baseline','sensor':'',
          'freq_start_ghz':2.4,'freq_stop_ghz':3.0,'points':20,'repeat':3,
          'carrier_ghz':2.4,'rate':250000,'tone':10000,'tone_start':2000,'tone_step':1000,
          'amplitude':.3,'tx_gain':20,'rx_gain':30,'duration':120,'max_seconds':None,
          'point_timeout':8,'weak_point_extra_seconds':8,
          'setup_confirmed':False,'setup_confirmation_source':'manual',
          'liquid_temperature':None,'temperature_sweep':None}

class LabError(Exception):
    def __init__(self,message,status=400):
        super().__init__(message);self.status=status

def read_json(path,default=None):
    try: return json.loads(path.read_text())
    except (OSError,ValueError): return default

def write_json(path,value):
    # UI 元数据不写设备序列号；临时文件替换防止读取半个 JSON。
    temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value,indent=2,ensure_ascii=False,allow_nan=False)+'\n')
    temporary.replace(path)

def validate_request(value,require_setup=True):
    if not isinstance(value,dict): raise LabError('实验配置必须是对象')
    if set(value)-set(DEFAULTS)-{'profile'}: raise LabError('存在未知实验参数')
    cfg={**DEFAULTS,**value}
    # 兼容旧草稿/API 字段，但不让历史总时限重新启用自动停止。
    cfg['max_seconds']=None
    if cfg['liquid_temperature'] is not None:
        from sspp_experiments.liquid_temperature.metadata import validate_metadata
        try: cfg['liquid_temperature']=validate_metadata(cfg['liquid_temperature'])
        except ValueError as exc: raise LabError(str(exc)) from exc
    if cfg['temperature_sweep'] is not None:
        from sspp_experiments.temperature_sweep.metadata import validate_config
        if cfg['mode'] != 'sweep':
            raise LabError('SSPP 温度联测必须使用独立扫频模式，不能用于固定频率采集')
        try: cfg['temperature_sweep']=validate_config(cfg['temperature_sweep'])
        except (TypeError, ValueError) as exc: raise LabError(str(exc)) from exc
    if cfg['mode'] not in ('sweep','phase'): raise LabError('请选择扫频或连续幅相')
    if cfg['condition'] not in ('empty','finger','material'): raise LabError('实验条件无效')
    for key in ['label','sensor']:
        if not isinstance(cfg[key],str) or len(cfg[key])>80: raise LabError('名称过长或格式无效')
    if not cfg['label'] or not cfg['label'].replace('_','').replace('-','').isalnum():
        raise LabError('实验标签仅允许文字、数字、下划线和连字符')
    bounds={'freq_start_ghz':(.07,6.),'freq_stop_ghz':(.07,6.),'carrier_ghz':(.07,6.),
            'rate':(200000,2000000),'tone':(1,50000),'tone_start':(1,50000),
            'tone_step':(0,20000),'amplitude':(.001,.3),'tx_gain':(0,30),'rx_gain':(0,76),
            'duration':(10,180),'point_timeout':(1,30),'weak_point_extra_seconds':(0,30),
            'points':(2,100),'repeat':(1,20)}
    for key,(lower,upper) in bounds.items():
        number=cfg[key]
        if isinstance(number,bool) or not isinstance(number,(int,float)) or not math.isfinite(number):
            raise LabError(f'{key} 必须是有限数值')
        if not lower<=number<=upper: raise LabError(f'{key} 超出允许范围 {lower}–{upper}')
    for key in ('points','repeat'):
        if cfg[key]!=int(cfg[key]): raise LabError(f'{key} 必须是整数')
        cfg[key]=int(cfg[key])
    if cfg['mode']=='sweep' and cfg['freq_start_ghz']>=cfg['freq_stop_ghz']:
        raise LabError('扫频终点必须高于起点')
    highest=cfg['tone_start']+(cfg['points']-1)*cfg['tone_step']
    if cfg['tone']>=cfg['rate']/2 or (cfg['mode']=='sweep' and highest>=cfg['rate']/2):
        raise LabError('基带单音超出采样率允许范围')
    if cfg['setup_confirmation_source'] not in ('manual', 'global_initialization'):
        raise LabError('设备准备依据无效')
    if require_setup and cfg['setup_confirmed'] is not True:
        raise LabError('请先确认设备接线、线缆固定及初始状态')
    return cfg

class LabController:
    def __init__(self,root=ROOT):
        self.root=Path(root)
        self.lock=threading.RLock()
        self.job=None
        self.proc=None
        self.closed=False
        self.profiles={}
        self.logs=deque(maxlen=100)
        self.points=[]
        self.events=[]
        self.redactions=[]
        self.diagnostic_cache={}
        self.diagnostic_lock=threading.Lock()
        self._archive_rows=OrderedDict()
        self._archive_lock=threading.Lock()
        self.exporting=False
        self.preparing=False
        self.prepare_cancelled=False
        self.collector_thread=None

    def child_env(self):
        return dict(os.environ)

    def _reset_realtime(self):
        pass

    def _handle_event(self,event,job):
        pass

    @staticmethod
    def _record_failure_diagnosis(job, evidence):
        # Only expose a specific cause when the acquisition process recorded it.
        # A transport diagnosis does not establish which cable, hub or supply failed.
        diagnosis = evidence.get('failure_diagnosis')
        if diagnosis != 'usrp_transport_control_unresponsive':
            return
        stopped = (evidence.get('tx_end_of_burst_sent') is True
                   and (evidence.get('rx_stop_sent') is True or evidence.get('rx_stop_command_sent') is True)
                   and evidence.get('workers_stopped') is True)
        job['failure_diagnosis'] = diagnosis
        job['failure_summary'] = ('B210 通信无响应；' + ('收发停止已确认。' if stopped else '停止状态未确认。')
                                  + '请检查连接并重新连接 B210，再初始化。')

    def _write_control(self,proc,message):
        """小于 PIPE_BUF 的控制消息非阻塞写入，不能占住停止/快照锁。"""
        payload=json.dumps(message,ensure_ascii=False)+'\n'
        stream=proc.stdin
        if isinstance(stream,io.StringIO):
            # 离线契约测试使用内存流；真实子进程总是操作管道 FD。
            stream.write(payload);return
        fd=stream.fileno()
        os.set_blocking(fd,False)
        data=payload.encode('utf-8')
        if len(data)>4096:raise LabError('控制消息过长')
        if os.write(fd,data)!=len(data):raise OSError('控制消息未完整写入')

    def refresh_profiles(self):
        candidates=[('own','自有 B210',self.root/'Data/device_profiles/own_device_20261008.json'),
                    ('teacher','老师 B210',self.root/'local_device.json')]
        result=[]
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                return [{'id':k,'name':v[0]} for k,v in self.profiles.items()]
            self.profiles={}
            self.redactions=[]
            for ident,name,path in candidates:
                value=read_json(path)
                if not isinstance(value,dict) or not all(k in value for k in ('serial','fpga','fpga_sha256','channel')): continue
                self.profiles[ident]=(name,path)
                self.redactions.append(str(value['serial']))
                result.append({'id':ident,'name':name,'channel':value['channel']})
        return result

    def public_log(self,text):
        for secret in self.redactions:
            if secret: text=text.replace(secret,'[设备标识]')
        return text

    def command(self,cfg,profile,mode='acquire'):
        # 不经过 shell；网页不能提供脚本路径或任意命令。
        args=[sys.executable,'-B','-u',str(self.root/('run_local.py' if cfg['mode']=='sweep' else 'run_phase.py')),
              mode,'--device-config',str(profile),'--rate',str(cfg['rate']),
              '--amplitude',str(cfg['amplitude']),'--tx-gain',str(cfg['tx_gain']),'--rx-gain',str(cfg['rx_gain'])]
        if cfg['mode']=='sweep':
            args+=['--headless','--label',cfg['label'],'--freq-start',str(cfg['freq_start_ghz']*1e9),
                   '--freq-stop',str(cfg['freq_stop_ghz']*1e9),'--points',str(cfg['points']),
                   '--repeat',str(cfg['repeat']),
                   '--point-timeout',str(cfg['point_timeout']),'--tone-start',str(cfg['tone_start']),
                   '--weak-point-extra-seconds',str(cfg.get('weak_point_extra_seconds',DEFAULTS['weak_point_extra_seconds'])),
                   '--tone-step',str(cfg['tone_step'])]
        else:
            args+=['--initial-label',cfg['label'],'--carrier',str(cfg['carrier_ghz']*1e9),
                   '--duration',str(cfg['duration']),'--tone',str(cfg['tone'])]
        if cfg.get('temperature_sweep') is not None:
            args += ['--temperature-config', json.dumps(cfg['temperature_sweep'], ensure_ascii=False, allow_nan=False)]
        return args

    def preflight(self,request):
        cfg=validate_request(request,require_setup=False)
        self.refresh_profiles()
        if not isinstance(cfg.get('profile'),str) or cfg['profile'] not in self.profiles: raise LabError('请选择可用的设备配置')
        try:
            result=subprocess.run(self.command(cfg,self.profiles[cfg['profile']][1],'plan'),
                                  cwd=self.root,capture_output=True,text=True,timeout=20,env=self.child_env())
        except subprocess.TimeoutExpired: raise LabError('配置检查超时；未启动实验',503)
        if result.returncode: raise LabError(self.public_log(result.stderr[-1500:] or result.stdout[-1500:]))
        budget=budget_info(cfg)
        summary=self.public_log(result.stdout.strip())
        if budget:summary+='\n'+budget['summary']+('\n提示：'+budget['warning'] if budget.get('warning') else '')
        return {'valid':True,'summary':summary,'budget':budget,'hardware_opened':False}

    def start(self,request):
        # 仅短时间持锁；预检和进程检查不能让 GUI 的内存快照等待几十秒。
        with self.lock:
            if self.closed: raise LabError('服务正在关闭',503)
            if self.exporting: raise LabError('图册正在生成，请等待完成后开始实验',409)
            if self.preparing or (self.proc is not None and self.proc.poll() is None) or (self.job and self.job['status'] in ('starting','running','stopping','saving')): raise LabError('已有实验正在运行或保存，请先等待完成',409)
            cfg=validate_request(request)
            self.preparing=True
            self.prepare_cancelled=False
        try:
            self.preflight(cfg)
            # 同时检查原仓库与新仓库的已知采集入口，不初始化设备。
            listing=subprocess.run(['ps','-axo','pid=,command='],capture_output=True,text=True,timeout=5).stdout
            for line in listing.splitlines():
                if any(name in line for name in ('sspp-usrp','multisensor-workbench')) and any(x in line for x in ('run_local.py acquire','run_phase.py acquire','run_teacher.py','sspp_lab.acquire_')):
                    raise LabError('发现另一个射频采集进程，请先结束它',409)
            with self.lock:
                if self.closed or self.prepare_cancelled:
                    raise LabError('启动已取消；未启动采集',409)
                self.logs.clear();self.points=[];self.events=[]
                self._reset_realtime()
                self.job={'id':uuid.uuid4().hex,'status':'starting','config':cfg,'started_utc':datetime.now(timezone.utc).isoformat(),
                          'output':None,'last_event':None,'exit_code':None,'rf_stopped':False,'errors':[]}
                try:
                    self.proc=subprocess.Popen(self.command(cfg,self.profiles[cfg['profile']][1]),cwd=self.root,
                                               stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                                               text=True,bufsize=1,start_new_session=True,env=self.child_env())
                except OSError as exc:
                    self.job['status']='failed';self.job['errors'].append(str(exc))
                    raise LabError('无法启动采集进程：'+str(exc),503)
                self.collector_thread=threading.Thread(target=self._collect,args=(self.proc,self.job),daemon=True)
                self.collector_thread.start()
                threading.Thread(target=self._watchdog,args=(self.proc,self.job),daemon=True).start()
                return self.snapshot()
        finally:
            with self.lock:self.preparing=False

    def _watchdog(self,proc,job):
        # UHD 的 C++ 初始化可能不响应 Python 的 stop_event，必须在父进程计时。
        started=time.monotonic()
        while proc.poll() is None:
            elapsed=time.monotonic()-started
            reason=None
            with self.lock:
                if not job.get('hardware_ready') and elapsed>30: reason='设备初始化超过 30 秒，已请求中止'
                elif job['config']['mode']=='phase' and elapsed>job['config']['duration']+45:
                    reason='实验超过总进程时限，已请求中止'
                if reason:
                    job['errors'].append(reason);job['status']='stopping';job['timeout_requested']=True
            if reason:
                self._end_process(proc,job);return
            time.sleep(.25)

    def _end_process(self,proc,job,send_initial=True):
        if proc.poll() is not None:return
        if send_initial:proc.send_signal(signal.SIGINT)
        wait_s=60 if (job.get("config",{}).get("temperature_sweep") or {}).get("mode")=="continuous" else 8
        try:proc.wait(timeout=wait_s);return
        except subprocess.TimeoutExpired:pass
        if proc.poll() is not None:return
        proc.send_signal(signal.SIGTERM)
        try:proc.wait(timeout=5);return
        except subprocess.TimeoutExpired:pass
        if proc.poll() is None:
            with self.lock:job['forced_exit']=True
            proc.kill();proc.wait(timeout=3)

    def _save_context(self,job):
        if not job.get('output'): return
        output=self.root/job['output']
        if not output.is_dir(): return
        write_json(output/'web_context.json',{'schema_version':1,'source':'local_browser_manual_experiment',
                   'job_id':job['id'],'condition':job['config']['condition'],'sensor':job['config']['sensor'],
                   'initial_setup_user_confirmed':job['config']['setup_confirmed'] and job['config'].get('setup_confirmation_source','manual')=='manual',
                   'setup_confirmation_source':job['config'].get('setup_confirmation_source','manual'),'parameters':job['config'],
                   'manual_events':self.events,'stage_timestamps_are_not_measured_motion':True,
                   'web_status':job['status'],'rf_stopped_confirmed':job.get('rf_stopped',False),
                   'failure_diagnosis':job.get('failure_diagnosis'),'failure_summary':job.get('failure_summary'),
                   'source_hashes':{n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in
                                   ['sspp_lab/controller.py','sspp_lab/diagnostics.py']}})

    def _collect(self,proc,job):
        # 所有 RF 工作在子进程中；stdout 仅用于进度，最终状态以 manifest 验收字段为准。
        log_handle=None
        try:
            for raw in proc.stdout:
                line=raw.rstrip()
                try: event=json.loads(line)
                except ValueError: event=None
                with self.lock:
                    # 高频显示批次直接进入内存，不重复保存为冗长控制日志。
                    if isinstance(event,dict) and event.get('event') in ('TRACE','PREVIEW','TEMP_SAMPLE'):
                        self._handle_event(event,job)
                        continue
                    self.logs.append(self.public_log(line))
                    if line.startswith('OUTPUT_DIR='):
                        output=Path(line.split('=',1)[1]).resolve()
                        try: job['output']=str(output.relative_to(self.root.resolve()))
                        except ValueError: job['errors'].append('采集输出路径无效')
                        if job.get('output'):
                            log_handle=(output/'web_console.log').open('w')
                            log_handle.write('\n'.join(self.logs)+'\n');log_handle.flush()
                        self._save_context(job)
                    elif log_handle:
                        log_handle.write(self.public_log(line)+'\n');log_handle.flush()
                    if isinstance(event,dict):
                        kind=event.get('event')
                        if kind:
                            job['last_event']=event
                            if kind=='HARDWARE_READY':job['hardware_ready']=True
                            if kind=='ACQUIRING': job['status']='running'
                            if kind=='ACQUIRING': job['acquisition_started']=True
                            if kind=='BASELINE_READY': job['baseline']=event['baseline']
                            if kind=='BASELINE_REJECTED': job['errors'].extend(event.get('issues',[]))
                            if kind=='COMPLETE':
                                for error in event.get('errors',[]):
                                    error=self.public_log(str(error))
                                    if error not in job['errors']:job['errors'].append(error)
                            if kind=='LIVE': job['live']=event
                            if kind=='MEASUREMENT': self.points.append(event);job['status']='running'
                            if kind in ('MARK_ACCEPTED','CONDITION_READY'): self.events.append(event);self._save_context(job)
                            if kind in ('RF_STOPPED','STOP_STATUS'): job['status']='saving'
                            if kind=='STOP_STATUS':
                                job['stop_status']=event
                                self._record_failure_diagnosis(job,event)
                            self._handle_event(event,job)
                    if 'RX/TX stopped; saving results.' in line: job['status']='saving'
            code=proc.wait()
            with self.lock:
                job['exit_code']=code
                manifest=read_json(self.root/job['output']/'manifest.json') if job.get('output') else None
                if manifest:
                    t=manifest.get('telemetry',{})
                    job['rf_stopped']=bool(t.get('tx_end_of_burst_sent') and
                                          (t.get('rx_stop_sent') or t.get('rx_stop_command_sent')) and t.get('workers_stopped'))
                    self._record_failure_diagnosis(job,t)
                    job['result_status']=manifest.get('status')
                    job['quality']=manifest.get('quality',{})
                    for error in manifest.get('errors',[]):
                        error=self.public_log(str(error))
                        if error not in job['errors']:job['errors'].append(error)
                temperature_complete=True
                if (job.get('config',{}).get('temperature_sweep') or {}).get('mode')!='manual' and job.get('config',{}).get('temperature_sweep') and manifest and manifest.get('status') in ('complete','complete_with_gaps'):
                    thermal=manifest.get('temperature_sweep') or {}
                    temperature_complete=(thermal.get('status')=='complete' and all(thermal.get(name) is True for name in
                        ('serial_workers_stopped','serial_closed','data_files_closed')))
                    if not temperature_complete:
                        job['errors'].append('温度采集未取得完整保存与关闭证据，不能标记联测完成')
                if job['rf_stopped'] and code==0 and manifest and manifest.get('status') in ('complete','complete_with_gaps') and temperature_complete: job['status']=manifest['status']
                elif job['rf_stopped']:
                    expected_interrupt=all(e.startswith('Interrupted by signal ') for e in job['errors'])
                    job['status']='stopped' if job.get('stop_requested') and expected_interrupt else 'failed'
                elif manifest and manifest.get('telemetry',{}).get('rf_start_attempted') is False:
                    job['status']='initialization_failed';job['acquisition_started']=False
                    job['errors'].append('采集准备未完成，未启动射频收发')
                elif (not job.get('hardware_ready') and manifest and manifest.get('status')=='initializing'
                      and manifest.get('telemetry',{}).get('rf_start_attempted') is not True):
                    # 未到 HARDWARE_READY，执行路径尚未创建/启动收发线程。
                    job['status']='initialization_failed';job['acquisition_started']=False
                    job['errors'].append('设备初始化未完成，未开始实验采集')
                else: job['status']='stop_unconfirmed';job['errors'].append('未取得完整收发停止证据，请检查设备和日志')
                job['ended_utc']=datetime.now(timezone.utc).isoformat()
                self._save_context(job)
        except Exception as exc:
            if proc.poll() is None:self._end_process(proc,job)
            with self.lock: job['status']='stop_unconfirmed';job['errors'].append(self.public_log(str(exc)))
        finally:
            if log_handle:log_handle.close()

    def mark(self,label,settle=3,contact=None):
        with self.lock:
            if not self.proc or self.proc.poll() is not None or not self.job: raise LabError('当前没有运行中的实验',409)
            if self.job['config']['mode']!='phase': raise LabError('阶段标记用于连续幅相实验',409)
            if self.job['status'] in ('stopping','saving'): raise LabError('正在停止或保存，不能再标记',409)
            if not self.job.get('baseline'): raise LabError('静止基线建立后才能改变阶段',409)
            if not isinstance(label,str) or not label or len(label)>80 or not label.replace('_','').replace('-','').isalnum(): raise LabError('阶段标签格式无效')
            if isinstance(settle,bool) or not isinstance(settle,(int,float)) or not math.isfinite(settle) or not 0<=settle<=30: raise LabError('过渡等待应为 0–30 秒')
            if contact is not None and not isinstance(contact,bool): raise LabError('接触标记无效')
            try:self._write_control(self.proc,{'mark':label,'settle':settle})
            except (OSError,ValueError) as exc:raise LabError('采集进程暂时未接收标记，请稍后重试',503) from exc
            event={'event':'WEB_MARK_REQUEST','label':label,'settle_s':settle,'contact_reported':contact,
                   'host_utc':datetime.now(timezone.utc).isoformat()}
            self.events.append(event);self._save_context(self.job)
            return {'accepted':True}

    def stop(self):
        with self.lock:
            if self.preparing:self.prepare_cancelled=True
            if not self.proc or self.proc.poll() is not None: return self.snapshot()
            self.job['stop_requested']=True;self.job['status']='stopping'
            if self.job['config']['mode']=='phase':
                try: self._write_control(self.proc,{'stop':True})
                except (OSError,ValueError): self.proc.send_signal(signal.SIGINT)
            else: self.proc.send_signal(signal.SIGINT)
            if not self.job.get('stop_cleanup_started'):
                self.job['stop_cleanup_started']=True
                threading.Thread(target=self._end_process,args=(self.proc,self.job,False),daemon=True).start()
            return self.snapshot()

    def shutdown(self):
        with self.lock: self.closed=True
        self.stop()
        proc=self.proc
        if proc and proc.poll() is None:
            try: proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.send_signal(signal.SIGTERM)
                try: proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    # 不将强制结束当作 RF 停止已确认；仍需人工检查设备。
                    proc.kill();proc.wait(timeout=3)
        # 进程退出后还需等待尾部事件、manifest与desktop_context收口。
        collector=self.collector_thread
        if collector is not None and collector is not threading.current_thread():
            collector.join(timeout=5)

    def snapshot(self):
        with self.lock:
            return {'job':json.loads(json.dumps(self.job)) if self.job else None,
                    'logs':list(self.logs),'points':list(self.points),'events':list(self.events),
                    'preparing':self.preparing,
                    'server_version':'1.0.0','real_hardware_only':True}

    def run_path(self,key):
        # 只接受指定类型及真实目录名；禁止 ../、绝对路径和符号链接绕过。
        if not isinstance(key,str) or not re.fullmatch(r'(runs|phase_runs|teacher_runs|temperature_pressure_runs)/[A-Za-z0-9_-]+',key): raise LabError('实验编号无效',404)
        target=self.root/'Data'/key
        try: target.resolve().relative_to((self.root/'Data').resolve())
        except ValueError: raise LabError('实验路径无效',404)
        if not target.is_dir(): raise LabError('实验不存在',404)
        return target

    @staticmethod
    def _archive_signature(paths):
        result=[]
        for path in paths:
            try:
                stat=path.stat()
                result.append((stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns))
            except OSError:
                result.append(None)
        return tuple(result)

    def _cached_archive_row(self,key,paths,build):
        """Cache display summaries only; analysis/export always read their sources."""
        signature=self._archive_signature(paths)
        with self._archive_lock:
            cached=self._archive_rows.get(key)
            if cached and cached[0]==signature:
                self._archive_rows.move_to_end(key)
                return deepcopy(cached[1])
        row=build()
        # Do not retain a snapshot if acquisition changed metadata while reading.
        if row is not None and self._archive_signature(paths)==signature:
            with self._archive_lock:
                self._archive_rows[key]=(signature,deepcopy(row))
                self._archive_rows.move_to_end(key)
                while len(self._archive_rows)>2048:self._archive_rows.popitem(last=False)
        return row

    def _history_row(self,key,kind,manifest,context):
        context_status=context.get('desktop_status',context.get('web_status'))
        return {'key':key,'mode':{'runs':'sweep','phase_runs':'phase','teacher_runs':'teacher'}[kind],
                'started_utc':manifest.get('started_utc',''),'status':context_status if context_status in ('complete','complete_with_gaps','stopped','failed','stop_unconfirmed','initialization_failed') else manifest.get('status','unknown'),
                'label':manifest.get('label') or manifest.get('initial_label') or manifest.get('material_input') or key.split('/')[-1],
                'sensor':context.get('sensor',''),'source':'desktop' if context.get('source')=='local_desktop_manual_experiment' else ('web' if context else 'legacy')}

    def history(self):
        out=[]
        for kind in ('runs','phase_runs','teacher_runs'):
            base=self.root/'Data'/kind
            if not base.exists(): continue
            for folder in base.iterdir():
                if not folder.is_dir() or folder.is_symlink(): continue
                key=kind+'/'+folder.name
                paths=[folder/('feature_report.json' if kind=='teacher_runs' else 'manifest.json'),
                       folder/'desktop_context.json',folder/'web_context.json']
                def build():
                    manifest=read_json(paths[0],{})
                    context=read_json(paths[1],{}) or read_json(paths[2],{})
                    return self._history_row(key,kind,manifest,context)
                out.append(self._cached_archive_row(key,paths,build))
        return sorted(out,key=lambda x:x['started_utc'],reverse=True)

    def _archive_details(self,details,manifest):
        return details

    def details(self,key,*,include_curve=True):
        path=self.run_path(key)
        m=read_json(path/('feature_report.json' if key.startswith('teacher_runs/') else 'manifest.json'),{})
        # 只展示实验元数据；设备身份不进入界面，错误文字单独脱敏。
        allowed={'status','label','initial_label','material_input','started_utc','ended_utc','stop_reason',
                 'telemetry','quality','warnings','baseline','events','condition_summary','processed_windows','processed_samples',
                 'raw_bytes','method','measurement','limitations','experiment_scope',
                 'timestamp_continuity','timestamp_failure_file','temperature_sweep'}
        exports=[]
        with os.scandir(path) as entries:
            for entry in entries:
                if (Path(entry.name).suffix in ('.png','.pdf','.csv','.xlsx','.npz','.cf32')
                        or entry.name in ('events.json','condition_summary.json','web_context.json','desktop_context.json','amplitude_phase.jsonl','rx_frames.jsonl','baseline.json','diagnostic_figures.json','rx_timestamp_failure.json','sweep_quality.jsonl','sweep_invalid_points.jsonl')):
                    if entry.is_file(follow_symlinks=False):exports.append(entry.name)
        figure_index=read_json(path/'diagnostic_figures.json',{}) or {}
        figures=[{k:v for k,v in item.items() if k in ('file','title','group')} for item in figure_index.get('plots',[]) if isinstance(item,dict)]
        metadata={k:v for k,v in m.items() if k in allowed}
        serial=str((m.get('device_profile') or {}).get('serial') or '')
        metadata['errors']=[self.public_log(str(error)).replace(serial,'[设备标识]') if serial else self.public_log(str(error))
                            for error in m.get('errors',[])]
        details={'key':key,'metadata':metadata,'files':sorted(exports),'figures':figures,
                 'context':read_json(path/'desktop_context.json',{}) or read_json(path/'web_context.json',{}),
                 'curve_loaded':include_curve,
                 'curve':self.curve(key) if include_curve else {'mode':'phase' if key.startswith('phase_runs/') else 'sweep','points':[],'loaded':False}}
        return self._archive_details(details,m)

    def curve(self,key=None):
        if key is None:
            with self.lock:
                if not self.job: return {'mode':None,'points':[]}
                if self.job['config']['mode']=='sweep': return {'mode':'sweep','points':list(self.points)}
                if not self.job.get('output'): return {'mode':'phase','points':[]}
                path=self.root/self.job['output']
        else: path=self.run_path(key)
        series=path/'amplitude_phase.jsonl'
        if series.exists():
            rows=[]
            baseline=read_json(path/'baseline.json',{})
            with series.open() as source:
                for line in source:
                    try: row=json.loads(line)
                    except ValueError: continue
                    if not baseline or row['t_s']<baseline['end_s']: continue
                    a=row.get('relative_amplitude_dB')
                    ph=row.get('relative_phase_deg')
                    if a is None or ph is None: continue
                    rows.append({'x':row['t_s'],'amplitude':a,'phase':ph,'label':row['label']})
            step=max(1,math.ceil(len(rows)/2500))
            sampled=rows[::step]
            if rows and sampled[-1]!=rows[-1]: sampled.append(rows[-1])
            return {'mode':'phase','points':sampled,'display_decimation':step,'stored_windows':len(rows)}
        table=path/'measurements.csv'
        if table.exists():
            with table.open() as source:
                points=[{'carrier_hz':float(r['requested_carrier_GHz'])*1e9,'amplitude_dB':None if r.get('measurement_status')=='invalid' else float(r['peak_amplitude_dBFS_est']),
                         'repeat':int(r['repeat']),'point':int(r['point'])} for r in csv.DictReader(source)]
            return {'mode':'sweep','points':points}
        return {'mode':None,'points':[]}

    def diagnostic_view(self,key=None):
        # 在控制锁之外做只读图形运算，停止按钮不会等绘图计算。
        if key is None:
            with self.lock:
                if not self.job or not self.job.get("output"):
                    return {"mode":None,"groups":{},"facts":[],"chart_count":0,"sample":{"available":False,"reason":"等待真实记录或选择历史实验"}}
                key=str(Path(self.job["output"]).relative_to("Data"))
        path=self.run_path(key)
        if key.startswith("teacher_runs/"):
            return {"mode":"teacher","groups":{},"facts":[],"chart_count":0,"sample":{"available":False,"reason":"老师历史GUI未保存完整RX IQ，可查看原有导出图"}}
        with self.diagnostic_lock:
            cached=self.diagnostic_cache.get(key)
            # 活跃文件每3秒重读；结束记录缓存到文件变化后再更新。
            files=[path/n for n in ("manifest.json","baseline.json","amplitude_phase.jsonl","rx_frames.jsonl","rx_iq.cf32","measurements.csv","web_context.json","desktop_context.json")]
            files+=list(path.glob("iq_point_[0-9]*.npz"))
            signature=tuple((p.name,p.stat().st_size,p.stat().st_mtime_ns) for p in files if p.exists() and not p.is_symlink())
            if cached and (cached[1]==signature or time.monotonic()-cached[0]<3):return cached[2]
            from .diagnostics import build_diagnostics
            result=build_diagnostics(path,"phase" if key.startswith("phase_runs/") else "sweep");result["key"]=key
            self.diagnostic_cache[key]=(time.monotonic(),signature,result)
            if len(self.diagnostic_cache)>8:self.diagnostic_cache.pop(next(iter(self.diagnostic_cache)))
            return result

    def export_diagnostics(self,key=None):
        with self.lock:
            if self.preparing or (self.proc and self.proc.poll() is None):raise LabError("请等待当前采集停止后再生成图册",409)
            if self.exporting:raise LabError("图册正在生成，请等待完成",409)
            if key is None:
                if not self.job or not self.job.get("output"):raise LabError("请先选择有真实数据的实验")
                key=str(Path(self.job["output"]).relative_to("Data"))
            self.run_path(key)
            if key.startswith("teacher_runs/"):raise LabError("老师历史GUI没有完整IQ，不能生成这些诊断图")
            self.exporting=True
        try:
            result=subprocess.run([sys.executable,"-B","-m","sspp_lab.export_figures","--root",str(self.root),"--key",key],
                                  cwd=self.root,capture_output=True,text=True,timeout=60,env=self.child_env())
            if result.returncode:raise LabError(self.public_log(result.stderr[-1200:] or "图册生成失败"),500)
            return json.loads(result.stdout)
        except subprocess.TimeoutExpired:raise LabError("图册生成超过60秒，导出进程已停止，请重试",503)
        finally:
            with self.lock:self.exporting=False

    def download(self,key,name):
        details=self.details(key)
        if name not in details['files']: raise LabError('文件不可下载',404)
        path=self.run_path(key)/name
        if path.is_symlink(): raise LabError('文件不可下载',404)
        return path

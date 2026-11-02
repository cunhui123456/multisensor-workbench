"""从真实保存数据构造图形；不导入UHD、不改IQ/manifest，不把TX设定当RF实测。

图形采用声明式模型，网页与PNG/PDF导出共用数据来源和单位。
原始IQ只读映射，频谱图是指定窗口的数字估计，不是标定S21或dBm。
"""
import csv
import json
import math
import zipfile
from pathlib import Path
import numpy as np

COLORS=['#087f85','#d28b32','#7474bd','#be6687','#4c9271','#527fa2']

def read_json(path,default=None):
    if path.is_symlink():return default
    try:return json.loads(path.read_text())
    except (OSError,ValueError):return default

def json_rows(path):
    if not path.exists() or path.is_symlink():return []
    rows=[]
    with path.open() as stream:
        for line in stream:
            try:row=json.loads(line)
            except ValueError:continue  # 活跃写入可能有尚未完成的末行。
            if isinstance(row,dict):rows.append(row)
    return rows

def sampled(x,y,limit=1800):
    """保留每个桶的极小/极大与两端点，避免质量尖峰被固定步进吞掉。"""
    x=np.asarray(x);y=np.asarray(y)
    valid=np.isfinite(x)&np.isfinite(y);x=x[valid];y=y[valid]
    if len(x)<=limit:return np.column_stack((x,y)).tolist()
    width=max(1,math.ceil(len(x)/max(1,limit//2)));chosen={0,len(x)-1}
    for first in range(0,len(x),width):
        part=y[first:first+width];chosen.update([first+int(np.argmin(part)),first+int(np.argmax(part))])
    indices=sorted(chosen);return np.column_stack((x[indices],y[indices])).tolist()

def series(label,x,y,color=None,errors=None):
    item={'label':label,'color':color or COLORS[0]}
    if errors is None:
        item['data']=sampled(x,y)
    else:
        # Summary points and their SDs are one observation each. Keep every
        # finite x with its matching error, including missing means as gaps;
        # independently decimating the means would misassign uncertainties.
        x=np.asarray(x,dtype=float);y=np.asarray(y,dtype=float);errors=np.asarray(errors,dtype=float)
        if x.ndim!=1 or y.shape!=x.shape or errors.shape!=x.shape:
            raise ValueError('均值与误差棒必须逐点对应')
        finite_x=np.isfinite(x)
        item['data']=[[float(a),float(b) if np.isfinite(b) else None] for a,b in zip(x[finite_x],y[finite_x])]
        item['errors']=[float(v) if np.isfinite(v) else None for v in errors[finite_x]]
    return item

def chart(ident,title,xlabel,ylabel,items=None,kind='line',note='',**extra):
    return {'id':ident,'title':title,'xlabel':xlabel,'ylabel':ylabel,'kind':kind,
            'series':items or [],'note':note,**extra}

def finite_number(value):
    return isinstance(value,(int,float)) and math.isfinite(value)

def raw_preview(run,manifest,mode):
    """连续记录读取最新完整样点；扫频只读取最新已落盘频点，明确可能滞后一频点。"""
    params=manifest.get('actual_sdr') or manifest.get('requested_sdr') or {}
    rate=params.get('rx_rate_actual') or params.get('rate');tone=params.get('tone')
    carrier=params.get('freq');amp=params.get('amp');raw=None;origin=0;source=''
    if mode=='phase':
        path=run/'rx_iq.cf32'
        if path.exists() and not path.is_symlink():
            count=path.stat().st_size//8
            if count:
                raw=np.memmap(path,dtype=np.complex64,mode='r',shape=(count,))
                origin=max(0,count-8192);source=f'RX原始IQ样点 {origin}–{count-1}'
    else:
        files=sorted((p for p in run.glob('iq_point_[0-9]*.npz') if p.is_file() and not p.is_symlink()),key=lambda p:int(p.stem.rsplit('_',1)[-1]))
        if files:
            try:
                with np.load(files[-1],allow_pickle=False) as data:
                    raw=np.array(data['iq']);rate=float(data['rx_rate'])
                    info=json.loads(str(data['point_info'])) if 'point_info' in data else {}
                    tone=info.get('tone_hz',tone);carrier=info.get('requested_carrier_hz',carrier)
                    origin=max(0,len(raw)-8192);source=f'{files[-1].name}：最新已保存频点（运行时可能滞后一频点）'
            except (OSError,ValueError,KeyError,EOFError,zipfile.BadZipFile):pass
    return raw,origin,rate,tone,carrier,amp,source

def receiver_charts(raw,origin,rate,tone,carrier,amp,source):
    if raw is None or not rate or len(raw)<8:return [],{'available':False,'reason':'尚无已保存RX IQ；有真实数据后显示。'}
    tail=np.asarray(raw[-min(8192,len(raw)):]);finite=bool(np.isfinite(tail).all())
    if not finite:return [],{'available':False,'reason':'IQ窗口含非有限值，图形未生成。'}
    wave_n=min(len(tail),2048,max(32,int(math.ceil(8*rate/max(abs(tone or 0),1)))))
    wave=tail[-wave_n:];indices=np.unique(np.linspace(0,wave_n-1,min(512,wave_n),dtype=int));ms=indices/rate*1000
    plots=[chart('rx_wave','RX接收时域 I/Q','窗口内时间（ms）','数字幅值',[
        series('RX I',ms,wave.real[indices]),series('RX Q',ms,wave.imag[indices],COLORS[1])],
        note=source+'；I/Q来自同一个接收通道。')]
    n=len(tail);window=np.hanning(n);freq=np.fft.fftshift(np.fft.fftfreq(n,1/rate))
    spec=np.abs(np.fft.fftshift(np.fft.fft(tail*window)))/window.sum()
    db=20*np.log10(np.maximum(spec,1e-15));peak=int(np.argmax(spec));peak_hz=float(freq[peak])
    plots.append(chart('rx_spectrum','RX全带宽幅度频谱','相对载频偏移（kHz）','幅度（dBFS估计）',[
        series('Hann窗频谱',freq/1000,db)],vertical=[{'x':(tone or 0)/1000,'label':'设定单音'}],
        note=f'{source}；FFT {n}点，频率栅格{rate/n:.2f} Hz；峰值{peak_hz:.2f} Hz。'))
    scatter=tail[np.linspace(0,len(tail)-1,min(800,len(tail)),dtype=int)]
    plots.append(chart('rx_iq','RX原始IQ复平面','I（数字幅值）','Q（数字幅值）',[
        {'label':'RX样点（不是锁相系数）','color':COLORS[0],'data':np.column_stack((scatter.real,scatter.imag)).tolist()}],
        kind='scatter',note=source+'；理想单音形成圆/椭圆，形状本身不能证明传感器传播路径。',equal_aspect=True))
    if finite_number(amp) and finite_number(tone):
        tx=amp*np.exp(2j*np.pi*tone*indices/rate)
        plots.append(chart('tx_reference','TX基带设定：数学参考','窗口内时间（ms）','设定数字幅值',[
            series('设定I',ms,tx.real,COLORS[2]),series('设定Q',ms,tx.imag,COLORS[3])],
            note='由幅值/单音设定计算，局部相位起点为零；未采样实际TX射频输出，不能作实测TX/RX损耗对照。',source_type='computed_setting'))
    # 从原始文件均匀选择最多64个真实短窗。各列不连接成连续IQ，不补点。
    nfft=min(2048,len(raw));starts=np.unique(np.linspace(0,len(raw)-nfft,min(64,max(1,len(raw)//nfft)),dtype=int))
    w=np.hanning(nfft);full_f=np.fft.fftshift(np.fft.fftfreq(nfft,1/rate))
    bands=np.array_split(np.arange(nfft),min(160,nfft));f=np.array([full_f[b].mean() for b in bands])/1000
    powers=[]
    for start in starts:
        x=np.asarray(raw[start:start+nfft]);p=np.abs(np.fft.fftshift(np.fft.fft(x*w)))**2/(rate*np.sum(w*w))
        powers.append([float(10*np.log10(max(float(p[b].mean()),1e-30))) if np.isfinite(p[b]).all() else None for b in bands])
    plots.append(chart('rx_waterfall','RX抽样时频图','原始记录内时间（s）','载频偏移（kHz）',kind='heatmap',
        x=((starts+(nfft-1)/2)/rate).tolist(),y=f.tolist(),z=np.asarray(powers,dtype=object).T.tolist(),colorbar='PSD（dBFS/Hz估计）',
        note=f'均匀抽取{len(starts)}个{nfft}点真实窗口，按频率带平均PSD；时间列之间可能有间隔。扫频只覆盖本频点已保存IQ。'))
    return plots,{'available':True,'source':source,'rate':float(rate),'tone_hz':tone,'carrier_hz':carrier,
        'fft_points':n,'fft_bin_hz':rate/n,'wideband_peak_hz':peak_hz,
        'peak_error_hz':abs(peak_hz-tone) if tone is not None else None,
        'window_rms_dBFS_est':float(20*np.log10(max(float(np.sqrt(np.mean(np.abs(tail)**2))),1e-15))),
        'window_peak_component':float(max(np.max(abs(tail.real)),np.max(abs(tail.imag)))),
        'window_near_fullscale_components':int(np.sum(abs(tail.real)>=.98)+np.sum(abs(tail.imag)>=.98)),
        'finite':finite}

def sweep_charts(run):
    path=run/'measurements.csv'
    if not path.exists() or path.is_symlink():return [],[]
    with path.open() as stream:rows=list(csv.DictReader(stream))
    required=['repeat','point','requested_carrier_GHz']
    clean=[]
    for row in rows:
        try:
            if not all(k in row and math.isfinite(float(row[k])) for k in required):continue
            int(row['repeat']);int(row['point'])
            if row.get('measurement_status')!='invalid' and not math.isfinite(float(row['peak_amplitude_dBFS_est'])):continue
        except (ValueError,TypeError):continue  # 活跃CSV可能只写完一部分。
        clean.append(row)
    rows=clean
    if not rows:return [],[]
    freqs=sorted({float(r['requested_carrier_GHz']) for r in rows});repeats=sorted({int(r['repeat']) for r in rows})
    matrix=np.full((len(repeats),len(freqs)),np.nan)
    for row in rows:
        if row.get('measurement_status')!='invalid':matrix[repeats.index(int(row['repeat'])),freqs.index(float(row['requested_carrier_GHz']))]=float(row['peak_amplitude_dBFS_est'])
    counts=np.isfinite(matrix).sum(axis=0);mean=np.array([np.mean(matrix[:,i][np.isfinite(matrix[:,i])]) if counts[i] else np.nan for i in range(len(freqs))])
    sd=np.array([np.std(matrix[:,i][np.isfinite(matrix[:,i])],ddof=1) if counts[i]>1 else np.nan for i in range(len(freqs))])
    note='同次扫频的技术重复；均值/样本SD在dB域计算，不能当作独立取放重复或标定S21。'
    plots=[chart('sweep_mean','扫频均值与±1样本SD','载频（GHz）','幅度（dBFS估计）',[
        series('各轮dB均值',freqs,mean,errors=sd)],note=note),
        chart('sweep_deviation','各轮相对同频点均值的偏差','载频（GHz）','偏差（dB）',[
            series(f'轮{repeat}',freqs,matrix[i]-mean,COLORS[i%len(COLORS)]) for i,repeat in enumerate(repeats)],note=note),
        chart('sweep_sd','逐频点重复标准差','载频（GHz）','样本SD（dB）',[
            series('重复SD（至少两轮）',freqs,sd)],note='单轮频点SD不可估计，保持空缺。'),
        chart('sweep_heatmap','轮次×载频响应热图','载频（GHz）','轮次',kind='heatmap',x=freqs,y=repeats,
              z=[[float(v) if np.isfinite(v) else None for v in row] for row in matrix],colorbar='幅度（dBFS估计）',note='缺失测量格保持空白。')]
    tones=[]
    for row in rows:
        try:
            if all(math.isfinite(float(row[k])) for k in ('expected_tone_Hz','measured_tone_Hz')):tones.append(row)
        except (KeyError,ValueError,TypeError):pass
    if tones:
        items=[];errors=[]
        for i,repeat in enumerate(repeats):
            part=[r for r in tones if int(r['repeat'])==repeat];x=[float(r['requested_carrier_GHz']) for r in part]
            expected=np.array([float(r['expected_tone_Hz']) for r in part]);measured=np.array([float(r['measured_tone_Hz']) for r in part])
            if i==0:items.append(series('设定单音',x,expected/1000,'#45576b'))
            items.append(series(f'轮{repeat}实测',x,measured/1000,COLORS[i%len(COLORS)]))
            errors.append(series(f'轮{repeat}',x,measured-expected,COLORS[i%len(COLORS)]))
        plots.extend([chart('tone_tracking','设定与实测单音跟踪','载频（GHz）','基带单音（kHz）',items,note='频谱峰应跟随设定；切频过渡VERIFY数据不作为正式测量。'),
                      chart('tone_error','正式测量单音频差','载频（GHz）','实测−设定（Hz）',errors,
                            horizontal=[{'y':200,'label':'+200 Hz门槛'},{'y':-200,'label':'−200 Hz门槛'}],note='200 Hz为原算法验证门槛；FFT栅格误差并非绝对频率标定误差。')])
    return plots,[{'name':f'轮{repeat}测量点','value':int(np.isfinite(matrix[i]).sum())} for i,repeat in enumerate(repeats)]

def phase_charts(run,manifest,context):
    rows=[r for r in json_rows(run/'amplitude_phase.jsonl') if isinstance(r.get('label'),str) and
          all(finite_number(r.get(k)) for k in ['t_s','amplitude_dBFS_est','phase_unwrapped_deg','i','q'])]
    if not rows:return [],[],[]
    t=np.array([r['t_s'] for r in rows]);a=np.array([r['amplitude_dBFS_est'] for r in rows]);ph=np.array([r['phase_unwrapped_deg'] for r in rows])
    b=read_json(run/'baseline.json',{}) or manifest.get('baseline') or {}
    plots=[chart('absolute_amplitude','接收单音绝对数字幅度','RX时间（s）','幅度（dBFS估计）',[
        series('锁相单音幅度',t,a)],note='包括启动/参考阶段；不是RF输入功率或S21。')]
    quality=[];segments=[]
    current=None
    contacts={e.get('label'):e.get('contact_reported') for e in context.get('manual_events',[]) if e.get('event')=='WEB_MARK_REQUEST'}
    stage_colors={label:COLORS[i%len(COLORS)] for i,label in enumerate(dict.fromkeys(r['label'] for r in rows if not r['label'].startswith('transition')))}
    for row in rows:
        label=row['label']
        if current is None or label!=current['label']:
            if current is not None:current['end']=row['t_s'];segments.append(current)
            current={'start':row['t_s'],'end':row['t_s'],'label':label,'transition':label.startswith('transition'),
                     'contact':contacts.get(label),'color':stage_colors.get(label,'#b5bac0')}
        current['end']=row['t_s']
    if current:segments.append(current)
    quality.append(chart('stages','操作阶段时间线','RX时间（s）','操作者阶段',kind='segments',segments=segments,
        note='标记是操作指令/等待结束时间，不是实测位置时间；灰色为过渡，接触报告单独显示。'))
    for key,title,ylabel,threshold in [('coherent_fraction','单音相干占比估计','相干占比',.5),('phase_step_deg','逐窗相位增量','相邻窗口相位差（°）',120)]:
        part=[r for r in rows if finite_number(r.get(key))]
        if part:quality.append(chart(key,title,'RX时间（s）',ylabel,[series(title,[r['t_s'] for r in part],[r[key] for r in part])],
            horizontal=[{'y':threshold,'label':'工程门槛'}]+([{'y':-threshold,'label':'负门槛'}] if key=='phase_step_deg' else []),
            note='相干占比不是严格SNR。' if key=='coherent_fraction' else '120°/窗是停止门槛；解缠不能唯一恢复超过180°的真实窗间变化。'))
    if b:
        rel_a=a-20*np.log10(b['reference_amplitude']);rel_ph=ph-b['reference_phase_unwrapped_deg']
        usable=np.array([r['t_s']>=b['end_s'] for r in rows]);stable=usable&np.array([not r['label'].startswith('transition') for r in rows])
        plots.append(chart('relative_amplitude','相对静止参考的幅度变化','RX时间（s）','相对幅度（dB）',[
            series('相对幅度',t[usable],rel_a[usable])],note='同次静止参考；保留过渡变化，灰色区间为操作等待，绿色区间为参考。未换算毫米位移。'))
        plots.append(chart('phase_wrap','折返与连续解缠相位','RX时间（s）','相对相位（°）',[
            series('解缠相位',t[usable],rel_ph[usable]),series('折返至±180°',t[usable],(rel_ph[usable]+180)%360-180,COLORS[1])],note='同次静止参考；不跨重调谐/重启比较绝对相位。'))
        normalized=np.array([complex(r['i'],r['q']) for r in rows])/b['reference_amplitude']*np.exp(-1j*np.deg2rad(b['reference_phase_unwrapped_deg']))
        labels=list(dict.fromkeys(r['label'] for r,valid in zip(rows,stable) if valid))
        groups=[];responses=[];stage_a=[];stage_p=[];stage_counts=[]
        for idx,label in enumerate(labels):
            mask=stable&np.array([r['label']==label for r in rows]);indices=np.flatnonzero(mask)[::max(1,int(mask.sum())//800)]
            groups.append({'label':label,'color':COLORS[idx%len(COLORS)],'data':np.column_stack((normalized[indices].real,normalized[indices].imag)).tolist()})
            responses.append({'label':label,'color':COLORS[idx%len(COLORS)],'data':np.column_stack((rel_a[indices],rel_ph[indices])).tolist()})
            stage_a.append((float(rel_a[mask].mean()),float(rel_a[mask].std(ddof=1)) if mask.sum()>1 else np.nan))
            stage_p.append((float(rel_ph[mask].mean()),float(rel_ph[mask].std(ddof=1)) if mask.sum()>1 else np.nan))
            stage_counts.append(int(mask.sum()))
        plots.extend([chart('lockin_iq','相对参考的锁相复系数','相对I','相对Q',groups,kind='scatter',equal_aspect=True,note='锁相系数除以本次参考幅值并旋转参考相位；排除过渡，参考点为(1,0)。'),
                      chart('response_scatter','幅度−相位响应轨迹','相对幅度（dB）','相对相位（°）',responses,kind='scatter',note='按操作阶段着色，连续窗口有时间相关性，不是独立重复试验。')])
        for ident,title,values,ylabel in [('stage_amplitude','各阶段幅度均值±1样本SD',stage_a,'相对幅度（dB）'),('stage_phase','各阶段相位均值±1样本SD',stage_p,'相对相位（°）')]:
            plots.append(chart(ident,title,'操作阶段',ylabel,[series('均值±SD',range(len(labels)),[v[0] for v in values],errors=[v[1] for v in values])],
                kind='bar',xticks=labels,note='排除参考前及过渡窗口；误差棒是技术窗口SD，不是置信区间。'))
        quality.append(chart('stage_windows','各阶段纳入统计的窗口数','操作阶段','技术窗口数',[series('窗口数',range(len(labels)),stage_counts)],kind='bar',xticks=labels,note='不能将连续窗口数当成独立动作/取放次数。'))
        for ident,title,v,ylabel in [('amplitude_hist','稳定阶段幅度分布',rel_a[stable],'相对幅度（dB）'),('phase_hist','稳定阶段相位分布',rel_ph[stable],'相对相位（°）')]:
            if len(v):
                counts,edges=np.histogram(v,bins=min(40,max(5,int(np.sqrt(len(v))))));centers=(edges[:-1]+edges[1:])/2
                plots.append(chart(ident,title,ylabel,'窗口数',[series('排除过渡后的窗口',centers,counts)],kind='bar',note='混合各稳定操作阶段的分布；不代表独立重复性。'))
        # 以真实时间滚动1秒，不假定固定帧长或采样率。
        for ident,title,v,ylabel in [('amplitude_rolling','1秒滚动幅度波动',rel_a,'样本SD（dB）'),('phase_rolling','1秒滚动相位波动',rel_ph,'样本SD（°）')]:
            times=[];values=[]
            for index in np.unique(np.linspace(0,len(t)-1,min(800,len(t)),dtype=int)):
                start=int(np.searchsorted(t,t[index]-1));part=v[start:index+1]
                if len(part)>1 and t[index]-t[start]>=.8:times.append(t[index]);values.append(float(np.std(part,ddof=1)))
            plots.append(chart(ident,title,'RX时间（s）',ylabel,[series('前1秒窗口SD',times,values)],note='包含操作变化与漂移，不自动去趋势；大SD可能是动作，而非仪器噪声。'))
        baseline_mask=(t>=b['start_s'])&(t<=b['end_s']);bt=t[baseline_mask]
        ba=rel_a[baseline_mask];bp=rel_ph[baseline_mask]
        quality.append(chart('baseline_amplitude','静止参考幅度细节','RX时间（s）','相对参考幅度（dB）',[series('参考窗口',bt,ba)],note=f'幅度SD {b.get("amplitude_sd_dB",0):.5f} dB。'))
        if len(bt)>1:
            fit=np.polyval(np.polyfit(bt-bt.mean(),bp,1),bt-bt.mean())
            quality.append(chart('baseline_phase','静止参考相位与漂移拟合','RX时间（s）','相对参考相位（°）',[
                series('实测参考相位',bt,bp),series('线性拟合（仅描述）',bt,fit,COLORS[1])],note='拟合只描述参考质量；未从实验幅相中扣除漂移。'))
        for item in plots+quality:
            if item['xlabel']=='RX时间（s）':item['segments']=segments;item['baseline_interval']=[b['start_s'],b['end_s']]
    frames=json_rows(run/'rx_frames.jsonl');rate=(manifest.get('actual_sdr') or manifest.get('requested_sdr') or {}).get('rx_rate_actual') or (manifest.get('requested_sdr') or {}).get('rate')
    if frames and rate:
        times=[f['start_sample']/rate for f in frames];sizes=[f['count'] for f in frames]
        quality.append(chart('rx_frame_size','RX逐帧有效样本数','RX帧开始时间（s）','样本数',[series('每帧有效长度',times,sizes)],note='帧长变化本身不等于丢样。'))
        gaps=[];legacy=False
        for previous,current in zip(frames,frames[1:]):
            if 'device_tick_at_master_rate' in previous and 'device_tick_at_master_rate' in current:
                stride=previous.get('ticks_per_sample')
                clock=previous.get('master_clock_rate_hz')
                if finite_number(stride) and stride>=1 and stride==current.get('ticks_per_sample') and finite_number(clock) and clock>0 and clock==current.get('master_clock_rate_hz'):
                    delta=current['device_tick_at_master_rate']-previous['device_tick_at_master_rate']-previous['count']*stride
                    gaps.append(delta/stride)
                else:gaps.append(float('nan'))
            elif 'device_tick_at_master_rate' not in previous and 'device_tick_at_master_rate' not in current:
                legacy=True
                gaps.append(current['device_tick_at_rx_rate']-previous['device_tick_at_rx_rate']-previous['count'])
            else:gaps.append(float('nan'))
        note='按设备主时钟整数计数比较后换算为样本数；0为相邻已保存帧首尾相接，正数为间隙，负数为重叠。'
        if legacy:note='含旧记录的采样率取整时间戳；±1偏差可能来自舍入，不能单凭此图认定丢样或重叠。'
        note+=' 仅涵盖已保存帧；新记录拒绝帧的详情见 rx_timestamp_failure.json，缺失证据不补零。'
        quality.append(chart('hardware_gaps','硬件时间戳连续性','RX帧开始时间（s）','偏离期望的样本数',[series('时间戳间隙',times[1:],gaps)],note=note))
    return plots,quality,segments

def build_diagnostics(run,mode):
    run=Path(run);manifest=read_json(run/'manifest.json',{}) or {};context=read_json(run/'desktop_context.json',{}) or read_json(run/'web_context.json',{}) or {}
    response={'mode':mode,'groups':{'diagnostic':[],'sweep':[],'phase':[],'quality':[]},'facts':[],
              'status':manifest.get('status','unknown'),'run_name':run.name,'segments':[],'sample':{}}
    raw,origin,rate,tone,carrier,amp,source=raw_preview(run,manifest,mode)
    receiver,sample=receiver_charts(raw,origin,rate,tone,carrier,amp,source)
    response['groups']['diagnostic']=receiver;response['sample']=sample
    if mode=='sweep':
        response['groups']['sweep'],response['facts']=sweep_charts(run)
        if response['facts']:
            response['groups']['quality']=[chart('sweep_counts','每轮正式测量点数','技术重复轮次','测量点数',[
                series('CSV正式测量点',range(len(response['facts'])),[v['value'] for v in response['facts']])],
                kind='bar',xticks=[v['name'].replace('测量点','') for v in response['facts']],note='按正式测量CSV计数；结合运行状态、预设点数及IQ文件核查完整性。')]
    else:response['groups']['phase'],response['groups']['quality'],response['segments']=phase_charts(run,manifest,context)
    telemetry=manifest.get('telemetry',{})
    for key,label in [('tx_samples','累计TX样本'),('rx_samples','累计RX样本'),('rx_frames','RX帧数'),('tx_partial_sends','TX部分发送次数'),('rx_timeouts','RX timeout次数')]:
        if key in telemetry:response['facts'].append({'name':label,'value':telemetry[key]})
    for key,label in [('tx_end_of_burst_sent','TX EOB'),('workers_stopped','工作线程退出')]:
        response['facts'].append({'name':label,'value':'已确认' if telemetry.get(key) is True else '未确认/无记录'})
    response['facts'].append({'name':'RX停止命令','value':'已确认' if telemetry.get('rx_stop_sent') or telemetry.get('rx_stop_command_sent') else '未确认/无记录'})
    response['facts'].append({'name':'处理窗口','value':manifest.get('processed_windows','无记录')})
    response['facts'].append({'name':'错误条数','value':len(manifest.get('errors',[]))})
    if sample.get('available'):
        response['facts'].extend([{'name':'IQ预览窗口RMS（dBFS估计）','value':round(sample['window_rms_dBFS_est'],3)},
                                  {'name':'IQ预览近满幅分量','value':sample['window_near_fullscale_components']}])
    if mode=='sweep':response['facts'].append({'name':'连续性依据','value':'分点软件索引；无全会话硬件时间戳'})
    response['chart_count']=sum(len(v) for v in response['groups'].values())
    return response

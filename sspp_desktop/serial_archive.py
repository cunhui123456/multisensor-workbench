"""温压／温度标定档案的只读索引、详情与封存文件导出。"""
import csv
import hashlib
import json
import math
from pathlib import Path
import uuid
import zipfile
from sspp_lab.controller import LabError, read_json
from sspp_experiments.temperature_pressure.corrections import load_corrections, correction_path, update_calibration_points

SERIAL_PREFIX='temperature_pressure_runs/'
ARTIFACTS=('manifest.json','samples.csv','raw_serial.bin','serial_events.jsonl',
           'voltage_response.csv','calibration_points.json','calibration_events.jsonl')


def serial_category(path, manifest):
    if manifest.get('temperature_calibration') or manifest.get('temperature_ramp') or (path/'calibration_points.json').is_file():
        return 'temperature_calibration'
    return 'temperature_pressure'


def ordinary_file(path):
    return path.is_file() and not path.is_symlink()


class SerialArchiveMixin:
    def history(self):
        rows=super().history()
        base=self.root/'Data'/'temperature_pressure_runs'
        if base.is_dir() and not base.is_symlink():
            for path in base.iterdir():
                if not path.is_dir() or path.is_symlink() or not ordinary_file(path/'manifest.json'):continue
                manifest=read_json(path/'manifest.json',{}) or {}
                if not manifest:continue
                rows.append({'key':SERIAL_PREFIX+path.name,'mode':'serial','category':serial_category(path,manifest),
                             'started_utc':manifest.get('started_utc',''),'status':manifest.get('status','unknown'),
                             'label':manifest.get('sample_name') or manifest.get('label') or path.name,
                             'sensor':manifest.get('protocol',''),'source':'serial_capture'})
        return sorted(rows,key=lambda r:r.get('started_utc',''),reverse=True)

    def details(self,key):
        if not key.startswith(SERIAL_PREFIX):return super().details(key)
        path=self.run_path(key)
        if path.is_symlink() or not ordinary_file(path/'manifest.json'):raise LabError('温压档案不是普通记录目录')
        manifest=read_json(path/'manifest.json',{}) or {}
        fields=('status','label','sample_name','notes','started_utc','ended_utc','protocol','channels','rows','raw_bytes',
                'stop_reason','serial_closed','workers_stopped','data_files_closed','limitations','warnings',
                'temperature_calibration','temperature_ramp','event_counts')
        metadata={k:manifest[k] for k in fields if k in manifest}
        metadata['errors']=[self.public_log(str(e)) for e in manifest.get('errors',[])]
        category=serial_category(path,manifest)
        points=[];skipped=0;count=0;last={}
        csv_path=path/'samples.csv'
        if ordinary_file(csv_path):
            with csv_path.open(newline='',encoding='utf-8-sig') as f:count=sum(1 for _ in csv.DictReader(f))
            maximum=getattr(self.settings,'max_display_points',20000)
            step=max(1,math.ceil(count/maximum))
            with csv_path.open(newline='',encoding='utf-8-sig') as f:
                for index,row in enumerate(csv.DictReader(f)):
                    try:
                        for field in ('host_elapsed_s','voltage_v','mcu_time_ms','time_segment'):
                            row[field]=float(row[field])
                            if not math.isfinite(row[field]):raise ValueError('nonfinite sample')
                        row['adc']=float(row['adc']) if row.get('adc') not in (None,'') else None
                    except (ValueError,TypeError,KeyError):skipped+=1;continue
                    last[row['channel']]=row
                    if index%step==0 and len(points)<maximum:points.append(row)
            for row in last.values():
                if row not in points:points.append(row)
            points.sort(key=lambda r:(r['host_elapsed_s'],r['channel']))
        calibration={}
        if ordinary_file(path/'calibration_points.json'):
            content=(path/'calibration_points.json').read_bytes()
            calibration=json.loads(content)
            try:
                ledger=load_corrections(self.root,key,content)
            except (ValueError,OSError) as error:
                raise LabError(str(error)) from error
            for point in calibration.get('points',[]):
                deletion=ledger['deleted_points'].get(str(point.get('id')))
                point.update(deleted=deletion is not None,deletion=deletion)
            metadata['deleted_calibration_points']=len(ledger['deleted_points'])
            metadata['calibration_correction_history']=ledger.get('history',[])
        metadata['display_skipped_rows']=skipped
        return {'key':key,'metadata':metadata,'files':[name for name in ARTIFACTS if ordinary_file(path/name)],'figures':[],
                'context':{'experiment':category,'source':'serial_capture'},
                'curve':{'mode':'serial','points':points,'stored_rows':count},
                'calibration_points':(calibration or {}).get('points',[])}

    def correct_calibration_points(self,key,point_ids,reason,restore=False):
        with self._comparison_operation():
            try:
                return update_calibration_points(self.root,key,point_ids,reason,restore=restore)
            except (ValueError,OSError) as error:
                raise LabError(str(error)) from error

    def export_bundle(self,key=None,destination=None,include_raw=False):
        if not isinstance(key,str) or not key.startswith(SERIAL_PREFIX):
            return super().export_bundle(key,destination,include_raw)
        path=self.run_path(key)
        if path.is_symlink() or not ordinary_file(path/'manifest.json'):raise LabError('温压档案不是普通记录目录')
        manifest=read_json(path/'manifest.json',{}) or {}
        if not all(manifest.get(k) is True for k in ('serial_closed','workers_stopped','data_files_closed')):
            raise LabError('请先停止温压采集并等待文件封存，再导出档案',409)
        target=Path(destination).expanduser()
        if not target.is_absolute() or target.suffix.lower()!='.zip' or not target.parent.is_dir() or target.is_symlink():
            raise LabError('请选择普通的绝对ZIP保存路径')
        if target.resolve().is_relative_to(path.resolve()):raise LabError('请保存到本次原始实验目录之外')
        with self.lock:
            if self.exporting:raise LabError('已有导出任务正在进行',409)
            self.exporting=True
        temporary=target.parent/('.'+target.name+'.'+uuid.uuid4().hex+'.tmp')
        expected={f['path']:f for f in manifest.get('files',[])}
        index={'schema':'sspp.serial_archive_bundle.v1','run_key':key,'files':[]}
        try:
            correction=None
            if ordinary_file(path/'calibration_points.json'):
                load_corrections(self.root,key,(path/'calibration_points.json').read_bytes())
                ledger_path=correction_path(self.root,key)
                if ledger_path.exists():correction=ledger_path.read_bytes()
            with zipfile.ZipFile(temporary,'w',compression=zipfile.ZIP_DEFLATED) as archive:
                for name in ARTIFACTS:
                    source=path/name
                    if not ordinary_file(source):continue
                    digest=hashlib.sha256();size=0
                    with source.open('rb') as reader,archive.open('results/'+name,'w') as writer:
                        while True:
                            chunk=reader.read(1024*1024)
                            if not chunk:break
                            digest.update(chunk);size+=len(chunk);writer.write(chunk)
                    saved=expected.get(name)
                    if saved and (saved['sha256']!=digest.hexdigest() or saved['bytes']!=size):
                        raise LabError('封存文件校验失败：'+name)
                    index['files'].append({'path':'results/'+name,'sha256':digest.hexdigest(),'bytes':size})
                if correction is not None:
                    archive.writestr('corrections.json',correction)
                    index['files'].append({'path':'corrections.json','sha256':hashlib.sha256(correction).hexdigest(),'bytes':len(correction)})
                    note='原始标定文件保留不变；corrections.json 的 deleted_points 为已删除的错误标定区间，不参与组合分析。可在实验档案恢复。'.encode('utf-8')
                    archive.writestr('CORRECTIONS_README.txt',note)
                    index['files'].append({'path':'CORRECTIONS_README.txt','sha256':hashlib.sha256(note).hexdigest(),'bytes':len(note)})
                archive.writestr('bundle_index.json',json.dumps(index,ensure_ascii=False,indent=2))
            temporary.replace(target)
            return {'path':str(target),'count':len(index['files'])+1,'files':index['files'],'warnings':[]}
        finally:
            if temporary.exists():temporary.unlink()
            with self.lock:self.exporting=False

"""Reversible calibration-point corrections; captured records remain immutable."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile


def correction_path(root, key):
    relative = Path(key)
    if relative.is_absolute() or len(relative.parts) != 2 or relative.parts[0] != 'temperature_pressure_runs' or relative.name in ('.', '..'):
        raise ValueError('温度标定记录路径无效')
    data = Path(root) / 'Data'
    target = data / 'corrections' / 'temperature_pressure_runs' / (relative.name + '.json')
    for path in (data, target.parent.parent, target.parent, target):
        if path.is_symlink():
            raise ValueError('标定修正不能使用符号链接路径')
    return target


def load_corrections(root, key, calibration_bytes):
    path = correction_path(root, key)
    digest = hashlib.sha256(calibration_bytes).hexdigest()
    if not path.exists():
        return {'version': 1, 'run_key': key, 'source_calibration_sha256': digest,
                'deleted_points': {}, 'history': []}
    if not path.is_file():
        raise ValueError('标定修正记录不是普通文件')
    result = json.loads(path.read_text(encoding='utf-8'))
    if (not isinstance(result, dict) or type(result.get('version')) is not int or result['version'] != 1 or result.get('run_key') != key
            or result.get('source_calibration_sha256') != digest
            or not isinstance(result.get('deleted_points'), dict) or not isinstance(result.get('history', []), list)):
        raise ValueError('标定修正记录与原始标定文件不匹配')
    point_ids = {str(point['id']) for point in json.loads(calibration_bytes).get('points', [])}
    for ident, entry in result['deleted_points'].items():
        if ident not in point_ids or not isinstance(entry, dict) or not isinstance(entry.get('reason'),str) or not entry['reason'].strip() or not isinstance(entry.get('deleted_utc'),str):
            raise ValueError('标定修正点或原因无效')
        if datetime.fromisoformat(entry['deleted_utc'].replace('Z','+00:00')).tzinfo is None:
            raise ValueError('标定修正时间必须包含时区')
    return result


def update_calibration_points(root, key, point_ids, reason, restore=False):
    target = correction_path(root, key)
    run = Path(root) / 'Data' / key
    if run.parent.is_symlink() or run.is_symlink():
        raise ValueError('标定来源不能使用符号链接')
    for name in ('manifest.json', 'calibration_points.json'):
        if not (run/name).is_file() or (run/name).is_symlink():
            raise ValueError('缺少普通标定来源文件')
    manifest_bytes = (run/'manifest.json').read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest.get('status') != 'complete' or not all(manifest.get(k) is True for k in ('serial_closed', 'workers_stopped', 'data_files_closed')):
        raise ValueError('请等待温度标定停止并封存后再修正数据')
    content = (run/'calibration_points.json').read_bytes()
    for entry in manifest.get('files', []):
        if entry.get('path') == 'calibration_points.json' and (entry.get('sha256') != hashlib.sha256(content).hexdigest() or entry.get('bytes') != len(content)):
            raise ValueError('原始标定文件封存校验失败')
    points = {p.get('id'): p for p in json.loads(content).get('points', [])}
    if not isinstance(point_ids, (list, tuple)) or not point_ids or any(type(i) is not int or i not in points or points[i].get('status') != 'saved' for i in point_ids):
        raise ValueError('请选择有效的已保存标定点')
    reason = str(reason or '').strip()
    if not reason or len(reason) > 1000:
        raise ValueError('请填写 1–1000 字的修正原因')
    ledger = load_corrections(root, key, content)
    ids = list(dict.fromkeys(point_ids))
    if any((restore and str(ident) not in ledger['deleted_points']) or
           (not restore and str(ident) in ledger['deleted_points']) for ident in ids):
        raise ValueError('所选点已发生变化，请刷新档案后重试')
    now = datetime.now(timezone.utc).isoformat()
    for ident in ids:
        if restore:
            del ledger['deleted_points'][str(ident)]
        else:
            ledger['deleted_points'][str(ident)] = {'reason': reason, 'deleted_utc': now}
    ledger.setdefault('history', []).append({'action': 'restore' if restore else 'delete',
                                           'point_ids': ids, 'reason': reason, 'utc': now})
    # Reject changed sources instead of attaching corrections to a new capture.
    if (run/'calibration_points.json').read_bytes() != content or (run/'manifest.json').read_bytes() != manifest_bytes:
        raise ValueError('标定来源已变化，请刷新档案后重试')
    target.parent.mkdir(parents=True, exist_ok=True)
    correction_path(root, key)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=target.parent, prefix='.correction-', suffix='.tmp', delete=False) as output:
        temporary = Path(output.name)
        json.dump(ledger, output, ensure_ascii=False, indent=2, allow_nan=False)
        output.write('\n')
    try:
        temporary.replace(target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {'key': key, 'point_ids': ids, 'action': 'restore' if restore else 'delete',
            'correction_path': str(target)}

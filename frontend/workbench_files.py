"""Read only a ledger-referenced, content-addressed collaboration delivery."""
import base64
import hashlib
import re
from pathlib import Path

from mewcode.kernel.events import digest
from mewcode.orchestration.process_files import MAX_FILE_BYTES, MAX_PREVIEW_BYTES


def collaborative_file(snapshot, file_id, work_dir):
    if not file_id.startswith('collab:'):
        return None
    artifact = next((artifact for attempt in (snapshot.get('collaboration') or {}).get('attempts', {}).values()
        for index, artifact in enumerate(attempt.get('report', {}).get('artifacts', []))
        if artifact.get('kind') == 'file' and file_id == 'collab:' + digest([attempt['id'], index, artifact.get('sha256')])), None)
    if artifact is None:
        return None
    sha = artifact.get('sha256', '')
    if not re.fullmatch(r'[0-9a-f]{64}', sha):
        raise ValueError('产物版本指纹无效')
    root = Path(work_dir).resolve()
    path = (root / '.mewcode' / 'collaboration' / 'artifacts' / sha).resolve()
    if not path.is_relative_to(root) or path != (root / str(artifact.get('snapshot_path', ''))).resolve():
        raise ValueError('固定产物不属于当前工作区')
    with path.open('rb') as stream:
        data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError('固定产物超过预览上限（8 MiB）')
    if hashlib.sha256(data).hexdigest() != sha:
        raise ValueError('固定产物已损坏，不能展示为原交付版本')
    sample = data[:MAX_PREVIEW_BYTES]
    try:
        preview = sample.decode('utf-8-sig') if b'\0' not in sample else '二进制文件，请下载查看。'
    except UnicodeError:
        preview = '二进制文件，请下载查看。'
    return dict(id=file_id, sha256=sha, size=len(data), preview=preview, preview_type='text',
                preview_truncated=len(data) > MAX_PREVIEW_BYTES, content_b64=base64.b64encode(data).decode('ascii'))

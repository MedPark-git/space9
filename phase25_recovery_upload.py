import hashlib, os
from pathlib import Path
from flask import Response, abort, request
import app as pkg
import phase25_recovery as recovery

app=pkg.app;core=pkg.core;REGISTERED=False

def _auth():
    token=os.getenv('PHASE25_RECOVERY_TOKEN')
    if not token or request.args.get('token')!=token: abort(404)

def register():
    global REGISTERED
    if REGISTERED:return
    def upload():
        _auth();kind=(request.form.get('kind') or '').strip();f=request.files.get('file')
        if kind not in recovery.FILES or not f: return Response('ERROR invalid kind/file',status=400,mimetype='text/plain')
        raw=f.read();expected=recovery.FILES[kind]['sha'];actual=hashlib.sha256(raw).hexdigest()
        if actual!=expected:return Response(f'ERROR sha mismatch actual={actual}',status=400,mimetype='text/plain')
        recovery.ROOT.mkdir(parents=True,exist_ok=True);p=recovery.ROOT/f'{kind}.xls';p.write_bytes(raw)
        parsed=core.parse_file(p,kind)
        return Response(f"OK kind={kind} sha={actual} period={parsed['query_start_date']}~{parsed['query_end_date']} rows={len(parsed['rows'])} review={len(parsed['reviews'])} error={len(parsed['errors'])}",mimetype='text/plain')
    app.add_url_rule('/maintenance/phase25-recovery-upload','phase25_recovery_upload',upload,methods=['POST'])
    REGISTERED=True

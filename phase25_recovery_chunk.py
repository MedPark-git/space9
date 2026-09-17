import os
from pathlib import Path
from flask import Response, abort, request
import app as pkg

app=pkg.app;REGISTERED=False
CHUNK_DIR=Path('/app/user_data/phase25_chunks')

def _auth():
    token=os.getenv('PHASE25_RECOVERY_TOKEN')
    if not token or request.args.get('token')!=token:abort(404)

def register():
    global REGISTERED
    if REGISTERED:return
    def receive():
        _auth();kind=(request.args.get('kind') or '').strip();idx=request.args.get('index');data=request.args.get('data') or ''
        if kind not in {'krw','fx'} or not idx or not idx.isdigit() or not data:return Response('ERROR',status=400,mimetype='text/plain')
        CHUNK_DIR.mkdir(parents=True,exist_ok=True);p=CHUNK_DIR/f'{kind}_{int(idx):03d}.txt';p.write_text(data,encoding='utf-8');return Response(f'OK {kind} {idx} {len(data)}',mimetype='text/plain')
    app.add_url_rule('/maintenance/phase25-recovery-chunk','phase25_recovery_chunk',receive,methods=['GET'])
    REGISTERED=True

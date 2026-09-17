import base64, hashlib, hmac, lzma, os
from pathlib import Path
import phase25_recovery as r

PAYLOAD_DIR=Path(__file__).resolve().parent/'recovery_payload'
CHUNK_DIR=Path('/app/user_data/phase25_chunks')

def _decrypt_blob(enc,kind):
    key_b64=os.getenv('PHASE25_RECOVERY_KEY')
    if not key_b64:return None
    blob=base64.urlsafe_b64decode(enc.encode()) if ('-' in enc or '_' in enc) else base64.b64decode(enc)
    nonce,tag,cipher=blob[:16],blob[16:48],blob[48:]
    key=base64.b64decode(key_b64);calc=hmac.new(key,nonce+cipher,hashlib.sha256).digest()
    if not hmac.compare_digest(tag,calc):raise RuntimeError(f'{kind} encrypted payload authentication failed')
    plain=bytearray(len(cipher))
    for i in range(0,len(cipher),32):
        ks=hmac.new(key,nonce+(i//32).to_bytes(8,'big'),hashlib.sha256).digest();block=cipher[i:i+32]
        plain[i:i+len(block)]=bytes(a^b for a,b in zip(block,ks))
    return lzma.decompress(bytes(plain))

def _chunk_payload(kind):
    files=sorted(CHUNK_DIR.glob(f'{kind}_*.txt'))
    if not files:return None
    return ''.join(''.join(p.read_text(encoding='utf-8').split()) for p in files)

def _repo_payload(kind):
    files=sorted(PAYLOAD_DIR.glob(f'{kind}_*.txt'),key=lambda p:int(p.stem.split('_')[-1]))
    if not files:return None
    return ''.join(''.join(p.read_text(encoding='utf-8').split()) for p in files)

def apply():
    def materialize(kind):
        c=r.FILES[kind];r.ROOT.mkdir(parents=True,exist_ok=True);p=r.ROOT/f'{kind}.xls'
        if p.exists() and hashlib.sha256(p.read_bytes()).hexdigest()==c['sha']:return p
        enc=_chunk_payload(kind) or _repo_payload(kind)
        raw=_decrypt_blob(enc,kind) if enc else None
        if raw is None:
            parts=[]
            for i in range(100):
                v=os.getenv(c['prefix']+str(i))
                if v:parts.append(v)
            old=''.join(parts)
            if not old:raise RuntimeError(f'{kind} source file missing')
            raw=lzma.decompress(base64.b64decode(old))
        if hashlib.sha256(raw).hexdigest()!=c['sha']:raise RuntimeError(f'{kind} sha mismatch')
        p.write_bytes(raw);return p
    r._materialize=materialize

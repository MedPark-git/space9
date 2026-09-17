import base64, hashlib, lzma, os
import phase25_recovery as r

def apply():
    def materialize(kind):
        c=r.FILES[kind];r.ROOT.mkdir(parents=True,exist_ok=True);p=r.ROOT/f'{kind}.xls'
        if p.exists() and hashlib.sha256(p.read_bytes()).hexdigest()==c['sha']:
            return p
        parts=[]
        for i in range(100):
            v=os.getenv(c['prefix']+str(i))
            if v:parts.append(v)
        enc=''.join(parts)
        if not enc:raise RuntimeError(f'{kind} source file missing')
        raw=lzma.decompress(base64.b64decode(enc))
        if hashlib.sha256(raw).hexdigest()!=c['sha']:raise RuntimeError(f'{kind} sha mismatch')
        p.write_bytes(raw);return p
    r._materialize=materialize

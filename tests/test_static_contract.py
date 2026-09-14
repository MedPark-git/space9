from pathlib import Path
import ast
ROOT=Path(__file__).resolve().parents[1]
def test_python_syntax(): ast.parse((ROOT/'app.py').read_text(encoding='utf-8'))
def test_required_files_exist():
    for name in ['app.py','requirements.txt','runtime.txt','Procfile','.gitignore','.env.example','README.md','RESTORE_GUIDE_KO.md','BACKUP_METADATA.json','templates','static','migrations','tests','user_data']: assert (ROOT/name).exists(),name
def test_no_sqlite_production_fallback():
    text=(ROOT/'app.py').read_text(encoding='utf-8').lower(); assert 'sqlite:///' not in text; assert 'db_username' not in text
def test_start_command(): assert 'gunicorn --workers 2 --threads 4 --timeout 120 --bind 0.0.0.0:${PORT:-8000} app:app' in (ROOT/'Procfile').read_text(encoding='utf-8')
def test_bootstrap_password_not_hardcoded():
    for path in ROOT.rglob('*'):
        if path.is_file() and path.suffix!='.pyc':
            try: data=path.read_text(encoding='utf-8')
            except UnicodeDecodeError: continue
            for line in data.splitlines():
                if line.strip().startswith('BOOTSTRAP_ADMIN_PASSWORD='): assert line.strip()=='BOOTSTRAP_ADMIN_PASSWORD=',f'bootstrap secret assigned in {path}'

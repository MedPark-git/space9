from pathlib import Path
import traceback


def safe_register():
    try:
        from phase21_fx_routes import register
        register()
        Path('/app/user_data').mkdir(parents=True, exist_ok=True)
        Path('/app/user_data/phase21_debug.txt').write_text('OK\n', encoding='utf-8')
    except Exception:
        Path('/app/user_data').mkdir(parents=True, exist_ok=True)
        Path('/app/user_data/phase21_debug.txt').write_text(traceback.format_exc(), encoding='utf-8')

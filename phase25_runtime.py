"""Phase 2.5 production runtime registration.
Startup is schema/route registration only; business data changes happen only on explicit import-confirm triggers.
"""

def register():
    import phase2_feature as p2
    original_seed = p2._ensure_category_seed
    original_annotations = p2._ensure_annotations
    original_daily = p2._refresh_daily_checks
    try:
        p2._ensure_category_seed = lambda: 0
        p2._ensure_annotations = lambda: 0
        p2._refresh_daily_checks = lambda *args, **kwargs: 0
        p2.register()
    finally:
        p2._ensure_category_seed = original_seed
        p2._ensure_annotations = original_annotations
        p2._refresh_daily_checks = original_daily

    from phase2_enhancements import register as register_enhancements
    register_enhancements()

    import phase25_core as p25
    import app as pkg
    with pkg.app.app_context():
        p25.migrate_schema()
        p25.assert_category_master_unchanged()
    p25.install_patches()

    from phase21_fx_priority import apply as apply_fx_priority
    apply_fx_priority()
    from phase21_migrate_guard_fix import apply as apply_fx_migrate_guard
    apply_fx_migrate_guard()
    from phase21_fx_routes import register as register_fx
    register_fx()

    from phase25_analysis import register as register_analysis
    register_analysis()

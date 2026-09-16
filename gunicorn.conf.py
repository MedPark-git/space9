def post_worker_init(worker):
    from phase2_feature import register as register_phase2
    register_phase2()
    from phase2_enhancements import register as register_phase2_enhancements
    register_phase2_enhancements()
    from phase21_fx_priority import apply as apply_phase21_fx_priority
    apply_phase21_fx_priority()
    from phase21_fx_routes import register as register_phase21_fx
    register_phase21_fx()
    from phase21_historical_backfill import register as register_phase21_historical_backfill
    register_phase21_historical_backfill()

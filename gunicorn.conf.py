def post_worker_init(worker):
    from phase2_feature import register as register_phase2
    register_phase2()
    from phase2_enhancements import register as register_phase2_enhancements
    register_phase2_enhancements()
    from phase21_debug import safe_register
    safe_register()
    from phase21_validation import register as register_phase21_validation
    register_phase21_validation()

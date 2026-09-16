def post_worker_init(worker):
    from phase2_feature import register as register_phase2
    register_phase2()
    from phase2_enhancements import register as register_phase2_enhancements
    register_phase2_enhancements()

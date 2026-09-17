def post_worker_init(worker):
    from phase25_runtime import register as register_phase25
    register_phase25()
    from phase25_recovery import register as register_phase25_recovery
    register_phase25_recovery()

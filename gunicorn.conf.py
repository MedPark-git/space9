def post_worker_init(worker):
    from phase2_feature import register as register_phase2
    register_phase2()

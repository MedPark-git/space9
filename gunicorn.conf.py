def post_worker_init(worker):
    from phase25_runtime import register as register_phase25
    register_phase25()
    from phase25_recovery import register as register_phase25_recovery
    register_phase25_recovery()
    from phase25_recovery_payload_patch import apply as apply_recovery_payload_patch
    apply_recovery_payload_patch()
    from phase25_recovery_upload import register as register_phase25_recovery_upload
    register_phase25_recovery_upload()

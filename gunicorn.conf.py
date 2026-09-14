def post_worker_init(worker):
    from data_patch_wrapper import register as apply_data_patch
    from reconciliation_feature import register as register_reconciliation
    apply_data_patch()
    register_reconciliation()

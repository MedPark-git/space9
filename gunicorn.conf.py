def post_worker_init(worker):
    from data_patch_wrapper import register as apply_data_patch
    from opening_balance_zero_patch import register as apply_opening_balance_patch
    from reconciliation_feature import register as register_reconciliation
    from reupload_validation_once import run_validation
    apply_data_patch()
    apply_opening_balance_patch()
    register_reconciliation()
    run_validation()

def post_worker_init(worker):
    from data_patch_wrapper import register as apply_data_patch
    from opening_balance_zero_patch import register as apply_opening_balance_patch
    from bank_transaction_reset import register as reset_bank_transactions
    from reconciliation_feature import register as register_reconciliation
    apply_data_patch()
    apply_opening_balance_patch()
    reset_bank_transactions()
    register_reconciliation()

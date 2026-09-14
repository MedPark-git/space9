def post_worker_init(worker):
    from opening_balance_feature import register
    register()

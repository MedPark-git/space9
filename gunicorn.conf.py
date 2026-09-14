def post_worker_init(worker):
    from reconciliation_feature import register
    register()

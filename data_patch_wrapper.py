def register():
    import app as pkg
    from data_patch_feature import register as apply_patch
    with pkg.app.app_context():
        apply_patch()

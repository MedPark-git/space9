from sqlalchemy import func, text
import app as pkg
import phase2_feature as p2
core=pkg.core;db=pkg.db;app=pkg.app

def validate_or_raise():
    """Read-only Phase 2.5 validation gate. Never changes business data."""
    with app.app_context():
        krw_batches=core.ImportBatch.query.filter_by(file_type='krw',status='confirmed').count()
        fx_used=core.BankAccount.query.filter(core.BankAccount.currency!='KRW').count()>0
        fx_batches=core.ImportBatch.query.filter_by(file_type='fx',status='confirmed').count()
        tx_count=core.BankTransaction.query.count()
        tx_min,tx_max=db.session.query(func.min(core.BankTransaction.transaction_date),func.max(core.BankTransaction.transaction_date)).one()
        opening=core.OpeningBalance.query.filter_by(base_date=p2.BASE_DATE).count()
        orphan=db.session.execute(text('SELECT COUNT(*) FROM bank_transactions t LEFT JOIN bank_accounts a ON a.id=t.account_id WHERE a.id IS NULL')).scalar_one()
        bad_batches=[]
        for b in core.ImportBatch.query.filter(core.ImportBatch.file_type.in_(['krw','fx']),core.ImportBatch.status=='confirmed').all():
            actual=core.BankTransaction.query.filter_by(import_batch_id=b.id).count()
            if actual!=b.new_rows: bad_batches.append(f'{b.batch_code}:new={b.new_rows},actual={actual}')
        ann=p2.TransactionAnnotation.query.count();cats=p2.TransactionCategory.query.filter_by(is_active=True).count();other=p2.TransactionCategory.query.filter_by(name='기타비용(기술)',is_active=True).count();mmt=core.MmtTransaction.query.count()
        problems=[]
        if krw_batches<=0:problems.append('KRW confirmed Import Batch 없음')
        if fx_used and fx_batches<=0:problems.append('FX confirmed Import Batch 없음')
        if tx_count<=0:problems.append('BankTransaction 0건')
        if tx_min is None or tx_max is None:problems.append('거래기간 없음')
        if opening<=0:problems.append('OpeningBalance 없음')
        if orphan:problems.append(f'계좌연결 오류 {orphan}건')
        if bad_batches:problems.append('Batch new_rows 불일치: '+';'.join(bad_batches))
        if ann!=tx_count:problems.append(f'Annotation 불일치 tx={tx_count} ann={ann}')
        if cats!=33 or other!=1:problems.append(f'거래분류 Master 오류 count={cats} 기타기술={other}')
        if mmt!=368:problems.append(f'MMT 보존건수 오류 {mmt}')
        db.session.rollback()
        if problems: raise RuntimeError('PHASE25_VALIDATION_FAILED | '+' | '.join(problems))
        print(f'PHASE25_VALIDATION_OK krw_batches={krw_batches} fx_batches={fx_batches} tx={tx_count} period={tx_min}~{tx_max} opening={opening} mmt={mmt}',flush=True)
        return {'krw_batches':krw_batches,'fx_batches':fx_batches,'transactions':tx_count,'min':str(tx_min),'max':str(tx_max),'opening':opening,'mmt':mmt}

import hashlib
import struct
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

FREESECT = 0xFFFFFFFF
ENDOFCHAIN = 0xFFFFFFFE

class EBranchFormatError(ValueError):
    pass

EXPECTED_HEADERS = {
    'krw': {'사업장','은행','계좌번호','계좌별칭','거래일자','입금액','출금액','잔액','적요','적요1','적요2','취급점','지로코드','거래시간','타점권','계좌과목','연동계좌번호','상대계좌예금주명','거래월','CMS번호'},
    'fx': {'사업장','은행','계좌번호','통화','적요','일자','입금액','환율적용입금','출금액','환율적용출금','잔액','적용환율','환율적용잔액','계좌별칭','취급점','적요1','적요2','입금인명'},
    'mmt': {'사업장','은행','계좌번호','계좌별칭','일자','입금액','출금액','잔액','적요','적요1','적요2','취급점','지로코드','거래시간','거래회차','거래월분'},
    'loan': {'사업장','은행','계좌번호','통화','대출명','일자','거래구분','거래금액','이자금액','거래원금','대출잔액','대출이율','이자계산시작일','적용환율','대출원화액','이자계산종료일','이자계산기간','이자상세1','이자상세2','이자상세3','비고1','비고2'},
}

def _cfb_workbook_stream(path):
    raw = Path(path).read_bytes()
    if raw[:8] != bytes.fromhex('D0CF11E0A1B11AE1'):
        raise EBranchFormatError('구형 Excel(.xls) BIFF 파일이 아닙니다.')
    major = struct.unpack_from('<H', raw, 0x1A)[0]
    sector_size = 1 << struct.unpack_from('<H', raw, 0x1E)[0]
    mini_sector_size = 1 << struct.unpack_from('<H', raw, 0x20)[0]
    num_fat = struct.unpack_from('<I', raw, 0x2C)[0]
    first_dir = struct.unpack_from('<I', raw, 0x30)[0]
    mini_cutoff = struct.unpack_from('<I', raw, 0x38)[0]
    first_minifat = struct.unpack_from('<I', raw, 0x3C)[0]
    num_minifat = struct.unpack_from('<I', raw, 0x40)[0]
    first_difat = struct.unpack_from('<I', raw, 0x44)[0]
    num_difat = struct.unpack_from('<I', raw, 0x48)[0]
    difat = [x for x in struct.unpack_from('<109I', raw, 0x4C) if x not in (FREESECT, ENDOFCHAIN)]
    sid = first_difat
    for _ in range(num_difat):
        if sid in (FREESECT, ENDOFCHAIN): break
        off = (sid + 1) * sector_size
        vals = list(struct.unpack_from(f'<{sector_size // 4}I', raw, off))
        difat.extend(x for x in vals[:-1] if x not in (FREESECT, ENDOFCHAIN)); sid = vals[-1]
    fat = []
    for fsid in difat[:num_fat]:
        off = (fsid + 1) * sector_size; fat.extend(struct.unpack_from(f'<{sector_size // 4}I', raw, off))
    def read_chain(start, size=None):
        out = bytearray(); seen = set(); cur = start
        while cur not in (ENDOFCHAIN, FREESECT) and cur < len(fat):
            if cur in seen: raise EBranchFormatError('Excel 내부 FAT 체인이 손상되었습니다.')
            seen.add(cur); off = (cur + 1) * sector_size; out.extend(raw[off:off + sector_size]); cur = fat[cur]
            if size is not None and len(out) >= size: break
        return bytes(out[:size] if size is not None else out)
    directory = read_chain(first_dir); entries = []
    for i in range(0, len(directory), 128):
        ent = directory[i:i + 128]
        if len(ent) < 128: break
        name_len = struct.unpack_from('<H', ent, 64)[0]
        name = ent[:max(0, name_len - 2)].decode('utf-16le', 'ignore') if name_len >= 2 else ''
        obj_type = ent[66]; start = struct.unpack_from('<I', ent, 116)[0]; size = struct.unpack_from('<Q', ent, 120)[0]
        if major == 3: size &= 0xFFFFFFFF
        entries.append((name, obj_type, start, size))
    target = next((e for e in entries if e[0] in ('Workbook', 'Book') and e[1] == 2), None)
    if not target: raise EBranchFormatError('Excel Workbook 스트림을 찾을 수 없습니다.')
    _, _, start, size = target
    if size >= mini_cutoff: return read_chain(start, size)
    root = next((e for e in entries if e[1] == 5), None)
    if not root: raise EBranchFormatError('Excel Root 스트림을 찾을 수 없습니다.')
    mini_stream = read_chain(root[2], root[3]); minifat_bytes = read_chain(first_minifat, num_minifat * sector_size)
    minifat = list(struct.unpack_from(f'<{len(minifat_bytes)//4}I', minifat_bytes, 0)) if minifat_bytes else []
    out = bytearray(); cur = start; seen = set()
    while cur not in (ENDOFCHAIN, FREESECT) and cur < len(minifat):
        if cur in seen: raise EBranchFormatError('Excel MiniFAT 체인이 손상되었습니다.')
        seen.add(cur); off = cur * mini_sector_size; out.extend(mini_stream[off:off + mini_sector_size]); cur = minifat[cur]
        if len(out) >= size: break
    return bytes(out[:size])

def _records(buf, start=0):
    pos = start
    while pos + 4 <= len(buf):
        rid, length = struct.unpack_from('<HH', buf, pos); end = pos + 4 + length
        if end > len(buf): break
        yield pos, rid, buf[pos + 4:end]; pos = end

def _label(data):
    row, col, _xf, cch = struct.unpack_from('<HHHH', data, 0); flags = data[8]; off = 9
    if flags & 0x08: off += 2
    if flags & 0x04: off += 4
    size = cch * (2 if flags & 1 else 1)
    value = data[off:off + size].decode('utf-16le' if flags & 1 else 'latin1', 'replace')
    return row, col, value

def _rk_value(rk):
    is_div100 = rk & 1; is_int = rk & 2
    if is_int: value = struct.unpack('<i', struct.pack('<I', rk))[0] >> 2
    else: value = struct.unpack('<d', struct.pack('<Q', (rk & 0xFFFFFFFC) << 32))[0]
    return value / 100 if is_div100 else value

def read_legacy_xls(path):
    workbook = _cfb_workbook_stream(path); sheet_offset = None
    for _pos, rid, data in _records(workbook):
        if rid == 0x0085: sheet_offset = struct.unpack_from('<I', data, 0)[0]; break
    if sheet_offset is None: raise EBranchFormatError('Excel 시트를 찾을 수 없습니다.')
    cells = {}
    for _pos, rid, data in _records(workbook, sheet_offset):
        if rid == 0x0204:
            r, c, v = _label(data); cells[(r, c)] = v
        elif rid == 0x0203:
            r, c, _xf = struct.unpack_from('<HHH', data, 0); cells[(r, c)] = str(struct.unpack_from('<d', data, 6)[0])
        elif rid == 0x027E:
            r, c, _xf, rk = struct.unpack_from('<HHHI', data, 0); cells[(r, c)] = str(_rk_value(rk))
        elif rid == 0x00BD:
            r, first_col = struct.unpack_from('<HH', data, 0); last_col = struct.unpack_from('<H', data, len(data) - 2)[0]
            for idx, col in enumerate(range(first_col, last_col + 1)):
                base = 4 + idx * 6; rk = struct.unpack_from('<I', data, base + 2)[0]; cells[(r, col)] = str(_rk_value(rk))
        elif rid == 0x000A: break
    if not cells: raise EBranchFormatError('Excel 데이터 셀을 읽지 못했습니다.')
    max_row = max(r for r, _ in cells); max_col = max(c for _, c in cells)
    return [[str(cells.get((r, c), '')).strip() for c in range(max_col + 1)] for r in range(max_row + 1)]

def _dec(value):
    s = str(value or '').strip().replace(',', '')
    if s in ('', '-'): return Decimal('0')
    negative = s.startswith('(') and s.endswith(')')
    if negative: s = s[1:-1]
    try: d = Decimal(s)
    except InvalidOperation as exc: raise ValueError(f'금액 형식 오류: {value}') from exc
    return -d if negative else d

def _date(value):
    s = str(value or '').strip()
    for fmt in ('%Y-%m-%d', '%Y.%m.%d', '%Y/%m/%d'):
        try: return datetime.strptime(s, fmt).date()
        except ValueError: pass
    raise ValueError(f'일자 형식 오류: {value}')

def _time(value):
    s = str(value or '').strip()
    if not s: return None
    for fmt in ('%H:%M:%S', '%H:%M'):
        try: return datetime.strptime(s, fmt).time()
        except ValueError: pass
    raise ValueError(f'시간 형식 오류: {value}')

def _hash(parts):
    normalized = '|'.join('' if v is None else str(v).strip() for v in parts)
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()

def _headers(row): return {str(v).strip(): i for i, v in enumerate(row) if str(v).strip()}

def detect_type(rows):
    if not rows: raise EBranchFormatError('빈 파일입니다.')
    h = set(_headers(rows[0])); file_type = None
    if {'대출명', '거래구분', '대출잔액', '대출이율'} <= h: file_type = 'loan'
    elif {'통화', '환율적용입금', '환율적용출금', '적용환율'} <= h: file_type = 'fx'
    elif {'거래일자', '계좌과목', '거래시간', 'CMS번호'} <= h: file_type = 'krw'
    elif {'계좌별칭', '일자', '거래시간', '거래회차', '거래월분'} <= h and '대출명' not in h: file_type = 'mmt'
    if not file_type: raise EBranchFormatError('기존 등록된 기업은행 파일 형식과 일치하지 않습니다. 파일 구조 확인이 필요합니다.')
    expected = EXPECTED_HEADERS[file_type]
    if h != expected:
        missing = sorted(expected - h); extra = sorted(h - expected); detail = []
        if missing: detail.append('누락 컬럼: ' + ', '.join(missing))
        if extra: detail.append('추가 컬럼: ' + ', '.join(extra))
        raise EBranchFormatError('기존 등록된 기업은행 파일 형식과 일치하지 않습니다. 파일 구조 확인이 필요합니다. ' + ' / '.join(detail))
    return file_type

def _raw(row, headers): return {name: row[idx] if idx < len(row) else '' for name, idx in headers.items()}

def parse_file(path, expected_type=None):
    rows = read_legacy_xls(path); file_type = detect_type(rows)
    if expected_type and expected_type != file_type:
        names = {'krw':'원화 거래내역','fx':'외화 거래내역','mmt':'MMT','loan':'대출내역'}
        raise EBranchFormatError(f'선택한 업로드 유형({names.get(expected_type, expected_type)})과 실제 파일 유형({names.get(file_type, file_type)})이 다릅니다.')
    headers = _headers(rows[0]); structure_hash = hashlib.sha256('|'.join(sorted(headers)).encode('utf-8')).hexdigest(); parsed, errors, reviews = [], [], []
    date_key = '거래일자' if file_type == 'krw' else '일자'
    for idx, row in enumerate(rows[1:], start=2):
        raw = _raw(row, headers); account = raw.get('계좌번호', '').strip(); date_text = raw.get(date_key, '').strip()
        if account and not date_text and account not in ('261 건', '4580 건', '368 건', '34 건') and not account.endswith(' 건'):
            reviews.append({'source_row_number': idx, 'message': '거래일자 없음 - 거래로 등록하지 않고 확인 필요', 'raw': raw}); continue
        if not account or not date_text or not date_text[:4].isdigit(): continue
        try:
            tx_date = _date(date_text); common = {'source_row_number': idx, 'raw': raw, 'bank': raw.get('은행','').strip(), 'account_number': account, 'transaction_date': tx_date.isoformat()}
            if file_type == 'krw':
                dep, wd, bal = _dec(raw.get('입금액')), _dec(raw.get('출금액')), _dec(raw.get('잔액')); tx_time = _time(raw.get('거래시간'))
                item = {**common, 'currency':'KRW', 'account_name':raw.get('계좌별칭','').strip(), 'transaction_time':tx_time.isoformat() if tx_time else None, 'deposit':str(dep), 'withdrawal':str(wd), 'balance':str(bal), 'description':raw.get('적요','').strip(), 'detail1':raw.get('적요1','').strip(), 'detail2':raw.get('적요2','').strip(), 'branch':raw.get('취급점','').strip(), 'counter_account':raw.get('연동계좌번호','').strip(), 'counterparty':raw.get('상대계좌예금주명','').strip(), 'account_subject':raw.get('계좌과목','').strip(), 'cms_number':raw.get('CMS번호','').strip()}
                item['transaction_hash'] = _hash([account,'KRW',tx_date.isoformat(),item['transaction_time'],dep,wd,bal,item['description']])
            elif file_type == 'fx':
                dep, wd, bal = _dec(raw.get('입금액')), _dec(raw.get('출금액')), _dec(raw.get('잔액')); curr = raw.get('통화','').strip().upper()
                if not curr: raise ValueError('통화 누락')
                item = {**common, 'currency':curr, 'account_name':raw.get('계좌별칭','').strip(), 'transaction_time':None, 'deposit':str(dep), 'withdrawal':str(wd), 'balance':str(bal), 'description':raw.get('적요','').strip(), 'detail1':raw.get('적요1','').strip(), 'detail2':raw.get('적요2','').strip(), 'branch':raw.get('취급점','').strip(), 'counter_account':'', 'counterparty':raw.get('입금인명','').strip(), 'account_subject':'', 'cms_number':''}
                item['transaction_hash'] = _hash([account,curr,tx_date.isoformat(),None,dep,wd,bal,item['description']])
            elif file_type == 'mmt':
                dep, wd, bal = _dec(raw.get('입금액')), _dec(raw.get('출금액')), _dec(raw.get('잔액')); tx_time = _time(raw.get('거래시간'))
                item = {**common, 'currency':'KRW', 'account_name':raw.get('계좌별칭','').strip(), 'transaction_time':tx_time.isoformat() if tx_time else None, 'deposit':str(dep), 'withdrawal':str(wd), 'balance':str(bal), 'description':raw.get('적요','').strip(), 'detail1':raw.get('적요1','').strip(), 'detail2':raw.get('적요2','').strip(), 'branch':raw.get('취급점','').strip()}
                item['transaction_hash'] = _hash([account,'KRW',tx_date.isoformat(),item['transaction_time'],dep,wd,bal,item['description']])
            else:
                amount, interest, principal, balance = _dec(raw.get('거래금액')), _dec(raw.get('이자금액')), _dec(raw.get('거래원금')), _dec(raw.get('대출잔액')); rate = _dec(raw.get('대출이율'))
                item = {**common, 'currency':raw.get('통화','KRW').strip().upper() or 'KRW', 'loan_name':raw.get('대출명','').strip(), 'transaction_type':raw.get('거래구분','').strip(), 'transaction_amount':str(amount), 'interest_amount':str(interest), 'principal_amount':str(principal), 'loan_balance':str(balance), 'interest_rate':str(rate), 'interest_start':raw.get('이자계산시작일','').strip() or None, 'interest_end':raw.get('이자계산종료일','').strip() or None, 'interest_days':raw.get('이자계산기간','').strip() or None, 'applied_fx_rate':raw.get('적용환율','').replace(',','').strip() or None, 'loan_krw_amount':raw.get('대출원화액','').replace(',','').strip() or None, 'detail1':raw.get('이자상세1','').strip(), 'detail2':raw.get('이자상세2','').strip(), 'detail3':raw.get('이자상세3','').strip(), 'note1':raw.get('비고1','').strip(), 'note2':raw.get('비고2','').strip()}
                item['transaction_hash'] = _hash([account,item['currency'],tx_date.isoformat(),item['transaction_type'],amount,interest,principal,balance,rate])
            parsed.append(item)
        except Exception as exc:
            errors.append({'source_row_number': idx, 'message': str(exc), 'raw': raw})
    if not parsed: raise EBranchFormatError('등록 가능한 거래행을 찾지 못했습니다. 파일 구조 확인이 필요합니다.')
    dates = [x['transaction_date'] for x in parsed]
    return {'file_type':file_type, 'headers':list(headers), 'structure_hash':structure_hash, 'rows':parsed, 'errors':errors, 'reviews':reviews, 'query_start_date':min(dates), 'query_end_date':max(dates)}

def file_sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''): h.update(chunk)
    return h.hexdigest()

def select_final_balance(rows):
    if not rows: return None, '기초잔액 확인 필요', '2025.12.31 거래내역 없음'
    timed = [r for r in rows if r.get('transaction_time')]
    if len(timed) == len(rows):
        final = max(timed, key=lambda r: r['transaction_time']); return final, '확인', '2025.12.31 e-Branch 거래시간 기준 최종잔액'
    pres = []
    for r in rows:
        try:
            b = Decimal(r['balance']); d = Decimal(r['deposit']); w = Decimal(r['withdrawal']); pres.append(b - d + w)
        except Exception: return None, '기초잔액 확인 필요', '잔액 연속성 검증 불가'
    pre_set = set(pres); candidates = [r for r in rows if Decimal(r['balance']) not in pre_set]
    if len(candidates) == 1: return candidates[0], '확인', '2025.12.31 e-Branch 잔액 연속성 검증 기준 최종잔액'
    return None, '기초잔액 확인 필요', '동일일자 최종 거래를 유일하게 확정할 수 없음'

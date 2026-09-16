import hashlib, http.cookiejar, re, ssl
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import quote
from urllib.request import Request, build_opener, HTTPCookieProcessor, HTTPSHandler

SOURCE='서울외국환중개'; RATE_TYPE='매매기준율'
BASES=('https://www.smbs.biz','http://www.smbs.biz')
MAIN='/ExRate/StdExRate.jsp'; XML='/ExRate/StdExRate_xml.jsp'
UA='Mozilla/5.0 (compatible; MedPark-Cash/2.1)'
UNIT={'JPY':Decimal('100'),'IDR':Decimal('100'),'VND':Decimal('100')}

class ExchangeRateProvider:
    def lookup(self,base_date,currencies,previous=False): raise NotImplementedError

class SeoulMoneyBrokerageProvider(ExchangeRateProvider):
    def __init__(self,timeout=12): self.timeout=timeout
    def _opener(self,base):
        jar=http.cookiejar.CookieJar(); op=build_opener(HTTPCookieProcessor(jar),HTTPSHandler(context=ssl.create_default_context()))
        with op.open(Request(base+MAIN,headers={'User-Agent':UA,'Accept':'text/html,*/*'}),timeout=self.timeout) as r:r.read(256)
        return op
    def _decode(self,b):
        for e in ('utf-8','cp949','euc-kr','latin1'):
            try:return b.decode(e)
            except:pass
        return b.decode('utf-8','replace')
    def _fetch(self,op,base,c,s,e):
        u=base+XML+'?arr_value='+quote(f'{c}_{s.isoformat()}_{e.isoformat()}',safe='_-:')
        with op.open(Request(u,headers={'User-Agent':UA,'Referer':base+MAIN,'Accept':'application/xml,text/xml,text/html,*/*'}),timeout=self.timeout) as r:b=r.read(2_000_000)
        return u,b,self._decode(b)
    def _parse(self,t):
        out=[]
        for tag in re.findall(r'<set\b[^>]*>',t,re.I):
            lm=re.search(r"\blabel\s*=\s*['\"]([^'\"]+)['\"]",tag,re.I);vm=re.search(r"\bvalue\s*=\s*['\"]([^'\"]+)['\"]",tag,re.I)
            if not lm or not vm:continue
            d=None
            for f in ('%y.%m.%d','%Y.%m.%d','%Y-%m-%d'):
                try:d=datetime.strptime(lm.group(1).strip(),f).date();break
                except ValueError:pass
            if not d:continue
            try:v=Decimal(vm.group(1).strip().replace(',',''))
            except InvalidOperation:continue
            out.append((d,v))
        return out
    def lookup(self,base_date,currencies,previous=False):
        day=base_date if isinstance(base_date,date) else date.fromisoformat(str(base_date));currs=sorted({str(c).upper() for c in currencies if c and str(c).upper()!='KRW'});start=day-timedelta(days=14) if previous else day;last=None
        for base in BASES:
            try:
                op=self._opener(base);result=[]
                for c in currs:
                    try:
                        url,raw,txt=self._fetch(op,base,c,start,day);pairs=[x for x in self._parse(txt) if x[0]<=day];pairs=pairs if previous else [x for x in pairs if x[0]==day]
                        if not pairs:result.append({'currency':c,'status':'no_data','message':f'{day} 기준 고시환율 없음','source_url':url,'response_hash':hashlib.sha256(raw).hexdigest()});continue
                        sd,rate=max(pairs,key=lambda x:x[0]);unit=UNIT.get(c,Decimal('1'))
                        if rate<=0 or unit<=0:result.append({'currency':c,'status':'error','message':'환율 또는 적용단위 오류'});continue
                        result.append({'currency':c,'rate':str(rate),'unit_amount':str(unit),'rate_type':RATE_TYPE,'source':SOURCE,'source_date':sd.isoformat(),'source_url':url,'retrieved_at':datetime.now(timezone.utc).isoformat(),'response_hash':hashlib.sha256(raw).hexdigest(),'status':'lookup_ok'})
                    except Exception as e:result.append({'currency':c,'status':'error','message':str(e)[:250]})
                return result
            except Exception as e:last=e
        raise RuntimeError(f'서울외국환중개 연결 실패: {last}')

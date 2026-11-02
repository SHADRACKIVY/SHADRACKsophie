import base64, json, datetime as dt, urllib.request, urllib.parse, secrets, os, time

class MpesaError(Exception):
    pass

def _setting(core, key, default=''):
    return core.get_setting(key, default)

def _base(core):
    env=_setting(core,'mpesa_environment','sandbox').lower().strip()
    return 'https://api.safaricom.co.ke' if env=='production' else 'https://sandbox.safaricom.co.ke'

def normalize_phone(phone):
    s=''.join(ch for ch in str(phone or '') if ch.isdigit() or ch=='+')
    s=s.replace('+','')
    if s.startswith('0'): s='254'+s[1:]
    elif s.startswith('7') and len(s)==9: s='254'+s
    if not (s.startswith('254') and len(s)==12 and s.isdigit()):
        raise MpesaError('Enter a valid Kenyan Safaricom number, e.g. 0712345678.')
    return s

def _request(url, data=None, headers=None, timeout=30):
    body=None if data is None else json.dumps(data).encode('utf-8')
    req=urllib.request.Request(url, data=body, headers=headers or {}, method='POST' if data is not None else 'GET')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw=resp.read().decode('utf-8')
            return json.loads(raw or '{}')
    except Exception as exc:
        raise MpesaError(f'M-Pesa connection failed: {exc}') from exc

def get_access_token(core):
    key=_setting(core,'mpesa_consumer_key','').strip(); secret=_setting(core,'mpesa_consumer_secret','').strip()
    if not key or not secret: raise MpesaError('M-Pesa Consumer Key and Consumer Secret are not configured in Administrator Settings.')
    url=_base(core)+'/oauth/v1/generate?grant_type=client_credentials'
    raw=(key+':'+secret).encode('utf-8'); auth=base64.b64encode(raw).decode('ascii')
    req=urllib.request.Request(url, headers={'Authorization':'Basic '+auth,'Accept':'application/json'}, method='GET')
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data=json.loads(resp.read().decode('utf-8') or '{}')
    except Exception as exc:
        raise MpesaError(f'Unable to authenticate with Daraja: {exc}') from exc
    token=data.get('access_token')
    if not token: raise MpesaError(data.get('errorMessage') or 'Daraja did not return an access token.')
    return token

def stk_push(core, amount, phone, account_reference, description):
    shortcode=_setting(core,'mpesa_shortcode','').strip(); passkey=_setting(core,'mpesa_passkey','').strip(); callback=_setting(core,'mpesa_callback_base_url','').strip().rstrip('/')
    if not shortcode or not passkey or not callback: raise MpesaError('Configure M-Pesa Short Code, Passkey and Public Callback Base URL in Administrator Settings.')
    phone=normalize_phone(phone); token=get_access_token(core)
    timestamp=dt.datetime.now().strftime('%Y%m%d%H%M%S')
    password=base64.b64encode((shortcode+passkey+timestamp).encode()).decode('ascii')
    payload={'BusinessShortCode':shortcode,'Password':password,'Timestamp':timestamp,'TransactionType':'CustomerPayBillOnline' if _setting(core,'mpesa_shortcode_type','PAYBILL')=='PAYBILL' else 'CustomerBuyGoodsOnline','Amount':int(round(float(amount))),'PartyA':phone,'PartyB':shortcode,'PhoneNumber':phone,'CallBackURL':callback+'/api/mpesa/stk/callback','AccountReference':account_reference[:12],'TransactionDesc':description[:13] or 'KAKA LIQUOR WINES AND SPIRIT'}
    data=_request(_base(core)+'/mpesa/stkpush/v1/processrequest',payload,{'Authorization':'Bearer '+token,'Content-Type':'application/json','Accept':'application/json'})
    if data.get('ResponseCode') not in (None,'0',0) and not data.get('CheckoutRequestID'):
        raise MpesaError(data.get('errorMessage') or data.get('ResponseDescription') or 'STK Push was rejected.')
    return data

def register_c2b_urls(core):
    shortcode=_setting(core,'mpesa_shortcode','').strip(); callback=_setting(core,'mpesa_callback_base_url','').strip().rstrip('/')
    if not shortcode or not callback: raise MpesaError('Configure M-Pesa Short Code and Public Callback Base URL first.')
    token=get_access_token(core)
    payload={'ShortCode':shortcode,'ResponseType':'Completed','ConfirmationURL':callback+'/api/mpesa/c2b/confirmation','ValidationURL':callback+'/api/mpesa/c2b/validation'}
    return _request(_base(core)+'/mpesa/c2b/v2/registerurl',payload,{'Authorization':'Bearer '+token,'Content-Type':'application/json','Accept':'application/json'})

def parse_stk_callback(payload):
    body=(payload or {}).get('Body',{})
    cb=body.get('stkCallback',{})
    meta={}
    for item in cb.get('CallbackMetadata',{}).get('Item',[]) or []:
        name=item.get('Name'); meta[name]=item.get('Value')
    return {'checkout_request_id':cb.get('CheckoutRequestID',''),'merchant_request_id':cb.get('MerchantRequestID',''),'result_code':int(cb.get('ResultCode',1)),'result_desc':cb.get('ResultDesc',''),'mpesa_code':str(meta.get('MpesaReceiptNumber','') or ''),'amount':float(meta.get('Amount',0) or 0),'phone':str(meta.get('PhoneNumber','') or ''),'transaction_date':str(meta.get('TransactionDate','') or '')}

def parse_c2b(payload):
    p=payload or {}
    return {'transaction_type':p.get('TransactionType',''),'trans_id':p.get('TransID',''),'trans_time':p.get('TransTime',''),'amount':float(p.get('TransAmount',0) or 0),'business_short_code':p.get('BusinessShortCode',''),'bill_ref_number':p.get('BillRefNumber',''),'invoice_number':p.get('InvoiceNumber',''),'org_account_balance':p.get('OrgAccountBalance',''),'msisdn':p.get('MSISDN',''),'first_name':p.get('FirstName',''),'middle_name':p.get('MiddleName',''),'last_name':p.get('LastName',''),'raw_json':json.dumps(p,ensure_ascii=False)}

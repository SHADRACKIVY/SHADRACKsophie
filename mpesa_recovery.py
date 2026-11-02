"""Standard-library M-Pesa statement import and normalization for KAKA LIQUOR WINES AND SPIRIT.
Supports CSV and modern XLSX exports without third-party packages.
"""
import csv, io, zipfile, xml.etree.ElementTree as ET, re, datetime as dt

NS = {'a':'http://schemas.openxmlformats.org/spreadsheetml/2006/main', 'r':'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}

ALIASES = {
    'code': [
        'receipt no', 'receipt number', 'transaction receipt number', 'transaction receipt',
        'transid', 'transaction id', 'transaction id number', 'transactionid', 'mpesa receipt',
        'mpesa code', 'm-pesa code', 'receipt', 'transaction code'
    ],
    'date': [
        'completion time', 'transaction time', 'transaction date', 'trans time', 'transtime',
        'trans date', 'completion date', 'date time', 'datetime', 'date'
    ],
    'amount': [
        'paid in', 'amount', 'trans amount', 'transamount', 'credit', 'money in', 'paid in amount',
        'transaction amount', 'amount received', 'received amount'
    ],
    'phone': [
        'msisdn', 'phone', 'phone number', 'mobile number', 'customer phone', 'other party info'
    ],
    'shortcode': [
        'businessshortcode', 'business short code', 'short code', 'shortcode', 'till', 'till number',
        'business number'
    ],
    'reference': [
        'billrefnumber', 'bill ref number', 'account reference', 'reference', 'details', 'account',
        'invoice number', 'invoice'
    ],
    'status': ['transaction status', 'status']
}

def norm(s):
    return re.sub(r'[^a-z0-9]+', ' ', str(s or '').strip().lower()).strip()

def parse_amount(v):
    s=str(v or '').strip().replace(',', '').replace('KSh','').replace('KES','')
    if not s: return 0.0
    neg='(' in s and ')' in s
    s=re.sub(r'[^0-9.\-]','',s)
    try:
        x=float(s)
        return -abs(x) if neg else x
    except Exception:
        return 0.0

def normalize_phone(v):
    s=str(v or '')
    digits=''.join(ch for ch in s if ch.isdigit())
    if len(digits)>=9:
        if digits.startswith('254') and len(digits)>=12: return digits[:12]
        if digits.startswith('0') and len(digits)>=10: return '254'+digits[1:11]
        if digits.startswith('7') and len(digits)==9: return '254'+digits
    # preserve masked/other-party strings for audit; matcher will ignore unusable values
    return digits

def normalize_datetime(v):
    if v is None: return ''
    s=str(v).strip()
    # Excel serial date
    try:
        if re.fullmatch(r'\d+(?:\.\d+)?', s) and float(s)>1000:
            x=float(s)
            base=dt.datetime(1899,12,30)
            return (base+dt.timedelta(days=x)).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        pass
    patterns=['%d/%m/%Y %H:%M:%S','%d/%m/%Y %H:%M','%m/%d/%Y %H:%M:%S','%m/%d/%Y %H:%M','%Y-%m-%d %H:%M:%S','%Y-%m-%d %H:%M','%d-%m-%Y %H:%M:%S','%d-%m-%Y %H:%M']
    for p in patterns:
        try: return dt.datetime.strptime(s,p).strftime('%Y-%m-%d %H:%M:%S')
        except ValueError: pass
    return s

def find_header(headers, aliases):
    nheads={norm(h):h for h in headers}
    # exact normalized alias
    for a in aliases:
        na=norm(a)
        if na in nheads: return nheads[na]
    # substring fallback
    for h in headers:
        nh=norm(h)
        for a in aliases:
            na=norm(a)
            if na and (na in nh or nh in na): return h
    return None

def parse_csv(data):
    for enc in ('utf-8-sig','utf-8','cp1252','latin-1'):
        try:
            text=data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    rows=[]; reader=csv.DictReader(io.StringIO(text))
    headers=reader.fieldnames or []
    if not headers: return []
    for row in reader:
        rows.append(dict(row))
    return rows

def _shared_strings(z):
    try:
        root=ET.fromstring(z.read('xl/sharedStrings.xml'))
    except KeyError:
        return []
    out=[]
    for si in root.findall('a:si',NS):
        parts=[]
        for t in si.iter('{%s}t'%NS['a']): parts.append(t.text or '')
        out.append(''.join(parts))
    return out

def _sheet_path(z):
    try:
        wb=ET.fromstring(z.read('xl/workbook.xml'))
        rels=ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
        relmap={r.attrib.get('Id'):r.attrib.get('Target') for r in rels}
        sheet=wb.find('a:sheets/a:sheet',NS)
        rid=sheet.attrib.get('{%s}id'%NS['r']) if sheet is not None else None
        target=relmap.get(rid,'worksheets/sheet1.xml')
        if target.startswith('/'): return target.lstrip('/')
        if target.startswith('xl/'): return target
        return 'xl/'+target
    except Exception:
        return 'xl/worksheets/sheet1.xml'

def parse_xlsx(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        shared=_shared_strings(z); sheet=ET.fromstring(z.read(_sheet_path(z)))
        rows=[]
        for row in sheet.findall('.//a:sheetData/a:row',NS):
            vals=[]
            for cell in row.findall('a:c',NS):
                ref=cell.attrib.get('r','')
                col=''.join(ch for ch in ref if ch.isalpha())
                v=cell.find('a:v',NS)
                val='' if v is None else (v.text or '')
                if cell.attrib.get('t')=='s':
                    try: val=shared[int(val)]
                    except Exception: val=''
                elif cell.attrib.get('t')=='inlineStr':
                    isel=cell.find('a:is',NS); val=''.join(t.text or '' for t in isel.iter('{%s}t'%NS['a'])) if isel is not None else ''
                vals.append((col,val))
            rows.append(vals)
        if not rows: return []
        # expand by column order
        hdr_map={c:v for c,v in rows[0]}; headers=[v for _,v in rows[0]]
        normalized=[]
        for rr in rows[1:]:
            d={}
            pos={c:v for c,v in rr}
            cols=[c for c,_ in rows[0]]
            for col,head in zip(cols,headers): d[head]=pos.get(col,'')
            normalized.append(d)
        return normalized

def parse_statement(data, filename):
    name=str(filename or '').lower()
    if name.endswith('.csv') or name.endswith('.txt'):
        raw_rows=parse_csv(data)
    elif name.endswith('.xlsx'):
        raw_rows=parse_xlsx(data)
    else:
        raise ValueError('Upload a Safaricom M-Pesa statement in CSV or XLSX format. Old XLS files are not supported; export as XLSX or CSV.')
    if not raw_rows: return []
    headers=list(raw_rows[0].keys())
    maps={k:find_header(headers,v) for k,v in ALIASES.items()}
    if not maps['code'] or not maps['amount']:
        raise ValueError('The uploaded statement must contain a transaction/receipt code and an amount/paid-in column.')
    out=[]
    for idx,row in enumerate(raw_rows, start=2):
        code=str(row.get(maps['code'],'') or '').strip().upper()
        amount=parse_amount(row.get(maps['amount'],'') if maps['amount'] else '')
        status=str(row.get(maps['status'],'') or '').strip().upper() if maps['status'] else ''
        # Ignore withdrawals or zero/negative rows; this recovery tool is for money received.
        if amount <= 0: continue
        if status and status not in ('COMPLETED','SUCCESS','SUCCESSFUL','PAID','COMPLETE') and 'COMPLETE' not in status:
            continue
        raw_phone=row.get(maps['phone'],'') if maps['phone'] else ''
        out.append({
            'source_row':idx,
            'trans_id':code,
            'amount':amount,
            'trans_time':normalize_datetime(row.get(maps['date'],'') if maps['date'] else ''),
            'msisdn':normalize_phone(raw_phone),
            'raw_phone':str(raw_phone or ''),
            'business_short_code':str(row.get(maps['shortcode'],'') or '').strip() if maps['shortcode'] else '',
            'bill_ref_number':str(row.get(maps['reference'],'') or '').strip() if maps['reference'] else '',
            'status_text':status,
            'raw_row':row,
        })
    return out

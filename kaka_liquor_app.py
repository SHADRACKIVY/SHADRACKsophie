#!/usr/bin/env python3
"""MONTEZ LIQUOR
Zero-third-party-dependency web application.
Uses Python standard library + SQLite; core business logic is reused from the tested core.
"""
import os, io, json, time, secrets, socket, datetime as dt, html, re, zipfile, mimetypes, traceback, sqlite3
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http import cookies
from urllib.parse import urlparse, parse_qs, quote, urlencode
from urllib import request as urlrequest, error as urlerror

# Keep the database outside the app folder so upgrades do not erase data.
DATA_DIR = os.environ.get('MONTEZ_DATA_DIR') or os.environ.get('KAKA_DATA_DIR') or os.path.join(os.path.expanduser('~'), 'MONTEZ_LIQUOR_DATA')
os.makedirs(DATA_DIR, exist_ok=True)
os.environ['KAKA_DATA_DIR'] = DATA_DIR
os.environ.setdefault('KAKA_PORT', os.environ.get('MONTEZ_PORT', os.environ.get('PORT', '5000')))

import kaka_core as core
import mpesa_integration as mpesa
import mpesa_recovery as mpesa_recovery

# Cloud safety: require a private admin password on first deployment.
_admin_password = (os.environ.get('MONTEZ_INITIAL_ADMIN_PASSWORD') or os.environ.get('KAKA_INITIAL_ADMIN_PASSWORD', '')).strip()
try:
    _c = core.db.c()
    _admin = _c.execute("SELECT id, password_hash FROM users WHERE username='admin'").fetchone()
    if _admin and _admin['password_hash'] == core.sha('admin1234'):
        if len(_admin_password) < 12:
            raise RuntimeError('Set KAKA_INITIAL_ADMIN_PASSWORD in Render to a strong password of at least 12 characters.')
        _c.execute('UPDATE users SET password_hash=?, updated_at=? WHERE id=?', (core.sha(_admin_password), core.now_str(), _admin['id']))
    for _demo_user, _demo_password in (('sales','sales1234'), ('management','management1234')):
        _demo = _c.execute('SELECT id, password_hash FROM users WHERE lower(username)=lower(?)', (_demo_user,)).fetchone()
        if _demo and _demo['password_hash'] == core.sha(_demo_password):
            _c.execute('UPDATE users SET active=0, updated_at=? WHERE id=?', (core.now_str(), _demo['id']))
    _c.commit()
finally:
    try: _c.close()
    except Exception: pass

APP_NAME = 'MONTEZ LIQUOR'
HOST = '0.0.0.0'
PORT = int(os.environ.get('KAKA_PORT', '5000'))
SECURE_COOKIE = (os.environ.get('MONTEZ_SECURE_COOKIE') or os.environ.get('KAKA_SECURE_COOKIE', '0')) == '1'
COOKIE_SUFFIX = '; Secure' if SECURE_COOKIE else ''
SESSIONS = {}
CSRF = {}

ROLE_LABELS = {
    'ADMIN': 'ADMINISTRATOR / DEVELOPER',
    'MANAGEMENT': 'MANAGEMENT',
    'SALES': 'SALES PERSON'
}
MANAGEMENT_ONLY = {'reports', 'stock', 'ledger', 'profit_loss'}


def esc(v):
    return html.escape('' if v is None else str(v), quote=True)


def money(v):
    return f"{core.get_setting('currency','KSh')} {float(v or 0):,.2f}"


def now():
    return core.now_str()


def role_name(u):
    return str(u.get('access_role') or u.get('role') or 'SALES').upper() if u else ''

def period_from_query(q):
    q = q or {}
    today = dt.date.today()
    preset = str(_qsval(q, 'preset', '') or '').upper()
    fd = str(_qsval(q, 'from_date', '') or '')
    td = str(_qsval(q, 'to_date', '') or '')
    ft = str(_qsval(q, 'from_time', '00:00') or '00:00')
    tt = str(_qsval(q, 'to_time', '23:59') or '23:59')
    if preset in ('TODAY','END_DAY'):
        fd = td = today.isoformat(); ft='00:00'; tt='23:59'; preset='TODAY'
    elif preset in ('THIS_WEEK','END_WEEK'):
        monday=today-dt.timedelta(days=today.weekday()); sunday=monday+dt.timedelta(days=6)
        fd=monday.isoformat(); td=sunday.isoformat(); ft='00:00'; tt='23:59'; preset='THIS WEEK'
    elif preset in ('THIS_MONTH','MONTHLY'):
        fd=today.replace(day=1).isoformat(); next_month=(today.replace(day=28)+dt.timedelta(days=4)).replace(day=1)
        td=(next_month-dt.timedelta(days=1)).isoformat(); ft='00:00'; tt='23:59'; preset='THIS MONTH'
    elif preset == 'LAST_MONTH' and not fd and not td:
        first=today.replace(day=1); last=first-dt.timedelta(days=1)
        fd=last.replace(day=1).isoformat(); td=last.isoformat(); ft='00:00'; tt='23:59'; preset='LAST MONTH'
    elif preset in ('THIS_YEAR','LAST_YEAR'):
        year=today.year if preset=='THIS_YEAR' else today.year-1
        fd=f'{year}-01-01'; td=f'{year}-12-31'; ft='00:00'; tt='23:59'; preset='THIS YEAR' if preset=='THIS_YEAR' else 'LAST YEAR'
    else:
        preset='CUSTOM' if (_qsval(q,'from_date','') or _qsval(q,'to_date','')) else 'DEFAULT PERIOD'
        if not fd and not td:
            days=max(1,min(365,int(float(core.get_setting('report_default_days','7')))))
            fd=(today-dt.timedelta(days=days-1)).isoformat(); td=today.isoformat(); ft='00:00'; tt='23:59'
            preset=f'LAST {days} DAYS' if days>1 else 'TODAY'
        else:
            if not fd: fd=today.isoformat()
            if not td: td=fd
    try:
        dt.date.fromisoformat(fd); dt.date.fromisoformat(td)
        dt.datetime.strptime(ft, '%H:%M'); dt.datetime.strptime(tt, '%H:%M')
    except ValueError:
        fd=td=today.isoformat(); ft='00:00'; tt='23:59'; preset='TODAY'
    return fd,ft,td,tt,preset

def range_toolbar(action, fd, ft, td, tt, branch_id=None):
    form=('<form method="get" action="%s" class="form-grid" style="align-items:end">'
          '<input type="hidden" name="branch_id" value="%s">'
          '<div><label>From Date</label><input type="date" name="from_date" value="%s"></div>'
          '<div><label>From Time</label><input type="time" name="from_time" value="%s"></div>'
          '<div><label>To Date</label><input type="date" name="to_date" value="%s"></div>'
          '<div><label>To Time</label><input type="time" name="to_time" value="%s"></div>'
          '<div><button class="btn gold" type="submit">APPLY</button></div></form>') % (esc(action),esc(branch_id or ''),esc(fd),esc(ft),esc(td),esc(tt))
    presets=[('END_DAY','report_end_day','END DAY REPORT'),('END_WEEK','report_end_week','END WEEK REPORT'),('TODAY','report_today','TODAY'),('THIS_WEEK','report_this_week','THIS WEEK REPORT'),('MONTHLY','report_monthly','MONTHLY REPORT'),('THIS_MONTH','report_this_month','THIS MONTH'),('LAST_MONTH','report_last_month','LAST MONTH'),('THIS_YEAR','report_this_year','THIS YEAR'),('LAST_YEAR','report_last_year','LAST YEAR')]
    links=''.join(f'<a class="btn" href="{esc(action)}?preset={key}&branch_id={esc(branch_id or "")}">{esc(setting_label(setting,default))}</a>' for key,setting,default in presets)
    return form+f'<div class="actions report-presets" style="margin-top:10px;flex-wrap:wrap">{links}</div>'

def setting_label(key, fallback=''):
    return core.get_setting(key, fallback) or fallback

def role_display(role):
    return {'ADMIN':setting_label('role_admin_label','ADMINISTRATOR / DEVELOPER'),'MANAGEMENT':setting_label('role_management_label','MANAGEMENT'),'SALES':setting_label('role_sales_label','SALES PERSON')}.get(str(role).upper(), str(role))

def business_logo_path():
    fn=core.get_setting('business_logo_filename','')
    if fn:
        safe=os.path.basename(fn)
        path=os.path.join(DATA_DIR,safe)
        if os.path.isfile(path):
            return path
    return None

def business_logo_bytes():
    path=business_logo_path()
    if path:
        with open(path,'rb') as f:
            return f.read(), mimetypes.guess_type(path)[0] or 'application/octet-stream'
    return LOGO.encode('utf-8'), 'image/svg+xml'


SCANNER_MODAL = '<div id="scanner_modal" class="scanner-modal"><div class="scanner-box"><div class="section-head"><h3>Scan Product</h3><button class="btn" type="button" onclick="closeScanner()">CLOSE</button></div><video id="scanner_video" playsinline muted></video><div id="scanner_status" class="scanner-status">Point the camera at a barcode…</div></div></div>'

SCANNER_JS = r"""
window.scannerBeep=function(){try{var AC=window.AudioContext||window.webkitAudioContext;if(!AC)return;var ctx=new AC();var osc=ctx.createOscillator(),gain=ctx.createGain();osc.type='sine';osc.frequency.value=880;gain.gain.setValueAtTime(0.0001,ctx.currentTime);gain.gain.exponentialRampToValueAtTime(0.16,ctx.currentTime+0.01);gain.gain.exponentialRampToValueAtTime(0.0001,ctx.currentTime+0.13);osc.connect(gain);gain.connect(ctx.destination);osc.start();osc.stop(ctx.currentTime+0.14);setTimeout(function(){try{ctx.close()}catch(e){}},250);}catch(e){}};
window.lookupProductBySku=function(inputId){var q=document.getElementById(inputId),sel=document.getElementById('sale_product');if(!q||!sel)return;var term=(q.value||'').trim().toLowerCase();if(!term){alert('Enter or scan an SKU.');return;}var found=false;var productLabel='';Array.from(sel.options).forEach(function(o){if(String(o.getAttribute('data-sku')||'').toLowerCase()===term){sel.value=o.value;found=true;productLabel=o.textContent||'';}});if(found){window.scannerBeep();if(window.showRegisteredPrice)window.showRegisteredPrice();var st=document.getElementById('scanner_status');if(st)st.textContent='Product scanned: '+productLabel;q.value='';}else alert('Product SKU not found. Register the product first.');};
window.lookupGenericSku=function(inputId,selectId){var q=document.getElementById(inputId),sel=document.getElementById(selectId);if(!q||!sel)return;var term=(q.value||'').trim().toLowerCase();if(!term){alert('Enter or scan an SKU.');return;}var found=false;Array.from(sel.options).forEach(function(o){if(String(o.getAttribute('data-sku')||'').toLowerCase()===term){sel.value=o.value;found=true;}});if(found){window.scannerBeep();q.value='';}else alert('Product SKU not found. Register the product first.');};
window.closeScanner=function(){var modal=document.getElementById('scanner_modal'),video=document.getElementById('scanner_video');try{if(video&&video.srcObject){video.srcObject.getTracks().forEach(function(t){t.stop()});video.srcObject=null;}}catch(e){}if(modal)modal.classList.remove('open');};
window.openScanner=function(targetId){var modal=document.getElementById('scanner_modal'),video=document.getElementById('scanner_video'),status=document.getElementById('scanner_status'),target=document.getElementById(targetId);if(!modal||!video||!target){alert('Scanner is unavailable.');return;}if(!('BarcodeDetector' in window)){alert('Camera scanning is not supported by this browser. Use a USB/Bluetooth scanner or type the SKU.');return;}if(!navigator.mediaDevices||!navigator.mediaDevices.getUserMedia){alert('Camera access is unavailable. Use a scanner or type the SKU.');return;}modal.classList.add('open');status.textContent='Point the camera at the product barcode…';navigator.mediaDevices.getUserMedia({video:{facingMode:{ideal:'environment'}}}).then(function(stream){video.srcObject=stream;video.play();var detector=new BarcodeDetector({formats:['ean_13','ean_8','upc_a','upc_e','code_128','code_39','qr_code']});function loop(){if(!modal.classList.contains('open'))return;detector.detect(video).then(function(codes){if(codes.length&&codes[0].rawValue){target.value=codes[0].rawValue;window.scannerBeep();status.textContent='Barcode scanned: '+codes[0].rawValue;setTimeout(function(){window.closeScanner();if(targetId==='sales_sku_scan')window.lookupProductBySku(targetId);else if(targetId==='purchase_sku_scan')window.lookupGenericSku(targetId,'purchase_product_id');},120);return;}requestAnimationFrame(loop);}).catch(function(){requestAnimationFrame(loop);});}requestAnimationFrame(loop);}).catch(function(){status.textContent='Camera could not be opened. You can type/scan the SKU instead.';});};
window.openScannerFill=function(targetId){window.openScanner(targetId);};
"""

def page_link(label, href, active=''):
    cls = 'active' if href == active else ''
    return f'<a class="nav {cls}" href="{esc(href)}">{esc(label)}</a>'


def parse_form(handler):
    n = int(handler.headers.get('Content-Length', '0') or 0)
    raw = handler.rfile.read(n).decode('utf-8', 'replace') if n else ''
    d = {k: vals[-1] for k, vals in parse_qs(raw, keep_blank_values=True).items()}
    return d


def parse_multipart_form(handler):
    ctype=handler.headers.get('Content-Type','')
    length=int(handler.headers.get('Content-Length','0') or 0)
    body=handler.rfile.read(length) if length else b''
    msg=BytesParser(policy=policy.default).parsebytes(b'Content-Type: '+ctype.encode('utf-8')+b'\r\nMIME-Version: 1.0\r\n\r\n'+body)
    fields={}; upload=None
    for part in msg.iter_parts():
        name=part.get_param('name', header='content-disposition')
        filename=part.get_filename()
        payload=part.get_payload(decode=True) or b''
        if filename is not None:
            upload={'name':name or 'file','filename':os.path.basename(filename),'content_type':part.get_content_type(),'data':payload}
        elif name:
            fields[name]=payload.decode(part.get_content_charset() or 'utf-8','replace')
    return fields,upload


def csrf_for(token):
    return CSRF.setdefault(token, secrets.token_urlsafe(18))


def check_csrf(handler, form):
    token = session_token(handler)
    expected = CSRF.get(token, '')
    return bool(expected and secrets.compare_digest(expected, form.get('csrf', '')))


def session_token(handler):
    c = cookies.SimpleCookie(handler.headers.get('Cookie', ''))
    return c.get('kaka_session').value if c.get('kaka_session') else ''


def same_origin_request(handler):
    """Allow same-origin authenticated AJAX requests when legacy CSRF state is missing.
    This supports deployments where the in-memory CSRF map is reset independently.
    """
    host = (handler.headers.get('Host') or '').strip().lower()
    origin = (handler.headers.get('Origin') or '').strip()
    referer = (handler.headers.get('Referer') or '').strip()
    try:
        if origin:
            parsed = urlparse(origin)
            return bool(host and parsed.netloc.lower() == host and parsed.scheme in ('http', 'https'))
        if referer:
            parsed = urlparse(referer)
            return bool(host and parsed.netloc.lower() == host and parsed.scheme in ('http', 'https'))
    except Exception:
        return False
    return False


def session_user(handler, touch=True):
    token = session_token(handler)
    if not token or token not in SESSIONS:
        return None
    entry = SESSIONS[token]
    last = float(entry.get('last_activity', 0))
    idle = max(1, int(float(core.get_setting('idle_timeout_minutes', '2')) * 60))
    current_ts = time.time()
    if last and current_ts - last > idle:
        uid = int(entry.get('uid', 0))
        try:
            c = core.db.c(); r = c.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
            if r:
                core.db.audit(c, dict(r), 'AUTO_LOGOUT', 'USER', uid, 'Automatic logout after inactivity')
                c.commit(); c.close()
        except Exception:
            pass
        SESSIONS.pop(token, None); CSRF.pop(token, None)
        return None
    if touch:
        SESSIONS[token]['last_activity'] = current_ts
    c = core.db.c(); r = c.execute('SELECT * FROM users WHERE id=? AND active=1', (int(entry['uid']),)).fetchone(); c.close()
    return dict(r) if r else None


def require_user(handler):
    u = session_user(handler, touch=True)
    if not u:
        raise PermissionError('Please sign in.')
    return u


def allowed(u, perm):
    if role_name(u) == 'ADMIN':
        return True
    return core.has_perm(u, perm)


def require_perm_user(u, perm):
    if not allowed(u, perm):
        raise PermissionError('This function is disabled for your account. Contact Administrator.')


def redirect(handler, location, status=303):
    handler.send_response(status)
    handler.send_header('Location', location)
    handler.send_header('Content-Length', '0')
    handler.end_headers()


def send_html(handler, body, status=200, title=APP_NAME):
    raw = body.encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type', 'text/html; charset=utf-8')
    handler.send_header('Cache-Control', 'no-store')
    handler.send_header('Content-Length', str(len(raw)))
    handler.end_headers(); handler.wfile.write(raw)


def send_json(handler, obj, status=200):
    raw=json.dumps(obj,ensure_ascii=False).encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type','application/json; charset=utf-8')
    handler.send_header('Cache-Control','no-store')
    handler.send_header('Content-Length',str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def send_file(handler, data, content_type, filename, disposition='attachment'):
    if isinstance(data, str): data = data.encode()
    handler.send_response(200)
    handler.send_header('Content-Type', content_type)
    handler.send_header('Cache-Control', 'no-store')
    handler.send_header('Content-Disposition', f'{disposition}; filename="{filename}"')
    handler.send_header('Content-Length', str(len(data)))
    handler.end_headers(); handler.wfile.write(data)


def login_page(message=''):
    msg = f'<div class="flash error">{esc(message)}</div>' if message else ''
    name=core.get_setting('business_name',APP_NAME); subtitle=core.get_setting('business_subtitle','Wines and Spirit')
    return f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#123c31"><link rel="manifest" href="/manifest.json"><link rel="icon" href="/business-logo"><title>{esc(name)}</title><style>{CSS}.app-powered{{text-align:center;color:#718096;font-size:12px;letter-spacing:.08em;padding:18px 8px 10px;margin-top:18px;border-top:1px solid rgba(120,140,130,.18)}}</style></head><body class="login-page"><div class="login-card"><div class="logo-wrap"><img src="/business-logo" class="login-logo"><div><div class="brand-big">{esc(name)}</div><div class="subbrand">{esc(subtitle)}</div></div></div><div class="bottles"><span class="bottle wine"></span><span class="bottle red"></span><span class="bottle dark"></span></div><h1>Secure Login</h1><p class="muted">Executive business management system</p>{msg}<form method="post" action="/login"><label>Username</label><input name="username" autocomplete="username" autofocus required><label>Password</label><input type="password" name="password" autocomplete="current-password" required><button class="btn primary full" type="submit">SIGN IN</button></form><div class="login-note">Every sale, purchase, expense, stock movement and report is recorded against the responsible username, role, date and exact time.</div><footer class="app-powered">Powered by SHADYTECHSOLUTIONS</footer></div></body></html>'''



def layout(u, title, content, active='', subtitle='Business control center'):
    settings=core.get_settings(); name=settings.get('business_name',APP_NAME); business_subtitle=settings.get('business_subtitle','Wines and Spirit'); branch=settings.get('business_branch_name',''); role=role_name(u); dashboard_enabled=settings.get('dashboard_enabled','YES')=='YES'
    if role in ('ADMIN','MANAGEMENT'): branch=''
    elif u.get('branch_id'):
        c=core.db.c(); br=c.execute('SELECT name FROM branches WHERE id=? AND active=1',(int(u['branch_id']),)).fetchone(); c.close(); branch=br['name'] if br else ''
    nav=([(setting_label('nav_dashboard','Dashboard'),'/')] if dashboard_enabled and allowed(u,'VIEW_DASHBOARD') else [])+[(setting_label('nav_sales','Sales / POS'),'/sales')]+([(setting_label('nav_sales_dashboard','Sales Dashboard'),'/sales-dashboard')] if dashboard_enabled and allowed(u,'VIEW_DASHBOARD') else [])+[(setting_label('nav_purchases','Purchases'),'/purchases'),(setting_label('nav_expenses','Expenses'),'/expenses'),(setting_label('nav_customers','Customers / Credit'),'/customers'),(setting_label('nav_reports','Reports'),'/reports'),(setting_label('nav_stock_report','Stock Report'),'/stock-report'),('Stock Transfers / Stock Count','/stock-transfers'),(setting_label('nav_reorder_report','Reorder Report'),'/reorder-report'),(setting_label('nav_live','Live Monitoring'),'/live'),(setting_label('nav_products','Product Registration'),'/products'),('MONTEZ LIQUOR AI Assistant','/ai')]
    if role=='ADMIN':
        nav += [('Branch Registration','/branches'),('Branch Performance','/branch-performance'),('Cash & Paybill Statements','/financials'),(setting_label('nav_users','User Management'),'/users'),(setting_label('nav_settings','Settings'),'/settings'),('M-Pesa Monitor','/mpesa/monitor'),('Payment Recovery','/mpesa/recovery'),(setting_label('nav_ledger','Ledger & P&L'),'/ledger'),(setting_label('nav_profit_loss','Profit & Loss'),'/profit-loss'),(setting_label('nav_expense_report','Expense Report'),'/expense-report'),(setting_label('nav_backup','Backup'),'/backup'),(setting_label('nav_audit','Audit Log'),'/audit')]
    else:
        perm_map={'/':'VIEW_DASHBOARD','/sales':'CREATE_SALE','/sales-dashboard':'VIEW_DASHBOARD','/purchases':'CREATE_PURCHASE','/expenses':'RECORD_EXPENSE','/customers':'MANAGE_CUSTOMERS','/reports':'VIEW_REPORTS','/stock-report':'VIEW_STOCK','/reorder-report':'VIEW_STOCK','/live':'LIVE_MONITORING','/products':'MANAGE_PRODUCTS','/ai':'AI_ASSISTANT','/expense-report':'VIEW_REPORTS','/ledger':'VIEW_LEDGER','/profit-loss':'VIEW_LEDGER'}
        if role=='MANAGEMENT': perm_map['/stock-transfers']='VIEW_STOCK'
        nav=[(label,href) for label,href in nav if perm_map.get(href) and allowed(u,perm_map[href])]
        if role=='MANAGEMENT':
            if allowed(u,'VIEW_REPORTS'): nav.append(('Branch Performance','/branch-performance'))
    nav.append(('Cash → Paybill Requests','/cash-paybill-transfer'))
    nav_html=''.join(page_link(label,href,active) for label,href in nav)
    home_href='/' if dashboard_enabled and allowed(u,'VIEW_DASHBOARD') else '/sales'
    role_label={'ADMIN':setting_label('role_admin_label','ADMINISTRATOR / DEVELOPER'),'MANAGEMENT':setting_label('role_management_label','MANAGEMENT'),'SALES':setting_label('role_sales_label','SALES PERSON')}.get(role,role)
    idle_ms=int(float(settings.get('idle_timeout_minutes','2'))*60*1000)
    refresh_s=max(2,int(float(settings.get('live_refresh_seconds','5'))))
    # Outstanding credit invoices automatically surface once their contractual due date arrives.
    credit_alert_html=''
    try:
        cc=core.db.c()
        alert_sql="""SELECT ca.id,ca.principal,ca.paid_amount,MAX(ca.principal-ca.paid_amount,0) AS balance,ca.due_date,c.full_name,s.receipt_no,b.name AS branch_name
                     FROM credit_accounts ca JOIN customers c ON c.id=ca.customer_id
                     JOIN sales s ON s.id=ca.sale_id LEFT JOIN branches b ON b.id=s.branch_id
                     WHERE ca.status='OPEN' AND MAX(ca.principal-ca.paid_amount,0)>0.005
                       AND date(ca.due_date)<=date('now','localtime')"""
        alert_args=[]
        if role not in ('ADMIN','MANAGEMENT'):
            alert_sql+=" AND s.branch_id=?"; alert_args.append(int(u.get('branch_id') or 0))
        alert_sql+=" ORDER BY date(ca.due_date),ca.id LIMIT 8"
        mature=cc.execute(alert_sql,alert_args).fetchall(); cc.close()
        if mature:
            overdue_count=len(mature)
            alert_items=[]
            for item in mature:
                due_label='DUE TODAY' if str(item['due_date'])==dt.date.today().isoformat() else 'OVERDUE'
                alert_items.append(f'<li><b>{esc(item["full_name"])}</b> · Invoice {esc(item["receipt_no"])} · Due {esc(item["due_date"])} · Balance {money(item["balance"])} · <b>{due_label}</b></li>')
            more=' (showing first 8)' if overdue_count==8 else ''
            credit_alert_html=f'<section class="card" role="alert" style="margin:12px 0;border:2px solid #b7791f;background:#fff8e8"><div class="section-head"><h2 style="color:#8a4b08">⚠ CREDIT INVOICE MATURITY ALERT</h2><span class="pill">{overdue_count}+ DUE / OVERDUE{more}</span></div><p>One or more credit invoices have reached their payment due date and still have an outstanding balance. Please follow up with the customers and record repayments.</p><ul style="line-height:1.9">{"".join(alert_items)}</ul><a class="btn gold" href="/customers">REVIEW CREDIT INVOICES & RECORD PAYMENT</a></section>'
    except Exception:
        try: cc.close()
        except Exception: pass
    return f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#123c31"><link rel="manifest" href="/manifest.json"><link rel="icon" href="/business-logo"><title>{esc(title)} · {esc(name)}</title><style>{CSS}</style></head><body><header class="mobile-head"><div class="brand-mini"><img src="/business-logo"> <span>{esc(name)}</span></div><button class="menu" type="button" onclick="document.body.classList.toggle('open-nav')">☰</button></header><aside class="sidebar"><div class="brand"><img src="/business-logo"><span><b>{esc(name)}</b><small>{esc(branch or business_subtitle)}</small></span></div><nav>{nav_html}</nav><div class="side-bottom"><div class="userbox"><b>{esc(u.get('full_name',''))}</b><span>@{esc(u.get('username',''))}</span><span>{esc(role_label)}</span></div><a class="nav" href="/change-password">Change Password</a><a class="nav logout" href="/logout">LOG OUT</a></div></aside><main><header class="topbar"><div><h1>{esc(title)}</h1><p class="muted">{esc(name)} · {esc(business_subtitle)} · {esc(subtitle)}</p></div><div class="clock" id="clock"></div></header><div class="actions movement-controls" aria-label="Page navigation"><a class="btn" href="{home_href}">⌂ HOME</a><button class="btn" type="button" onclick="if(history.length>1){{history.back()}}else{{location.href='{home_href}'}}">← BACK</button><a class="btn" href="{home_href}" onclick="return confirm('Cancel this screen and return to the home screen? Unsaved form changes will be lost.')">✕ CANCEL</a></div>{flash_html(handler=None)}{credit_alert_html}{content}<footer class="app-powered">Powered by SHADYTECHSOLUTIONS</footer></main>{SCANNER_MODAL}<script>{SCANNER_JS}</script><script>(function(){{function tick(){{var e=document.getElementById('clock');if(e)e.textContent=new Date().toLocaleString('en-KE',{{dateStyle:'medium',timeStyle:'medium'}})}}tick();setInterval(tick,1000);var mpesaPollSeconds={refresh_s},currencySymbol={json.dumps(settings.get('currency','KSh'))};function paintMpesaLive(payload){{var box=document.getElementById('mpesa-live-messages');if(!box||!payload)return;var items=payload.messages||[];if(!items.length){{box.innerHTML='<div class="mpesa-live-empty">Waiting for M-Pesa payment messages…</div>';return;}}box.innerHTML=items.map(function(i){{var safeTitle=String(i.title||'M-PESA PAYMENT').replace(/[&<>\"]/g,function(c){{return {{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}}[c]}});return '<div class="mpesa-live-item"><span class="mpesa-live-dot '+(i.source==='C2B'?'pulse':'')+'"></span><div class="mpesa-live-main"><b>'+safeTitle+'</b><span>'+String(i.ts||'')+' · '+String(i.detail||'')+'</span></div><span class="mpesa-live-amt">'+currencySymbol+' '+Number(i.amount||0).toLocaleString('en-KE',{{minimumFractionDigits:2,maximumFractionDigits:2}})+'</span></div>'}}).join('')}};function pollMpesaLive(){{var box=document.getElementById('mpesa-live-messages');if(!box)return;fetch('/api/mpesa/live-messages'+(location.search||''),{{cache:'no-store',credentials:'same-origin'}}).then(function(r){{if(r.status===401){{location.href='/login';return null;}}return r.ok?r.json():null;}}).then(paintMpesaLive).catch(function(){{}})}};if(document.getElementById('mpesa-live-messages')){{pollMpesaLive();setInterval(pollMpesaLive,Math.max(2,mpesaPollSeconds)*1000);}}var idleMs={idle_ms},lastActivity=Date.now();function touch(){{lastActivity=Date.now()}};['click','keydown','touchstart','input','change','scroll'].forEach(function(ev){{document.addEventListener(ev,touch,{{passive:true}})}});setInterval(function(){{if(Date.now()-lastActivity>=idleMs){{location.href='/logout?auto=1'}}}},1000);}})();</script></body></html>'''

FLASH = {}
CURRENT_HANDLER = None

def add_flash(token, msg, kind='success'):
    if token: FLASH.setdefault(token, []).append((kind,msg))


def pop_flashes(token):
    return FLASH.pop(token, [])


def flash_html(handler=None):
    token = session_token(CURRENT_HANDLER) if CURRENT_HANDLER is not None else ''
    return ''.join(f'<div class="flash {esc(k)}">{esc(m)}</div>' for k,m in pop_flashes(token))


def csrf_input(handler):
    tok = session_token(handler); return f'<input type="hidden" name="csrf" value="{esc(csrf_for(tok))}">'


def current_token(handler):
    return session_token(handler)


def do_logout(handler, auto=False):
    tok = session_token(handler); u = session_user(handler, touch=False)
    if u:
        try:
            c = core.db.c(); core.db.audit(c,u,'AUTO_LOGOUT' if auto else 'LOGOUT','AUTH',u['id'],'Automatic inactivity logout' if auto else 'Logout'); c.commit(); c.close()
        except Exception: pass
    SESSIONS.pop(tok,None); CSRF.pop(tok,None); FLASH.pop(tok,None)
    handler.send_response(303); handler.send_header('Location','/login'); handler.send_header('Set-Cookie',f'kaka_session=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax{COOKIE_SUFFIX}'); handler.send_header('Content-Length','0'); handler.end_headers()


def _indexed_lines(form, prefix, fields, minimum=1):
    """Read dynamically added form rows with names such as product_id_1, product_id_2."""
    try:
        count = max(1, min(200, int(form.get(prefix + '_line_count', '1') or 1)))
    except (TypeError, ValueError):
        count = 1
    lines=[]
    for i in range(1, count+1):
        row={field: str(form.get(f'{field}_{i}', '') or '').strip() for field in fields}
        if not any(row.values()):
            continue
        lines.append(row)
    if len(lines) < minimum:
        raise ValueError('Enter at least one complete line before saving.')
    return lines


def save_purchase(u, form):
    supplier=form.get('supplier','').strip()
    method=form.get('payment_method','Cash')
    ref=form.get('reference','').strip()
    if not supplier:
        raise ValueError('Supplier is required.')
    if method not in ('Cash','M-Pesa','Credit'):
        raise ValueError('Invalid payment method.')
    if method=='M-Pesa' and not ref:
        raise ValueError('M-Pesa code/reference is required for M-Pesa purchases.')

    # Support both the new multi-line form and the legacy single-line fields.
    if 'product_id_1' in form or 'purchase_line_count' in form:
        lines=_indexed_lines(form,'purchase',('product_id','qty','unit_cost'))
    else:
        lines=[{'product_id':str(form.get('product_id','')), 'qty':str(form.get('qty','')), 'unit_cost':str(form.get('unit_cost',''))}]
    clean=[]; total=0.0
    for row in lines:
        try:
            product_id=int(row.get('product_id') or 0)
            qty=int(row.get('qty') or 0)
            cost=float(row.get('unit_cost') or 0)
        except (TypeError,ValueError):
            raise ValueError('Each purchase line needs a product, whole-number quantity and valid unit cost.')
        if product_id<=0 or qty<=0 or cost<0:
            raise ValueError('Each purchase line needs a product, quantity greater than zero and non-negative unit cost.')
        clean.append((product_id,qty,cost,qty*cost)); total+=qty*cost

    c=core.db.c()
    try:
        c.execute('BEGIN IMMEDIATE')
        branch_id=(u.get('branch_id') if role_name(u) not in ('ADMIN','MANAGEMENT') else (int(form.get('branch_id') or 0) or u.get('branch_id')))
        if not branch_id and role_name(u) not in ('ADMIN','MANAGEMENT'): raise ValueError('Assign this Sales Person to a branch in User Management before recording purchases.')
        if not branch_id and role_name(u) in ('ADMIN','MANAGEMENT'): raise ValueError('Select the receiving branch before saving this purchase.')
        if not c.execute('SELECT 1 FROM branches WHERE id=? AND active=1',(branch_id,)).fetchone(): raise ValueError('Selected branch is inactive or unavailable.')
        products={}
        for product_id,qty,cost,line_total in clean:
            p=c.execute('SELECT * FROM products WHERE id=? AND active=1',(product_id,)).fetchone()
            if not p:
                raise ValueError(f'Product #{product_id} was not found or is inactive.')
            c.execute('INSERT OR IGNORE INTO branch_stock(branch_id,product_id,qty,avg_cost,buy_price,sell_price,wholesale_price,reorder_level,updated_at) VALUES(?,?,0,?,?,?,?,?,?)',(branch_id,product_id,float(p['avg_cost'] or 0),float(p['avg_cost'] or 0),float(p['sell_price'] or 0),float(p['wholesale_price'] or p['sell_price'] or 0),float(p['reorder_level'] or 0),now()))
            products[product_id]=p
        ts=now()
        no='PUR-'+dt.datetime.now().strftime('%Y%m%d%H%M%S')+'-'+secrets.token_hex(2).upper()
        cur=c.execute('INSERT INTO purchases(purchase_no,purchased_at,user_id,supplier,payment_method,total,reference,branch_id) VALUES(?,?,?,?,?,?,?,?)',(no,ts,u['id'],supplier,method,total,ref,branch_id))
        pid=cur.lastrowid
        # Apply every line in one database transaction. Repeated products are handled sequentially.
        for product_id,qty,cost,line_total in clean:
            p=c.execute('SELECT * FROM products WHERE id=? AND active=1',(product_id,)).fetchone()
            bs=c.execute('SELECT * FROM branch_stock WHERE branch_id=? AND product_id=?',(branch_id,product_id)).fetchone()
            old=float(bs['qty']); new=old+qty
            avg=((old*float(bs['avg_cost'])+(qty*cost))/new) if new else cost
            c.execute('INSERT INTO purchase_lines(purchase_id,product_id,qty,unit_cost,total) VALUES(?,?,?,?,?)',(pid,product_id,qty,cost,line_total))
            c.execute('UPDATE branch_stock SET qty=?,avg_cost=?,buy_price=?,updated_at=? WHERE branch_id=? AND product_id=?',(new,avg,cost,ts,branch_id,product_id))
            c.execute('INSERT INTO stock_movements(movement_time,user_id,product_id,movement_type,qty,before_qty,after_qty,unit_cost,reference,branch_id) VALUES(?,?,?,?,?,?,?,?,?,?)',(ts,u['id'],product_id,'PURCHASE',qty,old,new,cost,no,branch_id))
        core.db.audit(c,u,'CREATE','PURCHASE',pid,f'{no} lines={len(clean)} total={total:.2f}')
        c.commit()
        return no,total
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def save_expense(u, form):
    method=form.get('payment_method','Cash')
    ref=form.get('reference','').strip()
    if method not in ('Cash','M-Pesa','Bank'):
        raise ValueError('Invalid expense payment method.')
    if 'category_1' in form or 'expense_line_count' in form:
        lines=_indexed_lines(form,'expense',('category','description','amount'))
    else:
        lines=[{'category':str(form.get('category','')), 'description':str(form.get('description','')), 'amount':str(form.get('amount',''))}]
    clean=[]
    for row in lines:
        cat=row.get('category','').strip()
        desc=row.get('description','').strip()
        try: amount=float(row.get('amount') or 0)
        except (TypeError,ValueError): raise ValueError('Each expense line needs a valid amount.')
        if not cat or not desc or amount<=0:
            raise ValueError('Every expense line needs a category, description and amount greater than zero.')
        clean.append((cat,desc,amount))
    c=core.db.c()
    try:
        c.execute('BEGIN IMMEDIATE')
        branch_id=(int(u.get('branch_id') or 0) if role_name(u) not in ('ADMIN','MANAGEMENT') else int(form.get('branch_id') or 0))
        if not branch_id: raise ValueError('Select an active branch before saving expenses.')
        if not c.execute('SELECT 1 FROM branches WHERE id=? AND active=1',(branch_id,)).fetchone(): raise ValueError('Selected branch is inactive or unavailable.')
        ts=now(); numbers=[]
        for cat,desc,amount in clean:
            no='EXP-'+dt.datetime.now().strftime('%Y%m%d%H%M%S')+'-'+secrets.token_hex(2).upper()
            cur=c.execute('INSERT INTO expenses(expense_no,expense_date,user_id,category,description,amount,payment_method,reference,branch_id) VALUES(?,?,?,?,?,?,?,?,?)',(no,ts,u['id'],cat,desc,amount,method,ref,branch_id))
            core.db.audit(c,u,'CREATE','EXPENSE',cur.lastrowid,f'{no} amount={amount:.2f}')
            numbers.append(no)
        c.commit()
        return numbers
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def save_product(u, form):
    sku=form.get('sku','').strip(); name=form.get('name','').strip(); cat=form.get('category','').strip(); unit=form.get('unit','bottle').strip(); sell=float(form.get('sell_price','0') or 0); wholesale=float(form.get('wholesale_price',form.get('sell_price','0')) or 0); stock=float(form.get('stock_qty','0') or 0); opening_cost=float(form.get('cogs_unit_cost', form.get('opening_unit_cost','0')) or 0); reorder=float(form.get('reorder_level','5') or 5)
    if not sku or not name or not cat or sell<=0 or wholesale<=0 or stock<0 or opening_cost<0 or reorder<0: raise ValueError('Complete the product details with valid non-negative amounts.')
    if stock>0 and opening_cost<=0: raise ValueError('COGS / unit cost must be greater than zero when opening stock is entered. This is required for correct COGS.')
    c=core.db.c(); c.execute('BEGIN')
    try:
        ts=now(); avg_cost=opening_cost
        cur=c.execute('INSERT INTO products(product_no,sku,name,category,unit,sell_price,wholesale_price,avg_cost,stock_qty,reorder_level,active,created_at,updated_at) VALUES(NULL,?,?,?,?,?,?,?,?,?,1,?,?)',(sku,name,cat,unit,sell,wholesale,avg_cost,0,reorder,ts,ts)); pid=cur.lastrowid; product_no=f'PRD-{pid:05d}'
        c.execute('UPDATE products SET product_no=? WHERE id=?',(product_no,pid))
        branch_id=(u.get('branch_id') if role_name(u) not in ('ADMIN','MANAGEMENT') else (int(form.get('branch_id') or 0) or u.get('branch_id')))
        if not branch_id and role_name(u) not in ('ADMIN','MANAGEMENT'): raise ValueError('Assign this Sales Person to a branch in User Management before registering products.')
        if not branch_id and role_name(u) in ('ADMIN','MANAGEMENT'): raise ValueError('Select a branch for the opening stock before saving this product.')
        if not c.execute('SELECT 1 FROM branches WHERE id=? AND active=1',(branch_id,)).fetchone(): raise ValueError('Selected branch is inactive or unavailable.')
        for br in c.execute('SELECT id FROM branches WHERE active=1').fetchall():
            c.execute('INSERT OR IGNORE INTO branch_stock(branch_id,product_id,qty,avg_cost,buy_price,sell_price,wholesale_price,reorder_level,updated_at) VALUES(?,?,0,?,?,?,?,?,?)',(br['id'],pid,avg_cost,opening_cost,sell,wholesale,reorder,ts))
        c.execute('UPDATE branch_stock SET qty=?,avg_cost=?,buy_price=?,sell_price=?,wholesale_price=?,reorder_level=?,updated_at=? WHERE branch_id=? AND product_id=?',(stock,avg_cost,opening_cost,sell,wholesale,reorder,ts,branch_id,pid))
        if stock>0:
            c.execute('INSERT INTO stock_movements(movement_time,user_id,product_id,movement_type,qty,before_qty,after_qty,unit_cost,reference,branch_id) VALUES(?,?,?,?,?,?,?,?,?,?)',(ts,u['id'],pid,'OPENING_STOCK',stock,0,stock,opening_cost,product_no,branch_id))
        core.db.audit(c,u,'CREATE','PRODUCT',pid,f'{product_no} {name}; opening_stock={stock:g}; retail_price={sell:.2f}; wholesale_price={wholesale:.2f}; cogs_unit_cost={opening_cost:.2f}'); c.commit(); return pid,product_no
    except Exception:
        c.rollback(); raise
    finally: c.close()


def admin_update_product(u, form):
    if role_name(u)!='ADMIN': raise PermissionError('Administrator permission required.')
    pid=int(form.get('product_id','0') or 0); sku=form.get('sku','').strip(); name=form.get('name','').strip(); cat=form.get('category','').strip(); unit=form.get('unit','bottle').strip(); sell=float(form.get('sell_price','0') or 0); wholesale=float(form.get('wholesale_price',form.get('sell_price','0')) or 0); reorder=float(form.get('reorder_level','5') or 5); reason=form.get('reason','').strip()
    if not reason: reason='Administrator product edit'
    if pid<=0 or not sku or not name or not cat or sell<=0 or wholesale<=0 or reorder<0: raise ValueError('Complete the product details with positive retail and wholesale prices.')
    c=core.db.c()
    try:
        p=c.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone()
        if not p: raise ValueError('Product not found.')
        ts=now(); c.execute('UPDATE products SET sku=?,name=?,category=?,unit=?,sell_price=?,wholesale_price=?,reorder_level=?,updated_at=? WHERE id=?',(sku,name,cat,unit,sell,wholesale,reorder,ts,pid)); core.db.audit(c,u,'UPDATE','PRODUCT',pid,f'Edited {p["product_no"] or pid}; retail_price={sell:.2f}; wholesale_price={wholesale:.2f}; reorder={reorder}; reason={reason}'); c.commit()
    except Exception:
        c.rollback(); raise
    finally: c.close()



def admin_bulk_update_product_costs_prices(u, form):
    """Update cost, retail/wholesale prices and reorder threshold for one selected branch only."""
    if role_name(u)!='ADMIN': raise PermissionError('Administrator permission required.')
    branch_id=int(form.get('branch_id','0') or 0)
    reason=str(form.get('bulk_reason','')).strip() or 'Administrator branch-specific stock update'
    c=core.db.c(); changed=0
    try:
        c.execute('BEGIN IMMEDIATE')
        if not branch_id or not c.execute('SELECT 1 FROM branches WHERE id=? AND active=1',(branch_id,)).fetchone():
            raise ValueError('Select an active branch before saving branch-specific prices and COGS.')
        products=c.execute('SELECT * FROM products ORDER BY id').fetchall(); ts=now()
        for p in products:
            pid=int(p['id']); bs=c.execute('SELECT * FROM branch_stock WHERE branch_id=? AND product_id=?',(branch_id,pid)).fetchone()
            if not bs: raise ValueError(f'Missing branch stock row for {p["name"]}; refresh stock setup before saving.')
            retail=float(form.get(f'bulk_sell_{pid}',bs['sell_price']) or 0)
            wholesale=float(form.get(f'bulk_wholesale_{pid}',bs['wholesale_price'] if bs['wholesale_price'] is not None else retail) or 0)
            cost=float(form.get(f'bulk_cost_{pid}',bs['avg_cost']) or 0)
            reorder=float(form.get(f'bulk_reorder_{pid}',bs['reorder_level']) or 0)
            if retail<=0 or wholesale<=0 or cost<0 or reorder<0: raise ValueError(f'Invalid price, COGS or reorder level for {p["name"]}.')
            old_retail=float(bs['sell_price'] or 0); old_wholesale=float(bs['wholesale_price'] or 0); old_cost=float(bs['avg_cost'] or 0); old_reorder=float(bs['reorder_level'] or 0)
            if abs(retail-old_retail)>1e-9 or abs(wholesale-old_wholesale)>1e-9 or abs(cost-old_cost)>1e-9 or abs(reorder-old_reorder)>1e-9:
                c.execute('UPDATE branch_stock SET sell_price=?,wholesale_price=?,avg_cost=?,buy_price=?,reorder_level=?,updated_at=? WHERE branch_id=? AND product_id=?',(retail,wholesale,cost,cost,reorder,ts,branch_id,pid))
                if abs(cost-old_cost)>1e-9:
                    qty=float(bs['qty'] or 0)
                    c.execute('INSERT INTO stock_movements(movement_time,user_id,product_id,movement_type,qty,before_qty,after_qty,unit_cost,reference,branch_id) VALUES(?,?,?,?,?,?,?,?,?,?)',(ts,u['id'],pid,'COST_ADJUSTMENT',0,qty,qty,cost,reason,branch_id))
                core.db.audit(c,u,'BRANCH PRICE/COGS UPDATE','BRANCH STOCK',f'{branch_id}:{pid}',f'{p["name"]}; retail {old_retail:.2f}->{retail:.2f}; wholesale {old_wholesale:.2f}->{wholesale:.2f}; avg_cost {old_cost:.2f}->{cost:.2f}; reorder {old_reorder:g}->{reorder:g}; reason={reason}')
                changed+=1
        c.commit(); return changed
    except Exception:
        c.rollback(); raise
    finally: c.close()

def admin_set_stock(u, form):
    if role_name(u)!='ADMIN': raise PermissionError('Administrator permission required.')
    pid=int(form.get('product_id','0') or 0); new_qty=float(form.get('new_stock','-1') or -1); reason=form.get('reason','').strip()
    if len(reason)<5: raise ValueError('A reason of at least 5 characters is required for stock corrections.')
    if pid<=0 or new_qty<0: raise ValueError('Enter a valid stock quantity.')
    c=core.db.c()
    try:
        c.execute('BEGIN IMMEDIATE'); p=c.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone()
        if not p: raise ValueError('Product not found.')
        branch_id=(int(form.get('branch_id') or 0) if role_name(u) in ('ADMIN','MANAGEMENT') else int(u.get('branch_id') or 0))
        if not branch_id or not c.execute('SELECT 1 FROM branches WHERE id=? AND active=1',(branch_id,)).fetchone(): raise ValueError('Select an active branch before correcting stock.')
        bs=c.execute('SELECT * FROM branch_stock WHERE branch_id=? AND product_id=?',(branch_id,pid)).fetchone()
        if not bs: raise ValueError('No stock record exists for this product in the selected branch. Register or initialise the product first.')
        before=float(bs['qty']); after=float(new_qty); ts=now(); delta=after-before
        c.execute('UPDATE branch_stock SET qty=?,updated_at=? WHERE branch_id=? AND product_id=?',(after,ts,branch_id,pid)); c.execute('INSERT INTO stock_movements(movement_time,user_id,product_id,movement_type,qty,before_qty,after_qty,unit_cost,reference,branch_id) VALUES(?,?,?,?,?,?,?,?,?,?)',(ts,u['id'],pid,'ADMIN_ADJUSTMENT',delta,before,after,bs['avg_cost'],reason,branch_id)); core.db.audit(c,u,'UPDATE','STOCK',pid,f'{p["product_no"]} branch={branch_id} {before:g} -> {after:g}; {reason}'); c.commit()
    except Exception:
        c.rollback(); raise
    finally: c.close()


def record_opening_stock(u, form):
    if role_name(u) not in ('ADMIN','MANAGEMENT','SALES') or not allowed(u,'MANAGE_PRODUCTS'):
        raise PermissionError('Product Registration permission is required.')
    pid=int(form.get('product_id','0') or 0)
    qty=float(form.get('opening_qty','0') or 0)
    cost=float(form.get('opening_cost','0') or 0)
    reason=form.get('opening_reason','').strip()
    if pid<=0 or qty<=0 or cost<=0 or len(reason)<5:
        raise ValueError('Select a product, enter opening quantity and unit cost greater than zero, and provide a reason of at least 5 characters.')
    c=core.db.c()
    try:
        c.execute('BEGIN IMMEDIATE')
        if role_name(u) in ('ADMIN','MANAGEMENT'):
            branch_id=int(form.get('branch_id','0') or 0)
        else:
            branch_id=int(u.get('branch_id') or 0)
        if not branch_id or not c.execute('SELECT 1 FROM branches WHERE id=? AND active=1',(branch_id,)).fetchone():
            raise ValueError('Select an active branch.')
        p=c.execute('SELECT * FROM products WHERE id=? AND active=1',(pid,)).fetchone()
        if not p: raise ValueError('Product not found or inactive.')
        bs=c.execute('SELECT * FROM branch_stock WHERE branch_id=? AND product_id=?',(branch_id,pid)).fetchone()
        if not bs:
            c.execute('INSERT INTO branch_stock(branch_id,product_id,qty,avg_cost,buy_price,sell_price,wholesale_price,reorder_level,updated_at) VALUES(?,?,0,?,?,?,?,?,?)',
                      (branch_id,pid,0,cost,float(p['sell_price']),float(p['wholesale_price'] or p['sell_price']),float(p['reorder_level']),now()))
            bs=c.execute('SELECT * FROM branch_stock WHERE branch_id=? AND product_id=?',(branch_id,pid)).fetchone()
        prior=c.execute('SELECT COUNT(*) n FROM stock_movements WHERE branch_id=? AND product_id=?',(branch_id,pid)).fetchone()['n']
        if float(bs['qty'] or 0)>0 or prior:
            raise ValueError('Opening stock has already been recorded or stock activity exists for this product in this branch. Use an approved stock adjustment instead.')
        ts=now()
        c.execute('UPDATE branch_stock SET qty=?,avg_cost=?,buy_price=?,updated_at=? WHERE branch_id=? AND product_id=?',(qty,cost,cost,ts,branch_id,pid))
        c.execute('INSERT INTO stock_movements(movement_time,user_id,product_id,movement_type,qty,before_qty,after_qty,unit_cost,reference,branch_id) VALUES(?,?,?,?,?,?,?,?,?,?)',
                  (ts,u['id'],pid,'OPENING_STOCK',qty,0,qty,cost,'OPENING STOCK: '+reason,branch_id))
        core.db.audit(c,u,'OPENING STOCK','BRANCH STOCK',f'{branch_id}:{pid}',f'{p["name"]}; qty={qty:g}; unit_cost={cost:.2f}; reason={reason}')
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def stock_count_adjustment(u, form):
    require_perm_user(u,'VIEW_STOCK')
    role=role_name(u)
    branch_id=int(u.get('branch_id') or 0) if role not in ('ADMIN','MANAGEMENT') else int(form.get('branch_id','0') or 0)
    product_id=int(form.get('product_id','0') or 0)
    try: physical=float(form.get('physical_qty','-1'))
    except Exception: raise ValueError('Enter a valid physical count.')
    reason=str(form.get('count_reason','')).strip()
    if not branch_id or product_id<=0 or physical<0 or len(reason)<5: raise ValueError('Select a branch and product, enter a non-negative physical count, and give a reason of at least 5 characters.')
    c=core.db.c()
    try:
        c.execute('BEGIN IMMEDIATE')
        if not c.execute('SELECT 1 FROM branches WHERE id=? AND active=1',(branch_id,)).fetchone(): raise ValueError('Selected branch is inactive or unavailable.')
        p=c.execute('SELECT * FROM products WHERE id=? AND active=1',(product_id,)).fetchone()
        if not p: raise ValueError('Product not found or inactive.')
        bs=c.execute('SELECT * FROM branch_stock WHERE branch_id=? AND product_id=?',(branch_id,product_id)).fetchone()
        if not bs: raise ValueError('This product has no stock record for the selected branch.')
        before=float(bs['qty'] or 0); delta=round(physical-before,3); ts=now()
        if abs(delta)>0.000001:
            c.execute('UPDATE branch_stock SET qty=?,updated_at=? WHERE branch_id=? AND product_id=?',(physical,ts,branch_id,product_id))
            c.execute('INSERT INTO stock_movements(movement_time,user_id,product_id,movement_type,qty,before_qty,after_qty,unit_cost,reference,branch_id) VALUES(?,?,?,?,?,?,?,?,?,?)',(ts,u['id'],product_id,'STOCK_COUNT_ADJUSTMENT',delta,before,physical,float(bs['avg_cost'] or 0),reason,branch_id))
        c.execute("INSERT INTO audit_log(created_at,user_id,username_snapshot,action,entity,entity_id,details) VALUES(?,?,?,?,?,?,?)",(ts,u['id'],u.get('username',''),'STOCK COUNT','BRANCH STOCK',f'{branch_id}:{product_id}',f'{p["name"]}; system={before:g}; physical={physical:g}; variance={delta:+g}; reason={reason}'))
        c.commit(); return {'before':before,'physical':physical,'variance':delta,'product':p['name']}
    except Exception: c.rollback(); raise
    finally: c.close()


def stock_transfer_action(u, form):
    require_perm_user(u,'VIEW_STOCK')
    action=str(form.get('action','request')).lower(); role=role_name(u); ts=now()
    c=core.db.c()
    try:
        c.execute('BEGIN IMMEDIATE')
        if action=='request':
            source=int(u.get('branch_id') or 0) if role not in ('ADMIN','MANAGEMENT') else int(form.get('source_branch_id','0') or 0)
            dest=int(form.get('destination_branch_id','0') or 0); pid=int(form.get('product_id','0') or 0)
            qty=float(form.get('qty','0') or 0); reason=str(form.get('reason','')).strip()
            if not source or not dest or source==dest or pid<=0 or qty<=0 or len(reason)<5: raise ValueError('Select different source and destination branches, a product, quantity greater than zero, and a reason of at least 5 characters.')
            if not c.execute('SELECT 1 FROM branches WHERE id=? AND active=1',(source,)).fetchone() or not c.execute('SELECT 1 FROM branches WHERE id=? AND active=1',(dest,)).fetchone(): raise ValueError('Both branches must be active.')
            bs=c.execute('SELECT * FROM branch_stock WHERE branch_id=? AND product_id=?',(source,pid)).fetchone()
            if not bs: raise ValueError('Source branch has no stock record for this product.')
            if qty>float(bs['qty'] or 0): raise ValueError(f'Insufficient source branch stock. Available: {float(bs["qty"] or 0):g}.')
            no='TRF-'+dt.datetime.now().strftime('%Y%m%d%H%M%S')+'-'+secrets.token_hex(2).upper()
            c.execute("INSERT INTO stock_transfers(transfer_no,source_branch_id,destination_branch_id,product_id,qty,unit_cost,reason,status,requested_by,created_at) VALUES(?,?,?,?,?,?,?,'PENDING',?,?)",(no,source,dest,pid,qty,float(bs['avg_cost'] or 0),reason,u['id'],ts))
            core.db.audit(c,u,'REQUEST','STOCK TRANSFER',no,f'{source}->{dest}; product={pid}; qty={qty:g}; reason={reason}')
            c.commit(); return f'Transfer request {no} submitted for Management approval.'
        if role not in ('ADMIN','MANAGEMENT'): raise PermissionError('Only Administrator or Management can approve/reject stock transfers.')
        transfer_id=int(form.get('transfer_id','0') or 0); row=c.execute("SELECT * FROM stock_transfers WHERE id=? AND status='PENDING'",(transfer_id,)).fetchone()
        if not row: raise ValueError('Pending transfer request not found.')
        if action=='reject':
            reason=str(form.get('rejection_reason','')).strip()
            if len(reason)<5: raise ValueError('Enter a rejection reason of at least 5 characters.')
            c.execute("UPDATE stock_transfers SET status='REJECTED',approved_by=?,approved_at=?,rejection_reason=? WHERE id=?",(u['id'],ts,reason,transfer_id))
            core.db.audit(c,u,'REJECT','STOCK TRANSFER',row['transfer_no'],f'reason={reason}')
            c.commit(); return f'Transfer {row["transfer_no"]} rejected.'
        if action!='approve': raise ValueError('Unknown stock transfer action.')
        source=int(row['source_branch_id']); dest=int(row['destination_branch_id']); pid=int(row['product_id']); qty=float(row['qty'])
        src=c.execute('SELECT * FROM branch_stock WHERE branch_id=? AND product_id=?',(source,pid)).fetchone()
        if not src or float(src['qty'] or 0)<qty: raise ValueError('Approval blocked: source branch no longer has enough stock.')
        product=c.execute('SELECT * FROM products WHERE id=? AND active=1',(pid,)).fetchone()
        if not product: raise ValueError('Product is inactive or missing.')
        dst=c.execute('SELECT * FROM branch_stock WHERE branch_id=? AND product_id=?',(dest,pid)).fetchone()
        if not dst:
            c.execute('INSERT INTO branch_stock(branch_id,product_id,qty,avg_cost,buy_price,sell_price,wholesale_price,reorder_level,updated_at) VALUES(?,?,0,?,?,?,?,?,?)',(dest,pid,float(src['avg_cost'] or 0),float(src['buy_price'] or src['avg_cost'] or 0),float(src['sell_price'] or product['sell_price']),float(src['wholesale_price'] or product['wholesale_price'] or product['sell_price']),float(src['reorder_level'] or product['reorder_level']),ts))
            dst=c.execute('SELECT * FROM branch_stock WHERE branch_id=? AND product_id=?',(dest,pid)).fetchone()
        src_before=float(src['qty']); src_after=src_before-qty; dst_before=float(dst['qty']); dst_after=dst_before+qty; cost=float(src['avg_cost'] or row['unit_cost'] or 0)
        dst_cost=((dst_before*float(dst['avg_cost'] or 0))+(qty*cost))/dst_after if dst_after else cost
        c.execute('UPDATE branch_stock SET qty=?,updated_at=? WHERE branch_id=? AND product_id=?',(src_after,ts,source,pid))
        c.execute('UPDATE branch_stock SET qty=?,avg_cost=?,updated_at=? WHERE branch_id=? AND product_id=?',(dst_after,dst_cost,ts,dest,pid))
        c.execute('INSERT INTO stock_movements(movement_time,user_id,product_id,movement_type,qty,before_qty,after_qty,unit_cost,reference,branch_id) VALUES(?,?,?,?,?,?,?,?,?,?)',(ts,u['id'],pid,'TRANSFER_OUT',-qty,src_before,src_after,cost,row['transfer_no'],source))
        c.execute('INSERT INTO stock_movements(movement_time,user_id,product_id,movement_type,qty,before_qty,after_qty,unit_cost,reference,branch_id) VALUES(?,?,?,?,?,?,?,?,?,?)',(ts,u['id'],pid,'TRANSFER_IN',qty,dst_before,dst_after,cost,row['transfer_no'],dest))
        c.execute("UPDATE stock_transfers SET status='APPROVED',approved_by=?,approved_at=? WHERE id=?",(u['id'],ts,transfer_id))
        core.db.audit(c,u,'APPROVE','STOCK TRANSFER',row['transfer_no'],f'{source}->{dest}; product={product["name"]}; qty={qty:g}; source {src_before:g}->{src_after:g}; destination {dst_before:g}->{dst_after:g}')
        c.commit(); return f'Transfer {row["transfer_no"]} approved. Stock moved from source to destination.'
    except Exception: c.rollback(); raise
    finally: c.close()


def admin_set_cost(u, form):
    if role_name(u)!='ADMIN': raise PermissionError('Administrator permission required.')
    pid=int(form.get('product_id','0') or 0); branch_id=int(form.get('branch_id','0') or 0); new_cost=float(form.get('new_avg_cost','-1') or -1); reason=form.get('reason','').strip()
    if pid<=0 or branch_id<=0 or new_cost<=0 or len(reason)<5: raise ValueError('Select a branch and product, enter a positive average unit cost, and give a reason of at least 5 characters.')
    c=core.db.c()
    try:
        c.execute('BEGIN IMMEDIATE')
        bs=c.execute('SELECT bs.*,p.name,p.product_no FROM branch_stock bs JOIN products p ON p.id=bs.product_id WHERE bs.branch_id=? AND bs.product_id=?',(branch_id,pid)).fetchone()
        if not bs: raise ValueError('Product has no stock record in the selected branch.')
        ts=now(); qty=float(bs['qty'] or 0); old=float(bs['avg_cost'] or 0)
        c.execute('UPDATE branch_stock SET avg_cost=?,buy_price=?,updated_at=? WHERE branch_id=? AND product_id=?',(new_cost,new_cost,ts,branch_id,pid))
        c.execute('INSERT INTO stock_movements(movement_time,user_id,product_id,movement_type,qty,before_qty,after_qty,unit_cost,reference,branch_id) VALUES(?,?,?,?,?,?,?,?,?,?)',(ts,u['id'],pid,'COST_ADJUSTMENT',0,qty,qty,new_cost,reason,branch_id))
        core.db.audit(c,u,'UPDATE','BRANCH PRODUCT COST',f'{branch_id}:{pid}',f'{bs["name"]} avg_cost {old:.2f} -> {new_cost:.2f}; reason={reason}')
        c.commit()
    except Exception:
        c.rollback(); raise
    finally: c.close()
def admin_product_delete(u, product_id, restore=False, reason=''):
    if role_name(u)!='ADMIN': raise PermissionError('Administrator permission required.')
    reason=str(reason).strip()
    if len(reason)<5: raise ValueError('A reason of at least 5 characters is required to archive, delete or restore a product.')
    pid=int(product_id); c=core.db.c()
    try:
        c.execute('BEGIN'); p=c.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone()
        if not p: raise ValueError('Product not found.')
        ts=now(); active=1 if restore else 0; action='RESTORE' if restore else 'DELETE / ARCHIVE'; c.execute('UPDATE products SET active=?,updated_at=? WHERE id=?',(active,ts,pid)); core.db.audit(c,u,action,'PRODUCT',pid,f'{p["product_no"]} {p["name"]}; reason={reason}'); c.commit()
    except Exception:
        c.rollback(); raise
    finally: c.close()


def save_user(u, form):
    username=form.get('username','').strip(); full=form.get('full_name','').strip(); pw=form.get('password',''); access_role=form.get('role','SALES').upper(); branch_id=int(form.get('branch_id','0') or 0)
    if not username or not full or len(pw)<8: raise ValueError('Username, full name and password (minimum 8 characters) are required.')
    if access_role not in ('ADMIN','MANAGEMENT','SALES'): raise ValueError('Invalid role.')
    if access_role=='SALES' and branch_id<=0: raise ValueError('Every Sales Person must be assigned to a branch.')
    storage_role='ADMIN' if access_role=='ADMIN' else 'SALES'
    home_branch_id=branch_id if access_role=='SALES' else None
    c=core.db.c(); c.execute('BEGIN')
    try:
        if branch_id and not c.execute('SELECT 1 FROM branches WHERE id=? AND active=1',(branch_id,)).fetchone(): raise ValueError('Select an active branch.')
        ts=now(); cur=c.execute('INSERT INTO users(username,full_name,password_hash,role,access_role,active,created_at,updated_at,branch_id,home_branch_id) VALUES(?,?,?,?,?,?,?,?,?,?)',(username,full,core.sha(pw),storage_role,access_role,1,ts,ts,branch_id or None,home_branch_id)); core.db.audit(c,u,'CREATE','USER',cur.lastrowid,f'{username} role={access_role}; branch_id={branch_id or ""}; home_branch_id={home_branch_id or ""}'); c.commit(); return cur.lastrowid
    except Exception:
        c.rollback(); raise
    finally: c.close()


def admin_edit_user(u, form):
    if role_name(u)!='ADMIN': raise PermissionError('Administrator permission required.')
    uid=int(form.get('user_id','0') or 0); full=form.get('full_name','').strip(); access_role=form.get('role','SALES').upper(); active=1 if form.get('active','1')=='1' else 0; branch_id=int(form.get('branch_id','0') or 0)
    if uid<=0 or not full or access_role not in ('ADMIN','MANAGEMENT','SALES'): raise ValueError('Complete valid user details.')
    if access_role=='SALES' and branch_id<=0: raise ValueError('Every Sales Person must be assigned to a branch.')
    c=core.db.c()
    try:
        c.execute('BEGIN'); target=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
        if not target: raise ValueError('User not found.')
        # Preserve a Sales Person's first assigned branch even if the account is
        # temporarily changed to Management or Administrator and later changed back.
        home_branch_id=int(target['home_branch_id'] or (target['branch_id'] if target['access_role']=='SALES' else 0) or 0) if 'home_branch_id' in target.keys() else (int(target['branch_id'] or 0) if target['access_role']=='SALES' else 0)
        if home_branch_id:
            if branch_id and branch_id!=home_branch_id:
                raise PermissionError('This account is permanently linked to its original branch and cannot be moved to another branch.')
            branch_id=home_branch_id
        elif access_role=='SALES':
            if branch_id<=0: raise ValueError('Every Sales Person must be assigned to a branch.')
            home_branch_id=branch_id
        if branch_id and not c.execute('SELECT 1 FROM branches WHERE id=? AND active=1',(branch_id,)).fetchone(): raise ValueError('Select an active branch.')
        if uid==u['id'] and (active==0 or access_role!='ADMIN'): raise ValueError('You cannot remove or restrict your own Administrator access.')
        if target['access_role']=='ADMIN' and (active==0 or access_role!='ADMIN'):
            admins=c.execute("SELECT COUNT(*) n FROM users WHERE access_role='ADMIN' AND active=1").fetchone()['n']
            if admins<=1: raise ValueError('At least one active Administrator must remain.')
        storage_role='ADMIN' if access_role=='ADMIN' else 'SALES'; ts=now()
        if target['access_role']!=access_role:
            # Old per-user overrides must not silently defeat the newly assigned role profile.
            c.execute('DELETE FROM user_permissions WHERE user_id=?',(uid,))
        c.execute('UPDATE users SET full_name=?,role=?,access_role=?,active=?,updated_at=?,branch_id=?,home_branch_id=? WHERE id=?',(full,storage_role,access_role,active,ts,branch_id or None,home_branch_id or None,uid))
        core.db.audit(c,u,'UPDATE','USER',uid,f'Edited {target["username"]}: role={access_role}; active={active}; branch_id={branch_id or ""}; home_branch_id={home_branch_id or ""}; role_overrides_reset={target["access_role"]!=access_role}')
        c.commit()
    except Exception:
        c.rollback(); raise
    finally: c.close()


def admin_reset_password(u, user_id, new_password):
    if role_name(u)!='ADMIN': raise PermissionError('Administrator permission required.')
    user_id=int(user_id); pw=str(new_password or '')
    if len(pw)<8: raise ValueError('New password must be at least 8 characters.')
    c=core.db.c()
    try:
        c.execute('BEGIN'); target=c.execute('SELECT * FROM users WHERE id=?',(user_id,)).fetchone()
        if not target: raise ValueError('User not found.')
        c.execute('UPDATE users SET password_hash=?,updated_at=? WHERE id=?',(core.sha(pw),now(),user_id)); core.db.audit(c,u,'PASSWORD_RESET','USER',user_id,f'Administrator reset password for {target["username"]}'); c.commit()
    except Exception:
        c.rollback(); raise
    finally: c.close()


def admin_toggle_user(u, user_id, active):
    if role_name(u)!='ADMIN': raise PermissionError('Administrator permission required.')
    user_id=int(user_id); active=1 if int(active) else 0; c=core.db.c()
    try:
        c.execute('BEGIN'); target=c.execute('SELECT * FROM users WHERE id=?',(user_id,)).fetchone()
        if not target: raise ValueError('User not found.')
        if user_id==u['id'] and not active: raise ValueError('You cannot disable your own Administrator access.')
        if target['access_role']=='ADMIN' and not active:
            admins=c.execute("SELECT COUNT(*) n FROM users WHERE access_role='ADMIN' AND active=1").fetchone()['n']
            if admins<=1: raise ValueError('At least one active Administrator must remain.')
        c.execute('UPDATE users SET active=?,updated_at=? WHERE id=?',(active,now(),user_id)); core.db.audit(c,u,'UPDATE','USER',user_id,('ACCESS ENABLED' if active else 'ACCESS RESTRICTED')); c.commit()
    except Exception:
        c.rollback(); raise
    finally: c.close()


def admin_delete_user(u, user_id):
    if role_name(u)!='ADMIN': raise PermissionError('Administrator permission required.')
    user_id=int(user_id); c=core.db.c()
    try:
        target=c.execute('SELECT * FROM users WHERE id=?',(user_id,)).fetchone()
        if not target: raise ValueError('User not found.')
        if user_id==u['id']: raise ValueError('You cannot delete your own account.')
        if target['access_role']=='ADMIN':
            admins=c.execute("SELECT COUNT(*) n FROM users WHERE access_role='ADMIN' AND active=1").fetchone()['n']
            if admins<=1: raise ValueError('At least one active Administrator must remain.')
        refs=0
        for table,col in [('sales','user_id'),('sale_payments','user_id'),('purchases','user_id'),('expenses','user_id'),('stock_movements','user_id'),('credit_payments','user_id'),('audit_log','user_id')]:
            refs += c.execute(f'SELECT COUNT(*) n FROM {table} WHERE {col}=?',(user_id,)).fetchone()['n']
        c.execute('BEGIN')
        if refs:
            c.execute('UPDATE users SET active=0,updated_at=? WHERE id=?',(now(),user_id)); action='DELETE REQUEST -> ACCESS RESTRICTED (historical records retained)'
        else:
            c.execute('DELETE FROM users WHERE id=?',(user_id,)); action='DELETE USER'
        core.db.audit(c,u,'DELETE' if not refs else 'UPDATE','USER',user_id,f'{target["username"]}; refs={refs}'); c.commit()
    except Exception:
        c.rollback(); raise
    finally: c.close()


def html_table(headers, rows, empty='No records.'):
    body=''.join('<tr>'+''.join(f'<td>{cell}</td>' for cell in r)+'</tr>' for r in rows)
    if not body: body=f'<tr><td colspan="{len(headers)}" class="empty">{esc(empty)}</td></tr>'
    return '<div class="table-wrap"><table><thead><tr>'+''.join(f'<th>{esc(h)}</th>' for h in headers)+'</tr></thead><tbody>'+body+'</tbody></table></div>'


def make_pdf(title, headers, rows):
    """Create a paginated, landscape, text-based business report PDF (never a screenshot)."""
    st=core.get_settings()
    rows=[list(r) for r in rows]
    wide=len(headers)>9
    page_w,page_h=(1191,842) if wide else (842,595)
    margin=34
    usable=page_w-2*margin
    # Allocate more space to descriptive columns while retaining a readable minimum.
    weights=[]
    for i,h in enumerate(headers):
        samples=[str(r[i] if i<len(r) else '') for r in rows[:120]]
        weights.append(max(8,min(30,max([len(str(h))]+[min(len(x),30) for x in samples])*.72)))
    minw=34 if wide else 48
    widths=[max(minw,usable*w/sum(weights)) for w in weights]
    scale=usable/sum(widths); widths=[w*scale for w in widths]
    def clean(v):
        s=str(v if v is not None else '')
        s=s.replace('KES ','').replace('\u00a0',' ')
        return s.encode('latin-1','replace').decode('latin-1')
    def esc(v):
        return clean(v).replace('\\','\\\\').replace('(','\\(').replace(')','\\)').replace('\r',' ').replace('\n',' ')
    def wrap(value, width, size):
        text=clean(value)
        limit=max(4,int((width-8)/(size*.50)))
        words=text.split()
        out=[]; line=''
        for word in words:
            while len(word)>limit:
                if line: out.append(line); line=''
                out.append(word[:limit]); word=word[limit:]
            candidate=(line+' '+word).strip()
            if len(candidate)>limit and line:
                out.append(line); line=word
            else: line=candidate
        if line or not out: out.append(line)
        return out
    pages=[]
    body_rows=rows or [['No records found for the selected period.']]
    chunks=[]; current=[]; estimated=0
    for row in body_rows:
        cells=[]
        for i in range(len(headers)):
            value=row[i] if i<len(row) else ''
            cells.append(wrap(value,widths[i],6.4 if wide else 7.2))
        row_h=max(18,max(len(c) for c in cells)*(8 if wide else 9)+7)
        if current and estimated+row_h>page_h-155:
            chunks.append(current); current=[]; estimated=0
        current.append((cells,row_h)); estimated+=row_h
    if current: chunks.append(current)
    if not chunks: chunks=[[]]
    for page_no,chunk in enumerate(chunks,1):
        cmds=[]
        def rect(x,y,w,h,fill=None,stroke=None):
            if fill: cmds.append(f'{fill[0]} {fill[1]} {fill[2]} rg')
            if stroke: cmds.append(f'{stroke[0]} {stroke[1]} {stroke[2]} RG 0.45 w')
            op='B' if fill and stroke else ('f' if fill else 'S')
            cmds.append(f'{x:.2f} {y:.2f} {w:.2f} {h:.2f} re {op}')
        def txt(x,y,text,size=8,bold=False,rgb=(.12,.18,.15)):
            cmds.append(f'{rgb[0]} {rgb[1]} {rgb[2]} rg BT /{"F2" if bold else "F1"} {size} Tf {x:.2f} {y:.2f} Td ({esc(text)}) Tj ET')
        y=page_h-38
        txt(margin,y,st.get('business_name',APP_NAME),16,True,(.05,.22,.16)); y-=19
        identity='  |  '.join(x for x in [st.get('business_branch_name',''),st.get('business_phone',''),st.get('business_address',''),st.get('business_website','')] if x)
        if identity: txt(margin,y,identity,7); y-=13
        if st.get('business_tax_pin'): txt(margin,y,'KRA PIN: '+st.get('business_tax_pin',''),7); y-=13
        txt(margin,y,clean(title),12,True,(.05,.22,.16)); y-=16
        txt(margin,y,'Generated: '+now()+'    |    Page '+str(page_no)+' of '+str(len(chunks)),7); y-=20
        x=margin; header_h=24
        rect(margin,y-header_h,usable,header_h,(.05,.22,.16),(.05,.22,.16))
        for i,h in enumerate(headers):
            txt(x+4,y-15,wrap(h,widths[i]-4,7)[0],6.8,True,(1,1,1)); x+=widths[i]
        y-=header_h
        for ridx,(cells,row_h) in enumerate(chunk):
            x=margin
            rect(margin,y-row_h,usable,row_h,(.965,.975,.968) if ridx%2==0 else (1,1,1),(.83,.87,.84))
            for i,lines in enumerate(cells):
                for j,line in enumerate(lines):
                    if j* (8 if wide else 9) >= row_h-4: break
                    txt(x+4,y-11-j*(8 if wide else 9),line,6.4 if wide else 7.2,False)
                x+=widths[i]
            # Vertical rules keep columns distinct.
            x=margin
            for w in widths[:-1]:
                x+=w; cmds.append(f'.86 .89 .87 RG .35 w {x:.2f} {y-row_h:.2f} m {x:.2f} {y:.2f} l S')
            y-=row_h
        footer=st.get('report_footer','').strip()
        if footer: txt(margin,22,footer[:180],7)
        txt(page_w-margin-130,22,'MONTEZ LIQUOR  |  CONFIDENTIAL',6,False,(.35,.4,.37))
        stream='\n'.join(cmds).encode('latin-1','replace')
        pages.append(stream)
    # Assemble a valid multi-page PDF with reusable Helvetica fonts.
    objects=[None,None]
    page_ids=[]
    for stream in pages:
        page_id=len(objects)+1; content_id=page_id+1; page_ids.append(page_id)
        objects.append(f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_w} {page_h}] /Resources << /Font << /F1 {len(objects)+3} 0 R /F2 {len(objects)+4} 0 R >> >> /Contents {content_id} 0 R >>'.encode())
        objects.append(f'<< /Length {len(stream)} >>\nstream\n'.encode()+stream+b'\nendstream')
    font1=len(objects)+1; objects.append(b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>')
    font2=len(objects)+1; objects.append(b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>')
    objects[0]=b'<< /Type /Catalog /Pages 2 0 R >>'
    kids=' '.join(f'{i} 0 R' for i in page_ids)
    objects[1]=f'<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>'.encode()
    # Correct page font references after object numbering is final.
    for idx,pid in enumerate(page_ids):
        content_id=pid+1
        objects[pid-1]=f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_w} {page_h}] /Resources << /Font << /F1 {font1} 0 R /F2 {font2} 0 R >> >> /Contents {content_id} 0 R >>'.encode()
    out=io.BytesIO(); out.write(b'%PDF-1.4\n%MONTEZ-REPORT\n'); offsets=[0]
    for n,obj in enumerate(objects,1):
        offsets.append(out.tell()); out.write(f'{n} 0 obj\n'.encode()); out.write(obj); out.write(b'\nendobj\n')
    xref=out.tell(); out.write(f'xref\n0 {len(objects)+1}\n0000000000 65535 f \n'.encode())
    for off in offsets[1:]: out.write(f'{off:010d} 00000 n \n'.encode())
    out.write(f'trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF'.encode())
    return out.getvalue()

def report_rows(start, end): return core.report_data(start,end)


def build_xlsx(title, headers, rows): return core.xlsx_bytes(title,headers,rows)


CSS = r'''
:root{--bg:#f4f7f5;--card:#fff;--ink:#18241f;--muted:#68756f;--line:#dfe8e3;--green:#0d3b2f;--green2:#164f40;--gold:#d7ae5c;--red:#a92f33;--good:#1a6a49}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:Segoe UI,Arial,sans-serif}a{color:inherit;text-decoration:none}button,input,select{font:inherit}button{cursor:pointer}.sidebar{position:fixed;left:0;top:0;bottom:0;width:248px;background:linear-gradient(180deg,#0b3026,#123e31);color:#e9f2ee;padding:18px 14px;display:flex;flex-direction:column;z-index:20}.brand{display:flex;gap:10px;align-items:center;padding:5px 6px 18px}.brand img{width:46px;height:46px}.brand b{display:block;font-size:16px}.brand small{display:block;font-size:9px;letter-spacing:1.5px;color:#c8ddd5}.sidebar nav{display:flex;flex-direction:column;gap:4px;overflow:auto}.nav{padding:10px 12px;border-radius:9px;color:#dcebe5;font-size:13px}.nav:hover,.nav.active{background:#1b5444}.nav.logout{background:#7f292d;margin-top:4px}.side-bottom{margin-top:auto}.userbox{background:#174739;border:1px solid #2c6655;padding:11px;border-radius:10px;margin-bottom:7px;font-size:12px}.userbox b{font-size:13px}.userbox span{display:block;color:#bdd0c9;margin-top:2px}.mobile-head{display:none}main{margin-left:248px;padding:20px;min-height:100vh}.topbar{display:flex;justify-content:space-between;align-items:start;margin-bottom:18px}.topbar h1{margin:0 0 4px;font-size:27px}.clock{font-size:12px;background:#fff;border:1px solid var(--line);padding:9px 11px;border-radius:9px;font-weight:700}.muted{color:var(--muted);font-size:12px}.grid{display:grid;gap:14px}.two{grid-template-columns:repeat(2,1fr)}.three{grid-template-columns:repeat(3,1fr)}.four{grid-template-columns:repeat(4,1fr)}.card{background:var(--card);border:1px solid var(--line);border-radius:15px;padding:16px;box-shadow:0 5px 18px rgba(17,46,36,.05)}.kpis{grid-template-columns:repeat(6,1fr)}.kpi .label{font-size:11px;color:var(--muted)}.kpi strong{display:block;font-size:22px;margin-top:5px}.section-head,.report-head{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:11px}.section-head h2,.section-head h3{margin:0}.pill{font-size:10px;padding:5px 8px;border-radius:99px;background:#edf4f0;color:var(--green)}.btn{display:inline-block;border:1px solid #ccd8d2;background:#fff;color:var(--ink);border-radius:9px;padding:9px 12px;font-weight:700}.btn.primary{background:var(--green);color:#fff;border-color:var(--green)}.btn.gold{background:var(--gold);border-color:var(--gold);color:#221908}.btn.danger{background:var(--red);color:#fff;border-color:var(--red)}.movement-controls{position:static!important;z-index:auto!important;background:transparent!important;padding:0!important;margin:0 0 14px!important;display:flex;gap:8px;flex-wrap:wrap}.movement-controls .btn{min-height:40px}.full{width:100%}.form-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.field label,label{display:block;font-size:12px;font-weight:700;margin-bottom:5px}.field input,.field select,input,select{width:100%;padding:10px;border:1px solid #ccd8d2;border-radius:8px;background:#fff}.actions{display:flex;gap:8px;flex-wrap:wrap}.table-wrap{overflow:auto}.table-wrap table{width:100%;border-collapse:collapse;font-size:12px}.table-wrap th,.table-wrap td{padding:9px;border-bottom:1px solid #edf1ef;text-align:left;white-space:nowrap}.table-wrap th{font-size:11px;background:#f8faf9}.flash{padding:10px 12px;border-radius:9px;margin-bottom:12px;font-size:13px}.flash.success{background:#eaf7ef;color:#12603b}.flash.error{background:#fdecec;color:#9c1f25}.notice{padding:10px;border-radius:8px;background:#eef6f1;color:#175c42}.empty{text-align:center;color:var(--muted);padding:20px}.toolbar{display:flex;flex-wrap:wrap;align-items:end;gap:8px}.pos-grid{display:grid;grid-template-columns:1.45fr .9fr;gap:14px}.cart-line,.pay-line{display:grid;gap:7px;align-items:end;margin:8px 0}.cart-line{grid-template-columns:1fr 90px 120px}.pay-line{grid-template-columns:100px 110px 1fr}.total-box{display:flex;justify-content:space-between;align-items:end;border-top:1px solid var(--line);margin-top:12px;padding-top:12px}.total-box strong{font-size:25px}.barchart{display:flex;align-items:end;gap:7px;height:190px}.bar-item{flex:1;height:100%;display:flex;flex-direction:column;justify-content:end;align-items:center}.bar{width:75%;max-width:30px;background:linear-gradient(180deg,var(--gold),var(--green));border-radius:5px 5px 0 0;min-height:2px}.bar-item span{font-size:9px;color:var(--muted);margin-top:5px}.login-page{min-height:100vh;display:grid;place-items:center;background:radial-gradient(circle at top right,#4a7b6b,#11382d 45%,#071b15)}.login-card{width:min(760px,92vw);background:#fff;border-radius:23px;padding:28px;box-shadow:0 22px 70px rgba(0,0,0,.28);text-align:left}.logo-wrap{display:flex;justify-content:center;align-items:center;gap:14px}.login-logo{width:65px;height:65px}.brand-big{font-size:27px;font-weight:900;letter-spacing:.3px}.subbrand{font-size:10px;color:#a2782c;font-weight:900;letter-spacing:2.2px}.bottles{display:flex;justify-content:center;gap:18px;align-items:end;margin:18px 0}.bottle{display:block;width:62px;border-radius:12px 12px 16px 16px;position:relative;box-shadow:0 14px 18px rgba(0,0,0,.12)}.bottle:before{content:'';position:absolute;width:22px;height:26px;left:20px;top:-23px;background:#cfae68;border-radius:4px}.bottle.wine{height:128px;background:linear-gradient(90deg,#cfd9c6,#eef4ea)}.bottle.red{height:115px;background:linear-gradient(90deg,#87332f,#541915)}.bottle.dark{height:105px;background:linear-gradient(90deg,#1d4a40,#0b231e)}.login-card h1{margin:3px 0}.login-card form{max-width:430px;margin:18px auto}.login-card input{margin:6px 0 11px}.login-note{max-width:600px;margin:13px auto 0;padding:11px;background:#fff8e2;border-radius:9px;font-size:11px;color:#6c5722}.quick-report-links{display:flex;flex-wrap:wrap;gap:6px;margin-top:7px}.quick-report-wrap{margin-top:12px}.quick-report-wrap>b{display:block;font-size:12px;margin-bottom:4px}.report-tools{display:flex;flex-wrap:wrap;gap:8px;align-items:end}.metric{padding:13px;border:1px solid var(--line);border-radius:11px}.metric .v{font-size:18px;font-weight:800;margin-top:5px}@media(max-width:1200px){.kpis{grid-template-columns:repeat(3,1fr)}.two{grid-template-columns:1fr}.form-grid{grid-template-columns:repeat(2,1fr)}}@media(max-width:800px){.sidebar{transform:translateX(-100%);transition:.2s;width:275px}.open-nav .sidebar{transform:translateX(0)}.mobile-head{display:flex;position:sticky;top:0;z-index:30;background:var(--green);color:#fff;justify-content:space-between;align-items:center;padding:10px 13px}.brand-mini{display:flex;align-items:center;gap:8px;font-weight:800}.brand-mini img{width:33px}.menu{background:transparent;color:#fff;border:0;font-size:23px}main{margin-left:0;padding:12px}.topbar{display:block}.clock{display:inline-block;margin-top:8px}.kpis{grid-template-columns:repeat(2,1fr)}.three,.four,.form-grid{grid-template-columns:1fr}.pos-grid{grid-template-columns:1fr}.cart-line,.pay-line{grid-template-columns:1fr 90px 110px}.login-card{padding:22px}.login-logo{width:54px;height:54px}.brand-big{font-size:23px}}
@media print{@page{size:landscape;margin:12mm}html,body{background:#fff!important;color:#111!important;font-size:10pt!important;-webkit-print-color-adjust:exact;print-color-adjust:exact}.sidebar,.mobile-head,.topbar,.btn,.actions,.toolbar,.inline-actions,.movement-controls,form{display:none!important}main{margin:0!important;padding:0!important;width:100%!important;min-height:0!important}.card{box-shadow:none!important;border:0!important;border-radius:0!important;padding:0!important;margin:0 0 10mm!important;break-inside:avoid}.report-print{display:block!important;width:100%!important}.table-wrap{overflow:visible!important}.table-wrap table{width:100%!important;border-collapse:collapse!important;font-size:8pt!important}.table-wrap th,.table-wrap td{white-space:normal!important;padding:5px!important;border:1px solid #b9c5be!important}.table-wrap th{background:#e8eee9!important;color:#132a20!important}.metric,.notice{break-inside:avoid}.report-print h2{font-size:16pt!important}a{color:#111!important;text-decoration:none!important}}
.inline-actions{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:2px 0}.inline-actions input,.inline-actions select{padding:7px;font-size:11px;width:auto}.inline-actions .btn{padding:7px 9px;font-size:11px}.notice{padding:10px;border-radius:8px;background:#eef6f1;color:#175c42;font-size:12px;margin-bottom:10px}
/* Live M-Pesa feed */
.mpesa-live-card{min-height:170px}.mpesa-live-head{display:flex;justify-content:space-between;align-items:center;gap:10px}.mpesa-live-feed{display:flex;flex-direction:column;gap:7px;margin-top:10px;max-height:250px;overflow:auto}.mpesa-live-item{display:grid;grid-template-columns:auto 1fr auto;gap:9px;align-items:center;padding:9px 10px;border:1px solid var(--line);border-radius:10px;background:#fbfdfc}.mpesa-live-dot{width:8px;height:8px;border-radius:50%;background:#1a6a49;box-shadow:0 0 0 4px #e7f4ee}.mpesa-live-main{min-width:0}.mpesa-live-main b{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.mpesa-live-main span{font-size:11px;color:var(--muted)}.mpesa-live-amt{font-weight:800;white-space:nowrap}.mpesa-live-empty{padding:18px;text-align:center;color:var(--muted);border:1px dashed var(--line);border-radius:10px}.pulse{animation:pulseMontez 1.4s ease-in-out infinite}@keyframes pulseMontez{0%,100%{opacity:1}50%{opacity:.45}}

/* Sales/customer UI controls */
.locked-price{background:#eef3f0;font-weight:700;border:2px solid #d7ae5c}.sale-total-bar{display:flex;justify-content:space-between;align-items:center;margin-top:14px;padding:14px 16px;border-radius:12px;background:#0d3b2f;color:#fff}.sale-total-bar strong{font-size:1.25rem;color:#f1d28d}.segmented{display:flex;gap:10px;flex-wrap:wrap}.segmented label{border:1px solid var(--line);padding:10px 12px;border-radius:10px;background:#fafcfa}.search-results{display:grid;gap:8px;margin-top:10px}.search-result{display:flex;justify-content:space-between;gap:12px;align-items:center;border:1px solid var(--line);border-radius:10px;padding:10px;background:#fafcfa}.selected-customer{margin-top:10px;padding:10px;border-left:4px solid var(--gold);background:#f6f0df;border-radius:8px}


/* AI Assistant, business performance and scanning */
.performance-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:14px}.performance-card{overflow:hidden}.performance-row{display:flex;align-items:center;gap:18px}.performance-ring{width:104px;height:104px;border-radius:50%;display:grid;place-items:center;flex:0 0 auto;background:conic-gradient(var(--good) calc(var(--pct)*1%),#e8eeeb 0)}.performance-ring.red{background:conic-gradient(var(--red) calc(var(--pct)*1%),#f0e5e5 0)}.performance-ring>div{width:76px;height:76px;border-radius:50%;background:#fff;display:grid;place-items:center;font-weight:900;font-size:21px}.performance-status{font-weight:900;letter-spacing:.7px}.performance-status.green{color:var(--good)}.performance-status.red{color:var(--red)}.performance-caption{font-size:11px;color:var(--muted);margin-top:3px}.ai-card{background:linear-gradient(135deg,#0d3b2f,#174f40);color:#fff}.ai-card .muted{color:#d1e4dc}.ai-quick{display:flex;flex-wrap:wrap;gap:7px}.ai-answer{white-space:pre-wrap;line-height:1.55;background:#fbfdfc;border:1px solid var(--line);border-radius:12px;padding:14px}.scan-inline{display:flex;gap:7px;align-items:end}.scan-inline>div{flex:1}.scan-btn{white-space:nowrap;min-width:88px;height:42px}.scanner-modal{position:fixed;inset:0;background:rgba(3,18,13,.72);display:none;align-items:center;justify-content:center;z-index:100}.scanner-modal.open{display:flex}.scanner-box{width:min(560px,94vw);background:#fff;border-radius:18px;padding:16px}.scanner-box video{width:100%;border-radius:12px;background:#101814;aspect-ratio:4/3;object-fit:cover}.scanner-status{margin-top:8px;font-size:12px;color:var(--muted)}@media(max-width:800px){.performance-grid{grid-template-columns:1fr}.performance-row{align-items:flex-start}.scan-inline{align-items:stretch}.scan-btn{height:auto}}
'''


def mpesa_live_messages(limit=8, branch_id=None):
    """Return newest M-Pesa updates; branch staff only see STK/C2B payments tied to their branch."""
    c=core.db.c(); items=[]
    stk_scope=' WHERE branch_id=?' if branch_id is not None else ''
    stk_args=(int(branch_id),int(limit)) if branch_id is not None else (int(limit),)
    stk=c.execute("""SELECT updated_at AS ts,amount,phone,status,mpesa_code,client_ref
                       FROM mpesa_stk_requests"""+stk_scope+" ORDER BY id DESC LIMIT ?",stk_args).fetchall()
    for r in stk:
        status=str(r['status'] or 'PENDING').upper(); code=r['mpesa_code'] or ''
        title='M-PESA PAYMENT CONFIRMED' if status in ('SUCCESS','SUCCEEDED','COMPLETED') else 'M-PESA PAYMENT UPDATE'
        detail=f"{status} · {r['phone'] or 'Phone unavailable'}"
        if code: detail += f" · Code {code}"
        items.append({'ts':r['ts'] or '','amount':float(r['amount'] or 0),'title':title,'detail':detail,'code':code,'source':'STK'})
    if branch_id is None:
        c2b=c.execute("""SELECT received_at AS ts,amount,msisdn,first_name,last_name,trans_id,status,bill_ref_number
                           FROM mpesa_transactions ORDER BY id DESC LIMIT ?""",(int(limit),)).fetchall()
    else:
        c2b=c.execute("""SELECT mt.received_at AS ts,mt.amount,mt.msisdn,mt.first_name,mt.last_name,mt.trans_id,mt.status,mt.bill_ref_number
                           FROM mpesa_transactions mt JOIN sale_payments sp ON sp.id=mt.matched_sale_payment_id JOIN sales s ON s.id=sp.sale_id
                           WHERE s.branch_id=? ORDER BY mt.id DESC LIMIT ?""",(int(branch_id),int(limit))).fetchall()
    for r in c2b:
        customer=' '.join(x for x in [(r['first_name'] or '').strip(),(r['last_name'] or '').strip()] if x) or 'Customer'
        status=str(r['status'] or 'RECEIVED').upper(); detail=f"{customer} · {r['msisdn'] or 'Phone unavailable'} · {status}"
        if r['trans_id']: detail += f" · Code {r['trans_id']}"
        if r['bill_ref_number']: detail += f" · Ref {r['bill_ref_number']}"
        items.append({'ts':r['ts'] or '','amount':float(r['amount'] or 0),'title':'M-PESA PAYMENT RECEIVED','detail':detail,'code':r['trans_id'] or '','source':'C2B'})
    c.close(); items.sort(key=lambda x:x.get('ts',''),reverse=True); return items[:int(limit)]

def mpesa_live_messages_html(items):
    if not items: return '<div class="mpesa-live-empty">Waiting for M-Pesa payment messages…</div>'
    out=[]
    for i in items:
        pulse=' pulse' if i.get('source')=='C2B' else ''
        out.append(f'''<div class="mpesa-live-item"><span class="mpesa-live-dot{pulse}"></span><div class="mpesa-live-main"><b>{esc(i['title'])}</b><span>{esc(i['ts'])} · {esc(i['detail'])}</span></div><span class="mpesa-live-amt">{money(i['amount'])}</span></div>''')
    return ''.join(out)


def mpesa_live_messages_json(u=None, query=None):
    bid=branch_scope_for(u,query) if u else None
    return {'timestamp':now(),'messages':mpesa_live_messages(8,bid),'refresh_seconds':max(2,int(float(core.get_setting('live_refresh_seconds','5'))))}

def branch_scope_for(u, query=None):
    """Resolve branch access from the authenticated role, never from an untrusted branch parameter for branch staff."""
    role=role_name(u); query=query or {}
    if role in ('ADMIN','MANAGEMENT'):
        raw=_qsval(query,'branch_id','')
        if not str(raw).strip(): return None
        if not str(raw).isdigit(): raise ValueError('Invalid branch selection.')
        bid=int(raw); c=core.db.c()
        try: ok=c.execute('SELECT 1 FROM branches WHERE id=? AND active=1',(bid,)).fetchone()
        finally: c.close()
        if not ok: raise PermissionError('You are not authorised to view that branch.')
        return bid
    bid=int(u.get('branch_id') or 0)
    if not bid: raise PermissionError('Your account has no assigned branch. Contact Administrator.')
    c=core.db.c()
    try: ok=c.execute('SELECT 1 FROM branches WHERE id=? AND active=1',(bid,)).fetchone()
    finally: c.close()
    if not ok: raise PermissionError('Your assigned branch is inactive. Contact Administrator.')
    return bid


def branch_scope_selector(u, query=None, path='/'):
    if role_name(u) not in ('ADMIN','MANAGEMENT'): return ''
    selected=branch_scope_for(u,query)
    c=core.db.c()
    try: branches=[dict(r) for r in c.execute('SELECT id,name FROM branches WHERE active=1 ORDER BY name').fetchall()]
    finally: c.close()
    opts='<option value="">ALL BRANCHES — COMPANY TOTAL</option>'+''.join(f'<option value="{int(b["id"])}" {"selected" if selected==int(b["id"]) else ""}>{esc(b["name"])}</option>' for b in branches)
    return f'<section class="card" style="margin-bottom:12px"><form method="get" action="{esc(path)}" class="form-grid"><div><label>Dashboard / Report Branch</label><select name="branch_id" onchange="this.form.submit()">{opts}</select></div><div class="muted" style="align-self:end">Select one branch to view only its stock and transactions, or choose company total.</div></form></section>'


def dashboard_greeting(u):
    hour=dt.datetime.now().hour
    if 5 <= hour < 12:
        greeting='Good morning'
    elif 12 <= hour < 17:
        greeting='Good afternoon'
    elif 17 <= hour < 22:
        greeting='Good evening'
    else:
        greeting='Good night'
    full=str(u.get('full_name') or u.get('username') or '').strip()
    first=full.split()[0] if full else 'User'
    return f'{greeting}, {first}'

def award_for_rank(rank, revenue=0, total=0):
    # Return medal label for a ranked period.
    if float(total or 0) <= 0 or float(revenue or 0) <= 0:
        return 'NO SALES'
    return {1: '🥇 GOLD', 2: '🥈 SILVER', 3: '🥉 BRONZE'}.get(int(rank), 'PARTICIPANT')

def monthly_awards_html(start, end):
    # Live monthly standings for the executive dashboard.
    c=core.db.c()
    branches=[dict(r) for r in c.execute('''SELECT b.id,b.name,COUNT(s.id) transactions,COALESCE(SUM(s.total),0) revenue,COALESCE(SUM((SELECT SUM(sl.qty) FROM sale_lines sl WHERE sl.sale_id=s.id)),0) units FROM branches b LEFT JOIN sales s ON s.branch_id=b.id AND s.sold_at BETWEEN ? AND ? GROUP BY b.id ORDER BY revenue DESC,b.name''',(start,end)).fetchall()]
    employees=[dict(r) for r in c.execute('''SELECT u.id,u.full_name,u.username,COALESCE(b.name,'Unassigned') branch_name,COUNT(s.id) transactions,COALESCE(SUM(s.total),0) revenue,COALESCE(SUM((SELECT SUM(sl.qty) FROM sale_lines sl WHERE sl.sale_id=s.id)),0) units FROM users u LEFT JOIN branches b ON b.id=COALESCE((SELECT s2.branch_id FROM sales s2 WHERE s2.user_id=u.id AND s2.branch_id IS NOT NULL ORDER BY s2.sold_at DESC LIMIT 1),u.branch_id) LEFT JOIN sales s ON s.user_id=u.id AND s.sold_at BETWEEN ? AND ? WHERE u.active=1 AND u.access_role='SALES' GROUP BY u.id ORDER BY revenue DESC,u.full_name''',(start,end)).fetchall()]
    c.close()
    def make_table(rows, label_key, label):
        total=sum(float(r.get('revenue') or 0) for r in rows)
        body=[]
        for rank,r in enumerate(rows,1):
            award=award_for_rank(rank,r.get('revenue'),total)
            who=esc(r.get(label_key,'') or '')
            if label_key=='full_name': who += ' <span class="muted">· '+esc(r.get('branch_name',''))+'</span>'
            body.append([award, f'#{rank}', who, money(r.get('revenue',0)), f"{(100*float(r.get('revenue') or 0)/total if total else 0):.1f}%"])
        return '<h3 style="margin-top:10px">'+label+'</h3>'+html_table(['Award','Rank',label,'Sales Revenue','Revenue Share'],body,'No salespeople/branches registered.')
    return f'''<section class="card" style="margin-top:14px"><div class="section-head"><div><h2>🏆 Monthly Branch & Employee Awards</h2><div class="muted">Live standings for {esc(start[:7])}. Rankings use recorded sales revenue and update as sales are recorded.</div></div><a class="btn gold" href="/branch-performance?preset=THIS_MONTH">OPEN FULL REPORT</a></div><div class="notice">🥇 Gold = 1st · 🥈 Silver = 2nd · 🥉 Bronze = 3rd · Participant = 4th onward. Periods with no sales receive no medal.</div>{make_table(branches,'name','Branch Awards')}{make_table(employees,'full_name','Employee Awards')}</section>'''

def home_page(u, query=None):
    require_perm_user(u,'VIEW_DASHBOARD')
    branch_id=branch_scope_for(u,query)
    settings=core.get_settings(); business_name=settings.get('business_name',APP_NAME); branch_name=settings.get('business_branch_name','')
    if branch_id is not None:
        c=core.db.c(); br=c.execute('SELECT name FROM branches WHERE id=?',(branch_id,)).fetchone(); c.close(); branch_name=br['name'] if br else 'Selected Branch'
    elif role_name(u) in ('ADMIN','MANAGEMENT'): branch_name='All Branches — Company Total'
    d=core.dashboard(branch_id); live=core.live_monitoring(branch_id); live['active_users']=active_user_count(); chart=daily_chart(branch_id); mp_live=mpesa_live_messages(8,branch_id)
    tstart=f"{now()[:10]} 00:00:00"; tend=f"{now()[:10]} 23:59:59"; pt=core.business_performance(tstart,tend,branch_id)
    mstart=f"{now()[:7]}-01 00:00:00"; mend=f"{now()[:10]} 23:59:59"; pm=core.business_performance(mstart,mend,branch_id)
    def perf_card(p,label):
        cls='green' if p['traffic']=='GREEN' else 'red'
        return f'<section class="card performance-card"><div class="section-head"><div><h2>{esc(label)}</h2><div class="muted">{esc(p["label"])} · Green threshold {p["threshold"]}%</div></div><span class="pill">{esc(p["basis"])}</span></div><div class="performance-row"><div class="performance-ring {cls}" style="--pct:{max(0,min(100,p["score"])):.1f}"><div>{p["percentage"]:.1f}%</div></div><div><div class="performance-status {cls}">{p["status"]}</div><div class="performance-caption">Revenue {money(p["revenue"])} · Expenses {money(p["expenses"])} · Net {money(p["net_profit"])}</div><div class="performance-caption">{esc(p["start"])} → {esc(p["end"])}</div></div></div></section>'
    performance_html=perf_card(pt,'Today Business Performance')+perf_card(pm,'This Month Business Performance')
    branch_selector=branch_scope_selector(u,query,'/')
    cogs_missing=int(d.get('cogs_missing_lines',0) or 0); cogs_estimated=int(d.get('cogs_estimated_lines',0) or 0); cogs_coverage=float(core.pl_summary(tstart,tend,branch_id).get('cogs_coverage_pct',0) or 0)
    cogs_status=(f'<div class="flash error">COGS WARNING: {cogs_missing} sold line(s) have no valid cost; {cogs_estimated} line(s) use estimated costs. Profit figures may be inaccurate until costs are corrected.</div>' if (cogs_missing or cogs_estimated) else f'<div class="notice">COGS data coverage: {cogs_coverage:.1f}% recorded-cost coverage for today.</div>')
    awards_html=monthly_awards_html(mstart,mend) if role_name(u) in ('ADMIN','MANAGEMENT') and branch_id is None else ''
    content=f'''{branch_selector}<section class="card" style="margin-bottom:14px"><div class="section-head"><div><h2 style="margin:0">{esc(dashboard_greeting(u))}</h2><div class="muted">Welcome to {esc(business_name)}{(' · '+esc(branch_name)) if branch_name else ''}</div></div><span class="pill">{esc(role_display(role_name(u)))}</span></div></section><div class="grid kpis"><div class="card kpi"><div class="label">TODAY SALES</div><strong>{money(d['today_sales'])}</strong><div class="muted">{d['transactions']} transactions</div></div><div class="card kpi"><div class="label">CASH</div><strong>{money(d['cash'])}</strong></div><div class="card kpi"><div class="label">M-PESA</div><strong>{money(d['mpesa'])}</strong></div><div class="card kpi"><div class="label">TODAY EXPENSES</div><strong>{money(d['expenses'])}</strong></div><div class="card kpi"><div class="label">TODAY COGS — COST OF GOODS SOLD</div><strong>{money(d['today_cogs'])}</strong><div class="muted">Recorded cost of products sold today</div><a class="muted" href="/profit-loss?branch_id={branch_id or ''}">View COGS &amp; P&amp;L</a></div><div class="card kpi"><div class="label">TODAY GROSS PROFIT</div><strong>{money(d['today_gross_profit'])}</strong><div class="muted">All sales (including credit) less COGS, before expenses</div></div><div class="card kpi"><div class="label">INVENTORY VALUE AT COST</div><strong>{money(d['stock_value'])}</strong><div class="muted">{d['stock_units']:,.0f} units in stock · current inventory cost value</div><a class="muted" href="/stock-report?branch_id={branch_id or ''}">View stock valuation</a></div><div class="card kpi"><div class="label">POTENTIAL RETAIL VALUE</div><strong>{money(d['stock_retail_value'])}</strong><div class="muted">Estimated value if all current stock sells at listed prices</div></div><div class="card kpi"><div class="label">LOW STOCK</div><strong>{live['low_stock']}</strong></div><div class="card kpi"><div class="label">ACTIVE USERS</div><strong>{live['active_users']}</strong></div></div>{cogs_status}<div class="performance-grid" style="margin-top:14px">{performance_html}</div>{awards_html}<div class="grid two" style="margin-top:14px"><section class="card"><div class="section-head"><h2>14-Day Sales Trend</h2><span class="pill">LIVE DATA</span></div>{chart}</section><section class="card"><div class="section-head"><h2>System Accountability</h2><span class="pill">SIGNED IN</span></div><div class="metric"><div class="muted">USER</div><div class="v">{esc(u['full_name'])}</div><div class="muted">@{esc(u['username'])} · {esc(role_display(role_name(u)))}</div></div><div class="metric" style="margin-top:9px"><div class="muted">SERVER TIME</div><div class="v">{now()}</div></div></section></div><div class="grid two" style="margin-top:14px"><section class="card mpesa-live-card"><div class="mpesa-live-head"><div><h2 style="margin:0">Live M-Pesa Messages</h2><div class="muted">{esc(settings.get('mpesa_shortcode',''))} · refreshes automatically</div></div><span class="pill">LIVE</span></div><div id="mpesa-live-messages">{mpesa_live_messages_html(mp_live)}</div></section><section class="card"><div class="section-head"><h2>Low Stock</h2><a class="btn" href="/stock-report">VIEW STOCK</a></div>{html_table(['Product','Stock','Reorder Level'],[[esc(p['name']),p['stock_qty'],p['reorder_level']] for p in low_stock(branch_id)],'No low-stock products.')}</section></div><div class="grid two" style="margin-top:14px"><section class="card"><div class="section-head"><h2>Recent Sales</h2><a class="btn" href="/sales">OPEN POS</a></div>{html_table(['Time','Receipt','Customer','Total','By'],[[esc(r['sold_at']),esc(r['receipt_no']),esc(r['customer']),money(r['total']),esc(r['user_name'])] for r in live['recent_sales']],'No sales yet.')}</section><section class="card"><div class="section-head"><h2>Recent Purchases</h2><a class="btn" href="/purchases">OPEN PURCHASES</a></div>{html_table(['Time','Purchase','Supplier','Total','By'],[[esc(r['purchased_at']),esc(r['purchase_no']),esc(r['supplier']),money(r['total']),esc(r['user_name'])] for r in live['recent_purchases']],'No purchases yet.')}</section></div>'''
    return layout(u,'Executive Dashboard',content,'/')


def active_user_count():
    """Count authenticated sessions that have not exceeded the configured inactivity timeout."""
    try:
        idle_seconds = max(1, int(float(core.get_setting('idle_timeout_minutes', '2')) * 60))
    except Exception:
        idle_seconds = 120
    cutoff = time.time() - idle_seconds
    count = 0
    for token, entry in list(SESSIONS.items()):
        last = float(entry.get('last_activity', 0) or 0)
        if last < cutoff:
            # Expired sessions are removed so they cannot remain in the live count.
            SESSIONS.pop(token, None)
            CSRF.pop(token, None)
            FLASH.pop(token, None)
            continue
        try:
            c = core.db.c()
            row = c.execute('SELECT active FROM users WHERE id=?', (int(entry.get('uid', 0)),)).fetchone()
            c.close()
            if row and int(row['active']) == 1:
                count += 1
            else:
                SESSIONS.pop(token, None)
                CSRF.pop(token, None)
                FLASH.pop(token, None)
        except Exception:
            # Keep a live session counted only when its user record is safely verified.
            pass
    return count


def low_stock(branch_id=None):
    c=core.db.c(); scope=' AND bs.branch_id=?' if branch_id is not None else ''
    rows=[dict(r) for r in c.execute('SELECT p.*,bs.qty stock_qty,bs.reorder_level FROM products p JOIN branch_stock bs ON bs.product_id=p.id WHERE p.active=1 AND bs.qty<=bs.reorder_level'+scope+' ORDER BY bs.qty,p.name',(int(branch_id),) if branch_id is not None else ())]; c.close(); return rows


def daily_chart(branch_id=None):
    scope=' AND branch_id=?' if branch_id is not None else ''
    args=(int(branch_id),) if branch_id is not None else ()
    c=core.db.c(); rows=c.execute("SELECT date(sold_at) day, COALESCE(SUM(total),0) revenue FROM sales WHERE substr(sold_at,1,10)>=date('now','-13 day','localtime') AND customer_type<>'CREDIT'"+scope+" GROUP BY date(sold_at) ORDER BY day",args).fetchall(); c.close()
    data=[dict(r) for r in rows]; maxv=max([float(r['revenue']) for r in data] or [1]); labels=''
    for r in data:
        h=max(2, int(float(r['revenue'])/maxv*150)); labels+=f'<div class="bar-item"><div class="bar" style="height:{h}px" title="{esc(money(r["revenue"]))}"></div><span>{esc(str(r["day"])[5:])}</span></div>'
    return f'<div class="barchart">{labels or "<div class=\"muted\">No sales data yet.</div>"}</div>'


def sales_dashboard(u,query=None):
    bid=branch_scope_for(u,query); scope=' AND s.branch_id=?' if bid is not None else ''; args=(now()[:10],int(bid)) if bid is not None else (now()[:10],); pargs=(now()[:7],int(bid)) if bid is not None else (now()[:7],)
    c=core.db.c(); staff=c.execute("SELECT u.full_name,SUM(sl.qty) units,SUM(sl.qty*sl.unit_price) revenue,COUNT(DISTINCT s.receipt_no) receipts FROM sales s JOIN sale_lines sl ON sl.sale_id=s.id JOIN users u ON u.id=s.user_id WHERE substr(s.sold_at,1,10)=? AND s.customer_type<>'CREDIT'"+scope+" GROUP BY u.id ORDER BY revenue DESC",args).fetchall(); products=c.execute("SELECT p.name,SUM(sl.qty) units,SUM(sl.qty*sl.unit_price) revenue FROM sales s JOIN sale_lines sl ON sl.sale_id=s.id JOIN products p ON p.id=sl.product_id WHERE substr(s.sold_at,1,7)=? AND s.customer_type<>'CREDIT'"+scope+" GROUP BY p.id ORDER BY revenue DESC LIMIT 10",pargs).fetchall(); c.close()
    selector=branch_scope_selector(u,query,'/sales-dashboard')
    content=f'{selector}<div class="grid two"><section class="card"><h2>Sales by Person Today</h2>{html_table(["Employee","Units","Revenue","Receipts"],[[esc(r["full_name"]),r["units"],money(r["revenue"]),r["receipts"]] for r in staff])}</section><section class="card"><h2>Top Products This Month</h2>{html_table(["Product","Units","Revenue"],[[esc(r["name"]),r["units"],money(r["revenue"])] for r in products])}</section></div>'
    return layout(u,'Sales Dashboard',content,'/sales-dashboard')


def sales_page(u, message='', search_query='', search_results=None, selected_customer_id=None, query=None):
    require_perm_user(u,'CREATE_SALE')
    branch_id=branch_scope_for(u,query)
    max_payments=max(1,min(5,int(float(core.get_setting('max_payment_transactions','5')))))
    default_payment_count=max(1,min(max_payments,int(float(core.get_setting('default_payment_transactions','1')))))
    ps=core.products(branch_id) if branch_id is not None else []
    product_opts=''.join(f'<option value="{p["id"]}" data-retail-price="{float(p["sell_price"]):.2f}" data-wholesale-price="{float(p.get('wholesale_price') or p['sell_price']):.2f}" data-sku="{esc(p.get("sku",""))}">{esc(p["name"])} · Retail {money(p["sell_price"])} / Wholesale {money(p.get('wholesale_price') or p['sell_price'])} · Stock {p["stock_qty"]}</option>' for p in ps) or '<option value="">No products registered</option>'
    bid=branch_scope_for(u); c=core.db.c(); sale_scope=' WHERE s.branch_id=?' if bid is not None else ''; sale_args=(int(bid),) if bid is not None else ()
    rows=c.execute("SELECT s.receipt_no,s.sold_at,s.customer_type,s.sale_type,s.total,s.branch_id,COALESCE(cu.full_name,'Walk-in Customer') customer,COALESCE(u.full_name,'') user_name FROM sales s LEFT JOIN customers cu ON cu.id=s.customer_id LEFT JOIN users u ON u.id=s.user_id"+sale_scope+" ORDER BY s.id DESC LIMIT 100",sale_args).fetchall(); c.close()
    payments='''<div class="pay-line" id="payment_row_1"><div><label>Method</label><select name="payment_method_1" id="payment_method_1" onchange="toggleMpesa(1)"><option value="CASH">CASH</option><option value="MPESA">M-PESA</option></select></div><div><label>Amount</label><input type="number" min="0.01" step="0.01" name="payment_amount_1" id="payment_amount_1" value="0" oninput="updatePaymentSummary()" required></div><div id="mpesa_wrap_1" style="display:none"><label>Customer M-Pesa Phone</label><input name="mpesa_phone_1" id="mpesa_phone_1" placeholder="0712345678" inputmode="tel"><button type="button" class="btn" onclick="requestMpesa(1)" id="mpesa_request_1">SEND M-PESA PROMPT</button><button type="button" class="btn" onclick="confirmDirectMpesa(1)" id="mpesa_confirm_1">CONFIRM PAYMENT</button><label for="mpesa_code_1">M-Pesa Transaction Code / Receipt Number *</label><input type="text" name="mpesa_code_1" id="mpesa_code_1" placeholder="e.g. QWE123ABC9" maxlength="30" autocomplete="off" autocapitalize="characters" spellcheck="false"><div class="muted" id="mpesa_status_1">For Till/Paybill payments, enter the code from the customer M-Pesa SMS. STK Push can fill it automatically.</div></div></div>'''
    result_html=''
    if search_results:
        result_rows=[]
        for r in search_results:
            name_arg=html.escape(json.dumps(str(r['full_name'])),quote=True); phone_arg=html.escape(json.dumps(str(r['phone'])),quote=True); id_arg=html.escape(json.dumps(str(r['id_no'] or '')),quote=True); address_arg=html.escape(json.dumps(str(r['address'] or '')),quote=True)
            result_rows.append(f'''<div class="search-result"><div><b>{esc(r['full_name'])}</b><div class="muted">ID: {esc(r['id_no'] or '—')} · Phone: {esc(r['phone'])}</div></div><button type="button" class="btn" onclick="selectCustomer({int(r['id'])}, {name_arg}, {phone_arg}, {id_arg}, {address_arg})">SELECT</button></div>''')
        result_html='<div class="search-results">'+''.join(result_rows)+'</div>'
    elif search_query:
        result_html='<div class="flash error">No registered customer found for that ID number or first name.</div>'
    selected_customer_html=''; selected_customer_id_val=''
    if selected_customer_id:
        try: scid=int(selected_customer_id)
        except Exception: scid=0
        if scid:
            c=core.db.c(); sc=c.execute('SELECT id,full_name,phone,id_no,address FROM customers WHERE id=? AND active=1'+(' AND branch_id=?' if role_name(u) not in ('ADMIN','MANAGEMENT') else ''),(scid,int(u.get('branch_id') or 0)) if role_name(u) not in ('ADMIN','MANAGEMENT') else (scid,)).fetchone(); c.close()
            if sc:
                selected_customer_id_val=str(sc['id']); selected_customer_html=f'<div class="selected-customer"><b>SAVED CUSTOMER</b><div>{esc(sc["full_name"])} · {esc(sc["phone"])}</div><div class="muted">ID: {esc(sc["id_no"] or "—")} · {esc(sc["address"] or "")}</div></div>'
    notice=f'<div class="flash error">{esc(message)}</div>' if message else ''
    if role_name(u) in ('ADMIN','MANAGEMENT'):
        c=core.db.c(); active_branches=[dict(b) for b in c.execute('SELECT id,name FROM branches WHERE active=1 ORDER BY name').fetchall()]; c.close()
        branch_field=f'<input type="hidden" name="branch_id" value="{branch_id or ""}">'
        branch_selector=branch_scope_selector(u,query,'/sales')
    else:
        branch_field=f'<input type="hidden" name="branch_id" value="{branch_id}">'; branch_selector=''
    content='''__NOTICE__
<form method="post" action="/sales" class="pos-grid" id="sales_form">
__CSRF__
__BRANCH_FIELD__
<input type="hidden" name="customer_id" id="selected_customer_id" value="__SELECTED_CUSTOMER_ID__"><input type="hidden" name="mpesa_batch_id" id="mpesa_batch_id" value="">
<div>
<section class="card"><div class="section-head"><h2>New Sale</h2><span class="pill">ATTENDED BY: __FULL_NAME__</span></div>
<div class="form-grid"><div><label>Sale Type</label><select name="sale_type" id="sale_type" onchange="showRegisteredPrice()"><option value="RETAIL">RETAIL</option><option value="WHOLESALE">WHOLESALE</option></select></div></div><div class="section-head" style="margin-top:12px"><h3>Products in this sale</h3><button type="button" class="btn gold" onclick="addSaleLine()">+ ADD LINE</button></div><div id="sale_lines"><div class="form-grid sale-line" style="border:1px solid var(--line);padding:10px;border-radius:10px;margin-bottom:8px"><div><label>Product</label><select name="product_id_1" id="sale_product_1" class="sale-product" required onchange="showRegisteredPrice()">__PRODUCT_OPTS__</select></div><div><label>Quantity</label><input type="number" name="qty_1" id="sale_qty_1" class="sale-qty" min="1" step="1" value="1" required oninput="showRegisteredPrice()"></div><div><label>Registered unit price</label><input id="registered_price_display_1" class="locked-price sale-price" readonly></div><div><label>Line total</label><input id="line_total_1" class="locked-price sale-line-total" readonly><button type="button" class="btn" onclick="removeSaleLine(this)">REMOVE LINE</button></div></div></div><input id="sales_sku_scan" placeholder="Scan/type SKU, then look up" style="margin-top:7px"><button type="button" class="btn" style="margin-top:7px" onclick="lookupProductBySku('sales_sku_scan')">LOOK UP SKU</button><div class="sale-total-bar"><span>SALE TOTAL</span><strong id="sale_total_display">KSh 0.00</strong></div></section>
<section class="card" style="margin-top:14px"><div class="section-head"><h2>Customer</h2><span class="pill">WALK-IN OR CREDIT CUSTOMER</span></div>
<div class="form-grid"><div><label>Customer Type</label><select name="customer_mode" id="customer_mode" onchange="toggleCustomerMode()"><option value="WALK_IN">WALK-IN CUSTOMER</option>__CREDIT_OPTION__</select></div></div>
<div id="credit_customer_panel" style="display:none;margin-top:14px"><div class="segmented"><label><input type="radio" name="credit_customer_choice" value="NEW" checked onchange="toggleCreditChoice()"> NEW CREDIT CUSTOMER</label><label><input type="radio" name="credit_customer_choice" value="EXISTING" onchange="toggleCreditChoice()"> EXISTING CUSTOMER</label></div>
<div id="new_customer_panel" style="margin-top:12px"><div class="form-grid"><div><label>Full Name *</label><input name="new_customer_name" id="new_customer_name"></div><div><label>Phone Number *</label><input name="new_customer_phone" id="new_customer_phone"></div><div><label>ID Number / Passport *</label><input name="new_customer_id_no" id="new_customer_id_no"></div><div><label>Address / Location</label><input name="new_customer_address" id="new_customer_address"></div></div><div class="actions" style="margin-top:12px"><button class="btn gold" type="submit" name="customer_action" value="SAVE_NEW_CUSTOMER">SAVE NEW CUSTOMER</button></div></div>
<div id="existing_customer_panel" style="display:none;margin-top:12px"><div class="form-grid"><div style="grid-column:1/-1"><label>Search by ID Number or First Name</label><div style="display:flex;gap:8px"><input id="customer_search" value="__SEARCH_QUERY__" placeholder="e.g. 12345678 or John"><button type="button" class="btn" onclick="searchCustomers()">SEARCH</button></div></div></div><div id="customer_search_results">__RESULT_HTML__</div><div id="selected_customer_card" class="selected-customer" style="display:none"></div>__SELECTED_CUSTOMER_HTML__<div class="actions" style="margin-top:12px"><button class="btn gold" type="submit" name="customer_action" value="SAVE_EXISTING_CUSTOMER">SAVE EXISTING CUSTOMER</button></div></div>
<div class="form-grid" style="margin-top:12px"><div><label>When to Pay *</label><input type="date" name="due_date" id="due_date"></div><div><label>Collateral Held for Exchange *</label><input name="collateral" id="collateral" placeholder="ID / item / asset"></div><div><label>Collateral Value</label><input type="number" min="0" step="0.01" name="collateral_value" id="collateral_value" value="0"></div></div><div class="actions" style="margin-top:12px"><button class="btn gold full" type="submit" id="create_credit_invoice">CREATE CREDIT INVOICE</button></div></div></section>
</div><div>
<section class="card" id="payment_card"><div class="section-head"><h2>Payment Allocation</h2><span class="pill">UP TO __MAX_PAYMENTS__ TRANSACTIONS</span></div><div class="form-grid"><div><label>How many payment transactions?</label><select name="payment_count" id="payment_count" onchange="renderPaymentRows()">__PAYMENT_OPTIONS__</select></div></div><p class="muted">Choose 1 to  __MAX_PAYMENTS__ payments. The system will not save the sale until the allocation equals the full sale total exactly.</p><div id="payment_rows">__PAYMENTS__</div><div class="sale-total-bar" style="margin-top:12px"><span>ALLOCATED / REMAINING</span><strong id="payment_summary">Allocated KSh 0.00 · Remaining KSh 0.00</strong></div><div class="actions" style="margin-top:12px"><button class="btn gold full" type="submit">SAVE / COMPLETE SALE</button></div></section>
</div></form><section class="card" style="margin-top:14px"><div class="section-head"><h2>Recent Sales</h2></div>__RECENT_SALES__</section>
<script>
function productPrice(sel){if(!sel||!sel.value)return 0;var o=sel.options[sel.selectedIndex],t=document.getElementById('sale_type'),n=Number(o.getAttribute(t&&t.value==='WHOLESALE'?'data-wholesale-price':'data-retail-price')||0);return Number.isFinite(n)&&n>0?n:0;}
function selectedProductPrice(){return productPrice(document.getElementById('sale_product_1'));}
function moneyText(n){return 'KSh '+(Number(n)||0).toLocaleString('en-KE',{minimumFractionDigits:2,maximumFractionDigits:2});}
function currentSaleTotal(){var total=0;document.querySelectorAll('.sale-line').forEach(function(r){total+=productPrice(r.querySelector('.sale-product'))*(parseFloat(r.querySelector('.sale-qty').value||'0')||0);});return Math.round(total*100)/100;}
function showRegisteredPrice(){var total=0;document.querySelectorAll('.sale-line').forEach(function(r){var p=productPrice(r.querySelector('.sale-product')),q=parseFloat(r.querySelector('.sale-qty').value||'0')||0,pe=r.querySelector('.sale-price'),le=r.querySelector('.sale-line-total');if(pe)pe.value=moneyText(p);if(le)le.value=moneyText(p*q);total+=p*q;});var te=document.getElementById('sale_total_display'),pc=document.getElementById('payment_count');if(te){te.dataset.amount=String(total);te.textContent=moneyText(total);}if(pc&&parseInt(pc.value||'1',10)===1){var a=document.getElementById('payment_amount_1');if(a)a.value=total.toFixed(2);}updatePaymentSummary();}
function addSaleLine(){var box=document.getElementById('sale_lines'),first=box.querySelector('.sale-line'),n=Number(box.dataset.nextLine||'2'),row=first.cloneNode(true);box.dataset.nextLine=String(n+1);row.querySelectorAll('[name],[id]').forEach(function(e){if(e.name)e.name=e.name.replace(/_\\d+$/,'_'+n);if(e.id)e.id=e.id.replace(/_\\d+$/,'_'+n);});row.querySelector('.sale-product').value='';row.querySelector('.sale-qty').value='1';row.querySelector('.sale-price').value='';row.querySelector('.sale-line-total').value='';box.appendChild(row);showRegisteredPrice();}
function removeSaleLine(btn){if(document.querySelectorAll('.sale-line').length<2){alert('At least one product line is required.');return;}btn.closest('.sale-line').remove();showRegisteredPrice();}
function toggleMpesa(i){var m=document.getElementById('payment_method_'+i),w=document.getElementById('mpesa_wrap_'+i),c=document.getElementById('mpesa_code_'+i);if(!m||!w||!c)return;var isMpesa=m.value==='MPESA';w.style.display=isMpesa?'block':'none';c.required=isMpesa;c.setAttribute('aria-required',isMpesa?'true':'false');if(!isMpesa)c.value='';}
function paymentRowHtml(i){return '<div class="pay-line" id="payment_row_'+i+'"><div><label>Method</label><select name="payment_method_'+i+'" id="payment_method_'+i+'" onchange="toggleMpesa('+i+')"><option value="CASH">CASH</option><option value="MPESA">M-PESA</option></select></div><div><label>Amount</label><input type="number" min="0.01" step="0.01" name="payment_amount_'+i+'" id="payment_amount_'+i+'" value="0" oninput="updatePaymentSummary()" required></div><div id="mpesa_wrap_'+i+'" style="display:none"><label>Customer M-Pesa Phone</label><input name="mpesa_phone_'+i+'" id="mpesa_phone_'+i+'" placeholder="0712345678" inputmode="tel"><button type="button" class="btn" onclick="requestMpesa('+i+')" id="mpesa_request_'+i+'">SEND M-PESA PROMPT</button><button type="button" class="btn" onclick="confirmDirectMpesa('+i+')" id="mpesa_confirm_'+i+'">CONFIRM PAYMENT</button><label for="mpesa_code_'+i+'">M-Pesa Transaction Code / Receipt Number *</label><input type="text" name="mpesa_code_'+i+'" id="mpesa_code_'+i+'" placeholder="e.g. QWE123ABC9" maxlength="30" autocomplete="off" autocapitalize="characters" spellcheck="false"><div class="muted" id="mpesa_status_'+i+'">For Till/Paybill payments, enter the code from the customer M-Pesa SMS. STK Push can fill it automatically.</div></div></div>';}
function renderPaymentRows(){var n=Math.min(parseInt(document.getElementById('payment_count').value||'1',10),__MAX_TX__);document.getElementById('payment_count').value=n;var box=document.getElementById('payment_rows');box.innerHTML='';for(var i=1;i<=n;i++){box.insertAdjacentHTML('beforeend',paymentRowHtml(i));toggleMpesa(i);}if(n===1){var first=document.getElementById('payment_amount_1');if(first)first.value=currentSaleTotal().toFixed(2);}updatePaymentSummary();}
function updatePaymentSummary(){var total=0,countEl=document.getElementById('payment_count'),n=parseInt(countEl?countEl.value:'1',10)||1,sale=currentSaleTotal();for(var i=1;i<=n;i++){var el=document.getElementById('payment_amount_'+i);if(el)total+=(parseFloat(el.value||'0')||0);}var rem=Math.round((sale-total)*100)/100,summary=document.getElementById('payment_summary');if(summary)summary.textContent='Allocated '+moneyText(total)+' · Remaining '+moneyText(rem); }
function toggleCustomerMode(){var mode=document.getElementById('customer_mode').value,panel=document.getElementById('credit_customer_panel'),pay=document.getElementById('payment_card');if(mode==='CREDIT'){panel.style.display='block';pay.style.display='none';}else{panel.style.display='none';pay.style.display='block';}}
function toggleCreditChoice(){var el=document.querySelector('input[name="credit_customer_choice"]:checked');if(!el)return;var v=el.value;document.getElementById('new_customer_panel').style.display=v==='NEW'?'block':'none';document.getElementById('existing_customer_panel').style.display=v==='EXISTING'?'block':'none';}
function selectCustomer(id,name,phone,idNo,address){var choice=document.querySelector('input[name="credit_customer_choice"][value="EXISTING"]');if(choice)choice.checked=true;toggleCreditChoice();var hid=document.getElementById('selected_customer_id');if(hid)hid.value=String(id);var card=document.getElementById('selected_customer_card');if(card){card.style.display='block';card.innerHTML='<b>SELECTED EXISTING CUSTOMER</b><div>'+escapeHtml(name)+' · '+escapeHtml(phone||'')+'</div><div class="muted">ID: '+escapeHtml(idNo||'—')+' · '+escapeHtml(address||'')+'</div>';}var old=document.querySelector('#existing_customer_panel .selected-customer:not(#selected_customer_card)');if(old)old.style.display='none';var b=document.querySelector('button[name="customer_action"][value="SAVE_EXISTING_CUSTOMER"]');if(b)b.textContent='USE SELECTED CUSTOMER';}
function searchCustomers(){var input=document.getElementById('customer_search'),box=document.getElementById('customer_search_results'),q=input?input.value.trim():'';if(!box)return;if(!q){box.innerHTML='<div class="flash error">Enter a customer name, ID number, or phone number.</div>';return;}box.innerHTML='<div class="muted">Searching registered customers…</div>';fetch('/api/customers/search?q='+encodeURIComponent(q),{headers:{'Accept':'application/json'},credentials:'same-origin',cache:'no-store'}).then(function(r){return r.text().then(function(raw){var d;try{d=JSON.parse(raw);}catch(e){throw new Error('Customer search did not return valid data. Refresh the page and sign in again.');}if(!r.ok)throw new Error(d.error||'Customer search failed.');return d;});}).then(function(d){var rows=d.results||[];if(!rows.length){box.innerHTML='<div class="flash error">No registered customer found. Check the spelling, ID number, or phone. Only register if this is genuinely a new customer.</div>';return;}box.innerHTML='<div class="search-results">'+rows.map(function(r){return '<div class="search-result"><div><b>'+escapeHtml(r.full_name)+'</b><div class="muted">ID: '+escapeHtml(r.id_no||'—')+' · Phone: '+escapeHtml(r.phone||'—')+'</div><div class="muted">'+escapeHtml(r.address||'')+'</div></div><button type="button" class="btn" data-customer-id="'+Number(r.id)+'">SELECT</button></div>';}).join('')+'</div>';box.querySelectorAll('button[data-customer-id]').forEach(function(b){b.addEventListener('click',function(){var r=rows.find(function(x){return Number(x.id)===Number(b.getAttribute('data-customer-id'));});if(r)selectCustomer(r.id,r.full_name,r.phone,r.id_no,r.address);});});}).catch(function(e){box.innerHTML='<div class="flash error">'+escapeHtml(e.message)+'</div>';});}
function confirmDirectMpesa(i){var phoneEl=document.getElementById('mpesa_phone_'+i),phone=phoneEl?phoneEl.value.trim():'';var status=document.getElementById('mpesa_status_'+i),btn=document.getElementById('mpesa_confirm_'+i),csrf=document.querySelector('input[name=csrf]');var pp=document.getElementById('sale_product'),qq=document.getElementById('sale_qty');if(!phone){alert('Enter the customer M-Pesa phone number.');return;}if(!pp||!pp.value||!qq||!(parseInt(qq.value||'0',10)>0)){alert('Select the product and quantity first.');return;}status.textContent='Searching latest M-Pesa payment…';btn.disabled=true;var body=new URLSearchParams();body.set('csrf',csrf?csrf.value:'');body.set('phone',phone);body.set('product_id',pp.value);body.set('qty',qq.value);var st=document.getElementById('sale_type');body.set('sale_type',st?st.value:'RETAIL');var branchInput=document.querySelector('[name=branch_id]');body.set('branch_id',branchInput?branchInput.value:'');fetch('/api/mpesa/confirm-direct',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:body.toString()}).then(function(r){return r.json().then(function(d){return {ok:r.ok,d:d};});}).then(function(x){if(!x.ok)throw new Error(x.d.error||'Unable to confirm payment.');var d=x.d;if(d.ok){status.textContent='PAYMENT CONFIRMED · SALE SAVED · '+d.receipt_no;window.location='/sales';return;}status.textContent=d.message||('Payment found: KSh '+Number(d.matched||0).toLocaleString('en-KE',{minimumFractionDigits:2})+' · Remaining: KSh '+Number(d.remaining||0).toLocaleString('en-KE',{minimumFractionDigits:2}));btn.disabled=false;}).catch(function(e){status.textContent=e.message;btn.disabled=false;});}
function requestMpesa(i){var amt=parseFloat(document.getElementById('payment_amount_'+i).value||'0')||0;var phone=document.getElementById('mpesa_phone_'+i).value.trim();var status=document.getElementById('mpesa_status_'+i),btn=document.getElementById('mpesa_request_'+i),csrf=document.querySelector('input[name=csrf]');if(amt<=0){alert('Enter the payment amount first.');return;}if(!phone){alert('Enter the customer M-Pesa phone number.');return;}status.textContent='Sending M-Pesa prompt…';btn.disabled=true;var body=new URLSearchParams();body.set('csrf',csrf?csrf.value:'');body.set('amount',amt.toFixed(2));body.set('phone',phone);body.set('sequence_no',String(i));body.set('all_mpesa',Array.prototype.slice.call(document.querySelectorAll('select[name^=payment_method_]')).every(function(x){return x.value==='MPESA'})?'1':'');var pp=document.getElementById('sale_product');var qq=document.getElementById('sale_qty');if(pp&&qq){body.set('product_id',pp.value||'');body.set('qty',qq.value||'');var st=document.getElementById('sale_type');body.set('sale_type',st?st.value:'RETAIL');var branchInput=document.querySelector('[name=branch_id]');body.set('branch_id',branchInput?branchInput.value:'');var n=document.getElementById('payment_count');body.set('payment_count',n?n.value:'1');body.set('batch_id',(document.getElementById('mpesa_batch_id')||{}).value||'');var pprice=selectedProductPrice();body.set('total',(pprice*(parseFloat(qq.value||'0')||0)).toFixed(2));for(var j=1;j<=parseInt(n?n.value:'1',10);j++){var ai=document.getElementById('payment_amount_'+j);body.set('payment_amount_'+j,ai?ai.value:'0');}}fetch('/api/mpesa/stk-request',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:body.toString()}).then(function(r){return r.json().then(function(d){return {ok:r.ok,d:d};});}).then(function(x){if(!x.ok)throw new Error(x.d.error||'Unable to send M-Pesa prompt.');status.textContent='Prompt sent. Waiting for customer payment…';if(x.d.batch_id){var bh=document.getElementById('mpesa_batch_id');if(bh)bh.value=x.d.batch_id;}var checkout=x.d.CheckoutRequestID||x.d.checkout_request_id,tries=0;var timer=setInterval(function(){tries++;fetch('/api/mpesa/stk-status?checkout_request_id='+encodeURIComponent(checkout),{cache:'no-store'}).then(function(r){return r.json();}).then(function(d){if(d.status==='SUCCESS'&&d.mpesa_code){clearInterval(timer);document.getElementById('mpesa_code_'+i).value=d.mpesa_code;status.textContent=d.message||(d.auto_saved?'PAYMENT RECEIVED · SALE SAVED AUTOMATICALLY · '+d.mpesa_code:'PAYMENT RECEIVED · M-Pesa code '+d.mpesa_code+' · Click SAVE SALE to finish.');btn.textContent=d.auto_saved?'SALE SAVED':'PAYMENT RECEIVED';btn.disabled=false;if(d.auto_saved){window.location='/sales';return;}}else if(d.status==='FAILED'||d.status==='REVIEW'){clearInterval(timer);status.textContent=d.message||'M-Pesa payment needs Administrator review.';btn.disabled=false;}else if(tries>=60){clearInterval(timer);status.textContent='Still pending. Check M-Pesa Monitor.';btn.disabled=false;}}).catch(function(){if(tries>=60){clearInterval(timer);status.textContent='Unable to check payment status.';btn.disabled=false;}});},2000);}).catch(function(e){status.textContent=e.message;btn.disabled=false;});}
function escapeHtml(v){return String(v).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
window.addEventListener('DOMContentLoaded',function(){renderPaymentRows();showRegisteredPrice();toggleCustomerMode();if(document.getElementById('selected_customer_id').value){document.getElementById('customer_mode').value='CREDIT';document.querySelector('input[value="EXISTING"]').checked=true;toggleCustomerMode();toggleCreditChoice();}var form=document.getElementById('sales_form');if(form){form.addEventListener('submit',function(ev){var mode=document.getElementById('customer_mode').value;if(mode==='CREDIT'){if(!document.getElementById('due_date').value||!document.getElementById('collateral').value.trim()){ev.preventDefault();alert('Credit invoices require a due date and collateral details.');return;}}else{var count=parseInt(document.getElementById('payment_count').value||'1',10);for(var i=1;i<=count;i++){var m=document.getElementById('payment_method_'+i),c=document.getElementById('mpesa_code_'+i);if(__REQUIRE_MPESA_CODE__&&m&&m.value==='MPESA'&&c&&!c.value.trim()){ev.preventDefault();c.required=true;c.focus();alert('Enter the M-Pesa transaction code for every M-Pesa payment.');return;}}}});}});
</script>'''
    recent=html_table(['Date & Time','Receipt','Customer','Type','Total','Recorded By','Actions'],[[esc(r['sold_at']),esc(r['receipt_no']),esc(r['customer']),esc(r['customer_type']+' · '+r['sale_type']),money(r['total']),esc(r['user_name']),f'<div class="inline-actions"><a class="btn" href="/sales/receipt?receipt={quote(r["receipt_no"])}">OPEN INVOICE / VIEW</a><a class="btn" href="/sales/export?receipt={quote(r["receipt_no"])}&format=pdf">PDF</a><a class="btn" href="/sales/export?receipt={quote(r["receipt_no"])}&format=xlsx">EXCEL</a></div>'] for r in rows])
    credit_option='<option value="CREDIT">CREDIT CUSTOMER</option>' if core.get_setting('allow_credit_sales','YES')=='YES' else ''
    require_mpesa_code_js='true' if core.get_setting('require_mpesa_code','YES')=='YES' else 'false'
    content=content.replace('__CREDIT_OPTION__',credit_option).replace('__REQUIRE_MPESA_CODE__',require_mpesa_code_js).replace('__NOTICE__',notice).replace('__CSRF__',csrf_input(CURRENT_HANDLER)).replace('__BRANCH_FIELD__',branch_field).replace('__FULL_NAME__',esc(u['full_name'])).replace('__PRODUCT_OPTS__',product_opts).replace('__SEARCH_QUERY__',esc(search_query)).replace('__RESULT_HTML__',result_html).replace('__PAYMENTS__',payments).replace('__PAYMENT_OPTIONS__',''.join(f'<option value="{i}" {"selected" if i==default_payment_count else ""}>{i}</option>' for i in range(1,max_payments+1))).replace('__MAX_PAYMENTS__',str(max_payments)).replace('__MAX_TX__',str(max_payments)).replace('__RECENT_SALES__',recent).replace('__SELECTED_CUSTOMER_ID__',selected_customer_id_val).replace('__SELECTED_CUSTOMER_HTML__',selected_customer_html)
    return layout(u,'Sales / POS',branch_selector+content,'/sales','Direct sales entry')


def sale_detail(receipt):
    c=core.db.c(); sale=c.execute('''SELECT s.*,COALESCE(cu.full_name,'Walk-in Customer') customer_name,COALESCE(cu.phone,'') customer_phone,COALESCE(cu.id_no,'') customer_id_no,COALESCE(u.full_name,'') attended_by,COALESCE(u.username,'') attended_username FROM sales s LEFT JOIN customers cu ON cu.id=s.customer_id JOIN users u ON u.id=s.user_id WHERE s.receipt_no=?''',(receipt,)).fetchone()
    if not sale: c.close(); return None
    lines=c.execute('''SELECT p.name product_name,sl.qty,sl.unit_price,(sl.qty*sl.unit_price) line_total FROM sale_lines sl JOIN products p ON p.id=sl.product_id WHERE sl.sale_id=? ORDER BY sl.id''',(sale['id'],)).fetchall()
    pays=c.execute('SELECT amount,payment_method,mpesa_code,paid_at FROM sale_payments WHERE sale_id=? ORDER BY id',(sale['id'],)).fetchall(); credit=c.execute('SELECT id,due_date,collateral,collateral_value,principal,paid_amount,status FROM credit_accounts WHERE sale_id=?',(sale['id'],)).fetchone()
    credit_payments=[]
    if credit: credit_payments=[dict(r) for r in c.execute('SELECT amount,payment_method,mpesa_code,payment_date,reference FROM credit_payments WHERE credit_id=? ORDER BY id',(credit['id'],)).fetchall()]
    c.close()
    return {'sale':dict(sale),'lines':[dict(r) for r in lines],'payments':[dict(r) for r in pays],'credit':dict(credit) if credit else None,'credit_payments':credit_payments}

def can_view_sale(u):
    return role_name(u)=='ADMIN' or allowed(u,'CREATE_SALE') or allowed(u,'VIEW_REPORTS')

def sale_receipt_page(u, receipt):
    settings=core.get_settings(); business_name=settings.get('business_name',APP_NAME); subtitle=settings.get('business_subtitle',''); phone=settings.get('business_phone',''); address=settings.get('business_address',''); website=settings.get('business_website',''); tax_pin=settings.get('business_tax_pin',''); branch=settings.get('business_branch_name',''); footer=settings.get('receipt_footer','')
    if not can_view_sale(u): raise PermissionError('Sales viewing is disabled for your account.')
    d=sale_detail(receipt)
    if not d: raise ValueError('Sale receipt was not found.')
    sale=d['sale']
    if role_name(u) not in ('ADMIN','MANAGEMENT') and int(sale.get('branch_id') or 0)!=int(u.get('branch_id') or 0): raise PermissionError('This sale belongs to another branch.')
    lines=d['lines']; pays=d['payments']; credit=d['credit']
    line_rows=[[esc(x['product_name']),x['qty'],money(x['unit_price']),money(x['line_total'])] for x in lines]
    pay_rows=[[esc(x['payment_method']),money(x['amount']),esc(x['mpesa_code'] or ''),esc(x['paid_at'])] for x in pays]
    if credit:
        balance=max(0.0,float(credit['principal'])-float(credit.get('paid_amount') or 0))
        pay_rows=[['INVOICE TOTAL',money(credit['principal']),'',esc(credit['due_date'])]]
        pay_rows.extend([[f"PAYMENT · {x['payment_method']}",money(x['amount']),esc(x.get('mpesa_code') or ''),esc(x['payment_date'])] for x in d.get('credit_payments',[])])
        pay_rows.extend([['PAID TO DATE',money(credit.get('paid_amount') or 0),'',''],['BALANCE DUE',money(balance),'',esc(credit.get('status','OPEN'))]])
    action_base=f'/sales/export?receipt={quote(receipt)}&format='
    heading='CREDIT INVOICE' if credit else 'SALES RECEIPT'
    label='Invoice Number' if credit else 'Receipt Number'
    credit_action='<a class="btn" href="/customers">UPDATE CREDIT PAYMENT</a>' if credit else ''
    content=f'''<section class="card report-print"><div class="report-head"><div><div style="display:flex;align-items:center;gap:12px"><img src="/business-logo" style="width:70px;height:70px;object-fit:contain"><div><h2>{esc(business_name)}</h2><div>{esc(subtitle)} · {esc(branch)}</div><h3>{heading}</h3></div></div><div>{esc(phone)} · {esc(address)}</div><div>{esc(website)} · PIN: {esc(tax_pin)}</div><div><b>{label}:</b> {esc(receipt)} · <b>Sale Type:</b> {esc(sale.get('sale_type','RETAIL'))}</div><div class="muted">Date/Time: {esc(sale['sold_at'])} · Attended By: {esc(sale['attended_by'])} @{esc(sale['attended_username'])}</div><div class="muted">Customer: {esc(sale['customer_name'])} · Phone: {esc(sale['customer_phone'])}</div></div><div class="actions"><button type="button" class="btn" onclick="window.print()">PRINT {heading}</button><a class="btn" href="{action_base}pdf">DOWNLOAD PDF</a><a class="btn" href="{action_base}xlsx">DOWNLOAD EXCEL</a>{credit_action}</div></div>{html_table(['Product','Qty','Unit Price','Line Total'],line_rows)}<div style="margin-top:14px"><h3>{'Invoice / Credit Status' if credit else 'Payments'}</h3>{html_table(['Method / Status','Amount','M-Pesa Code','Paid / Due Date'],pay_rows)}</div><div class="sale-total-bar" style="margin-top:14px"><span>{'OUTSTANDING BALANCE' if credit else 'TOTAL'}</span><strong>{money(max(0.0,float(credit['principal'])-float(credit.get('paid_amount') or 0)) if credit else sale['total'])}</strong></div><div class="muted" style="text-align:center;margin-top:14px">{esc(footer)}</div>{f'<div class="selected-customer"><b>CREDIT DETAILS</b><br>Due: {esc(credit["due_date"])} · Status: {esc(credit.get("status","OPEN"))} · Collateral: {esc(credit["collateral"])} · Collateral Value: {money(credit["collateral_value"])}</div>' if credit else ''}</section>'''
    return layout(u,'Credit Invoice' if credit else 'Sale Receipt',content,'/sales','Printable invoice or sale receipt')

def sale_export_data(receipt):
    d=sale_detail(receipt)
    if not d: raise ValueError('Sale receipt was not found.')
    sale=d['sale']; rows=[]
    if d['credit']:
        credit=d['credit']; balance=max(0.0,float(credit['principal'])-float(credit.get('paid_amount') or 0))
        for ln in d['lines']:
            rows.append([sale['receipt_no'],sale['sold_at'],sale['customer_name'],ln['product_name'],ln['qty'],ln['unit_price'],ln['line_total'],'CREDIT INVOICE',credit['principal'],'',sale['attended_by']])
        for pay in d.get('credit_payments',[]):
            rows.append([sale['receipt_no'],pay['payment_date'],sale['customer_name'],'CREDIT REPAYMENT','','','',pay['payment_method'],pay['amount'],pay.get('mpesa_code') or '',sale['attended_by']])
        rows.append([sale['receipt_no'],sale['sold_at'],sale['customer_name'],'PAID TO DATE','','','', 'INVOICE SUMMARY',credit.get('paid_amount') or 0,'',sale['attended_by']])
        rows.append([sale['receipt_no'],sale['sold_at'],sale['customer_name'],'OUTSTANDING BALANCE','','','',credit.get('status','OPEN'),balance,'',sale['attended_by']])
    else:
        if d['lines']:
            for ln in d['lines']:
                for pay in d['payments'] or [{'amount':'','payment_method':'','mpesa_code':'','paid_at':''}]:
                    rows.append([sale['receipt_no'],sale['sold_at'],sale['customer_name'],ln['product_name'],ln['qty'],ln['unit_price'],ln['line_total'],pay['payment_method'],pay['amount'],pay['mpesa_code'] or '',sale['attended_by']])
    headers=['Invoice / Receipt','Date/Time','Customer','Product / Record','Qty','Unit Price','Line Total','Payment / Status','Payment Amount','M-Pesa Code','Attended By']
    return 'MONTEZ LIQUOR INVOICE / SALE',headers,rows

def ai_local_answer(u, question):
    q=str(question or '').strip(); ql=q.lower(); branch_id=branch_scope_for(u); d=core.dashboard(branch_id); live=core.live_monitoring(branch_id)
    ds=f"{now()[:10]} 00:00:00"; de=f"{now()[:10]} 23:59:59"; ms=f"{now()[:7]}-01 00:00:00"; me=f"{now()[:10]} 23:59:59"
    pt=core.business_performance(ds,de,branch_id); pm=core.business_performance(ms,me,branch_id)
    c=core.db.c(); pc=c.execute('SELECT COUNT(*) n FROM products WHERE active=1').fetchone()['n']; low=c.execute('SELECT p.name,bs.qty stock_qty,bs.reorder_level FROM products p JOIN branch_stock bs ON bs.product_id=p.id WHERE p.active=1 AND bs.qty<=bs.reorder_level'+(' AND bs.branch_id=?' if branch_id is not None else '')+' ORDER BY bs.qty,p.name LIMIT 10',(int(branch_id),) if branch_id is not None else ()).fetchall(); c.close()
    if 'sales' in ql and 'today' in ql: return f"Today's sales are {money(d['today_sales'])} across {d['transactions']} transaction(s). Cash {money(d['cash'])}; M-Pesa {money(d['mpesa'])}."
    if 'expense' in ql: return f"Today's recorded expenses are {money(d['expenses'])}."
    if 'stock' in ql or 'inventory' in ql:
        names=', '.join(f"{r['name']} ({r['stock_qty']:g})" for r in low) if low else 'none'
        return f"There are {pc} active products. Stock value is {money(d['stock_value'])}. Low/reorder stock: {names}."
    if 'profit' in ql or 'loss' in ql or 'performance' in ql: return f"Today's performance is {pt['percentage']:.1f}% ({pt['status']}); this month is {pm['percentage']:.1f}% ({pm['status']}). Threshold {pt['threshold']}%. Today's net result is {money(pt['net_profit'])}."
    if 'user' in ql or 'online' in ql or 'active' in ql: return f"There are {active_user_count()} active signed-in user(s)."
    return f"MONTEZ LIQUOR AI Assistant: today sales {money(d['today_sales'])}, expenses {money(d['expenses'])}, stock value {money(d['stock_value'])}, active users {active_user_count()}. Ask about sales, expenses, stock, performance or users."


def ai_openai_answer(u, question):
    key=core.get_setting('ai_api_key','').strip()
    if not key: raise ValueError('OpenAI API key is not configured. Use LOCAL mode or enter a key in Administrator Settings.')
    endpoint=core.get_setting('ai_base_url','https://api.openai.com/v1/responses').strip() or 'https://api.openai.com/v1/responses'; model=core.get_setting('ai_model','gpt-5.6-luna').strip() or 'gpt-5.6-luna'
    branch_id=branch_scope_for(u); d=core.dashboard(branch_id); live=core.live_monitoring(branch_id); perf=core.business_performance(f"{now()[:10]} 00:00:00",f"{now()[:10]} 23:59:59",branch_id)
    context={'today_sales':d['today_sales'],'today_expenses':d['expenses'],'cash':d['cash'],'mpesa':d['mpesa'],'stock_value':d['stock_value'],'low_stock':live['low_stock'],'active_users':active_user_count(),'performance':perf}
    payload={'model':model,'input':[{'role':'system','content':'You are MONTEZ LIQUOR AI Assistant. Use only supplied MONTEZ LIQUOR context. Do not invent business figures.'},{'role':'user','content':f"Context: {json.dumps(context,default=str,ensure_ascii=False)}\nQuestion: {question}"}]}
    req=urlrequest.Request(endpoint,data=json.dumps(payload).encode('utf-8'),headers={'Content-Type':'application/json','Authorization':'Bearer '+key},method='POST')
    try:
        with urlrequest.urlopen(req,timeout=25) as resp: data=json.loads(resp.read().decode('utf-8'))
    except urlerror.HTTPError as e: raise ValueError(f'AI provider HTTP {e.code}.')
    except Exception as e: raise ValueError(f'AI provider connection failed: {e}')
    text=data.get('output_text','').strip()
    if not text:
        parts=[]
        for item in data.get('output',[]) or []:
            for ct in item.get('content',[]) or []:
                if isinstance(ct,dict) and ct.get('text'): parts.append(str(ct['text']))
        text='\n'.join(parts).strip()
    if not text: raise ValueError('AI provider returned no answer.')
    return text


def ai_page(u,message=''):
    require_perm_user(u,'AI_ASSISTANT')
    if core.get_setting('ai_assistant_enabled','YES')!='YES': return layout(u,'MONTEZ LIQUOR AI Assistant','<section class="card"><div class="flash error">AI Assistant is disabled in Administrator Settings.</div></section>','/ai')
    provider=core.get_setting('ai_provider','LOCAL'); answer='' if message.startswith('AI_ERROR:') else message; err=(f'<div class="flash error" style="margin-top:12px">{esc(message[9:])}</div>' if message.startswith('AI_ERROR:') else '')
    quick='<div class="ai-quick"><button type="button" class="btn" onclick="document.getElementById(\'ai_question\').value=\'How are sales today?\'">SALES TODAY</button><button type="button" class="btn" onclick="document.getElementById(\'ai_question\').value=\'What is the business performance?\'">PERFORMANCE</button><button type="button" class="btn" onclick="document.getElementById(\'ai_question\').value=\'Which products are low in stock?\'">LOW STOCK</button><button type="button" class="btn" onclick="document.getElementById(\'ai_question\').value=\'How many users are active?\'">ACTIVE USERS</button></div>'
    answer_html=f'<section class="ai-answer" style="margin-top:12px">{esc(answer)}</section>' if answer else ''
    content=f'<section class="card ai-card"><div class="section-head"><div><h2 style="margin:0">MONTEZ LIQUOR AI Assistant</h2><div class="muted">{esc(role_display(role_name(u)))} · {esc(provider)} mode</div></div><img src="/business-logo" style="width:54px;height:54px;background:#fff;border-radius:12px;padding:5px"></div><p>Ask about live sales, expenses, stock, business performance, active users and reports.</p>{quick}</section><section class="card" style="margin-top:14px"><form method="post" action="/ai">{csrf_input(CURRENT_HANDLER)}<label>ASK MONTEZ LIQUOR AI</label><textarea id="ai_question" name="question" rows="4" placeholder="Type your question..." required></textarea><div class="actions" style="margin-top:10px"><button class="btn gold" type="submit">ASK AI</button></div></form>{err}{answer_html}</section>'
    return layout(u,'MONTEZ LIQUOR AI Assistant',content,'/ai','Business assistant for all enabled roles')


def purchase_page(u, message='', query=None):
    require_perm_user(u,'CREATE_PURCHASE'); branch_id=branch_scope_for(u,query); ps=core.products(branch_id); opts=''.join(f'<option value="{p["id"]}" data-sku="{esc(p.get("sku",""))}">{esc(p["name"])} · Stock {p["stock_qty"]}</option>' for p in ps) or '<option value="">No products registered</option>'
    c=core.db.c(); branches=[dict(b) for b in c.execute('SELECT id,name FROM branches WHERE active=1 ORDER BY name').fetchall()]; purchase_sql='SELECT pu.id purchase_id,pu.purchase_no,pu.purchased_at,pu.supplier,pu.total,pu.payment_method,pu.reference,pu.branch_id,u.full_name,p.name product,pl.product_id,pl.qty,pl.unit_cost FROM purchases pu JOIN purchase_lines pl ON pl.purchase_id=pu.id JOIN products p ON p.id=pl.product_id JOIN users u ON u.id=pu.user_id'
    if branch_id is not None: rows=c.execute(purchase_sql+' WHERE pu.branch_id=? ORDER BY pu.id DESC LIMIT 100',(int(branch_id),)).fetchall()
    else: rows=c.execute(purchase_sql+' ORDER BY pu.id DESC LIMIT 100').fetchall()
    c.close()
    notice=f'<div class="flash error">{esc(message)}</div>' if message else ''
    purchase_branch_field=''
    if role_name(u) in ('ADMIN','MANAGEMENT'):
        purchase_branch_opts='<option value="">Select receiving branch</option>'+''.join(f'<option value="{b["id"]}" {"selected" if branch_id==int(b["id"]) else ""}>{esc(b["name"])}</option>' for b in branches)
        purchase_branch_field=f'<div><label>Receiving Branch *</label><select name="branch_id" required>{purchase_branch_opts}</select></div>'
    else: purchase_branch_field=f'<input type="hidden" name="branch_id" value="{int(u.get("branch_id") or 0)}">'
    purchase_selector=branch_scope_selector(u,query,'/purchases')
    if role_name(u)=='ADMIN':
        purchase_rows=[]
        for r in rows:
            pid=int(r['purchase_id'])
            payment_opts=''.join('<option '+('selected' if x==r['payment_method'] else '')+'>'+x+'</option>' for x in ('Cash','M-Pesa','Credit'))
            actions=f'''<details><summary class="btn">EDIT / DELETE</summary><form method="post" action="/admin/record-change" class="card" style="min-width:260px;margin-top:8px">{csrf_input(CURRENT_HANDLER)}<input type="hidden" name="entity" value="purchase"><input type="hidden" name="action" value="edit"><input type="hidden" name="record_id" value="{pid}"><label>Supplier</label><input name="supplier" value="{esc(r['supplier'])}" required><label>Quantity</label><input type="number" name="qty" min="1" step="1" value="{r['qty']}" required><label>Unit cost</label><input type="number" name="unit_cost" min="0" step="0.01" value="{float(r['unit_cost']):.2f}" required><label>Payment method</label><select name="payment_method">{payment_opts}</select><label>Reference</label><input name="reference" value="{esc(r['reference'] or '')}"><label>Reason (required)</label><textarea name="reason" required minlength="5"></textarea><button class="btn gold" name="action" value="edit">SAVE EDIT</button></form><form method="post" action="/admin/record-change" class="card" style="margin-top:8px" onsubmit="return confirm('Delete this purchase and reverse its stock impact?')">{csrf_input(CURRENT_HANDLER)}<input type="hidden" name="entity" value="purchase"><input type="hidden" name="action" value="delete"><input type="hidden" name="record_id" value="{pid}"><label>Reason (required)</label><textarea name="reason" required minlength="5"></textarea><button class="btn danger" type="submit">DELETE PURCHASE</button></form></details>'''
            purchase_rows.append([esc(r['purchased_at']),esc(r['purchase_no']),esc(r['supplier']),esc(r['product']),r['qty'],money(r['unit_cost']),money(r['total']),esc(r['payment_method']),esc(r['full_name']),actions])
        purchases_table=html_table(['Date & Time','Purchase','Supplier','Product','Qty','Unit Cost','Total','Payment','By','Admin Actions'],purchase_rows)
    else:
        purchases_table=html_table(['Date & Time','Purchase','Supplier','Product','Qty','Unit Cost','Total','Payment','By'],[[esc(r['purchased_at']),esc(r['purchase_no']),esc(r['supplier']),esc(r['product']),r['qty'],money(r['unit_cost']),money(r['total']),esc(r['payment_method']),esc(r['full_name'])] for r in rows])
    content=f'''{purchase_selector}{notice}<section class="card"><div class="section-head"><h2>New Purchase / Stock In</h2><span class="pill">RECORDED BY {esc(u['full_name'])}</span></div><form method="post" action="/purchases">{csrf_input(CURRENT_HANDLER)}<input type="hidden" id="purchase_line_count" name="purchase_line_count" value="1"><div class="form-grid">{purchase_branch_field}<div><label>Supplier *</label><input name="supplier" required></div><div><label>Payment *</label><select name="payment_method"><option>Cash</option><option>M-Pesa</option><option>Credit</option></select></div><div><label>Reference / M-Pesa Code</label><input name="reference"></div></div><div class="section-head" style="margin-top:14px"><h3>Purchase items</h3><button type="button" class="btn" onclick="addPurchaseLine()">+ ADD LINE</button></div><div id="purchase_lines"><div class="form-grid purchase-line" style="border:1px solid var(--line);padding:10px;border-radius:10px;margin-bottom:8px"><div><label>Product *</label><select name="product_id_1" id="purchase_product_id_1" class="purchase-product" required>{opts}</select><div class="scan-inline" style="margin-top:7px"><div><input id="purchase_sku_scan_1" placeholder="Scan/type SKU"></div><button type="button" class="btn scan-btn" onclick="openScanner('purchase_sku_scan_1')">SCAN</button></div><button type="button" class="btn" style="margin-top:7px" onclick="lookupGenericSku('purchase_sku_scan_1','purchase_product_id_1')">LOOK UP SKU</button></div><div><label>Quantity *</label><input type="number" min="1" step="1" name="qty_1" required></div><div><label>Unit Cost *</label><input type="number" min="0" step="0.01" name="unit_cost_1" required></div><div><label>Line Total</label><input class="purchase-line-total" readonly value="0.00"></div></div></div><div class="section-head"><strong>Total: KES <span id="purchase_grand_total">0.00</span></strong></div><div class="actions" style="margin-top:14px"><button class="btn gold" type="submit">SAVE PURCHASE</button><button class="btn" type="reset">CLEAR</button></div><script>
function purchaseRecalc(){{var total=0;document.querySelectorAll('#purchase_lines .purchase-line').forEach(function(r){{var q=parseFloat(r.querySelector('[name^="qty_"]').value||0)||0,c=parseFloat(r.querySelector('[name^="unit_cost_"]').value||0)||0,t=q*c;total+=t;r.querySelector('.purchase-line-total').value=t.toFixed(2)}});document.getElementById('purchase_grand_total').textContent=total.toFixed(2)}}
function addPurchaseLine(){{var box=document.getElementById('purchase_lines'),first=box.querySelector('.purchase-line'),n=Number(document.getElementById('purchase_line_count').value||1)+1,row=first.cloneNode(true);document.getElementById('purchase_line_count').value=n;row.querySelectorAll('[name],[id]').forEach(function(e){{if(e.name)e.name=e.name.replace(/_\\d+$/,'_'+n);if(e.id)e.id=e.id.replace(/_\\d+$/,'_'+n)}});row.querySelectorAll('input').forEach(function(e){{if(e.name.indexOf('qty_')===0)e.value='1';else if(e.name.indexOf('unit_cost_')===0||e.classList.contains('purchase-line-total')||e.id.indexOf('purchase_sku_scan_')===0)e.value=''}});row.querySelector('select').value='';box.appendChild(row);purchaseRecalc()}}
document.addEventListener('input',function(e){{if(e.target.closest&&e.target.closest('#purchase_lines'))purchaseRecalc()}});
</script></form></section></section><section class="card" style="margin-top:14px"><div class="section-head"><h2>Recent Purchases</h2></div>{purchases_table}</section>'''
    return layout(u,'Purchases / Stock In',content,'/purchases','Stock acquisition and supplier control')


def expense_page(u, message='', query=None):
    require_perm_user(u,'RECORD_EXPENSE')
    fd,ft,td,tt,preset=period_from_query(query or {})
    start=f'{fd} {ft}:00'; end=f'{td} {tt}:59'; branch_id=branch_scope_for(u,query)
    rows=core.expense_report_data(start,end,branch_id)
    selector=branch_scope_selector(u,query,'/expenses')
    notice=f'<div class="flash error">{esc(message)}</div>' if message else ''
    expense_categories=[x.strip() for x in core.get_setting('expense_categories','Rent,Utilities,Wages,Transport,Licensing,Repairs,Security,Marketing,Bank Charges,Other').split(',') if x.strip()]
    category_opts=''.join(f'<option>{esc(x)}</option>' for x in expense_categories)
    branch_opts=''
    if role_name(u) in ('ADMIN','MANAGEMENT'):
        c=core.db.c(); brs=[dict(b) for b in c.execute('SELECT id,name FROM branches WHERE active=1 ORDER BY name').fetchall()]; c.close()
        branch_opts='<div><label>Branch *</label><select name="branch_id" required><option value="">Select branch</option>'+''.join(f'<option value="{b["id"]}">{esc(b["name"])}</option>' for b in brs)+'</select></div>'
    else: branch_opts=f'<input type="hidden" name="branch_id" value="{int(u.get("branch_id") or 0)}">'
    if role_name(u)=='ADMIN':
        expense_rows=[]
        for r in rows:
            eid=int(r.get('expense_id') or 0)
            payment_opts=''.join('<option '+('selected' if x==r['payment_method'] else '')+'>'+x+'</option>' for x in ('Cash','M-Pesa','Bank'))
            actions=f'''<details><summary class="btn">EDIT / DELETE</summary><form method="post" action="/admin/record-change" class="card" style="min-width:240px;margin-top:8px">{csrf_input(CURRENT_HANDLER)}<input type="hidden" name="entity" value="expense"><input type="hidden" name="action" value="edit"><input type="hidden" name="record_id" value="{eid}"><label>Category</label><input name="category" value="{esc(r['category'])}" required><label>Description</label><input name="description" value="{esc(r['description'])}" required><label>Amount</label><input type="number" name="amount" min="0.01" step="0.01" value="{float(r['amount']):.2f}" required><label>Payment method</label><select name="payment_method">{payment_opts}</select><label>Reference</label><input name="reference" value="{esc(r['reference'] or '')}"><label>Reason (required)</label><textarea name="reason" required minlength="5"></textarea><button class="btn gold" type="submit">SAVE EDIT</button></form><form method="post" action="/admin/record-change" class="card" style="margin-top:8px" onsubmit="return confirm('Delete this expense?')">{csrf_input(CURRENT_HANDLER)}<input type="hidden" name="entity" value="expense"><input type="hidden" name="action" value="delete"><input type="hidden" name="record_id" value="{eid}"><label>Reason (required)</label><textarea name="reason" required minlength="5"></textarea><button class="btn danger" type="submit">DELETE EXPENSE</button></form></details>'''
            expense_rows.append([esc(r['expense_date']),esc(r['expense_no']),esc(r['category']),esc(r['description']),money(r['amount']),esc(r['payment_method']),esc(r['reference'] or ''),esc(r['attended_by']),actions])
        report_table=html_table(['Date & Time','Expense','Category','Description','Amount','Payment','Reference','Recorded By','Admin Actions'],expense_rows,'No expenses for selected period.')
    else:
        report_table=html_table(['Date & Time','Expense','Category','Description','Amount','Payment','Reference','Recorded By'],[[esc(r['expense_date']),esc(r['expense_no']),esc(r['category']),esc(r['description']),money(r['amount']),esc(r['payment_method']),esc(r['reference'] or ''),esc(r['attended_by'])] for r in rows],'No expenses for selected period.')
    exp=f'/report/export?kind=expenses&format={{fmt}}&start={esc(start)}&end={esc(end)}'
    content=f'''{selector}{notice}<section class="card"><div class="section-head"><h2>Business Expenses</h2><span class="pill">ACCOUNTABLE: {esc(u['full_name'])}</span></div><form method="post" action="/expenses">{csrf_input(CURRENT_HANDLER)}{branch_opts}<input type="hidden" id="expense_line_count" name="expense_line_count" value="1"><div class="form-grid"><div><label>Payment</label><select name="payment_method"><option>Cash</option><option>M-Pesa</option><option>Bank</option></select></div><div><label>Reference / Receipt No.</label><input name="reference"></div></div><div class="section-head" style="margin-top:14px"><h3>Expense items</h3><button type="button" class="btn" onclick="addExpenseLine()">+ ADD LINE</button></div><div id="expense_lines"><div class="form-grid expense-line" style="border:1px solid var(--line);padding:10px;border-radius:10px;margin-bottom:8px"><div><label>Category *</label><select name="category_1" required>{category_opts}</select></div><div><label>Description *</label><input name="description_1" required></div><div><label>Amount (KES) *</label><input type="number" name="amount_1" min="0.01" step="0.01" required></div></div></div><div class="actions" style="margin-top:14px"><button class="btn gold" type="submit">SAVE EXPENSES</button><button class="btn" type="reset">CLEAR</button></div><script>
function addExpenseLine(){{var box=document.getElementById('expense_lines'),first=box.querySelector('.expense-line'),n=Number(document.getElementById('expense_line_count').value||1)+1,row=first.cloneNode(true);document.getElementById('expense_line_count').value=n;row.querySelectorAll('[name],[id]').forEach(function(e){{if(e.name)e.name=e.name.replace(/_\\d+$/,'_'+n);if(e.id)e.id=e.id.replace(/_\\d+$/,'_'+n)}});row.querySelectorAll('input').forEach(function(e){{e.value=''}});box.appendChild(row)}}
</script></form></section></section><section class="card" style="margin-top:14px"><div class="section-head"><h2>Expense Report</h2><span class="pill">{esc(preset)}</span></div>{range_toolbar('/expenses',fd,ft,td,tt,branch_id)}<div class="actions" style="margin-top:10px"><button type="button" class="btn" onclick="window.print()">PRINT REPORT</button><a class="btn" href="{exp.format(fmt='pdf')}">DOWNLOAD PDF</a><a class="btn" href="{exp.format(fmt='xlsx')}">DOWNLOAD EXCEL</a></div><div class="report-print">{report_table}</div></section>'''
    return layout(u,'Expenses',content,'/expenses','Expense entry and expense reporting')

def expense_report_page(u, query=None):
    require_perm_user(u,'VIEW_REPORTS')
    fd,ft,td,tt,preset=period_from_query(query or {}); start=f'{fd} {ft}:00'; end=f'{td} {tt}:59'; branch_id=branch_scope_for(u,query)
    rows=core.expense_report_data(start,end,branch_id)
    if role_name(u)=='ADMIN':
        expense_rows=[]
        for r in rows:
            eid=int(r.get('expense_id') or 0)
            payment_opts=''.join('<option '+('selected' if x==r['payment_method'] else '')+'>'+x+'</option>' for x in ('Cash','M-Pesa','Bank'))
            actions=f'''<details><summary class="btn">EDIT / DELETE</summary><form method="post" action="/admin/record-change" class="card" style="min-width:240px;margin-top:8px">{csrf_input(CURRENT_HANDLER)}<input type="hidden" name="entity" value="expense"><input type="hidden" name="action" value="edit"><input type="hidden" name="record_id" value="{eid}"><label>Category</label><input name="category" value="{esc(r['category'])}" required><label>Description</label><input name="description" value="{esc(r['description'])}" required><label>Amount</label><input type="number" name="amount" min="0.01" step="0.01" value="{float(r['amount']):.2f}" required><label>Payment method</label><select name="payment_method">{payment_opts}</select><label>Reference</label><input name="reference" value="{esc(r['reference'] or '')}"><label>Reason (required)</label><textarea name="reason" required minlength="5"></textarea><button class="btn gold" type="submit">SAVE EDIT</button></form><form method="post" action="/admin/record-change" class="card" style="margin-top:8px" onsubmit="return confirm('Delete this expense?')">{csrf_input(CURRENT_HANDLER)}<input type="hidden" name="entity" value="expense"><input type="hidden" name="action" value="delete"><input type="hidden" name="record_id" value="{eid}"><label>Reason (required)</label><textarea name="reason" required minlength="5"></textarea><button class="btn danger" type="submit">DELETE EXPENSE</button></form></details>'''
            expense_rows.append([esc(r['expense_date']),esc(r['expense_no']),esc(r['category']),esc(r['description']),money(r['amount']),esc(r['payment_method']),esc(r['reference'] or ''),esc(r['attended_by']),actions])
        table=html_table(['Date & Time','Expense','Category','Description','Amount','Payment','Reference','Recorded By','Admin Actions'],expense_rows,'No expenses for selected period.')
    else:
        table=html_table(['Date & Time','Expense','Category','Description','Amount','Payment','Reference','Recorded By'],[[esc(r['expense_date']),esc(r['expense_no']),esc(r['category']),esc(r['description']),money(r['amount']),esc(r['payment_method']),esc(r['reference'] or ''),esc(r['attended_by'])] for r in rows],'No expenses for selected period.')
    exp=f'/report/export?kind=expenses&start={quote(start)}&end={quote(end)}&branch_id={branch_id or ""}&format={{fmt}}'
    selector=branch_scope_selector(u,query,'/expense-report')
    content=f'''{selector}<section class="card"><div class="section-head"><h2>Expense Report</h2><span class="pill">{esc(preset)}</span></div>{range_toolbar('/expense-report',fd,ft,td,tt,branch_id)}<div class="muted" style="margin-top:8px">Generated by {esc(u['full_name'])} @{esc(u['username'])} at {now()}</div><div class="actions" style="margin-top:10px"><button type="button" class="btn" onclick="window.print()">PRINT</button><a class="btn" href="{exp.format(fmt='pdf')}">DOWNLOAD PDF</a><a class="btn" href="{exp.format(fmt='xlsx')}">DOWNLOAD EXCEL</a></div></section><section class="card report-print" style="margin-top:14px">{table}</section>'''
    return layout(u,'Expense Report',content,'/expense-report','Custom expense reporting')

def customers_page(u,query=None):
    require_perm_user(u,'MANAGE_CUSTOMERS')
    branch_id=branch_scope_for(u,query)
    c=core.db.c()
    customer_scope=' AND c.branch_id=?' if branch_id is not None else ''
    credit_scope=' WHERE s.branch_id=?' if branch_id is not None else ''
    args=(int(branch_id),) if branch_id is not None else ()
    rows=c.execute("SELECT c.*,COALESCE(SUM(CASE WHEN ca.status='OPEN' AND ca_branch.branch_id IS NOT NULL THEN ca.principal-ca.paid_amount ELSE 0 END),0) balance FROM customers c LEFT JOIN credit_accounts ca ON ca.customer_id=c.id LEFT JOIN sales ca_branch ON ca_branch.id=ca.sale_id WHERE c.active=1"+customer_scope+" GROUP BY c.id ORDER BY c.full_name",args).fetchall()
    credits=c.execute("SELECT ca.id credit_id,ca.principal,ca.paid_amount,MAX(ca.principal-ca.paid_amount,0) balance,ca.due_date,ca.status,c.full_name,c.phone,s.receipt_no FROM credit_accounts ca JOIN customers c ON c.id=ca.customer_id JOIN sales s ON s.id=ca.sale_id"+credit_scope+" ORDER BY CASE WHEN ca.status='OPEN' THEN 0 ELSE 1 END,c.full_name,ca.id DESC",args).fetchall(); c.close()
    customer_table=html_table(['Customer','Phone','ID / Passport','Address','Credit Balance'],[[esc(r['full_name']),esc(r['phone']),esc(r['id_no'] or ''),esc(r['address'] or ''),money(r['balance'])] for r in rows])
    cards=[]
    for r in credits:
        b=float(r['balance'] or 0); cid=int(r['credit_id']); is_open=(str(r['status']).upper()=='OPEN' and b>0.009)
        invoice_link=f'<a class="btn" href="/sales/receipt?receipt={quote(r["receipt_no"])}">VIEW INVOICE</a>'
        if is_open:
            cards.append(f'''<div class="card" style="margin-top:10px"><div class="section-head"><div><h3 style="margin:0">{esc(r['full_name'])}</h3><div class="muted">Invoice: {esc(r['receipt_no'])} · Phone: {esc(r['phone'])} · Due: {esc(r['due_date'])}</div></div><span class="pill">BALANCE {money(b)}</span></div><div class="muted">Status: OPEN · Paid to date: {money(r['paid_amount'])} · {invoice_link}</div><div class="form-grid"><div><label>Payment Made</label><input type="number" min="0.01" max="{b:.2f}" step="0.01" id="credit_payment_{cid}" oninput="creditBalance({cid},{b:.2f})"></div><div><label>Payment Method</label><select id="credit_method_{cid}" onchange="toggleCreditMpesa({cid})"><option value="CASH">CASH</option><option value="MPESA">M-PESA</option></select></div><div><label>M-Pesa Transaction Code *</label><input id="credit_ref_{cid}" placeholder="Required for M-Pesa" autocomplete="off" maxlength="30"></div><div><label>Balance Remaining</label><input id="credit_balance_{cid}" value="{b:.2f}" readonly class="locked-price"></div></div><div class="actions" style="margin-top:10px"><button type="button" class="btn gold" onclick="saveCreditPayment({cid})">SAVE PAYMENT / UPDATE INVOICE</button><span class="muted" id="credit_status_{cid}"></span></div></div>''')
        else:
            cards.append(f'''<div class="card" style="margin-top:10px"><div class="section-head"><div><h3 style="margin:0">{esc(r['full_name'])}</h3><div class="muted">Invoice: {esc(r['receipt_no'])} · Phone: {esc(r['phone'])} · Due: {esc(r['due_date'])}</div></div><span class="pill">PAID IN FULL</span></div><div class="muted">Principal: {money(r['principal'])} · Paid: {money(r['paid_amount'])} · Balance: {money(b)} · {invoice_link}</div></div>''')
    credit_html=''.join(cards) or '<div class="muted">No open credit balances.</div>'
    js='''<script>function creditBalance(id,b){var p=parseFloat(document.getElementById('credit_payment_'+id).value||'0')||0;document.getElementById('credit_balance_'+id).value=Math.max(0,b-p).toFixed(2);}function toggleCreditMpesa(id){var m=document.getElementById('credit_method_'+id),c=document.getElementById('credit_ref_'+id);if(!m||!c)return;c.required=m.value==='MPESA';if(m.value!=='MPESA')c.value='';}function saveCreditPayment(id){var button=document.querySelector('button[onclick="saveCreditPayment('+id+')"]');if(button&&button.disabled)return;var csrf=document.querySelector('input[name=csrf]'),amount=parseFloat(document.getElementById('credit_payment_'+id).value||'0')||0,method=document.getElementById('credit_method_'+id).value,code=document.getElementById('credit_ref_'+id).value.trim().toUpperCase();if(amount<=0){alert('Enter the payment made by the customer.');return;}if(method==='MPESA'&&!code){alert('M-Pesa transaction code is required for every M-Pesa repayment.');document.getElementById('credit_ref_'+id).focus();return;}var body=new URLSearchParams();body.set('csrf',csrf?csrf.value:'');body.set('credit_id',id);body.set('amount',amount.toFixed(2));body.set('payment_method',method);body.set('reference',code);if(button){button.disabled=true;button.textContent='SAVING PAYMENT…';}document.getElementById('credit_status_'+id).textContent='Saving payment securely…';fetch('/customers/credit-payment',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded','Accept':'application/json'},credentials:'same-origin',body:body.toString()}).then(function(r){return r.text().then(function(raw){var d;try{d=JSON.parse(raw);}catch(e){var msg='The server returned a web page instead of JSON. Refresh the page and sign in again.';if(r.status===403)msg='Permission denied. Ask the administrator to enable customer/credit payment access for your account.';else if(r.status===400)msg='The form token may have expired. Refresh the page and try again.';else if(r.status===500)msg='Server error while updating the invoice. No success was confirmed; check the invoice before retrying.';throw new Error(msg+' (HTTP '+r.status+')');}return {ok:r.ok,d:d};});}).then(function(x){if(!x.ok||x.d.error)throw new Error(x.d.error||'Unable to save payment.');document.getElementById('credit_status_'+id).textContent='Payment saved · Invoice updated · Balance remaining '+Number(x.d.balance_remaining||0).toLocaleString('en-KE',{minimumFractionDigits:2});setTimeout(function(){location.reload();},500);}).catch(function(e){document.getElementById('credit_status_'+id).textContent=e.message;if(button){button.disabled=false;button.textContent='SAVE PAYMENT / UPDATE INVOICE';}});}</script>'''
    selector=branch_scope_selector(u,query,'/customers')
    content=f'''{selector}<section class="card"><div class="section-head"><h2>Customers & Credit</h2><span class="pill">REGISTERED CUSTOMERS</span></div>{customer_table}</section><section class="card" style="margin-top:14px"><div class="section-head"><h2>Credit Customer Payments</h2><span class="pill">PAYMENT & BALANCE</span></div><p class="muted">Sales staff can record each repayment against its invoice. M-Pesa repayments require a unique transaction code. The invoice balance and paid status update immediately, and settled invoices remain available for review.</p>{credit_html}</section>{js}'''
    return layout(u,'Customers / Credit',content,'/customers')


def reports_page(u, form=None, message='', query=None):
    require_perm_user(u,'VIEW_REPORTS')
    scope_src=query if query is not None else (form or {}); branch_id=branch_scope_for(u,scope_src)
    if form:
        fd=form.get('from_date',''); ft=form.get('from_time','00:00'); td=form.get('to_date',''); tt=form.get('to_time','23:59')
        if not fd or not td: fd,ft,td,tt,preset=period_from_query({})
        else: preset='CUSTOM'
    else:
        fd,ft,td,tt,preset=period_from_query(query or {})
    start=f'{fd} {ft}:00'; end=f'{td} {tt}:59'; rows=[]
    try:
        if start>end: raise ValueError('From date/time must be before To date/time.')
        rows=core.report_data(start,end,branch_id)
    except Exception as e: message=str(e)
    notice=f'<div class="flash error">{esc(message)}</div>' if message else ''
    if role_name(u)=='ADMIN':
        sales_rows=[]
        for r in rows:
            sid=int(r.get('sale_id') or 0); sale_type=str(r.get('sale_type') or 'RETAIL')
            actions=f'''<details><summary class="btn">EDIT / DELETE</summary><form method="post" action="/admin/record-change" class="card" style="min-width:230px;margin-top:8px">{csrf_input(CURRENT_HANDLER)}<input type="hidden" name="entity" value="sale"><input type="hidden" name="action" value="edit"><input type="hidden" name="record_id" value="{sid}"><label>Date & time</label><input type="datetime-local" name="sold_at" value="{esc(str(r['sold_at']).replace(' ','T')[:16])}" required><label>Sale channel</label><select name="sale_type"><option {'selected' if sale_type=='RETAIL' else ''}>RETAIL</option><option {'selected' if sale_type=='WHOLESALE' else ''}>WHOLESALE</option></select><label>Reason (required)</label><textarea name="reason" required minlength="5"></textarea><button class="btn gold" type="submit">SAVE EDIT</button></form><form method="post" action="/admin/record-change" class="card" style="margin-top:8px" onsubmit="return confirm('Delete this sale, remove its payment/credit records and return sold units to stock?')">{csrf_input(CURRENT_HANDLER)}<input type="hidden" name="entity" value="sale"><input type="hidden" name="action" value="delete"><input type="hidden" name="record_id" value="{sid}"><label>Reason (required)</label><textarea name="reason" required minlength="5"></textarea><button class="btn danger" type="submit">DELETE SALE</button></form></details>'''
            sales_rows.append([esc(r['receipt_no']),esc(r['sold_at']),esc(r['customer']),r['units'],money(r['total']),money(r['cash']),money(r['mpesa']),esc(r['attended_by']),actions])
        table=html_table(['Receipt','Date & Time','Customer','Units','Total','Cash','M-Pesa','Attended By','Admin Actions'],sales_rows,'No sales for selected period.')
    else:
        table=html_table(['Receipt','Date & Time','Customer','Units','Total','Cash','M-Pesa','Attended By'],[[esc(r['receipt_no']),esc(r['sold_at']),esc(r['customer']),r['units'],money(r['total']),money(r['cash']),money(r['mpesa']),esc(r['attended_by'])] for r in rows],'No sales for selected period.')
    exp=f'/report/export?kind=report&start={quote(start)}&end={quote(end)}&branch_id={branch_id or ""}&format={{fmt}}'
    total=sum(float(r['total']) for r in rows); cash=sum(float(r['cash']) for r in rows); mp=sum(float(r['mpesa']) for r in rows); units=sum(float(r['units']) for r in rows)
    selector=branch_scope_selector(u,scope_src,'/reports')
    content=f'''{selector}{notice}<section class="card"><div class="section-head"><h2>Sales & Monthly Reports</h2><span class="pill">{esc(preset)}</span></div>{range_toolbar('/reports',fd,ft,td,tt,branch_id)}<div class="grid four" style="margin-top:12px"><div class="metric"><div class="muted">SALES</div><div class="v">{money(total)}</div></div><div class="metric"><div class="muted">CASH</div><div class="v">{money(cash)}</div></div><div class="metric"><div class="muted">M-PESA</div><div class="v">{money(mp)}</div></div><div class="metric"><div class="muted">UNITS</div><div class="v">{units:g}</div></div></div><div class="actions" style="margin-top:10px"><button type="button" class="btn" onclick="window.print()">PRINT REPORT</button><a class="btn" href="{exp.format(fmt='pdf')}">DOWNLOAD PDF</a><a class="btn" href="{exp.format(fmt='xlsx')}">DOWNLOAD EXCEL</a></div></section><section class="card report-print" style="margin-top:14px"><div class="report-head"><div><h2>{esc(APP_NAME)}</h2><div class="muted">Period: {esc(start)} → {esc(end)} · Generated by {esc(u['full_name'])} @{esc(u['username'])} at {now()}</div></div></div>{table}</section>'''
    return layout(u,'Reports',content,'/reports','End day, week, monthly and custom reporting')

def stock_report_page(u, form=None, query=None):
    require_perm_user(u,'VIEW_STOCK')
    src=query if query is not None else (form or {})
    fd,ft,td,tt,preset=period_from_query(src); start=f'{fd} {ft}:00'; end=f'{td} {tt}:59'
    role=role_name(u); c=core.db.c()
    branches=[dict(x) for x in c.execute('SELECT id,name FROM branches WHERE active=1 ORDER BY name').fetchall()]
    c.close()
    if role not in ('ADMIN','MANAGEMENT'):
        branch_id=int(u.get('branch_id') or 0)
        if not branch_id: raise PermissionError('Your account has no assigned branch.')
        branches=[b for b in branches if int(b['id'])==branch_id]
    else:
        raw=(src.get('branch_id') or [''])[0] if isinstance(src.get('branch_id'),list) else src.get('branch_id','')
        branch_id=int(raw) if str(raw).isdigit() else None
        if branch_id is not None and not any(int(b['id'])==branch_id for b in branches): raise PermissionError('You are not authorised to view that branch.')
    rows=core.stock_report(start,end,branch_id)
    total_retail_units=sum(float(r['retail_units']) for r in rows); total_wholesale_units=sum(float(r['wholesale_units']) for r in rows); total_units=total_retail_units+total_wholesale_units
    total_retail_revenue=sum(float(r['retail_revenue']) for r in rows); total_wholesale_revenue=sum(float(r['wholesale_revenue']) for r in rows); total_revenue=total_retail_revenue+total_wholesale_revenue
    channel_summary=f'''<div class="grid two" style="margin-top:12px"><section class="card"><h3>Retail Performance</h3><div>Units sold: <b>{total_retail_units:g}</b> ({(100*total_retail_units/total_units if total_units else 0):.1f}%)</div><div>Revenue: <b>{money(total_retail_revenue)}</b> ({(100*total_retail_revenue/total_revenue if total_revenue else 0):.1f}%)</div></section><section class="card"><h3>Wholesale Performance</h3><div>Units sold: <b>{total_wholesale_units:g}</b> ({(100*total_wholesale_units/total_units if total_units else 0):.1f}%)</div><div>Revenue: <b>{money(total_wholesale_revenue)}</b> ({(100*total_wholesale_revenue/total_revenue if total_revenue else 0):.1f}%)</div></section></div>'''
    table=html_table(['Product Name','Category','Opening Stock','Purchased','Sold','Adjustments','Closing Stock','Retail Sold','Wholesale Sold','Retail Revenue','Wholesale Revenue','Retail Price','Wholesale Price','Avg Cost','Closing Value','Reorder','Status'],[[esc(r['name']),esc(r['category']),r['opening'],r['purchased'],r['sold'],r['adjustments'],r['closing'],r['retail_units'],r['wholesale_units'],money(r['retail_revenue']),money(r['wholesale_revenue']),money(r['retail_price']),money(r['wholesale_price']),money(r['avg_cost']),money(r['closing']*r['avg_cost']),r['reorder_level'],esc(r['status'])] for r in rows])
    branch_opts='<option value="">ALL BRANCHES / COMPANY TOTAL</option>' if role in ('ADMIN','MANAGEMENT') else ''
    branch_opts+=''.join(f'<option value="{b["id"]}" {"selected" if branch_id==int(b["id"]) else ""}>{esc(b["name"])}</option>' for b in branches)
    selector=f'''<form method="get" action="/stock-report" class="inline-actions"><label for="branch_id">Select Branch</label><select id="branch_id" name="branch_id" onchange="this.form.submit()">{branch_opts}</select><input type="hidden" name="from_date" value="{esc(fd)}"><input type="hidden" name="from_time" value="{esc(ft)}"><input type="hidden" name="to_date" value="{esc(td)}"><input type="hidden" name="to_time" value="{esc(tt)}"></form>'''
    selected_name=next((b['name'] for b in branches if branch_id and int(b['id'])==branch_id),'All Branches / Company Total')
    exp=f'/report/export?kind=stock&format={{fmt}}&start={esc(start)}&end={esc(end)}&branch_id={branch_id or ""}'
    content=f'''<section class="card"><div class="section-head"><h2>Branch Stock Movement Report</h2><span class="pill">{esc(preset)}</span></div><div class="notice"><b>Viewing: {esc(selected_name)}</b> · Each branch has its own opening, purchases, sales, adjustments and closing stock. A sale in one branch does not change another branch's quantity.</div>{selector}{range_toolbar('/stock-report',fd,ft,td,tt,branch_id)}{channel_summary}<div class="actions" style="margin-top:10px"><button type="button" class="btn" onclick="window.print()">PRINT</button><a class="btn" href="{exp.format(fmt='pdf')}">DOWNLOAD PDF</a><a class="btn" href="{exp.format(fmt='xlsx')}">DOWNLOAD EXCEL</a><a class="btn danger" href="/reorder-report?from_date={quote(fd)}&from_time={quote(ft)}&to_date={quote(td)}&to_time={quote(tt)}">REORDER REPORT</a></div></section><section class="card report-print" style="margin-top:14px">{table}</section>'''
    return layout(u,'Branch Stock Report',content,'/stock-report','Branch-by-branch opening, purchase, sales and closing stock')

def reorder_report_page(u, query=None):
    require_perm_user(u,'VIEW_STOCK')
    branch_id=branch_scope_for(u,query)
    rows=[r for r in core.stock_report('1900-01-01 00:00:00','2999-12-31 23:59:59',branch_id) if r['status'] in ('LOW STOCK','OUT OF STOCK')]
    selector=branch_scope_selector(u,query,'/reorder-report')
    table=html_table(['Product Name','Category','Current Stock','Reorder Level','Status','Avg Cost','Stock Value'],[[esc(r['name']),esc(r['category']),r['stock'],r['reorder_level'],esc(r['status']),money(r['avg_cost']),money(r['stock_value'])] for r in rows],'No products currently requiring reorder.')
    content=f'''{selector}<section class="card report-print"><div class="section-head"><h2>{esc(setting_label('nav_reorder_report','Reorder Report'))}</h2><span class="pill">CURRENT STOCK STATUS</span></div><div class="muted" style="margin-bottom:12px">Generated by {esc(u['full_name'])} @{esc(u['username'])} at {now()}</div><div class="actions" style="margin-bottom:12px"><button type="button" class="btn" onclick="window.print()">PRINT</button><a class="btn" href="/report/export?kind=reorder&format=pdf&start=1900-01-01%2000:00:00&end=2999-12-31%2023:59:59&branch_id={branch_id or ''}">DOWNLOAD PDF</a><a class="btn" href="/report/export?kind=reorder&format=xlsx&start=1900-01-01%2000:00:00&end=2999-12-31%2023:59:59&branch_id={branch_id or ''}">DOWNLOAD EXCEL</a></div>{table}</section>'''
    return layout(u,'Reorder Report',content,'/reorder-report','Current low-stock purchasing action list')


def stock_transfers_page(u, query=None, message=''):
    require_perm_user(u,'VIEW_STOCK')
    role=role_name(u)
    if role not in ('ADMIN','MANAGEMENT'):
        raise PermissionError('Stock transfers and cross-branch stock counts are restricted to Management and Administrator. Sales Persons can work only with stock in their assigned branch.')
    branch_id=branch_scope_for(u,query)
    c=core.db.c()
    branches=[dict(r) for r in c.execute('SELECT id,name FROM branches WHERE active=1 ORDER BY name').fetchall()]
    products=[dict(r) for r in c.execute('SELECT id,name,sku FROM products WHERE active=1 ORDER BY name').fetchall()]
    if branch_id is not None:
        transfers=[dict(r) for r in c.execute('''SELECT t.*,sb.name source_name,db.name destination_name,p.name product_name,u.full_name requested_name,COALESCE(a.full_name,'') approved_name FROM stock_transfers t JOIN branches sb ON sb.id=t.source_branch_id JOIN branches db ON db.id=t.destination_branch_id JOIN products p ON p.id=t.product_id JOIN users u ON u.id=t.requested_by LEFT JOIN users a ON a.id=t.approved_by WHERE t.source_branch_id=? OR t.destination_branch_id=? ORDER BY t.id DESC LIMIT 100''',(branch_id,branch_id)).fetchall()]
    else:
        transfers=[dict(r) for r in c.execute('''SELECT t.*,sb.name source_name,db.name destination_name,p.name product_name,u.full_name requested_name,COALESCE(a.full_name,'') approved_name FROM stock_transfers t JOIN branches sb ON sb.id=t.source_branch_id JOIN branches db ON db.id=t.destination_branch_id JOIN products p ON p.id=t.product_id JOIN users u ON u.id=t.requested_by LEFT JOIN users a ON a.id=t.approved_by ORDER BY t.id DESC LIMIT 200''').fetchall()]
    c.close()
    notice=f'<div class="flash error">{esc(message)}</div>' if message else ''
    selector=branch_scope_selector(u,query,'/stock-transfers')
    if role in ('ADMIN','MANAGEMENT'):
        source_opts='<option value="">Select source branch</option>'+''.join(f'<option value="{b["id"]}">{esc(b["name"])}</option>' for b in branches)
        count_branch_opts='<option value="">Select branch</option>'+''.join(f'<option value="{b["id"]}" {"selected" if branch_id==int(b["id"]) else ""}>{esc(b["name"])}</option>' for b in branches)
    else:
        source_opts=f'<option value="{int(u.get("branch_id") or 0)}">{esc(next((b["name"] for b in branches if int(b["id"])==int(u.get("branch_id") or 0)),"Assigned Branch"))}</option>'
        count_branch_opts=f'<option value="{int(u.get("branch_id") or 0)}" selected>{esc(next((b["name"] for b in branches if int(b["id"])==int(u.get("branch_id") or 0)),"Assigned Branch"))}</option>'
    destination_opts='<option value="">Select destination branch</option>'+''.join(f'<option value="{b["id"]}">{esc(b["name"])}</option>' for b in branches)
    product_opts='<option value="">Select product</option>'+''.join(f'<option value="{p["id"]}">{esc(p["name"])} · {esc(p["sku"])}</option>' for p in products)
    rows=[]
    for t in transfers:
        actions=''
        if role in ('ADMIN','MANAGEMENT') and t['status']=='PENDING':
            actions=f'''<form method="post" action="/stock-transfers" class="inline-actions">{csrf_input(CURRENT_HANDLER)}<input type="hidden" name="action" value="approve"><input type="hidden" name="transfer_id" value="{t["id"]}"><button class="btn gold">APPROVE & MOVE STOCK</button></form><form method="post" action="/stock-transfers" class="inline-actions">{csrf_input(CURRENT_HANDLER)}<input type="hidden" name="action" value="reject"><input type="hidden" name="transfer_id" value="{t["id"]}"><input name="rejection_reason" required minlength="5" placeholder="Reason to reject"><button class="btn danger">REJECT</button></form>'''
        rows.append([esc(t['created_at']),esc(t['transfer_no']),esc(t['source_name']),esc(t['destination_name']),esc(t['product_name']),f'{float(t["qty"]):g}',money(t['unit_cost']),esc(t['requested_name']),esc(t['status']),esc(t['approved_name'] or '—'),actions])
    transfer_table=html_table(['Requested','Transfer No.','From Branch','To Branch','Product','Qty','Unit Cost','Requested By','Status','Approved By','Action'],rows,'No stock transfers recorded.')
    count_rows=[]
    if branch_id is not None:
        for r in core.stock_report('1900-01-01 00:00:00','2999-12-31 23:59:59',branch_id):
            count_rows.append([esc(r['name']),f'{float(r["stock"]):g}',money(r['avg_cost']),money(r['stock_value']),money(float(r["stock"])*float(r["retail_price"] or 0))])
    stock_table=html_table(['Product','System Qty','Avg Cost','Stock Value at Cost','Retail Value'],count_rows,'Select a branch to view its stock count.')
    content=f'''{selector}{notice}<section class="card"><div class="section-head"><h2>Request Stock Transfer</h2><span class="pill">MANAGEMENT APPROVAL REQUIRED</span></div><p class="muted">A transfer does not change stock until Administrator or Management approves it. Approval deducts stock from the source and adds the same quantity to the destination, with both movements audited.</p><form method="post" action="/stock-transfers">{csrf_input(CURRENT_HANDLER)}<input type="hidden" name="action" value="request"><div class="form-grid">{f'<div><label>Source Branch *</label><select name="source_branch_id" required>{source_opts}</select></div>' if role in ('ADMIN','MANAGEMENT') else f'<input type="hidden" name="source_branch_id" value="{int(u.get("branch_id") or 0)}">'}<div><label>Destination Branch *</label><select name="destination_branch_id" required>{destination_opts}</select></div><div><label>Product *</label><select name="product_id" required>{product_opts}</select></div><div><label>Quantity *</label><input type="number" name="qty" min="0.001" step="0.001" required></div><div><label>Reason / Reference *</label><input name="reason" required minlength="5" placeholder="Stock shortage / replenishment"></div></div><div class="actions" style="margin-top:12px"><button class="btn gold">SUBMIT TRANSFER REQUEST</button></div></form></section><section class="card" style="margin-top:14px"><div class="section-head"><h2>Branch Stock Count & Variance</h2><span class="pill">AUDITED PHYSICAL COUNT</span></div><p class="muted">Enter the physically counted quantity. The system records the difference as a stock-count adjustment with user, timestamp, reason, and before/after balance.</p><form method="post" action="/stock-transfers">{csrf_input(CURRENT_HANDLER)}<input type="hidden" name="action" value="count"><div class="form-grid"><div><label>Branch *</label><select name="branch_id" required>{count_branch_opts}</select></div><div><label>Product *</label><select name="product_id" required>{product_opts}</select></div><div><label>Physical Quantity *</label><input type="number" name="physical_qty" min="0" step="0.001" required></div><div><label>Reason / Count Sheet Reference *</label><input name="count_reason" minlength="5" required placeholder="Physical count and date"></div></div><div class="actions" style="margin-top:12px"><button class="btn gold">SAVE STOCK COUNT</button></div></form>{stock_table}</section><section class="card" style="margin-top:14px"><div class="section-head"><h2>Transfer Register</h2><span class="pill">AUDIT TRAIL</span></div>{transfer_table}</section>'''
    return layout(u,'Stock Transfers & Stock Count',content,'/stock-transfers','Branch stock transfers require management approval; stock counts are audited')


def live_page(u, query=None):
    require_perm_user(u,'LIVE_MONITORING'); branch_id=branch_scope_for(u,query); d=core.live_monitoring(branch_id); d['active_users']=active_user_count(); mp_live=mpesa_live_messages(12,branch_id); selector=branch_scope_selector(u,query,'/live')
    content=f'''{selector}<section class="card"><div class="section-head"><h2>Live Monitoring</h2><span class="pill">AUTO REFRESH</span></div><div class="grid four"><div class="card metric"><div class="muted">ACTIVE USERS</div><div class="v">{d['active_users']}</div></div><div class="card metric"><div class="muted">TODAY SALES</div><div class="v">{money(d['today_sales'])}</div></div><div class="card metric"><div class="muted">PURCHASES</div><div class="v">{money(d['today_purchases'])}</div></div><div class="card metric"><div class="muted">EXPENSES</div><div class="v">{money(d['today_expenses'])}</div></div><div class="card metric"><div class="muted">M-PESA</div><div class="v">{money(d['mpesa'])}</div></div></div></section><section class="card mpesa-live-card" style="margin-top:14px"><div class="mpesa-live-head"><div><h3 style="margin:0">Live M-Pesa Messages</h3><div class="muted">Incoming Till 1546252 payments and STK updates · refreshed automatically</div></div><span class="pill">LIVE</span></div><div id="mpesa-live-messages">{mpesa_live_messages_html(mp_live)}</div></section><div class="grid two" style="margin-top:14px"><section class="card"><div class="section-head"><h3>Recent Sales</h3><a class="btn" href="/live">REFRESH</a></div>{html_table(['Time','Receipt','Customer','Total','By'],[[esc(r['sold_at']),esc(r['receipt_no']),esc(r['customer']),money(r['total']),esc(r['user_name'])] for r in d['recent_sales']])}</section><section class="card"><h3>Recent Purchases</h3>{html_table(['Time','Purchase','Supplier','Total','By'],[[esc(r['purchased_at']),esc(r['purchase_no']),esc(r['supplier']),money(r['total']),esc(r['user_name'])] for r in d['recent_purchases']])}</section></div>'''; return layout(u,'Live Monitoring',content,'/live')


def products_page(u, message='', query=None):
    role=role_name(u)
    if role not in ('ADMIN','MANAGEMENT','SALES') or not allowed(u,'MANAGE_PRODUCTS'):
        raise PermissionError('Product Registration is disabled for your account. Contact Administrator.')
    c=core.db.c(); branches=[dict(x) for x in c.execute('SELECT id,name FROM branches WHERE active=1 ORDER BY name').fetchall()]
    if role in ('ADMIN','MANAGEMENT'):
        raw=_qsval(query or {},'branch_id',''); branch_id=int(raw) if str(raw).isdigit() else 0
        if branch_id and not any(int(b['id'])==branch_id for b in branches): c.close(); raise PermissionError('Selected branch is unavailable.')
    else: branch_id=int(u.get('branch_id') or 0)
    rows=[dict(r) for r in c.execute('''SELECT p.*,COALESCE(bs.qty,0) branch_qty,COALESCE(bs.avg_cost,p.avg_cost) branch_cost,COALESCE(bs.sell_price,p.sell_price) branch_sell_price,COALESCE(bs.wholesale_price,p.wholesale_price) branch_wholesale_price,COALESCE(bs.reorder_level,p.reorder_level) branch_reorder FROM products p LEFT JOIN branch_stock bs ON bs.product_id=p.id AND bs.branch_id=? ORDER BY p.active DESC,p.name''',(branch_id,)).fetchall()]; c.close()
    notice=f'<div class="flash error">{esc(message)}</div>' if message else ''
    if role=='ADMIN':
        price_rows=[]; action_rows=[]
        for r in rows:
            pid=r['id']; wholesale_value=r.get('branch_wholesale_price') if r.get('branch_wholesale_price') is not None else r['branch_sell_price']
            price_rows.append([
                esc(r['name']), esc(r['sku']),
                f'<input aria-label="Retail price for {esc(r["name"])}" name="bulk_sell_{pid}" type="number" min="0.01" step="0.01" value="{float(r["branch_sell_price"] or 0):.2f}" required style="width:120px">',
                f'<input aria-label="Wholesale price for {esc(r["name"])}" name="bulk_wholesale_{pid}" type="number" min="0.01" step="0.01" value="{float(wholesale_value or 0):.2f}" required style="width:120px">',
                f'<input aria-label="COGS unit cost for {esc(r["name"])}" name="bulk_cost_{pid}" type="number" min="0" step="0.01" value="{float(r["branch_cost"] or 0):.2f}" required style="width:120px">',
                r['branch_qty'], money(float(r['branch_qty'] or 0)*float(r['branch_cost'] or 0)),
                f'<input aria-label="Reorder level for {esc(r["name"])}" name="bulk_reorder_{pid}" type="number" min="0" step="1" value="{float(r["branch_reorder"] or 0):g}" required style="width:90px">',
                'ACTIVE' if r['active'] else 'ARCHIVED'
            ])
            stock=f'''<form method="post" action="/products" class="inline-actions">{csrf_input(CURRENT_HANDLER)}<input type="hidden" name="action" value="stock"><input type="hidden" name="product_id" value="{pid}"><input name="new_stock" aria-label="New stock quantity" type="number" step="1" min="0" value="{r['branch_qty']}" required style="width:90px"><input type="hidden" name="branch_id" value="{branch_id}"><input name="reason" placeholder="Stock correction reason" required minlength="5" style="width:180px"><button class="btn">ADJUST STOCK</button></form>'''
            action_btn=f'''<form method="post" action="/products" class="inline-actions" onsubmit="return confirm('Archive this product? Historical records will be preserved.');">{csrf_input(CURRENT_HANDLER)}<input type="hidden" name="action" value="delete"><input type="hidden" name="product_id" value="{pid}"><input name="reason" placeholder="Reason required" required minlength="5" style="width:180px"><button class="btn danger">ARCHIVE</button></form>''' if r['active'] else f'''<form method="post" action="/products" class="inline-actions">{csrf_input(CURRENT_HANDLER)}<input type="hidden" name="action" value="restore"><input type="hidden" name="product_id" value="{pid}"><input name="reason" placeholder="Reason required" required minlength="5" style="width:180px"><button class="btn">RESTORE</button></form>'''
            action_rows.append([esc(r['name']),stock,action_btn])
        bulk_table=html_table(['Product','SKU','Retail Price','Wholesale Price','COGS / Unit Cost','Branch Stock','Stock Value at Cost','Reorder Level','Status'],price_rows,'No products registered.')
        action_table=html_table(['Product','Stock Quantity Adjustment','Archive / Restore'],action_rows,'No products registered.')
        extra=f'''<section class="card" style="margin-top:14px"><div class="notice"><b>QUICK STOCK EDIT:</b> Edit any number of retail prices, wholesale prices, COGS/unit costs and reorder levels, then click SAVE ALL STOCK CHANGES once. No confirmation or reason is required; the system records an automatic audit note. Stock quantities are preserved and adjusted separately.</div><form method="post" action="/products" id="bulk-stock-edit">{csrf_input(CURRENT_HANDLER)}<input type="hidden" name="action" value="bulk_save"><input type="hidden" name="branch_id" value="{branch_id}">{bulk_table}<div class="actions" style="margin-top:14px"><button class="btn gold" type="submit">SAVE ALL STOCK CHANGES</button><button class="btn" type="reset">CANCEL UNSAVED CHANGES</button></div></form><h3 style="margin-top:24px">Separate Stock Quantity / Product Status Actions</h3><p class="muted">Use these only when you need to correct physical stock or archive/restore a product. They require their own reason to preserve the stock audit trail.</p>{action_table}</section>'''
        title='Product Registration & Branch Stock Editor'; subtitle='Edit costs and prices for the selected branch'
    else:
        table=html_table(['Product Name','SKU','Category','Unit','Retail / Wholesale Price','Stock','Reorder','Status'],[[esc(r['name']),esc(r['sku']),esc(r['category']),esc(r['unit']),money(r['branch_sell_price'])+' / '+money(r.get('branch_wholesale_price') or r['branch_sell_price']),r['branch_qty'],r['branch_reorder'],'ACTIVE' if r['active'] else 'ARCHIVED'] for r in rows])
        extra=f'<section class="card" style="margin-top:14px"><div class="notice">Management and Sales Person can register products with separate retail and wholesale prices. Product catalogue entries are shared, but stock, costs and prices remain branch-specific; existing branch prices and costs can only be changed by the Administrator.</div>{table}</section>'
        title='Product Registration'; subtitle='Management and Sales Person product registration access'
    product_opening_opts=''.join(f'<option value="{r["id"]}">{esc(r["name"])} · {esc(r["sku"])}</option>' for r in rows if r['active'])
    branch_opening_opts=('<option value="">Select branch</option>'+''.join(f'<option value="{b["id"]}" {"selected" if int(b["id"])==branch_id else ""}>{esc(b["name"])}</option>' for b in branches)) if role in ('ADMIN','MANAGEMENT') else f'<input type="hidden" name="branch_id" value="{branch_id}">'
    branch_opening_field=f'<div><label>Branch</label><select name="branch_id" required>{branch_opening_opts}</select></div>' if role in ('ADMIN','MANAGEMENT') else branch_opening_opts
    product_branch_field=f'<div><label>Opening Stock Branch *</label><select name="branch_id" required>{branch_opening_opts}</select></div>' if role in ('ADMIN','MANAGEMENT') else f'<input type="hidden" name="branch_id" value="{branch_id}">'
    product_branch_selector=branch_scope_selector(u,{'branch_id':str(branch_id) if branch_id else ''},'/products') if role in ('ADMIN','MANAGEMENT') else ''
    opening_form=f'''<section class="card" style="margin-top:14px"><h3>Opening Stock — Existing Product in a Branch</h3><p class="muted">Enter the physically counted stock for a product in this branch once. The system will keep each branch balance separate.</p><form method="post" action="/products">{csrf_input(CURRENT_HANDLER)}<input type="hidden" name="action" value="opening"><div class="form-grid"><div><label>Product</label><select name="product_id" required>{product_opening_opts}</select></div>{branch_opening_field}<div><label>Physical Opening Quantity</label><input type="number" name="opening_qty" min="0.001" step="0.001" required></div><div><label>Unit Cost</label><input type="number" name="opening_cost" min="0.01" step="0.01" required></div><div><label>Reason / Stock Take Reference</label><input name="opening_reason" minlength="5" required placeholder="Physical opening stock count"></div></div><button class="btn gold" style="margin-top:12px">SAVE BRANCH OPENING STOCK</button></form></section>'''
    content=f'''{product_branch_selector}{notice}<section class="card"><div class="section-head"><h2>{esc(title)}</h2><span class="pill">{esc(u['full_name'])} · {esc(role_display(role))}</span></div><form method="post" action="/products"><div class="form-grid">{csrf_input(CURRENT_HANDLER)}<input type="hidden" name="action" value="create">{product_branch_field}<div><label>SKU *</label><div class="scan-inline"><div><input name="sku" id="product_sku" required></div><button type="button" class="btn scan-btn" onclick="openScannerFill('product_sku')">SCAN</button></div></div><div><label>Product Name *</label><input name="name" required></div><div><label>Category *</label><input name="category" placeholder="Wine, Spirit, Beer..." required></div><div><label>Unit</label><input name="unit" value="{esc(core.get_setting('default_unit','bottle'))}" required></div><div><label>Retail Selling Price</label><input type="number" name="sell_price" min="0.01" step="0.01" required></div><div><label>Wholesale Selling Price</label><input type="number" name="wholesale_price" min="0.01" step="0.01" required><small class="muted">Catalogue is shared; stock quantity is recorded only in the selected branch.</small></div><div><label>Opening Stock</label><input type="number" name="stock_qty" min="0" step="1" required></div><div><label>COGS / Unit Cost (KSh) — Cost of Goods Sold *</label><input type="number" name="cogs_unit_cost" min="0" step="0.01" value="0" required><small class="muted">Enter the cost you pay per unit. Required to calculate accurate profit and COGS.</small></div><div><label>Reorder Level</label><input type="number" name="reorder_level" min="0" step="1" value="{esc(core.get_setting('default_reorder_level','5'))}" required></div></div><div class="actions" style="margin-top:14px"><button class="btn gold" type="submit">SAVE PRODUCT</button></div></form></section>{opening_form}{extra}'''
    return layout(u,title,content,'/products',subtitle)

def users_page(u, message=''):

    if role_name(u)!='ADMIN': raise PermissionError('Administrator permission required.')
    c=core.db.c(); branches=[dict(r) for r in c.execute('SELECT id,name FROM branches WHERE active=1 ORDER BY name').fetchall()]; rows=[dict(r) for r in c.execute('SELECT u.id,u.username,u.full_name,u.access_role,u.active,u.created_at,u.updated_at,u.branch_id,COALESCE(b.name,\'Unassigned\') branch_name FROM users u LEFT JOIN branches b ON b.id=u.branch_id ORDER BY u.username').fetchall()]; c.close(); notice=f'<div class="flash error">{esc(message)}</div>' if message else ''
    opts=''.join(f'<option value="{b["id"]}">{esc(b["name"])}</option>' for b in branches)
    table=[]
    for r in rows:
        status='ACTIVE' if r['active'] else 'RESTRICTED / DISABLED'
        edit_opts=''.join(f'<option value="{b["id"]}" {"selected" if r["branch_id"]==b["id"] else ""}>{esc(b["name"])}</option>' for b in branches)
        edit=f'''<form method="post" action="/users" class="inline-actions">{csrf_input(CURRENT_HANDLER)}<input type="hidden" name="action" value="edit"><input type="hidden" name="user_id" value="{r['id']}"><input name="full_name" value="{esc(r['full_name'])}" required style="width:130px"><select name="role"><option value="SALES" {'selected' if r['access_role']=='SALES' else ''}>{esc(setting_label('role_sales_label','SALES PERSON'))}</option><option value="MANAGEMENT" {'selected' if r['access_role']=='MANAGEMENT' else ''}>{esc(setting_label('role_management_label','MANAGEMENT'))}</option><option value="ADMIN" {'selected' if r['access_role']=='ADMIN' else ''}>{esc(setting_label('role_admin_label','ADMINISTRATOR / DEVELOPER'))}</option></select><select name="branch_id"><option value="">No branch (not for Sales)</option>{edit_opts}</select><select name="active"><option value="1" {'selected' if r['active'] else ''}>ACTIVE</option><option value="0" {'selected' if not r['active'] else ''}>RESTRICTED</option></select><button class="btn">SAVE CHANGES</button></form>'''
        reset=f'''<form method="post" action="/users" class="inline-actions">{csrf_input(CURRENT_HANDLER)}<input type="hidden" name="action" value="reset_password"><input type="hidden" name="user_id" value="{r['id']}"><input type="password" name="new_password" minlength="8" placeholder="New password" required style="width:135px"><button class="btn gold">RESET PASSWORD</button></form>'''
        toggle=f'''<form method="post" action="/users" class="inline-actions">{csrf_input(CURRENT_HANDLER)}<input type="hidden" name="action" value="toggle"><input type="hidden" name="user_id" value="{r['id']}"><input type="hidden" name="active" value="{0 if r['active'] else 1}"><button class="btn {'danger' if r['active'] else ''}">{'RESTRICT ACCESS' if r['active'] else 'ENABLE ACCESS'}</button></form>'''
        delete='' if r['id']==u['id'] else f'''<form method="post" action="/users" class="inline-actions" onsubmit="return confirm('Delete this user? Historical records are retained; users with history will be disabled instead of physically removed.');">{csrf_input(CURRENT_HANDLER)}<input type="hidden" name="action" value="delete"><input type="hidden" name="user_id" value="{r['id']}"><button class="btn danger">DELETE USER</button></form>'''
        table.append([esc(r['username']),esc(r['full_name']),esc(role_display(r['access_role'])),esc(r['branch_name']),status,esc(r['created_at']),edit+'<div style="margin-top:6px">'+reset+'</div><div style="margin-top:6px">'+toggle+delete+'</div>'])
    content=f'''{notice}<section class="card"><div class="section-head"><h2>User Management & Access Control</h2><span class="pill">ADMINISTRATOR / DEVELOPER ONLY</span></div><form method="post" action="/users">{csrf_input(CURRENT_HANDLER)}<input type="hidden" name="action" value="create"><div class="form-grid"><div><label>Username</label><input name="username" required></div><div><label>Full Name</label><input name="full_name" required></div><div><label>Password</label><input type="password" name="password" minlength="8" required></div><div><label>Role</label><select name="role" id="new-user-role" required><option value="SALES">{esc(setting_label('role_sales_label','SALES PERSON'))}</option><option value="MANAGEMENT">{esc(setting_label('role_management_label','MANAGEMENT'))}</option><option value="ADMIN">{esc(setting_label('role_admin_label','ADMINISTRATOR / DEVELOPER'))}</option></select></div><div><label>Assigned Branch <span class="muted">(required for Sales Person)</span></label><select name="branch_id" id="new-user-branch"><option value="">Select branch</option>{opts}</select></div></div><div class="actions" style="margin-top:14px"><button class="btn gold">ADD USER</button></div></form><script>(function(){{var r=document.getElementById('new-user-role'),b=document.getElementById('new-user-branch');function sync(){{b.required=r.value==='SALES';}}r.addEventListener('change',sync);sync();}})();</script></section><section class="card" style="margin-top:14px"><div class="notice">Every Sales Person must have an active branch. Sales are recorded against the assigned branch at the time of sale, preserving historical branch performance when staff later move branches.</div>{html_table(['Username','Full Name','Role','Branch','Status','Created','Administrator Controls'],table,'No users.')}</section>'''
    return layout(u,'User Management',content,'/users','Roles, branch assignments and access control')


def branches_page(u, message=''):
    if role_name(u)!='ADMIN': raise PermissionError('Administrator permission required.')
    c=core.db.c(); rows=[dict(r) for r in c.execute('SELECT b.*, (SELECT COUNT(*) FROM users x WHERE x.branch_id=b.id AND x.active=1) staff_count, (SELECT COUNT(*) FROM sales s WHERE s.branch_id=b.id) sales_count FROM branches b ORDER BY b.active DESC,b.name').fetchall()]; c.close()
    notice=f'<div class="flash error">{esc(message)}</div>' if message else ''
    table=[]
    for r in rows:
        status='ACTIVE' if r['active'] else 'INACTIVE'
        table.append([esc(r['name']),esc(r['code'] or ''),esc(r['location'] or ''),esc(r['phone'] or ''),r['staff_count'],r['sales_count'],status,f'''<form method="post" action="/branches" class="inline-actions">{csrf_input(CURRENT_HANDLER)}<input type="hidden" name="branch_id" value="{r['id']}"><input type="hidden" name="action" value="toggle"><input type="hidden" name="active" value="{0 if r['active'] else 1}"><button class="btn {'danger' if r['active'] else ''}">{'DEACTIVATE' if r['active'] else 'ACTIVATE'}</button></form>'''])
    content=f'''{notice}<section class="card"><div class="section-head"><h2>Branch Registration</h2><span class="pill">ADMINISTRATOR ONLY</span></div><p class="muted">Register every outlet here, then assign every Sales Person to one active branch under User Management.</p><form method="post" action="/branches">{csrf_input(CURRENT_HANDLER)}<input type="hidden" name="action" value="create"><div class="form-grid"><div><label>Branch Name *</label><input name="name" required></div><div><label>Branch Code</label><input name="code" placeholder="e.g. NKR-01"></div><div><label>Location / Address</label><input name="location"></div><div><label>Branch Phone</label><input name="phone"></div></div><div class="actions" style="margin-top:12px"><button class="btn gold">REGISTER BRANCH</button></div></form></section><section class="card" style="margin-top:14px">{html_table(['Branch','Code','Location','Phone','Active Staff','Sales Transactions','Status','Action'],table,'No branches registered.')}</section>'''
    return layout(u,'Branch Registration',content,'/branches','Register and manage company branches')


def branch_performance_page(u, query=None):
    if role_name(u) not in ('ADMIN','MANAGEMENT'): raise PermissionError('Branch Performance is available to Administrator and Management only.')
    require_perm_user(u,'VIEW_REPORTS')
    fd,ft,td,tt,preset=period_from_query(query or {}); start=f'{fd} {ft}:00'; end=f'{td} {tt}:59'
    c=core.db.c()
    branch_rows=[dict(r) for r in c.execute('''SELECT b.id,b.name,COUNT(s.id) transactions,COALESCE(SUM(s.total),0) revenue,COALESCE(SUM((SELECT SUM(sl.qty) FROM sale_lines sl WHERE sl.sale_id=s.id)),0) units FROM branches b LEFT JOIN sales s ON s.branch_id=b.id AND s.sold_at BETWEEN ? AND ? GROUP BY b.id ORDER BY revenue DESC,b.name''',(start,end)).fetchall()]
    employee_rows=[dict(r) for r in c.execute('''SELECT u.id,u.full_name,u.username,COALESCE(b.name,'Unassigned') branch_name,COUNT(s.id) transactions,COALESCE(SUM(s.total),0) revenue,COALESCE(SUM((SELECT SUM(sl.qty) FROM sale_lines sl WHERE sl.sale_id=s.id)),0) units FROM users u LEFT JOIN branches b ON b.id=COALESCE((SELECT s2.branch_id FROM sales s2 WHERE s2.user_id=u.id AND s2.branch_id IS NOT NULL ORDER BY s2.sold_at DESC LIMIT 1),u.branch_id) LEFT JOIN sales s ON s.user_id=u.id AND s.sold_at BETWEEN ? AND ? WHERE u.active=1 AND u.access_role='SALES' GROUP BY u.id ORDER BY revenue DESC,u.full_name''',(start,end)).fetchall()]
    c.close()
    for rows in (branch_rows,employee_rows):
        total=sum(float(r['revenue'] or 0) for r in rows)
        for i,r in enumerate(rows,1): r['rank']=i; r['share']=100*float(r['revenue'] or 0)/total if total else 0
    def rank_table(rows, who):
        total=sum(float(x['revenue'] or 0) for x in rows)
        return html_table(['Award','Rank',who,'Transactions','Units Sold','Sales Revenue','Share of Revenue'],[[award_for_rank(r['rank'],r['revenue'],total),f"#{r['rank']}",(f'<a href="/stock-report?branch_id={int(r["id"])}">{esc(r["name"])}</a>' if who=='Branch' else esc(r['full_name'])+' · '+esc(r['branch_name'])),r['transactions'],f"{float(r['units'] or 0):g}",money(r['revenue']),f"{r['share']:.1f}%"] for r in rows], 'No sales recorded for this period.')
    best_b=branch_rows[0] if branch_rows and float(branch_rows[0]['revenue'] or 0)>0 else None; low_b=branch_rows[-1] if branch_rows else None
    best_e=employee_rows[0] if employee_rows and float(employee_rows[0]['revenue'] or 0)>0 else None; low_e=employee_rows[-1] if employee_rows else None
    summary=f'''<div class="grid two"><section class="card"><h3>🏆 Best Performing Branch</h3><div class="v">{esc(best_b['name']) if best_b else 'No sales yet'}</div><div>{money(best_b['revenue']) if best_b else money(0)}</div></section><section class="card"><h3>⚠ Lowest Performing Branch</h3><div class="v">{esc(low_b['name']) if low_b else 'No branches'}</div><div>{money(low_b['revenue']) if low_b else money(0)}</div></section><section class="card"><h3>🏆 Best Performing Sales Person</h3><div class="v">{esc(best_e['full_name']) if best_e else 'No sales yet'}</div><div>{money(best_e['revenue']) if best_e else money(0)}{(' · '+esc(best_e['branch_name'])) if best_e else ''}</div></section><section class="card"><h3>⚠ Lowest Performing Sales Person</h3><div class="v">{esc(low_e['full_name']) if low_e else 'No sales yet'}</div><div>{money(low_e['revenue']) if low_e else money(0)}{(' · '+esc(low_e['branch_name'])) if low_e else ''}</div></section></div>'''
    content=f'''<section class="card"><div class="section-head"><h2>Branch & Employee Performance</h2><span class="pill">RANKED BY SALES REVENUE</span></div>{range_toolbar('/branch-performance',fd,ft,td,tt,None)}<div class="muted" style="margin-top:8px">Period: {esc(start)} to {esc(end)}. 🥇 Gold = first, 🥈 Silver = second, 🥉 Bronze = third, Participant = fourth onward. Awards are recalculated from recorded sales for the selected period, so you can revisit any previous month by selecting its dates. Shares are calculated from the total revenue of the listed branches or employees. Sales are attributed to the branch recorded when each sale was made.</div></section><div style="margin-top:14px">{summary}</div><section class="card" style="margin-top:14px"><h2>Branch Performance Ranking</h2>{rank_table(branch_rows,'Branch')}</section><section class="card" style="margin-top:14px"><h2>Sales Person Performance Ranking</h2>{rank_table(employee_rows,'Employee')}</section><section class="card" style="margin-top:14px"><button class="btn" onclick="window.print()" type="button">PRINT PERFORMANCE REPORT</button> <a class="btn" href="/report/export?kind=branch-performance&format=pdf&start={quote(start)}&end={quote(end)}">DOWNLOAD PDF</a> <a class="btn" href="/report/export?kind=branch-performance&format=xlsx&start={quote(start)}&end={quote(end)}">DOWNLOAD EXCEL</a></section>'''
    return layout(u,'Branch Performance',content,'/branch-performance','Best and lowest performing branches and sales staff')


def settings_page(u, message=''):
    if role_name(u)!='ADMIN': raise PermissionError('Administrator permission required.')
    s=core.get_settings(); payload=core.permissions_payload(u); notice=f'<div class="flash error">{esc(message)}</div>' if message else ''
    subscription_due_day=int(float(s.get('subscription_due_day','5')))
    subscription_due_day_label=f"{subscription_due_day}{'st' if subscription_due_day==1 else 'nd' if subscription_due_day==2 else 'rd' if subscription_due_day==3 else 'th'}"
    role_rows=[]
    for d in payload['definitions']:
        k=d['key']; m='checked' if payload['roles']['MANAGEMENT'].get(k) else ''; sa='checked' if payload['roles']['SALES'].get(k) else ''
        management_locked=k in core.ADMIN_ONLY_PERMISSION_KEYS
        sales_locked=k in core.ADMIN_ONLY_PERMISSION_KEYS
        management_lock_attr='disabled title="Not available to Management"' if management_locked else ''
        sales_lock_attr='disabled title="Administrator only"' if sales_locked else ''
        role_rows.append([esc(d['label']),esc(d['description']),f'<input type="checkbox" name="perm_m_{esc(k)}" {m} {management_lock_attr}>',f'<input type="checkbox" name="perm_s_{esc(k)}" {sa} {sales_lock_attr}>'])
    main_fields=[('business_name','Business Name','text',''),('business_subtitle','Business Subtitle','text',''),('business_phone','Business Phone','text',''),('business_address','Business Address','text',''),('business_website','Business Website','text',''),('business_tax_pin','KRA PIN / Tax PIN','text',''),('business_branch_name','Branch / Seat Name','text',''),('receipt_footer','Receipt Footer Message','text',''),('report_footer','Report Footer Message','text',''),('currency','Currency','text',''),('receipt_prefix','Receipt Prefix','text',''),('idle_timeout_minutes','Auto Logout (Minutes)','number','min="1" max="120"'),('live_refresh_seconds','Live Refresh (Seconds)','number','min="2" max="120"'),('default_reorder_level','Default Reorder Level','number','min="0"'),('default_unit','Default Product Unit','text',''),('expense_categories','Expense Categories (comma-separated)','text',''),('default_payment_transactions','Default Payment Transactions','number','min="1" max="5"'),('max_payment_transactions','Maximum Payment Transactions','number','min="1" max="5"'),('report_default_days','Default Report Days','number','min="1" max="365"'),('performance_green_threshold_pct','Business Performance Green Threshold (%)','number','min="1" max="100"')]
    fields=''.join(f'<div><label>{esc(label)}</label><input type="{typ}" name="{key}" value="{esc(s.get(key,""))}" {attrs}></div>' for key,label,typ,attrs in main_fields)
    credit_yes='selected' if s.get('allow_credit_sales','YES')=='YES' else ''; credit_no='selected' if s.get('allow_credit_sales','YES')=='NO' else ''
    mp_yes='selected' if s.get('require_mpesa_code','YES')=='YES' else ''; mp_no='selected' if s.get('require_mpesa_code','YES')=='NO' else ''
    fields+=f'<div><label>Dashboard Visible to Permitted Users</label><select name="dashboard_enabled"><option value="YES" {"selected" if s.get("dashboard_enabled","YES")=="YES" else ""}>YES</option><option value="NO" {"selected" if s.get("dashboard_enabled")=="NO" else ""}>NO — BLOCK DIRECT URL TOO</option></select></div><div><label>Performance Basis</label><select name="performance_basis"><option value="NET_PROFIT_MARGIN" {"selected" if s.get("performance_basis","NET_PROFIT_MARGIN")=="NET_PROFIT_MARGIN" else ""}>NET PROFIT MARGIN</option><option value="GROSS_PROFIT_MARGIN" {"selected" if s.get("performance_basis")=="GROSS_PROFIT_MARGIN" else ""}>GROSS PROFIT MARGIN</option><option value="SALES_LESS_EXPENSES" {"selected" if s.get("performance_basis")=="SALES_LESS_EXPENSES" else ""}>SALES LESS EXPENSES</option></select></div><div><label>AI Assistant Enabled</label><select name="ai_assistant_enabled"><option value="YES" {"selected" if s.get("ai_assistant_enabled","YES")=="YES" else ""}>YES</option><option value="NO" {"selected" if s.get("ai_assistant_enabled")=="NO" else ""}>NO</option></select></div><div><label>AI Provider</label><select name="ai_provider"><option value="LOCAL" {"selected" if s.get("ai_provider","LOCAL")=="LOCAL" else ""}>LOCAL (NO API KEY)</option><option value="OPENAI" {"selected" if s.get("ai_provider")=="OPENAI" else ""}>OPENAI RESPONSES API</option></select></div><div><label>AI Model</label><input name="ai_model" value="{esc(s.get("ai_model","gpt-5.6-luna"))}"></div><div><label>AI Base URL</label><input name="ai_base_url" value="{esc(s.get("ai_base_url","https://api.openai.com/v1/responses"))}"></div><div><label>AI API Key</label><input type="password" name="ai_api_key" value="{esc(s.get("ai_api_key",""))}"></div><div><label>Credit Sales — Activate / Deactivate</label><select name="allow_credit_sales"><option value="YES" {credit_yes}>YES</option><option value="NO" {credit_no}>NO</option></select></div><div><label>Require M-Pesa Code</label><select name="require_mpesa_code"><option value="YES" {mp_yes}>YES</option><option value="NO" {mp_no}>NO</option></select></div>'
    mp_enabled_yes='selected' if s.get('mpesa_enabled','NO')=='YES' else ''; mp_enabled_no='selected' if s.get('mpesa_enabled','NO')!='YES' else ''
    mp_env_s='selected' if s.get('mpesa_environment','sandbox')=='sandbox' else ''; mp_env_p='selected' if s.get('mpesa_environment','sandbox')=='production' else ''
    mp_pay='selected' if s.get('mpesa_shortcode_type','PAYBILL')=='PAYBILL' else ''; mp_till='selected' if s.get('mpesa_shortcode_type','PAYBILL')=='TILL' else ''
    mpesa_section=f'''<section class="card" style="margin-top:14px"><form method="post" action="/settings">{csrf_input(CURRENT_HANDLER)}<div><label>Reason for M-Pesa setting changes (required)</label><input name="change_reason" required minlength="5" placeholder="Explain the API/callback change"></div><div class="section-head"><h3>M-Pesa Automatic Payment Integration</h3><span class="pill">DARaja 3.0</span></div><p class="muted">The POS can send an M-Pesa prompt to the customer. After payment, Safaricom sends the receipt code to MONTEZ LIQUOR automatically; the salesperson does not type the code.</p><div class="form-grid"><div><label>Enable M-Pesa Integration</label><select name="mpesa_enabled"><option value="YES" {mp_enabled_yes}>YES</option><option value="NO" {mp_enabled_no}>NO</option></select></div><div><label>Environment</label><select name="mpesa_environment"><option value="sandbox" {mp_env_s}>SANDBOX</option><option value="production" {mp_env_p}>PRODUCTION</option></select></div><div><label>Short Code / Till</label><input name="mpesa_shortcode" value="{esc(s.get('mpesa_shortcode',''))}"></div><div><label>Short Code Type</label><select name="mpesa_shortcode_type"><option value="PAYBILL" {mp_pay}>PAYBILL</option><option value="TILL" {mp_till}>TILL / BUY GOODS</option></select></div><div><label>Consumer Key</label><input name="mpesa_consumer_key" value="{esc(s.get('mpesa_consumer_key',''))}"></div><div><label>Consumer Secret</label><input type="password" name="mpesa_consumer_secret" value="{esc(s.get('mpesa_consumer_secret',''))}"></div><div><label>STK Passkey</label><input type="password" name="mpesa_passkey" value="{esc(s.get('mpesa_passkey',''))}"></div><div><label>Public HTTPS Callback Base URL</label><input name="mpesa_callback_base_url" placeholder="https://your-domain.example.com" value="{esc(s.get('mpesa_callback_base_url',''))}"></div><div><label>Account Reference Prefix</label><input name="mpesa_account_reference_prefix" value="{esc(s.get('mpesa_account_reference_prefix','MONTEZ'))}"></div><div><label>Transaction Description</label><input name="mpesa_transaction_desc" value="{esc(s.get('mpesa_transaction_desc','MONTEZ LIQUOR'))}"></div><div><label>Payment Recovery Match Window (minutes)</label><input type="number" min="5" max="10080" name="mpesa_recovery_match_window_minutes" value="{esc(s.get('mpesa_recovery_match_window_minutes','1440'))}"><small class="muted">Used when an uploaded Till payment has to be matched back to a pending product/payment.</small></div></div><div class="actions" style="margin-top:12px"><button class="btn gold">SAVE M-PESA SETTINGS</button><button class="btn" type="button" onclick="fetch('/api/mpesa/test-connection',{{method:'POST',headers:{{'Content-Type':'application/x-www-form-urlencoded'}},body:'csrf='+encodeURIComponent(document.querySelector('input[name=csrf]').value)}}).then(r=>r.json()).then(d=>alert(d.message||d.error||'Done'))">TEST CONNECTION</button><button class="btn" type="button" onclick="fetch('/api/mpesa/register-c2b',{{method:'POST',headers:{{'Content-Type':'application/x-www-form-urlencoded'}},body:'csrf='+encodeURIComponent(document.querySelector('input[name=csrf]').value)}}).then(r=>r.json()).then(d=>alert(d.message||d.error||'Done'))">REGISTER C2B CALLBACKS</button></div></form></section>'''
    labels=[('role_admin_label','Administrator Role Name','ADMINISTRATOR / DEVELOPER'),('role_management_label','Management Role Name','MANAGEMENT'),('role_sales_label','Sales Person Role Name','SALES PERSON'),('nav_dashboard','Dashboard Label','Dashboard'),('nav_sales','Sales Label','Sales / POS'),('nav_sales_dashboard','Sales Dashboard Label','Sales Dashboard'),('nav_purchases','Purchases Label','Purchases'),('nav_expenses','Expenses Label','Expenses'),('nav_customers','Customers / Credit Label','Customers / Credit'),('nav_reports','Reports Label','Reports'),('nav_stock_report','Stock Report Label','Stock Report'),('nav_reorder_report','Reorder Report Label','Reorder Report'),('nav_live','Live Monitoring Label','Live Monitoring'),('nav_products','Product Registration Label','Product Registration'),('nav_users','User Management Label','User Management'),('nav_settings','Settings Label','Settings'),('nav_ledger','Ledger Label','Ledger & P&L'),('nav_profit_loss','Profit & Loss Label','Profit & Loss'),('nav_expense_report','Expense Report Label','Expense Report'),('nav_backup','Backup Label','Backup'),('nav_audit','Audit Log Label','Audit Log')]
    label_html=''.join(f'<div><label>{esc(label)}</label><input name="{key}" value="{esc(s.get(key,default))}"></div>' for key,label,default in labels)
    report_labels=[('report_end_day','End Day Report','END DAY REPORT'),('report_end_week','End Week Report','END WEEK REPORT'),('report_today','Today','TODAY'),('report_this_week','This Week Report','THIS WEEK REPORT'),('report_monthly','Monthly Report','MONTHLY REPORT'),('report_this_month','This Month','THIS MONTH'),('report_last_month','Last Month','LAST MONTH'),('report_this_year','This Year','THIS YEAR'),('report_last_year','Last Year','LAST YEAR')]
    report_html=''.join(f'<div><label>{esc(label)}</label><input name="{key}" value="{esc(s.get(key,default))}"></div>' for key,label,default in report_labels)
    logo_note='Custom business logo is currently active.' if business_logo_path() else 'Default MONTEZ LIQUOR logo is currently active.'
    content=f'''{notice}<section class="card"><div class="section-head"><h2>Administrator Settings</h2><span class="pill">NO CODING REQUIRED</span></div><form method="post" action="/settings">{csrf_input(CURRENT_HANDLER)}<div><label>Reason for these changes (required)</label><input name="change_reason" required minlength="5" placeholder="Explain why these settings are changing"></div><div class="form-grid">{fields}</div><h3 style="margin-top:18px">Role & Menu Labels</h3><div class="form-grid">{label_html}</div><div class="actions" style="margin-top:14px"><button class="btn gold">SAVE SETTINGS</button></div></form></section><section class="card" style="margin-top:14px"><div class="section-head"><h3>Report Button Labels</h3><span class="pill">EDIT WITHOUT CODING</span></div><form method="post" action="/settings">{csrf_input(CURRENT_HANDLER)}<div><label>Reason for these changes (required)</label><input name="change_reason" required minlength="5" placeholder="Explain why report labels are changing"></div><div class="form-grid">{report_html}</div><div class="actions" style="margin-top:12px"><button class="btn gold">SAVE REPORT LABELS</button></div></form></section><section class="card" style="margin-top:14px"><div class="section-head"><h3>Business Logo / Picture</h3><span class="pill">MAX 5 MB</span></div><p class="muted">{esc(logo_note)}</p><div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap"><img src="/business-logo" style="width:90px;height:90px;object-fit:contain;border:1px solid var(--line);border-radius:12px;background:#fff;padding:8px"><form method="post" action="/settings/logo" enctype="multipart/form-data">{csrf_input(CURRENT_HANDLER)}<input name="change_reason" placeholder="Reason for logo change" required minlength="5"><input type="file" name="logo" accept="image/png,image/jpeg,image/webp" required><div style="margin-top:10px"><button class="btn gold">UPLOAD BUSINESS LOGO</button></div></form><form method="post" action="/settings/logo">{csrf_input(CURRENT_HANDLER)}<input type="hidden" name="remove_logo" value="1"><input name="change_reason" placeholder="Reason for logo removal" required minlength="5"><button class="btn danger">REMOVE CUSTOM LOGO</button></form></div></section>{mpesa_section}<section class="card" style="margin-top:14px"><div class="section-head"><h3>MONTEZ LIQUOR Software Subscription</h3><span class="pill">ADMINISTRATOR ONLY</span></div><p class="muted">Monthly subscription. The first billable month and recurring due day are configured below. Non-administrator access is automatically suspended when the configured due date passes without confirmation. Administrator access remains available.</p><div class="form-grid"><div><label>Monthly Subscription Amount (KSh)</label><input value="{esc(s.get('subscription_monthly_amount','Not set'))}" readonly></div><div><label>Paybill Number</label><input value="{esc(s.get('subscription_paybill','516600'))}" readonly></div><div><label>Account Number</label><input value="{esc(s.get('subscription_account','319851'))}" readonly></div><div><label>Due Date</label><input value="{subscription_due_day_label} of every month" readonly></div><div><label>Billing Status</label><input value="{ 'SUSPENDED — PAYMENT OVERDUE' if subscription_billing_state().get('suspended') else ('PAID' if subscription_billing_state().get('paid') else 'UPCOMING / NOT YET DUE') }" readonly></div><div><label>Billing Month</label><input value="{esc(subscription_billing_state().get('due_month') or 'Not yet due')}" readonly></div></div><form method="post" action="/settings" style="margin-top:12px">{csrf_input(CURRENT_HANDLER)}<input type="hidden" name="subscription_settings_only" value="1"><div class="form-grid"><div><label>Reason for subscription setting change (required)</label><input name="change_reason" required minlength="5" placeholder="Explain why the subscription amount is changing"></div><div><label>Monthly Subscription Amount (KSh)</label><input type="number" name="subscription_monthly_amount" min="1" max="10000000" step="0.01" required value="{esc(s.get('subscription_monthly_amount',''))}" placeholder="Enter monthly amount"></div><div><label>First Billable Month (YYYY-MM)</label><input name="subscription_first_month" required value="{esc(s.get('subscription_first_month','2026-12'))}"></div><div><label>Monthly Due Day (1–28)</label><input type="number" name="subscription_due_day" min="1" max="28" required value="{esc(s.get('subscription_due_day','5'))}"></div><div><label>Subscription Paybill</label><input name="subscription_paybill" required value="{esc(s.get('subscription_paybill','516600'))}"></div><div><label>Subscription Account</label><input name="subscription_account" required value="{esc(s.get('subscription_account','319851'))}"></div></div><div class="actions" style="margin-top:12px"><button class="btn gold">SAVE SUBSCRIPTION SETTINGS</button></div></form><hr><h3>Confirm Monthly M-PESA Payment</h3><p class="muted">After checking the payment in the configured Paybill statement, the Administrator enters the transaction code here. This is a manual verification step.</p><form method="post" action="/subscription/confirm">{csrf_input(CURRENT_HANDLER)}<div class="form-grid"><div><label>Billing Month</label><input name="billing_month" value="{esc(subscription_billing_state().get('due_month') or '')}" readonly required></div><div><label>Amount Received (KSh)</label><input type="number" name="amount" min="0.01" step="0.01" value="{esc(s.get('subscription_monthly_amount',''))}" required></div><div><label>M-PESA Transaction Code</label><input name="mpesa_code" maxlength="15" minlength="8" required placeholder="Enter receipt code"></div><div><label>Reason / Verification Note</label><input name="change_reason" minlength="5" required placeholder="Verified against Paybill statement"></div></div><div class="actions" style="margin-top:12px"><button class="btn gold">CONFIRM SUBSCRIPTION PAYMENT</button></div></form><p class="muted">Last confirmed: {esc(s.get('subscription_last_paid_at','Not yet paid'))} · M-PESA code: {esc(s.get('subscription_last_mpesa_code','—'))}</p></section><section class="card" style="margin-top:14px"><div class="section-head"><h3>Management & Sales Permissions</h3><span class="pill">TICK TO ALLOW · UNTICK TO RESTRICT</span></div><p class="muted">Administrator / Developer always has full access. Every supported permission below can be granted or restricted independently for Management and Sales Person. Administrator-only controls and functions not supported for a role are locked so the screen cannot promise access that the application does not provide.</p><form method="post" action="/settings"><input type="hidden" name="permissions_only" value="1">{csrf_input(CURRENT_HANDLER)}<div><label>Reason for permission changes (required)</label><input name="change_reason" required minlength="5" placeholder="Explain why access is changing"></div>{html_table(['Permission','Purpose','Management','Sales Person'],role_rows)}<div class="actions" style="margin-top:12px"><button class="btn gold">SAVE ROLE PERMISSIONS</button></div></form></section>'''
    return layout(u,'Administrator Settings',content,'/settings','Overall system parameters and permissions')


def ledger_page(u, query=None):
    require_perm_user(u,'VIEW_LEDGER')
    fd,ft,td,tt,preset=period_from_query(query or {}); start=f'{fd} {ft}:00'; end=f'{td} {tt}:59'; branch_id=branch_scope_for(u,query); rows=core.ledger_data(start,end,branch_id)
    selector=branch_scope_selector(u,query,'/ledger')
    content=f'''{selector}<section class="card"><div class="section-head"><h2>General Ledger</h2><span class="pill">{esc(preset)}</span></div>{range_toolbar('/ledger',fd,ft,td,tt,branch_id)}<div class="muted" style="margin-top:8px">Generated by {esc(u['full_name'])} @{esc(u['username'])} at {now()}</div><div class="actions" style="margin-top:10px"><button type="button" class="btn" onclick="window.print()">PRINT</button><a class="btn" href="/ledger/export?format=pdf&start={esc(start)}&end={esc(end)}&branch_id={branch_id or ''}">DOWNLOAD PDF</a><a class="btn" href="/ledger/export?format=xlsx&start={esc(start)}&end={esc(end)}&branch_id={branch_id or ''}">DOWNLOAD EXCEL</a></div></section><section class="card report-print" style="margin-top:14px">{html_table(['Date & Time','Reference','Description','Debit Account','Debit','Credit Account','Credit'],[[esc(a),esc(b),esc(c),esc(d),money(e),esc(f),money(g)] for a,b,c,d,e,f,g in rows])}</section>'''
    return layout(u,'Ledger',content,'/ledger','Custom accounting ledger')

def pl_page(u, query=None):
    require_perm_user(u,'VIEW_LEDGER')
    fd,ft,td,tt,preset=period_from_query(query or {}); start=f'{fd} {ft}:00'; end=f'{td} {tt}:59'; branch_id=branch_scope_for(u,query); d=core.pl_summary(start,end,branch_id)
    cogs_notice=(f'<div class="flash error" style="margin-top:12px">COGS DATA WARNING: {d["cogs_missing_lines"]} sale line(s) have no usable unit cost and {d["cogs_estimated_lines"]} historic line(s) use an estimated current average cost. Profit may be inaccurate for these lines. Correct the COGS / Unit Cost in Product Registration for the affected branch; historical missing costs require source records to reconstruct accurately.</div>' if (d['cogs_missing_lines'] or d['cogs_estimated_lines']) else f'<div class="notice" style="margin-top:12px">COGS coverage: {d["cogs_coverage_pct"]:.1f}% of sale lines use a recorded sale-time cost.</div>')
    selector=branch_scope_selector(u,query,'/profit-loss')
    content=f'''{selector}<section class="card"><div class="section-head"><h2>Profit & Loss</h2><span class="pill">{esc(preset)}</span></div>{range_toolbar('/profit-loss',fd,ft,td,tt,branch_id)}<div class="muted" style="margin-top:8px">Generated by {esc(u['full_name'])} @{esc(u['username'])} at {now()}</div><div class="grid four" style="margin-top:12px"><div class="metric"><div class="muted">SALES REVENUE</div><div class="v">{money(d['revenue'])}</div></div><div class="metric"><div class="muted">COGS</div><div class="v">{money(d['cogs'])}</div></div><div class="metric"><div class="muted">GROSS PROFIT</div><div class="v">{money(d['gross_profit'])}</div></div><div class="metric"><div class="muted">NET PROFIT</div><div class="v">{money(d['net_profit'])}</div></div></div>{cogs_notice}<div class="muted" style="margin-top:8px">COGS is based on the unit-cost snapshot saved with each sale line, not today's selling price.</div><div class="actions" style="margin-top:12px"><button type="button" class="btn" onclick="window.print()">PRINT</button><a class="btn" href="/pl/export?format=pdf&start={esc(start)}&end={esc(end)}&branch_id={branch_id or ''}">DOWNLOAD PDF</a><a class="btn" href="/pl/export?format=xlsx&start={esc(start)}&end={esc(end)}&branch_id={branch_id or ''}">DOWNLOAD EXCEL</a></div></section>'''
    return layout(u,'Profit & Loss',content,'/profit-loss','Custom date and time financial performance')

def backup_page(u):
    if role_name(u)!='ADMIN': raise PermissionError('Administrator permission required.')
    backup_dir=os.path.join(DATA_DIR,'backups'); os.makedirs(backup_dir,exist_ok=True)
    backups=sorted((f for f in os.listdir(backup_dir) if f.lower().endswith('.db')),reverse=True)
    latest=f'<p class="muted">Latest saved backup: {esc(backups[0])}</p>' if backups else '<p class="muted">No saved backup yet. Download one now before continuing.</p>'
    content=f'<section class="card"><h2>Database Backup</h2><p>Creates a fresh, consistent SQLite snapshot of the current MONTEZ LIQUOR database and downloads it to your device.</p>{latest}<div class="actions"><a class="btn gold" href="/backup/download">CREATE &amp; DOWNLOAD BACKUP</a></div><p class="muted" style="margin-top:12px">Keep the downloaded file somewhere safe outside Render. This backup includes the database records; it does not include Daraja Consumer Keys or other environment secrets.</p></section>'
    return layout(u,'Backup',content,'/backup')


def audit_page(u):
    if role_name(u)!='ADMIN': raise PermissionError('Administrator permission required.')
    c=core.db.c(); rows=c.execute('SELECT created_at,username_snapshot,action,entity,details FROM audit_log ORDER BY id DESC LIMIT 500').fetchall(); c.close(); content=f'<section class="card">{html_table(["Date & Time","Username","Action","Entity","Details"],[[esc(r["created_at"]),esc(r["username_snapshot"]),esc(r["action"]),esc(r["entity"]),esc(r["details"])] for r in rows])}</section>'; return layout(u,'Audit Log',content,'/audit')



def _qsval(qs, key, default=''):
    v=qs.get(key, [default])
    return v[0] if isinstance(v,list) and v else v

def _export_rows(title, headers, rows, fmt):
    rows=list(rows)
    footer=core.get_setting('report_footer','').strip()
    if footer:
        rows.append([footer]+['']*max(0,len(headers)-1))
    safe=re.sub(r'[^A-Za-z0-9]+','_',title).strip('_') or 'MONTEZ_LIQUOR_REPORT'
    if fmt=='pdf':
        return make_pdf(title, headers, rows), 'application/pdf', safe+'.pdf'
    return build_xlsx(title, headers, rows), 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', safe+'.xlsx'

def export_sale(handler, u, qs):
    require_perm_user(u,'PRINT_EXPORT')
    receipt=_qsval(qs,'receipt')
    if role_name(u) not in ('ADMIN','MANAGEMENT'):
        c=core.db.c(); sale=c.execute('SELECT branch_id FROM sales WHERE receipt_no=?',(receipt,)).fetchone(); c.close()
        if not sale or int(sale['branch_id'] or 0)!=int(u.get('branch_id') or 0): raise PermissionError('This sale belongs to another branch.')
    fmt=_qsval(qs,'format','xlsx').lower()
    if fmt not in ('pdf','xlsx'): raise ValueError('Unsupported export format.')
    title,headers,rows=sale_export_data(receipt)
    st=core.get_settings(); title=f"{st.get('business_name',APP_NAME)} · {st.get('business_branch_name','')} · {title}"
    data,ctype,fn=_export_rows(title,headers,rows,fmt)
    return send_file(handler,data,ctype,fn)

def export_report_v2(handler, u, qs):
    require_perm_user(u,'PRINT_EXPORT')
    kind=_qsval(qs,'kind','report'); fmt=_qsval(qs,'format','xlsx').lower()
    start=_qsval(qs,'start'); end=_qsval(qs,'end')
    if not start or not end: raise ValueError('Report period is missing.')
    if start>end: raise ValueError('From date/time must be before To date/time.')
    if kind=='report':
        require_perm_user(u,'VIEW_REPORTS'); bid=branch_scope_for(u,qs); rows=core.report_data(start,end,bid)
        headers=['Receipt','Date/Time','Customer','Units','Total','Cash','M-Pesa','Attended By']
        data=[[r['receipt_no'],r['sold_at'],r['customer'],r['units'],r['total'],r['cash'],r['mpesa'],r['attended_by']] for r in rows]
        title=f"{core.get_setting('business_name',APP_NAME)} · {core.get_setting('business_branch_name','')} · SALES REPORT"
    elif kind=='stock':
        require_perm_user(u,'VIEW_STOCK'); bid=branch_scope_for(u,qs)
        rows=core.stock_report(start,end,bid)
        headers=['Product Name','Category','Opening Stock','Purchased','Sold','Adjustments','Closing Stock','Retail Units','Wholesale Units','Retail Revenue','Wholesale Revenue','Retail Price','Wholesale Price','Avg Cost','Closing Value','Reorder','Status']
        data=[[r['name'],r['category'],r['opening'],r['purchased'],r['sold'],r['adjustments'],r['closing'],r['retail_units'],r['wholesale_units'],r['retail_revenue'],r['wholesale_revenue'],r['retail_price'],r['wholesale_price'],r['avg_cost'],r['closing']*r['avg_cost'],r['reorder_level'],r['status']] for r in rows]
        title=f"{core.get_setting('business_name',APP_NAME)} · BRANCH STOCK REPORT"
    elif kind=='reorder':
        require_perm_user(u,'VIEW_STOCK'); bid=branch_scope_for(u,qs); rows=[r for r in core.stock_report(start,end,bid) if r['status'] in ('LOW STOCK','OUT OF STOCK')]
        headers=['Product Name','Category','Current Stock','Reorder Level','Status','Avg Cost','Stock Value']
        data=[[r['name'],r['category'],r['stock'],r['reorder_level'],r['status'],r['avg_cost'],r['stock_value']] for r in rows]
        title='MONTEZ LIQUOR REORDER REPORT'
    elif kind=='branch-performance':
        if role_name(u) not in ('ADMIN','MANAGEMENT'): raise PermissionError('Branch Performance is available to Administrator and Management only.')
        c=core.db.c()
        br=[dict(r) for r in c.execute('''SELECT b.name,COUNT(s.id) transactions,COALESCE(SUM(s.total),0) revenue,COALESCE(SUM((SELECT SUM(sl.qty) FROM sale_lines sl WHERE sl.sale_id=s.id)),0) units FROM branches b LEFT JOIN sales s ON s.branch_id=b.id AND s.sold_at BETWEEN ? AND ? GROUP BY b.id ORDER BY revenue DESC,b.name''',(start,end)).fetchall()]
        emp=[dict(r) for r in c.execute('''SELECT u.full_name,COALESCE(b.name,'Unassigned') branch_name,COUNT(s.id) transactions,COALESCE(SUM(s.total),0) revenue,COALESCE(SUM((SELECT SUM(sl.qty) FROM sale_lines sl WHERE sl.sale_id=s.id)),0) units FROM users u LEFT JOIN branches b ON b.id=u.branch_id LEFT JOIN sales s ON s.user_id=u.id AND s.sold_at BETWEEN ? AND ? WHERE u.active=1 AND u.access_role='SALES' GROUP BY u.id ORDER BY revenue DESC,u.full_name''',(start,end)).fetchall()]
        c.close(); headers=['Type','Award','Rank','Branch / Employee','Transactions','Units Sold','Revenue','Revenue Share %']; data=[]
        for label,rows,namekey in [('BRANCH',br,'name'),('EMPLOYEE',emp,'full_name')]:
            total=sum(float(r['revenue'] or 0) for r in rows)
            for i,r in enumerate(rows,1): data.append([label,award_for_rank(i,r['revenue'],total),i,(r[namekey]+(' · '+r['branch_name'] if namekey=='full_name' else '')),r['transactions'],r['units'],r['revenue'],100*float(r['revenue'] or 0)/total if total else 0])
        title='BRANCH AND EMPLOYEE PERFORMANCE REPORT'
    elif kind=='expenses':
        require_perm_user(u,'VIEW_REPORTS'); bid=branch_scope_for(u,qs); rows=core.expense_report_data(start,end,bid)
        headers=['Date/Time','Expense','Category','Description','Amount','Payment','Reference','Recorded By']
        data=[[r['expense_date'],r['expense_no'],r['category'],r['description'],r['amount'],r['payment_method'],r['reference'] or '',r['attended_by']] for r in rows]
        title='MONTEZ LIQUOR EXPENSE REPORT'
    elif kind=='purchases':
        require_perm_user(u,'VIEW_REPORTS'); bid=branch_scope_for(u,qs); rows=core.purchase_report_data(start,end,bid)
        headers=['Date/Time','Purchase','Supplier','Payment','Total','Units','Reference','Recorded By']
        data=[[r['purchased_at'],r['purchase_no'],r['supplier'],r['payment_method'],r['total'],r['units'],r['reference'] or '',r['attended_by']] for r in rows]
        title='MONTEZ LIQUOR PURCHASE REPORT'
    else:
        raise ValueError('Unknown report type.')
    st=core.get_settings(); title=f"{st.get('business_name',APP_NAME)} · {st.get('business_branch_name','')} · {title} · PERIOD {start} TO {end}"
    out,ctype,fn=_export_rows(title,headers,data,fmt)
    return send_file(handler,out,ctype,fn)

def export_ledger_v2(handler,u,qs):
    require_perm_user(u,'VIEW_LEDGER'); require_perm_user(u,'PRINT_EXPORT')
    start=_qsval(qs,'start'); end=_qsval(qs,'end'); fmt=_qsval(qs,'format','xlsx').lower()
    if not start or not end: raise ValueError('Report period is missing.')
    bid=branch_scope_for(u,qs); rows=core.ledger_data(start,end,bid); headers=['Date & Time','Reference','Description','Debit Account','Debit','Credit Account','Credit']
    st=core.get_settings(); out,ctype,fn=_export_rows(f"{st.get('business_name',APP_NAME)} · {st.get('business_branch_name','')} · GENERAL LEDGER · PERIOD {start} TO {end}",headers,rows,fmt); return send_file(handler,out,ctype,fn)

def export_pl_v2(handler,u,qs):
    require_perm_user(u,'VIEW_LEDGER'); require_perm_user(u,'PRINT_EXPORT')
    start=_qsval(qs,'start'); end=_qsval(qs,'end'); fmt=_qsval(qs,'format','xlsx').lower()
    if not start or not end: raise ValueError('Report period is missing.')
    bid=branch_scope_for(u,qs); d=core.pl_summary(start,end,bid)
    revenue=float(d['revenue'] or 0); cogs=float(d['cogs'] or 0); gross=float(d['gross_profit'] or 0); expenses=float(d['expenses'] or 0); net=float(d['net_profit'] or 0)
    pct=lambda value: round((100.0*value/revenue),2) if revenue else 0.0
    rows=[
        ['Sales Revenue',revenue,100.0,'Total recorded sales in the selected period'],
        ['Less: Cost of Goods Sold (COGS)',cogs,pct(cogs),'Quantity sold × sale-time unit cost'],
        ['Gross Profit',gross,pct(gross),'Sales Revenue − COGS'],
        ['Less: Operating Expenses',expenses,pct(expenses),'Expenses recorded in the selected period'],
        ['NET PROFIT / (LOSS)',net,pct(net),'Gross Profit − Operating Expenses'],
        ['COGS Recorded-Cost Coverage',float(d['cogs_coverage_pct']),None,'Percentage of sale lines with reliable recorded sale-time cost'],
        ['Sale Lines Missing Cost',int(d['cogs_missing_lines']),None,'These lines can make reported profit incomplete'],
        ['Sale Lines Using Estimated Cost',int(d['cogs_estimated_lines']),None,'Estimated current average cost; review before relying on profit'],
        ['Sale Lines Reviewed',int(d['cogs_lines']),None,'Total sale lines included in COGS coverage review']
    ]
    headers=['Profit & Loss Statement','Amount (KES)','% of Sales Revenue','Calculation / Management Note']
    st=core.get_settings(); out,ctype,fn=_export_rows(f"{st.get('business_name',APP_NAME)} · {st.get('business_branch_name','')} · PROFIT AND LOSS · PERIOD {start} TO {end}",headers,rows,fmt); return send_file(handler,out,ctype,fn)

def mpesa_test_connection(u):
    if role_name(u)!='ADMIN': raise PermissionError('Administrator permission required.')
    mpesa.get_access_token(core)
    return {'message':'M-Pesa/Daraja authentication successful.'}

def mpesa_stk_request(u, amount, phone, batch_payload=None, sequence_no=1):
    require_perm_user(u,'CREATE_SALE')
    if core.get_setting('mpesa_enabled','NO')!='YES': raise mpesa.MpesaError('M-Pesa integration is disabled in Administrator Settings.')
    amt=float(amount); phone_n=mpesa.normalize_phone(phone)
    if amt<=0: raise mpesa.MpesaError('Payment amount must be greater than zero.')
    if abs(amt-round(amt))>0.009: raise mpesa.MpesaError('M-Pesa STK amounts must be whole Kenyan shillings. Use a whole-shilling split or direct Till/Paybill payment.')
    c=core.db.c(); ts=core.now_str(); batch_id=None
    role=role_name(u); branch_id=int((batch_payload or {}).get('branch_id','0') or 0) if role in ('ADMIN','MANAGEMENT') else int(u.get('branch_id') or 0)
    if not branch_id: c.close(); raise mpesa.MpesaError('Select an active branch before requesting an M-Pesa prompt.')
    try:
        if not c.execute('SELECT 1 FROM branches WHERE id=? AND active=1',(branch_id,)).fetchone(): raise mpesa.MpesaError('Selected branch is inactive or unavailable.')
        # First STK request can create the pending M-Pesa-only sale batch.
        if batch_payload and batch_payload.get('all_mpesa'):
            pid=int(batch_payload['product_id']); qty=int(batch_payload['qty']); total=float(batch_payload['total']); n=int(batch_payload['payment_count'])
            if sequence_no<1 or sequence_no>n: raise mpesa.MpesaError('Invalid payment transaction number.')
            existing_rows=c.execute("SELECT id,customer_json,branch_id FROM pending_payment_batches WHERE user_id=? AND status='PENDING' AND product_id=? AND ABS(total-?)<0.01 AND branch_id=? ORDER BY id DESC LIMIT 20",(u['id'],pid,total,branch_id)).fetchall()
            requested_sale_type=str(batch_payload.get('sale_type','RETAIL')).upper()
            existing=None
            for candidate in existing_rows:
                try: candidate_type=str(json.loads(candidate['customer_json'] or '{}').get('sale_type','RETAIL')).upper()
                except Exception: candidate_type='RETAIL'
                if candidate_type==requested_sale_type:
                    existing=candidate; break
            if existing: batch_id=existing['id']
            else:
                p=c.execute('SELECT bs.qty FROM branch_stock bs JOIN products p ON p.id=bs.product_id WHERE bs.branch_id=? AND bs.product_id=? AND p.active=1',(branch_id,pid)).fetchone()
                if not p or float(p['qty'])<qty: raise mpesa.MpesaError('Insufficient stock in the selected branch for this pending sale.')
                cur=c.execute('INSERT INTO pending_payment_batches(created_at,updated_at,user_id,total,product_id,qty,customer_json,due_date,collateral,collateral_value,payment_count,status,branch_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(ts,ts,u['id'],total,pid,qty,json.dumps({'sale_type':str(batch_payload.get('sale_type','RETAIL')).upper()},ensure_ascii=False),'','',0,n,'PENDING',branch_id)); batch_id=cur.lastrowid
                for j in range(1,n+1):
                    amt_j=float(batch_payload.get(f'payment_amount_{j}',0) or 0)
                    c.execute('INSERT INTO pending_payment_allocations(batch_id,sequence_no,amount,payment_method,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',(batch_id,j,amt_j,'MPESA','PENDING',ts,ts))
        client_ref='K'+secrets.token_hex(6).upper(); account_ref=(core.get_setting('mpesa_account_reference_prefix','MONTEZ')+client_ref)[-12:]
        data=mpesa.stk_push(core,amt,phone_n,account_ref,core.get_setting('mpesa_transaction_desc','MONTEZ LIQUOR'))
        checkout=data.get('CheckoutRequestID'); merchant=data.get('MerchantRequestID')
        if not checkout: raise mpesa.MpesaError(data.get('ResponseDescription') or data.get('errorMessage') or 'No CheckoutRequestID returned by M-Pesa.')
        c.execute('INSERT INTO mpesa_stk_requests(created_at,user_id,checkout_request_id,merchant_request_id,client_ref,amount,phone,status,raw_response,updated_at,branch_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(ts,u['id'],checkout,merchant,client_ref,amt,phone_n,'PENDING',json.dumps(data,ensure_ascii=False),ts,branch_id))
        if batch_id:
            c.execute('UPDATE pending_payment_allocations SET checkout_request_id=?,phone=?,updated_at=? WHERE batch_id=? AND sequence_no=?',(checkout,phone_n,ts,batch_id,sequence_no))
            c.execute('UPDATE pending_payment_batches SET batch_ref=?,updated_at=? WHERE id=?',(account_ref,ts,batch_id))
        core.db.audit(c,u,'MPESA_STK_REQUEST','MPESA_STK',checkout,f'amount={amt:.2f} phone={phone_n} batch={batch_id or ""} sequence={sequence_no}')
        c.commit(); return {'CheckoutRequestID':checkout,'MerchantRequestID':merchant,'batch_id':batch_id,'sequence_no':sequence_no}
    except Exception:
        c.rollback(); raise
    finally: c.close()

def mpesa_confirm_direct_payment(u, form):
    """Confirm direct C2B/Till payment(s) made without an STK prompt."""
    require_perm_user(u,'CREATE_SALE')
    phone_raw=str(form.get('phone','')).strip(); pid=int(form.get('product_id','0') or 0); qty=int(form.get('qty','0') or 0); sale_type=str(form.get('sale_type','RETAIL') or 'RETAIL').upper(); role=role_name(u); branch_id=int(form.get('branch_id','0') or 0) if role in ('ADMIN','MANAGEMENT') else int(u.get('branch_id') or 0)
    if not branch_id: raise ValueError('Select an active branch before confirming a direct M-Pesa sale.')
    if sale_type not in ('RETAIL','WHOLESALE'): raise ValueError('Invalid sale type.')
    if not phone_raw: raise ValueError('Enter the customer M-Pesa phone number.')
    if pid<=0 or qty<=0: raise ValueError('Select a product and enter a valid quantity.')
    cart=[{'product_id':pid,'qty':qty}]
    phone=mpesa.normalize_phone(phone_raw); c=core.db.c()
    try:
        p=c.execute('SELECT p.*,COALESCE(bs.qty,0) branch_qty,COALESCE(bs.sell_price,p.sell_price) branch_sell_price,COALESCE(bs.wholesale_price,p.wholesale_price) branch_wholesale_price FROM products p LEFT JOIN branch_stock bs ON bs.product_id=p.id AND bs.branch_id=? WHERE p.id=? AND p.active=1',(branch_id,pid)).fetchone()
        if not p: raise ValueError('Product not found.')
        if float(p['branch_qty'])<qty: raise ValueError(f'Insufficient stock in this branch. Available: {p["branch_qty"]}.')
        unit_price=float(p['branch_wholesale_price'] or 0) if sale_type=='WHOLESALE' else float(p['branch_sell_price']); total=round(unit_price*qty,2)
        if unit_price<=0: raise ValueError('The selected sale type has no configured price.')
        rows=c.execute("""SELECT id,trans_id,amount,trans_time,received_at
                          FROM mpesa_transactions
                          WHERE status IN ('UNMATCHED','RECEIVED')
                            AND UPPER(COALESCE(transaction_type,'')) IN ('C2B','PAYMENT','RECOVERY_UPLOAD')
                            AND msisdn=? AND COALESCE(matched_sale_payment_id,0)=0
                          ORDER BY id DESC LIMIT 50""",(phone,)).fetchall()
        if not rows: return {'ok':False,'message':'No latest unmatched M-Pesa payment was found for this phone number.','matched':0.0,'remaining':total}
        selected=[]; accumulated=0.0
        for r in rows:
            amt=round(float(r['amount'] or 0),2)
            if amt<=0 or accumulated+amt>total+0.009: continue
            selected.append(r); accumulated=round(accumulated+amt,2)
            if abs(accumulated-total)<=0.009: break
        if abs(accumulated-total)>0.009:
            return {'ok':False,'partial':True,'message':f'Payment found so far: {money(accumulated)}. Remaining: {money(round(total-accumulated,2))}. Press CONFIRM PAYMENT again after the customer sends the remaining amount.','matched':accumulated,'remaining':round(total-accumulated,2),'transactions':[dict(r) for r in selected]}
        codes=[str(r['trans_id']).strip().upper() for r in selected]
        payments=[{'amount':float(r['amount']),'payment_method':'MPESA','mpesa_code':code} for r,code in zip(selected,codes)]
        d=core.create_sale(u,{'cart':cart,'payments':payments,'customer':{},'credit':None,'sale_type':sale_type,'branch_id':branch_id})
        c2=core.db.c()
        try:
            for code in codes:
                c2.execute('UPDATE mpesa_transactions SET status="AUTO_SAVED",matched_sale_payment_id=(SELECT id FROM sale_payments WHERE sale_id=? AND UPPER(mpesa_code)=?) WHERE UPPER(trans_id)=?',(d['sale_id'],code,code))
            c2.commit()
        finally: c2.close()
        return {'ok':True,'sale_id':d['sale_id'],'receipt_no':d['receipt_no'],'matched':accumulated,'remaining':0.0,'transactions':[dict(r) for r in selected]}
    finally: c.close()

def mpesa_stk_status(checkout,u=None,query=None):
    c=core.db.c(); r=c.execute('SELECT checkout_request_id,status,mpesa_code,amount,phone,transaction_date,sale_id,branch_id,raw_response FROM mpesa_stk_requests WHERE checkout_request_id=?',(checkout,)).fetchone(); c.close()
    if not r: return {'status':'NOT_FOUND'}
    if u:
        bid=branch_scope_for(u,query)
        if bid is not None and int(r['branch_id'] or 0)!=int(bid): return {'status':'NOT_FOUND'}
    d=dict(r); raw={}
    try: raw=json.loads(d.pop('raw_response') or '{}')
    except Exception: d.pop('raw_response',None)
    d['auto_saved']=bool(d.get('sale_id'))
    if d['status']=='REVIEW':
        d['message']='M-Pesa reported a payment, but the amount or phone did not match this prompt. Do not record it as paid; ask Administrator to reconcile it.'
    elif d['status']=='FAILED':
        cb=raw.get('Body',{}).get('stkCallback',{})
        d['message']=cb.get('ResultDesc') or 'Customer did not complete the M-Pesa payment.'
    elif d['status']=='SUCCESS' and not d['auto_saved']:
        d['message']='Payment confirmed. Review the receipt code and click SAVE SALE to complete this mixed-payment sale.'
    return d

def finalize_pending_mpesa_batch(batch_id):
    c=core.db.c(); b=c.execute('SELECT * FROM pending_payment_batches WHERE id=? AND status="PENDING"',(batch_id,)).fetchone()
    if not b: c.close(); return None
    rows=c.execute('SELECT * FROM pending_payment_allocations WHERE batch_id=? ORDER BY sequence_no',(batch_id,)).fetchall()
    if len(rows)!=b['payment_count'] or any(r['status']!='SUCCESS' or not r['mpesa_code'] for r in rows): c.close(); return None
    total=sum(float(r['amount']) for r in rows)
    if abs(total-float(b['total']))>0.009: c.close(); return None
    branch_id=int(b['branch_id'] or 0)
    p=c.execute('SELECT bs.qty FROM branch_stock bs JOIN products p ON p.id=bs.product_id WHERE bs.branch_id=? AND bs.product_id=? AND p.active=1',(branch_id,b['product_id'])).fetchone()
    if not branch_id or not p or float(p['qty'])<float(b['qty']): c.close(); return None
    c.close()
    payments=[{'amount':float(r['amount']),'payment_method':'MPESA','mpesa_code':r['mpesa_code']} for r in rows]
    u=core.db.c(); ur=u.execute('SELECT * FROM users WHERE id=? AND active=1',(b['user_id'],)).fetchone(); u.close()
    if not ur: return None
    
    try:
        customer_payload=json.loads(b['customer_json'] or '{}')
    except Exception:
        customer_payload={}
    sale=core.create_sale(dict(ur),{'cart':[{'product_id':b['product_id'],'qty':b['qty']}],'payments':payments,'customer':customer_payload,'credit':None,'sale_type':str(customer_payload.get('sale_type','RETAIL')).upper(),'branch_id':branch_id})
    c=core.db.c(); ts=core.now_str(); c.execute('UPDATE pending_payment_batches SET status="COMPLETED",created_sale_id=?,updated_at=? WHERE id=?',(sale['sale_id'],ts,batch_id)); c.execute('UPDATE mpesa_stk_requests SET sale_id=?,updated_at=? WHERE checkout_request_id IN (SELECT checkout_request_id FROM pending_payment_allocations WHERE batch_id=?)',(sale['sale_id'],ts,batch_id)); c.commit(); c.close(); return sale

def mpesa_receive_stk(payload):
    d=mpesa.parse_stk_callback(payload)
    c=core.db.c()
    try:
        r=c.execute('SELECT id,amount,phone FROM mpesa_stk_requests WHERE checkout_request_id=?',(d['checkout_request_id'],)).fetchone()
        status='PENDING'; ts=core.now_str()
        if r:
            if d['result_code']!=0:
                status='FAILED'
            elif not d['mpesa_code']:
                status='PENDING'
            else:
                amount_ok=d['amount']>0 and abs(float(d['amount'])-float(r['amount']))<0.009
                phone_ok=True
                if d['phone']:
                    try: phone_ok=mpesa.normalize_phone(d['phone'])==mpesa.normalize_phone(r['phone'])
                    except Exception: phone_ok=False
                status='SUCCESS' if amount_ok and phone_ok else 'REVIEW'
            c.execute('UPDATE mpesa_stk_requests SET status=?,mpesa_code=?,transaction_date=?,raw_response=?,updated_at=? WHERE checkout_request_id=?',(status,d['mpesa_code'],d['transaction_date'],json.dumps(payload,ensure_ascii=False),ts,d['checkout_request_id']))
            if status=='SUCCESS':
                a=c.execute('SELECT id,batch_id FROM pending_payment_allocations WHERE checkout_request_id=?',(d['checkout_request_id'],)).fetchone()
                if a: c.execute('UPDATE pending_payment_allocations SET status="SUCCESS",mpesa_code=?,updated_at=? WHERE id=?',(d['mpesa_code'],ts,a['id']))
        c.commit()
    except Exception:
        c.rollback(); raise
    finally: c.close()
    sale=None
    if status=='SUCCESS':
        c2=core.db.c(); a2=c2.execute('SELECT batch_id FROM pending_payment_allocations WHERE checkout_request_id=?',(d['checkout_request_id'],)).fetchone(); c2.close()
        if a2: sale=finalize_pending_mpesa_batch(a2['batch_id'])
    d['auto_saved']=bool(sale); d['sale_id']=sale['sale_id'] if sale else None; d['status']=status
    return d

def mpesa_receive_c2b(payload):
    d=mpesa.parse_c2b(payload); c=core.db.c(); status='UNMATCHED'; matched_sale_payment_id=None; matched_batch_id=None; sale=None
    configured=str(core.get_setting('mpesa_shortcode','1546252')).strip() or '1546252'
    if d['business_short_code'] and str(d['business_short_code'])!=configured:
        status='REJECTED_SHORTCODE'
    elif d['trans_id']:
        c.execute('INSERT OR IGNORE INTO mpesa_transactions(received_at,transaction_type,trans_id,trans_time,amount,business_short_code,bill_ref_number,invoice_number,org_account_balance,msisdn,first_name,middle_name,last_name,raw_json,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(core.now_str(),d['transaction_type'],d['trans_id'],d['trans_time'],d['amount'],d['business_short_code'],d['bill_ref_number'],d['invoice_number'],d['org_account_balance'],d['msisdn'],d['first_name'],d['middle_name'],d['last_name'],d['raw_json'],'UNMATCHED'))
        if d['msisdn'] and d['amount']>0:
            normalized_msisdn=''
            try:
                normalized_msisdn=mpesa.normalize_phone(d['msisdn'])
            except Exception:
                normalized_msisdn=''
            if normalized_msisdn:
                matches=c.execute('''SELECT ppa.id,ppa.batch_id FROM pending_payment_allocations ppa JOIN pending_payment_batches pb ON pb.id=ppa.batch_id WHERE ppa.status='PENDING' AND ppa.payment_method='MPESA' AND ABS(ppa.amount-?)<0.009 AND ppa.phone=? AND pb.status='PENDING' ORDER BY ppa.id''',(d['amount'],normalized_msisdn)).fetchall()
                if len(matches)==1:
                    matched_batch_id=matches[0]['batch_id']; c.execute('UPDATE pending_payment_allocations SET status="SUCCESS",mpesa_code=?,updated_at=? WHERE id=?',(d['trans_id'],core.now_str(),matches[0]['id'])); status='MATCHED'
        c.execute('UPDATE mpesa_transactions SET status=? WHERE trans_id=?',(status,d['trans_id']))
    c.commit(); c.close()
    if matched_batch_id:
        sale=finalize_pending_mpesa_batch(matched_batch_id)
        if sale:
            c2=core.db.c(); r=c2.execute('SELECT id FROM sale_payments WHERE sale_id=? AND mpesa_code=?',(sale['sale_id'],d['trans_id'])).fetchone();
            if r:
                matched_sale_payment_id=r['id']; c2.execute('UPDATE mpesa_transactions SET matched_sale_payment_id=?,status="MATCHED" WHERE trans_id=?',(matched_sale_payment_id,d['trans_id'])); c2.commit()
            c2.close()
    d['status']=status; d['auto_saved']=bool(matched_batch_id and sale); d['sale_id']=sale['sale_id'] if matched_batch_id and sale else None
    return d


def _recovery_time_minutes():
    try:
        return max(5, int(float(core.get_setting('mpesa_recovery_match_window_minutes','1440'))))
    except Exception:
        return 1440


def _parse_dt_for_match(v):
    if not v: return None
    s=str(v).strip()
    for fmt in ('%Y-%m-%d %H:%M:%S','%d/%m/%Y %H:%M:%S','%d/%m/%Y %H:%M','%m/%d/%Y %H:%M:%S','%m/%d/%Y %H:%M'):
        try:
            return dt.datetime.strptime(s,fmt).replace(tzinfo=dt.timezone(dt.timedelta(hours=3)))
        except ValueError:
            pass
    try:
        x=dt.datetime.fromisoformat(s.replace('Z','+00:00'))
        return x if x.tzinfo else x.replace(tzinfo=dt.timezone(dt.timedelta(hours=3)))
    except Exception:
        return None


def _recovery_candidates(c, txn, include_time=True):
    amount=float(txn['amount'])
    phone=str(txn.get('msisdn') or '').strip()
    window=_recovery_time_minutes()
    rows=c.execute('''
        SELECT ppa.id allocation_id,ppa.batch_id,ppa.amount,ppa.phone,ppa.sequence_no,
               pb.created_at,pb.total,pb.qty,pb.customer_json,pb.payment_count,pb.product_id,
               pb.batch_ref,p.name product_name, u.full_name attended_by
        FROM pending_payment_allocations ppa
        JOIN pending_payment_batches pb ON pb.id=ppa.batch_id
        JOIN products p ON p.id=pb.product_id
        JOIN users u ON u.id=pb.user_id
        WHERE ppa.status='PENDING' AND ppa.payment_method='MPESA' AND pb.status='PENDING'
          AND ABS(ppa.amount-?)<0.009
        ORDER BY ppa.created_at ASC, ppa.id ASC
    ''',(amount,)).fetchall()
    out=[dict(r) for r in rows]
    if phone and re.fullmatch(r'254\d{9}',phone):
        exact=[r for r in out if str(r.get('phone') or '')==phone]
        if exact:
            return exact
        # A usable phone number that does not match is a hard mismatch unless
        # the M-Pesa reference explicitly identifies the pending batch.
        ref=str(txn.get('bill_ref_number') or '').strip().lower()
        if ref:
            ref_hits=[r for r in out if r.get('batch_ref') and str(r['batch_ref']).lower() in ref]
            if ref_hits:
                return ref_hits
        return []
    ref=str(txn.get('bill_ref_number') or '').strip().lower()
    if ref:
        ref_hits=[r for r in out if r.get('batch_ref') and str(r['batch_ref']).lower() in ref]
        if ref_hits:
            return ref_hits
    if include_time and len(out)>1:
        td=_parse_dt_for_match(txn.get('trans_time'))
        if td:
            ranked=[]
            for r in out:
                bd=_parse_dt_for_match(r.get('created_at'))
                if bd:
                    secs=abs((td-bd).total_seconds())
                    if secs <= window*60: ranked.append((secs,r))
            ranked.sort(key=lambda x:x[0])
            if ranked:
                best=ranked[0][0]
                winners=[r for secs,r in ranked if abs(secs-best)<1]
                if len(winners)==1:
                    return winners
    if len(out)==1:
        return out
    return out


def _finalize_recovered_batch(batch_id):
    try:
        return finalize_pending_mpesa_batch(int(batch_id))
    except Exception:
        return None


def _apply_recovered_transaction(c, txn, allocation_id, auto=True):
    row=c.execute('''SELECT ppa.id,ppa.batch_id,ppa.amount,ppa.payment_method,ppa.status,pb.status batch_status
                     FROM pending_payment_allocations ppa JOIN pending_payment_batches pb ON pb.id=ppa.batch_id
                     WHERE ppa.id=?''',(int(allocation_id),)).fetchone()
    if not row: return {'ok':False,'message':'Payment allocation no longer exists.'}
    if row['status']!='PENDING' or row['batch_status']!='PENDING':
        return {'ok':False,'message':'Payment allocation is no longer pending.'}
    if abs(float(row['amount'])-float(txn['amount']))>0.009:
        return {'ok':False,'message':'Uploaded amount does not equal the pending payment allocation.'}
    code=str(txn.get('trans_id') or '').strip().upper()
    if not code: return {'ok':False,'message':'M-Pesa transaction/receipt code is required.'}
    existing=c.execute('SELECT 1 FROM sale_payments WHERE UPPER(mpesa_code)=? UNION ALL SELECT 1 FROM credit_payments WHERE UPPER(mpesa_code)=?',(code,code)).fetchone()
    if existing: return {'ok':False,'message':'This M-Pesa code has already been used in a posted payment.'}
    ts=core.now_str()
    c.execute('UPDATE pending_payment_allocations SET status="SUCCESS",mpesa_code=?,updated_at=? WHERE id=?',(code,ts,int(allocation_id)))
    return {'ok':True,'batch_id':row['batch_id']}


def _store_recovery_transaction(c, txn):
    code=str(txn.get('trans_id') or '').strip().upper()
    if not code: raise ValueError(f'Row {txn.get("source_row")}: missing transaction/receipt code.')
    old=c.execute('SELECT id,status FROM mpesa_transactions WHERE UPPER(trans_id)=?',(code,)).fetchone()
    if old:
        return None, 'DUPLICATE'
    configured=str(core.get_setting('mpesa_shortcode','1546252')).strip() or '1546252'
    provided=str(txn.get('business_short_code') or '').strip()
    if provided and re.sub(r'\D','',provided)!=re.sub(r'\D','',configured):
        c.execute('INSERT INTO mpesa_transactions(received_at,transaction_type,trans_id,trans_time,amount,business_short_code,bill_ref_number,invoice_number,org_account_balance,msisdn,first_name,middle_name,last_name,raw_json,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(core.now_str(),'RECOVERY_UPLOAD',code,txn.get('trans_time',''),float(txn['amount']),provided,txn.get('bill_ref_number',''),'','','','','','',json.dumps(txn.get('raw_row',{}),ensure_ascii=False),'REJECTED_SHORTCODE'))
        return code, 'REJECTED_SHORTCODE'
    c.execute('INSERT INTO mpesa_transactions(received_at,transaction_type,trans_id,trans_time,amount,business_short_code,bill_ref_number,invoice_number,org_account_balance,msisdn,first_name,middle_name,last_name,raw_json,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(core.now_str(),'RECOVERY_UPLOAD',code,txn.get('trans_time',''),float(txn['amount']),provided or configured,txn.get('bill_ref_number',''),'','','', '', '', '',json.dumps(txn.get('raw_row',{}),ensure_ascii=False),'UNMATCHED'))
    return code, 'UNMATCHED'


def _match_recovery_transaction(u, txn):
    code=str(txn.get('trans_id') or '').strip().upper()
    if not code: return {'status':'INVALID','message':'Missing M-Pesa code.'}
    c=core.db.c()
    try:
        candidates=_recovery_candidates(c,txn,include_time=True)
        if not candidates:
            return {'status':'UNMATCHED','message':'No safe pending product/payment match was found.'}
        if len(candidates)!=1:
            return {'status':'UNMATCHED','message':f'{len(candidates)} possible pending matches found; left for Administrator review.'}
        candidate=candidates[0]
        result=_apply_recovered_transaction(c,txn,candidate['allocation_id'])
        if not result.get('ok'):
            return {'status':'UNMATCHED','message':result.get('message','Could not match payment.')}
        c.execute('UPDATE mpesa_transactions SET status="MATCHED" WHERE trans_id=?',(code,))
        batch_id=result['batch_id']
        # Commit the allocation match before finalizing the sale on a separate connection.
        c.commit()
    except Exception:
        c.rollback(); raise
    finally:
        c.close()
    sale=_finalize_recovered_batch(batch_id)
    c=core.db.c()
    try:
        if sale:
            c.execute('UPDATE mpesa_transactions SET status="AUTO_SAVED",matched_sale_payment_id=(SELECT id FROM sale_payments WHERE sale_id=? AND UPPER(mpesa_code)=?) WHERE trans_id=?',(sale['sale_id'],code,code))
            c.commit()
            return {'status':'AUTO_SAVED','message':f'{code} matched to {candidate["product_name"]}; sale {sale["receipt_no"]} was saved automatically.','batch_id':batch_id,'sale_id':sale['sale_id']}
        c.execute('UPDATE mpesa_transactions SET status="MATCHED_PENDING" WHERE trans_id=?',(code,))
        c.commit()
        return {'status':'MATCHED_PENDING','message':f'{code} matched to {candidate["product_name"]}; the payment allocation is recovered and awaiting remaining payment(s) or finalization.','batch_id':batch_id}
    except Exception:
        c.rollback(); raise
    finally:
        c.close()

def import_mpesa_recovery(u, upload):
    records=mpesa_recovery.parse_statement(upload['data'],upload['filename'])
    if len(records)>5000: raise ValueError('The upload is too large. Please upload no more than 5,000 payment rows at a time.')
    if not records: raise ValueError('No completed incoming M-Pesa payments were found in the uploaded file.')
    matched=auto=unmatched=duplicates=rejected=0; details=[]
    for txn in records:
        c=core.db.c()
        try:
            c.execute('BEGIN')
            code,status=_store_recovery_transaction(c,txn)
            c.commit()
        except Exception:
            c.rollback(); c.close(); raise
        finally:
            try: c.close()
            except Exception: pass
        if status=='DUPLICATE':
            duplicates+=1; continue
        if status=='REJECTED_SHORTCODE':
            rejected+=1; continue
        result=_match_recovery_transaction(u,txn)
        if result['status']=='AUTO_SAVED': auto+=1; matched+=1
        elif result['status']=='MATCHED_PENDING': matched+=1
        else: unmatched+=1
        if len(details)<15:
            details.append(f"Row {txn['source_row']}: {txn['trans_id']} — {result['message']}")
    c=core.db.c()
    try:
        core.db.audit(c,u,'M-PESA_RECOVERY_UPLOAD','MPESA_RECOVERY','',f"file={upload['filename']} rows={len(records)} auto_saved={auto} matched={matched} unmatched={unmatched} duplicates={duplicates} rejected={rejected}")
        c.commit()
    finally:
        c.close()
    return f"Recovery complete: {auto} sale(s) auto-saved, {matched-auto} payment(s) matched, {unmatched} unmatched, {duplicates} duplicate(s), {rejected} rejected." + (" Details: " + " | ".join(details) if details else '')

def retry_unmatched_recovery(u):
    c=core.db.c(); rows=c.execute("SELECT * FROM mpesa_transactions WHERE status IN ('UNMATCHED','MATCHED_PENDING') ORDER BY id").fetchall(); c.close(); auto=matched=still=0
    for r in rows:
        txn={'trans_id':r['trans_id'],'amount':r['amount'],'trans_time':r['trans_time'],'msisdn':r['msisdn'],'bill_ref_number':r['bill_ref_number']}
        result=_match_recovery_transaction(u,txn)
        if result['status']=='AUTO_SAVED': auto+=1; matched+=1
        elif result['status']=='MATCHED_PENDING': matched+=1
        else: still+=1
    c=core.db.c()
    try:
        core.db.audit(c,u,'M-PESA_RECOVERY_RETRY','MPESA_RECOVERY','',f'rows={len(rows)} auto_saved={auto} matched={matched} still_unmatched={still}')
        c.commit()
    finally:
        c.close()
    return f'Retry complete: {auto} sale(s) auto-saved, {matched-auto} payment(s) matched, {still} still unmatched.'

def manual_match_recovery(u, trans_id, allocation_id):
    code=str(trans_id or '').strip().upper()
    if not code or not allocation_id: raise ValueError('Select a transaction and a pending payment allocation.')
    c=core.db.c()
    try:
        r=c.execute('SELECT * FROM mpesa_transactions WHERE UPPER(trans_id)=?',(code,)).fetchone()
        if not r: raise ValueError('Imported M-Pesa transaction was not found.')
        if r['status'] not in ('UNMATCHED','MATCHED_PENDING'): raise ValueError('This transaction is no longer available for recovery matching.')
        txn={'trans_id':r['trans_id'],'amount':r['amount'],'trans_time':r['trans_time'],'msisdn':r['msisdn'],'bill_ref_number':r['bill_ref_number']}
        result=_apply_recovered_transaction(c,txn,int(allocation_id))
        if not result.get('ok'): raise ValueError(result.get('message','Could not match transaction.'))
        batch_id=result['batch_id']
        c.execute('UPDATE mpesa_transactions SET status="MATCHED" WHERE UPPER(trans_id)=?',(code,))
        c.commit()
    except Exception:
        c.rollback(); c.close(); raise
    finally:
        try: c.close()
        except Exception: pass
    sale=_finalize_recovered_batch(batch_id)
    c=core.db.c()
    try:
        if sale:
            c.execute('UPDATE mpesa_transactions SET status="AUTO_SAVED",matched_sale_payment_id=(SELECT id FROM sale_payments WHERE sale_id=? AND UPPER(mpesa_code)=?) WHERE UPPER(trans_id)=?',(sale['sale_id'],code,code))
            msg=f'{code} manually matched and sale {sale["receipt_no"]} was saved automatically.'
        else:
            c.execute('UPDATE mpesa_transactions SET status="MATCHED_PENDING" WHERE UPPER(trans_id)=?',(code,))
            msg=f'{code} matched successfully. The payment is linked to the pending product/payment batch.'
        core.db.audit(c,u,'M-PESA_RECOVERY_MATCH','MPESA_RECOVERY',r['id'],msg)
        c.commit(); return msg
    except Exception:
        c.rollback(); raise
    finally: c.close()

def manual_reconcile_recovery(u, trans_id, customer_id, product_id, qty):
    if role_name(u)!='ADMIN': raise PermissionError('Administrator permission required.')
    code=str(trans_id or '').strip().upper(); cid=int(customer_id or 0); pid=int(product_id or 0); quantity=int(qty or 0)
    if not code or cid<=0 or pid<=0 or quantity<=0: raise ValueError('Select the M-Pesa transaction, customer, product and a valid quantity.')
    c=core.db.c()
    try:
        txn=c.execute('SELECT * FROM mpesa_transactions WHERE UPPER(trans_id)=?',(code,)).fetchone()
        if not txn: raise ValueError('Imported M-Pesa transaction was not found.')
        if txn['status'] not in ('UNMATCHED','MATCHED_PENDING'): raise ValueError('This M-Pesa payment is no longer available for manual reconciliation.')
        customer=c.execute('SELECT * FROM customers WHERE id=? AND active=1',(cid,)).fetchone(); product=c.execute('SELECT * FROM products WHERE id=? AND active=1',(pid,)).fetchone()
        if not customer: raise ValueError('Selected customer was not found.')
        if not product: raise ValueError('Selected product was not found.')
        total=round(float(product['sell_price'])*quantity,2); amount=round(float(txn['amount']),2)
        if amount > total + 0.009: raise ValueError(f'M-Pesa payment {money(amount)} is greater than the selected product sale total {money(total)}. Select the correct product or quantity.')
        existing=c.execute('SELECT 1 FROM sale_payments WHERE UPPER(mpesa_code)=? UNION ALL SELECT 1 FROM credit_payments WHERE UPPER(mpesa_code)=?',(code,code)).fetchone()
        if existing: raise ValueError('This M-Pesa code has already been used in a posted payment.')
        ts=core.now_str(); batch_ref='REC-'+code
        remaining=round(total-amount,2)
        payment_count=1 if remaining<=0.009 else 2
        cur=c.execute('INSERT INTO pending_payment_batches(created_at,updated_at,user_id,total,product_id,qty,customer_json,payment_count,status,batch_ref) VALUES(?,?,?,?,?,?,?,?,?,?)',(ts,ts,u['id'],total,pid,quantity,json.dumps({'id':cid},ensure_ascii=False),payment_count,'PENDING',batch_ref)); batch_id=cur.lastrowid
        cur=c.execute('INSERT INTO pending_payment_allocations(batch_id,sequence_no,amount,payment_method,phone,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',(batch_id,1,amount,'MPESA',customer['phone'],'PENDING',ts,ts)); allocation_id=cur.lastrowid
        if remaining>0.009:
            c.execute('INSERT INTO pending_payment_allocations(batch_id,sequence_no,amount,payment_method,phone,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',(batch_id,2,remaining,'MPESA',customer['phone'],'PENDING',ts,ts))
        result=_apply_recovered_transaction(c,{'trans_id':code,'amount':amount,'trans_time':txn['trans_time'],'msisdn':txn['msisdn'],'bill_ref_number':txn['bill_ref_number']},allocation_id)
        if not result.get('ok'): raise ValueError(result.get('message','Could not reconcile payment.'))
        c.execute('UPDATE mpesa_transactions SET status="MATCHED_PENDING" WHERE UPPER(trans_id)=?',(code,))
        core.db.audit(c,u,'M-PESA_MANUAL_RECONCILIATION','MPESA_RECOVERY',txn['id'],f'{code} customer={customer["full_name"]} product={product["name"]} qty={quantity} amount={amount:.2f} total={total:.2f}')
        c.commit()
    except Exception: c.rollback(); raise
    finally: c.close()
    sale=_finalize_recovered_batch(batch_id)
    c=core.db.c()
    try:
        if sale:
            c.execute('UPDATE mpesa_transactions SET status="AUTO_SAVED",matched_sale_payment_id=(SELECT id FROM sale_payments WHERE sale_id=? AND UPPER(mpesa_code)=?) WHERE UPPER(trans_id)=?',(sale['sale_id'],code,code)); msg=f'{code} manually reconciled to {customer["full_name"]} / {product["name"]}. Sale {sale["receipt_no"]} was saved automatically.'
        else:
            remaining=max(0.0,total-amount); c.execute('UPDATE mpesa_transactions SET status="MATCHED_PENDING" WHERE UPPER(trans_id)=?',(code,)); msg=f'{code} manually reconciled to {customer["full_name"]} / {product["name"]}. Payment {money(amount)} recorded; remaining balance is {money(remaining)}.'
        c.commit(); return msg
    except Exception: c.rollback(); raise
    finally: c.close()

def mpesa_recovery_page(u, message=''):
    if role_name(u)!='ADMIN': raise PermissionError('Administrator permission required.')
    c=core.db.c()
    pending=c.execute('''SELECT ppa.id allocation_id,ppa.sequence_no,ppa.amount,ppa.status,ppa.phone,pb.id batch_id,pb.total,pb.payment_count,pb.created_at,p.name product_name,u.full_name attended_by
                         FROM pending_payment_allocations ppa JOIN pending_payment_batches pb ON pb.id=ppa.batch_id JOIN products p ON p.id=pb.product_id JOIN users u ON u.id=pb.user_id
                         WHERE pb.status='PENDING' ORDER BY pb.created_at DESC,ppa.sequence_no''').fetchall()
    recent=c.execute('''SELECT received_at,trans_id,trans_time,amount,msisdn,bill_ref_number,status FROM mpesa_transactions ORDER BY id DESC LIMIT 100''').fetchall()
    unmatched=[r for r in recent if r['status'] in ('UNMATCHED','MATCHED_PENDING')]
    c.close()
    notice=f'<div class="flash error">{esc(message)}</div>' if message else ''
    pending_rows=[]
    for r in pending:
        pending_rows.append([esc(r['created_at']),esc(r['product_name']),r['sequence_no'],money(r['amount']),money(r['total']),esc(r['payment_count']),esc(r['attended_by']),esc(r['status'])])
    recent_rows=[[esc(r['received_at']),esc(r['trans_id']),esc(r['trans_time']),money(r['amount']),esc(r['msisdn']),esc(r['bill_ref_number'] or ''),esc(r['status'])] for r in recent]
    unmatched_html=[]
    if unmatched:
        c=core.db.c()
        for r in unmatched:
            txn={'trans_id':r['trans_id'],'amount':r['amount'],'trans_time':r['trans_time'],'msisdn':r['msisdn'],'bill_ref_number':r['bill_ref_number']}
            candidates=_recovery_candidates(c,txn,include_time=False)
            opts=''.join(f'<option value="{int(x["allocation_id"])}">{esc(x["product_name"])} · Payment {int(x["sequence_no"])} · {money(x["amount"])} · {esc(x["created_at"])}</option>' for x in candidates)
            action=f'''<div class="muted">{len(candidates)} candidate(s)</div>'''
            if candidates:
                action=f'''<form method="post" action="/mpesa/recovery" class="inline-form">{csrf_input(CURRENT_HANDLER)}<input type="hidden" name="action" value="match"><input type="hidden" name="trans_id" value="{esc(r['trans_id'])}"><select name="allocation_id" required>{opts}</select><button class="btn gold">MATCH & COMPLETE</button></form>'''
            ccustomers=c.execute('SELECT id,full_name,phone FROM customers WHERE active=1 ORDER BY full_name').fetchall()
            cproducts=c.execute('SELECT id,name,sell_price FROM products WHERE active=1 ORDER BY name').fetchall()
            cust_opts=''.join(f'<option value="{int(x["id"])}">{esc(x["full_name"])} · {esc(x["phone"])}</option>' for x in ccustomers)
            prod_opts=''.join(f'<option value="{int(x["id"])}">{esc(x["name"])} · {money(x["sell_price"])}</option>' for x in cproducts)
            manual=f'''<form method="post" action="/mpesa/recovery" style="margin-top:7px;display:grid;grid-template-columns:1fr 1fr auto;gap:6px;align-items:end">{csrf_input(CURRENT_HANDLER)}<input type="hidden" name="action" value="manual_reconcile"><input type="hidden" name="trans_id" value="{esc(r['trans_id'])}"><div><label>Customer</label><select name="customer_id" required><option value="">SELECT CUSTOMER</option>{cust_opts}</select></div><div><label>Product</label><select name="product_id" required><option value="">SELECT PRODUCT</option>{prod_opts}</select></div><div><label>Qty</label><input type="number" name="qty" min="1" step="1" value="1" required><button class="btn gold" style="margin-top:6px">MANUAL RECONCILE</button></div></form>'''
            action += manual
            unmatched_html.append([esc(r['trans_id']),money(r['amount']),esc(r['trans_time']),esc(r['msisdn']),esc(r['bill_ref_number'] or ''),action])
        c.close()
    upload_form=f'''<section class="card"><div class="section-head"><h2>Payment Recovery / M-Pesa Reconciliation</h2><span class="pill">ADMINISTRATOR ONLY</span></div><p class="muted">Use this when money reached Till {esc(core.get_setting('mpesa_shortcode','1546252'))} but the corresponding sale did not reach MONTEZ LIQUOR because of a technical failure. Export the Till statement from the M-Pesa Business portal/app and upload the CSV or XLSX here. The system will safely match by M-Pesa code, amount, phone, reference and transaction time, then restore the pending product sale automatically when all required payments are present.</p><form method="post" action="/mpesa/recovery" enctype="multipart/form-data"><input type="hidden" name="action" value="import">{csrf_input(CURRENT_HANDLER)}<div class="form-grid"><div><label>M-Pesa Statement (CSV / XLSX)</label><input type="file" name="file" accept=".csv,.xlsx" required></div></div><div class="actions" style="margin-top:12px"><button class="btn gold" type="submit">UPLOAD & AUTO-MATCH PAYMENTS</button><a class="btn" href="/mpesa/recovery/template.csv">DOWNLOAD CSV TEMPLATE</a></div></form><div class="actions" style="margin-top:10px"><form method="post" action="/mpesa/recovery">{csrf_input(CURRENT_HANDLER)}<input type="hidden" name="action" value="retry"><button class="btn" type="submit">RETRY UNMATCHED MATCHES</button></form></div></section>'''
    pending_section=f'<section class="card" style="margin-top:14px"><div class="section-head"><h3>Pending Product Payments</h3><span class="pill">AUTO MATCH TARGETS</span></div>{html_table(["Created","Product Name","Payment #","Payment Amount","Sale Total","Payments","Started By","Status"],pending_rows,"No pending M-Pesa product payments.")}</section>'
    unmatched_section=''
    if unmatched_html:
        unmatched_section=f'<section class="card" style="margin-top:14px"><div class="section-head"><h3>Unmatched / Review Queue</h3><span class="pill">ADMIN REVIEW</span></div>{html_table(["M-Pesa Code","Amount","Transaction Time","Phone","Reference","Action"],unmatched_html,"No unmatched payments.")}</section>'
    recent_section=f'<section class="card" style="margin-top:14px"><div class="section-head"><h3>Recent Recovered / Imported Payments</h3></div>{html_table(["Received","M-Pesa Code","Transaction Time","Amount","Phone","Reference","Status"],recent_rows,"No imported payments yet.")}</section>'
    return layout(u,'Payment Recovery',notice+upload_form+pending_section+unmatched_section+recent_section,'/mpesa/recovery','Recover payments that arrived at the Till but missed the sale record.')


def recovery_template_csv(handler):
    raw='Receipt No.,Completion Time,Paid In,MSISDN,Short Code,Bill Ref Number\nEXAMPLE123,04/10/2026 14:30:00,170,254712345678,1546252,MONTEZ\n'
    return send_file(handler,raw,'text/csv','MONTEZ_LIQUOR_MPESA_RECOVERY_TEMPLATE.csv')

def mpesa_monitor_page(u):
    if role_name(u)!='ADMIN': raise PermissionError('Administrator permission required.')
    c=core.db.c(); a=c.execute('SELECT created_at,checkout_request_id,amount,phone,status,mpesa_code,updated_at FROM mpesa_stk_requests ORDER BY id DESC LIMIT 100').fetchall(); b=c.execute('SELECT received_at,trans_id,trans_time,amount,msisdn,first_name,last_name,bill_ref_number,status FROM mpesa_transactions ORDER BY id DESC LIMIT 100').fetchall(); c.close()
    return layout(u,'M-Pesa Monitor',f'<section class="card"><h2>Automatic M-Pesa Monitor</h2><p class="muted">STK payments are matched by Checkout Request ID. C2B payments are captured automatically with their M-Pesa receipt code.</p></section><section class="card" style="margin-top:14px"><h3>STK Push Requests</h3>{html_table(["Created","Checkout","Amount","Phone","Status","M-Pesa Code","Updated"],[[esc(r["created_at"]),esc(r["checkout_request_id"]),money(r["amount"]),esc(r["phone"]),esc(r["status"]),esc(r["mpesa_code"] or ""),esc(r["updated_at"])] for r in a],"No STK requests yet.")}</section><section class="card" style="margin-top:14px"><h3>C2B Incoming Payments</h3>{html_table(["Received","M-Pesa Code","Transaction Time","Amount","Phone","Customer","Reference","Status"],[[esc(r["received_at"]),esc(r["trans_id"]),esc(r["trans_time"]),money(r["amount"]),esc(r["msisdn"]),esc((r["first_name"] or "")+" "+(r["last_name"] or "")),esc(r["bill_ref_number"] or ""),esc(r["status"])] for r in b],"No C2B payments received yet.")}</section>','/mpesa/monitor')


def _financial_account_events(account, end, branch_id=None, conn=None):
    """Build a deduplicated cash or Paybill movement stream from recorded business transactions."""
    account=account.upper()
    if account not in ('CASH','PAYBILL'): raise ValueError('Unknown financial account.')
    method='CASH' if account=='CASH' else 'MPESA'
    c=conn or core.db.c()
    events=[]
    try:
        if account=='CASH':
            sql="SELECT sp.paid_at ts,COALESCE(sp.mpesa_code,'') ref,'Sale '||s.receipt_no description,sp.amount amount,s.branch_id branch_id,'IN' direction FROM sale_payments sp JOIN sales s ON s.id=sp.sale_id WHERE UPPER(sp.payment_method)=?"
            args=[method]
            if branch_id is not None: sql+=" AND s.branch_id=?"; args.append(int(branch_id))
            for r in c.execute(sql,args).fetchall(): events.append(dict(r))
            sql="SELECT cp.payment_date ts,COALESCE(cp.reference,cp.mpesa_code,'') ref,'Credit repayment' description,cp.amount amount,s.branch_id branch_id,'IN' direction FROM credit_payments cp JOIN credit_accounts ca ON ca.id=cp.credit_id JOIN sales s ON s.id=ca.sale_id WHERE UPPER(cp.payment_method)=?"
            args=[method]
            if branch_id is not None: sql+=" AND s.branch_id=?"; args.append(int(branch_id))
            for r in c.execute(sql,args).fetchall(): events.append(dict(r))
            sql="SELECT p.purchased_at ts,COALESCE(p.reference,p.purchase_no) ref,'Purchase · '||p.supplier description,p.total amount,p.branch_id branch_id,'OUT' direction FROM purchases p WHERE UPPER(REPLACE(p.payment_method,'-',''))=?"
            args=[method]
            if branch_id is not None: sql+=" AND p.branch_id=?"; args.append(int(branch_id))
            for r in c.execute(sql,args).fetchall(): events.append(dict(r))
            sql="SELECT e.expense_date ts,COALESCE(e.reference,e.expense_no) ref,'Expense · '||e.description description,e.amount amount,e.branch_id branch_id,'OUT' direction FROM expenses e WHERE UPPER(REPLACE(e.payment_method,'-',''))=?"
            args=[method]
            if branch_id is not None: sql+=" AND e.branch_id=?"; args.append(int(branch_id))
            for r in c.execute(sql,args).fetchall(): events.append(dict(r))
            sql="SELECT t.created_at ts,t.mpesa_code ref,'Cash deposited to Paybill · '||t.transfer_no description,t.amount amount,t.branch_id branch_id,'OUT' direction FROM financial_transfers t"
            args=[]
            if branch_id is not None: sql+=" WHERE t.branch_id=?"; args.append(int(branch_id))
            for r in c.execute(sql,args).fetchall(): events.append(dict(r))
            sql="SELECT me.occurred_at ts,me.reference ref,me.description description,me.amount amount,me.branch_id branch_id,me.direction direction FROM financial_manual_entries me WHERE me.account='CASH'"
            args=[]
            if branch_id is not None: sql+=" AND me.branch_id=?"; args.append(int(branch_id))
            for r in c.execute(sql,args).fetchall(): events.append(dict(r))
            if branch_id is None:
                for r in c.execute("SELECT ob.effective_at ts,'OPENING-CASH-'||ob.branch_key ref,'Opening cash balance' description,ob.amount amount,ob.branch_key branch_id,'IN' direction FROM financial_opening_balances ob WHERE ob.account='CASH'").fetchall():
                    events.append(dict(r))
            else:
                for r in c.execute("SELECT ob.effective_at ts,'OPENING-CASH-'||ob.branch_key ref,'Opening cash balance' description,ob.amount amount,ob.branch_key branch_id,'IN' direction FROM financial_opening_balances ob WHERE ob.account='CASH' AND ob.branch_key=?",(int(branch_id),)).fetchall():
                    events.append(dict(r))
        else:
            for r in c.execute("SELECT sp.paid_at ts,COALESCE(sp.mpesa_code,'') ref,'Sale payment · '||s.receipt_no description,sp.amount amount,s.branch_id branch_id,'IN' direction FROM sale_payments sp JOIN sales s ON s.id=sp.sale_id WHERE UPPER(sp.payment_method)='MPESA'").fetchall():
                events.append(dict(r))
            for r in c.execute("SELECT cp.payment_date ts,COALESCE(cp.mpesa_code,cp.reference,'') ref,'Credit repayment' description,cp.amount amount,s.branch_id branch_id,'IN' direction FROM credit_payments cp JOIN credit_accounts ca ON ca.id=cp.credit_id JOIN sales s ON s.id=ca.sale_id WHERE UPPER(cp.payment_method)='MPESA'").fetchall():
                events.append(dict(r))
            for r in c.execute("SELECT p.purchased_at ts,COALESCE(p.reference,p.purchase_no) ref,'Purchase · '||p.supplier description,p.total amount,p.branch_id branch_id,'OUT' direction FROM purchases p WHERE UPPER(REPLACE(p.payment_method,'-',''))='MPESA'").fetchall():
                events.append(dict(r))
            for r in c.execute("SELECT e.expense_date ts,COALESCE(e.reference,e.expense_no) ref,'Expense · '||e.description description,e.amount amount,e.branch_id branch_id,'OUT' direction FROM expenses e WHERE UPPER(REPLACE(e.payment_method,'-',''))='MPESA'").fetchall():
                events.append(dict(r))
            for r in c.execute("SELECT t.created_at ts,t.mpesa_code ref,'Cash deposit received · '||t.transfer_no description,t.amount amount,t.branch_id branch_id,'IN' direction FROM financial_transfers t").fetchall():
                events.append(dict(r))
            # Include Paybill C2B receipts that have not already been posted against a sale or credit payment.
            for r in c.execute("""SELECT COALESCE(NULLIF(mt.trans_time,''),mt.received_at) ts,mt.trans_id ref,'Unmatched Paybill receipt · '||COALESCE(mt.bill_ref_number,'') description,mt.amount amount,NULL branch_id,'IN' direction
                FROM mpesa_transactions mt
                WHERE COALESCE(mt.trans_id,'')<>''
                AND NOT EXISTS(SELECT 1 FROM sale_payments sp WHERE UPPER(COALESCE(sp.mpesa_code,''))=UPPER(mt.trans_id))
                AND NOT EXISTS(SELECT 1 FROM credit_payments cp WHERE UPPER(COALESCE(cp.mpesa_code,''))=UPPER(mt.trans_id))
                AND NOT EXISTS(SELECT 1 FROM financial_transfers ft WHERE UPPER(ft.mpesa_code)=UPPER(mt.trans_id))""").fetchall():
                events.append(dict(r))
            for r in c.execute("SELECT COALESCE(NULLIF(sr.transaction_date,''),sr.updated_at,sr.created_at) ts,COALESCE(sr.mpesa_code,'') ref,'Unallocated STK payment' description,sr.amount amount,NULL branch_id,'IN' direction FROM mpesa_stk_requests sr WHERE UPPER(sr.status) IN ('SUCCESS','COMPLETED','SUCCESSFUL') AND COALESCE(sr.mpesa_code,'')<>'' AND NOT EXISTS(SELECT 1 FROM sale_payments sp WHERE UPPER(COALESCE(sp.mpesa_code,''))=UPPER(sr.mpesa_code)) AND NOT EXISTS(SELECT 1 FROM credit_payments cp WHERE UPPER(COALESCE(cp.mpesa_code,''))=UPPER(sr.mpesa_code))").fetchall():
                events.append(dict(r))
            for r in c.execute("SELECT me.occurred_at ts,me.reference ref,me.description description,me.amount amount,me.branch_id branch_id,me.direction direction FROM financial_manual_entries me WHERE me.account='PAYBILL'").fetchall():
                events.append(dict(r))
            for r in c.execute("SELECT ob.effective_at ts,'OPENING-PAYBILL' ref,'Opening Paybill balance' description,ob.amount amount,NULL branch_id,'IN' direction FROM financial_opening_balances ob WHERE ob.account='PAYBILL' AND ob.branch_key=0").fetchall():
                events.append(dict(r))
    finally:
        if conn is None: c.close()
    # Exclude events beyond the requested statement end and sort chronologically for a running balance.
    for e in events:
        raw_ts=str(e.get('ts') or '')
        if re.fullmatch(r'\d{14}',raw_ts):
            try: e['ts']=dt.datetime.strptime(raw_ts,'%Y%m%d%H%M%S').strftime('%Y-%m-%d %H:%M:%S')
            except ValueError: pass
    events=[e for e in events if str(e.get('ts') or '')<=end]
    events.sort(key=lambda e:(str(e.get('ts') or ''),str(e.get('ref') or '')))
    return events


def financial_statement_data(account, start, end, branch_id=None, conn=None):
    events=_financial_account_events(account,end,branch_id,conn)
    balance=0.0; opening=0.0; total_in=0.0; total_out=0.0; rows=[]
    for e in events:
        amount=round(float(e.get('amount') or 0),2)
        if e['direction']=='IN':
            balance=round(balance+amount,2)
        else:
            balance=round(balance-amount,2)
        if str(e.get('ts') or '')<start:
            opening=balance
            continue
        if str(e.get('ts') or '')<=end:
            if e['direction']=='IN': total_in+=amount
            else: total_out+=amount
            rows.append({'date':e.get('ts') or '', 'reference':e.get('ref') or '', 'description':e.get('description') or '',
                         'branch_id':e.get('branch_id'), 'direction':e['direction'], 'money_in':amount if e['direction']=='IN' else 0.0,
                         'money_out':amount if e['direction']=='OUT' else 0.0, 'balance':balance})
    return {'rows':rows,'opening':opening,'closing':balance,'money_in':round(total_in,2),'money_out':round(total_out,2)}


def ensure_cash_paybill_schema():
    c=core.db.c()
    try:
        c.execute("""CREATE TABLE IF NOT EXISTS cash_paybill_requests(
            id INTEGER PRIMARY KEY AUTOINCREMENT, request_no TEXT NOT NULL UNIQUE,
            branch_id INTEGER NOT NULL REFERENCES branches(id), amount REAL NOT NULL CHECK(amount>0),
            deposit_reference TEXT NOT NULL DEFAULT '', note TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','APPROVED','REJECTED')),
            requested_by INTEGER NOT NULL REFERENCES users(id), requested_at TEXT NOT NULL,
            reviewed_by INTEGER REFERENCES users(id), reviewed_at TEXT, review_note TEXT NOT NULL DEFAULT '',
            transfer_no TEXT UNIQUE, posted_at TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_cash_paybill_req_branch_status ON cash_paybill_requests(branch_id,status,requested_at)")
        c.commit()
    finally:
        c.close()


def cash_paybill_transfer_page(u, message=''):
    ensure_cash_paybill_schema()
    role=role_name(u)
    c=core.db.c()
    try:
        branches=[dict(x) for x in c.execute("SELECT id,name FROM branches WHERE active=1 ORDER BY name").fetchall()]
        if role in ('ADMIN','MANAGEMENT'):
            rows=[dict(x) for x in c.execute("""SELECT r.*,b.name branch_name,ru.full_name requester,au.full_name approver
                FROM cash_paybill_requests r JOIN branches b ON b.id=r.branch_id
                JOIN users ru ON ru.id=r.requested_by LEFT JOIN users au ON au.id=r.reviewed_by
                ORDER BY CASE r.status WHEN 'PENDING' THEN 0 ELSE 1 END,r.id DESC LIMIT 300""").fetchall()]
        else:
            bid=int(u.get('branch_id') or 0)
            if not bid: raise PermissionError('Your account has no assigned branch. Contact Administrator.')
            rows=[dict(x) for x in c.execute("""SELECT r.*,b.name branch_name,ru.full_name requester,au.full_name approver
                FROM cash_paybill_requests r JOIN branches b ON b.id=r.branch_id
                JOIN users ru ON ru.id=r.requested_by LEFT JOIN users au ON au.id=r.reviewed_by
                WHERE r.branch_id=? ORDER BY r.id DESC LIMIT 200""",(bid,)).fetchall()]
    finally:
        c.close()
    notice=f'<div class="flash error">{esc(message)}</div>' if message else ''
    if role in ('ADMIN','MANAGEMENT'):
        branch_field='<div><label>Branch *</label><select name="branch_id" required><option value="">SELECT BRANCH</option>'+''.join(f'<option value="{int(b["id"])}">{esc(b["name"])}</option>' for b in branches)+'</select></div>'
    else:
        bid=int(u.get('branch_id') or 0)
        bname=next((b['name'] for b in branches if int(b['id'])==bid),'Unassigned')
        branch_field=f'<input type="hidden" name="branch_id" value="{bid}"><div><label>Your assigned branch</label><input value="{esc(bname)}" readonly></div>'
    form=f'''<section class="card"><div class="section-head"><h2>Request Cash → Paybill Transfer</h2><span class="pill">PENDING APPROVAL</span></div>
    <p class="muted">Submitting a request does not change balances. Management or Administrator must approve before the system posts the transfer.</p>
    <form method="post" action="/cash-paybill-transfer" class="form-grid">{csrf_input(CURRENT_HANDLER)}<input type="hidden" name="action" value="request">
    {branch_field}<div><label>Amount (KSh) *</label><input type="number" name="amount" min="0.01" step="0.01" required></div>
    <div><label>Paybill deposit reference / M-Pesa code (optional)</label><input name="deposit_reference" maxlength="100"></div>
    <div style="grid-column:1/-1"><label>Reason *</label><input name="note" minlength="5" maxlength="250" required></div>
    <div class="actions"><button class="btn gold">SUBMIT REQUEST</button></div></form></section>'''
    items=[]
    for r in rows:
        action=''
        if role in ('ADMIN','MANAGEMENT') and r['status']=='PENDING':
            action=f'''<form method="post" action="/cash-paybill-transfer">{csrf_input(CURRENT_HANDLER)}<input type="hidden" name="request_id" value="{r["id"]}"><input name="review_note" placeholder="Review / rejection reason"><button class="btn gold" name="action" value="approve" onclick="return confirm('Approve and post this transfer?')">APPROVE & POST</button><button class="btn danger" name="action" value="reject">REJECT</button></form>'''
        items.append([esc(r['request_no']),esc(r['requested_at']),esc(r['branch_name']),money(r['amount']),esc(r['requester']),esc(r['deposit_reference'] or '—'),esc(r['note']),esc(r['status']),esc(r['approver'] or '—'),esc(r['reviewed_at'] or '—'),action or esc(r['review_note'] or '—')])
    table=html_table(['Request','Date','Branch','Amount','Requested By','Deposit Ref.','Reason','Status','Reviewed By','Reviewed At','Action / Note'],items,'No transfer requests yet.')
    note='<div class="notice">Only Management or Administrator can approve or reject. Approval rechecks branch cash and posts equal Cash OUT and Paybill IN entries once only. Transfers are not income or expenses.</div>'
    return layout(u,'Cash → Paybill Requests',notice+form+note+f'<section class="card" style="margin-top:14px"><h2>Transfer Requests</h2>{table}</section>','/cash-paybill-transfer','Request and approve branch cash deposits to Paybill')


def financials_page(u, query=None, message=''):
    if role_name(u)!='ADMIN': raise PermissionError('Administrator permission required.')
    query=query or {}
    account=str(_qsval(query,'account','CASH') or 'CASH').upper()
    if account not in ('CASH','PAYBILL'): account='CASH'
    fd,ft,td,tt,preset=period_from_query(query)
    start=f'{fd} {ft}:00'; end=f'{td} {tt}:59'
    raw_branch=str(_qsval(query,'branch_id','') or '').strip()
    branch_id=int(raw_branch) if raw_branch.isdigit() else None
    c=core.db.c()
    try:
        branches=[dict(r) for r in c.execute('SELECT id,name FROM branches WHERE active=1 ORDER BY name').fetchall()]
        opening=c.execute("SELECT account,branch_key,amount,effective_at FROM financial_opening_balances ORDER BY account,branch_key").fetchall()
    finally: c.close()
    if account=='PAYBILL': branch_id=None
    data=financial_statement_data(account,start,end,branch_id)
    rows=[]
    for r in data['rows']:
        branch_name=next((b['name'] for b in branches if r['branch_id'] is not None and int(b['id'])==int(r['branch_id'])),'Company / Paybill')
        rows.append([esc(r['date']),esc(r['reference']),esc(r['description']),esc(branch_name),
                     money(r['money_in']),money(r['money_out']),money(r['balance'])])
    notice=f'<div class="flash error">{esc(message)}</div>' if message else ''
    account_links=f'<div class="actions" style="margin:12px 0"><a class="btn {"gold" if account=="CASH" else ""}" href="/financials?account=CASH">CASH STATEMENT</a><a class="btn {"gold" if account=="PAYBILL" else ""}" href="/financials?account=PAYBILL">M-PESA PAYBILL STATEMENT</a></div>'
    branch_opts='<option value="">ALL BRANCHES — COMPANY CASH</option>'+''.join(f'<option value="{int(b["id"])}" {"selected" if branch_id==int(b["id"]) else ""}>{esc(b["name"])}</option>' for b in branches)
    branch_filter='' if account=='PAYBILL' else f'<div><label>Cash statement branch</label><select name="branch_id">{branch_opts}</select></div>'
    export=f'/financials/export?account={account}&format={{fmt}}&start={quote(start)}&end={quote(end)}&branch_id={branch_id or ""}'
    statement=f'''<section class="card"><div class="section-head"><h2>{"Cash Statement" if account=="CASH" else "M-Pesa Paybill Statement"}</h2><span class="pill">ADMINISTRATOR ONLY · {esc(preset)}</span></div>
    <form method="get" action="/financials" class="form-grid"><input type="hidden" name="account" value="{account}">{branch_filter}
    <div><label>From date</label><input type="date" name="from_date" value="{esc(fd)}" required></div><div><label>From time</label><input type="time" name="from_time" value="{esc(ft)}"></div>
    <div><label>To date</label><input type="date" name="to_date" value="{esc(td)}" required></div><div><label>To time</label><input type="time" name="to_time" value="{esc(tt)}"></div>
    <div class="actions" style="align-self:end"><button class="btn gold">APPLY FILTER</button></div></form>
    <div class="actions" style="margin:10px 0"><a class="btn" href="/financials?account={account}&preset=TODAY">TODAY</a><a class="btn" href="/financials?account={account}&preset=THIS_WEEK">THIS WEEK</a><a class="btn" href="/financials?account={account}&preset=THIS_MONTH">THIS MONTH</a><a class="btn" href="/financials?account={account}&preset=LAST_MONTH">LAST MONTH</a></div>
    <div class="metric-grid"><div class="metric"><div class="muted">OPENING BALANCE</div><div class="v">{money(data["opening"])}</div></div><div class="metric"><div class="muted">TOTAL MONEY IN</div><div class="v">{money(data["money_in"])}</div></div><div class="metric"><div class="muted">TOTAL MONEY OUT</div><div class="v">{money(data["money_out"])}</div></div><div class="metric"><div class="muted">CLOSING BALANCE</div><div class="v">{money(data["closing"])}</div></div></div>
    <div class="actions" style="margin:12px 0"><button class="btn" type="button" onclick="window.print()">PRINT STATEMENT</button><a class="btn" href="{export.format(fmt="pdf")}">DOWNLOAD PDF</a><a class="btn" href="{export.format(fmt="xlsx")}">DOWNLOAD EXCEL</a></div>
    {html_table(["Date & Time","Transaction Code / Reference","Description","Branch","Money In","Money Out","Running Balance"],rows,"No financial transactions found for this period.")}</section>'''
    transfer_form=f'''<section class="card" style="margin-top:14px"><h2>Cash → Paybill Approval Workflow</h2><p>All users submit requests on the Cash → Paybill Requests page. Management or Administrator approves before posting.</p><a class="btn gold" href="/cash-paybill-transfer">OPEN TRANSFER REQUESTS</a></section>'''
    opening_form=f'''<section class="card" style="margin-top:14px"><div class="section-head"><h2>Set Initial Financial Balances</h2><span class="pill">ONE-TIME SETUP</span></div>
    <p class="muted">Enter verified balances already held when the system's financial tracking starts. Each account/branch can be initialized only once; the entry is retained in the audit trail.</p>
    <form method="post" action="/financials" class="form-grid">{csrf_input(CURRENT_HANDLER)}<input type="hidden" name="action" value="opening">
    <div><label>Account</label><select name="account" id="opening_account" onchange="document.getElementById('opening_branch_wrap').style.display=this.value==='CASH'?'block':'none'"><option value="CASH">Cash</option><option value="PAYBILL">M-Pesa Paybill</option></select></div>
    <div id="opening_branch_wrap"><label>Branch (for cash opening balance)</label><select name="branch_id"><option value="">SELECT BRANCH</option>{''.join(f'<option value="{int(b["id"])}">{esc(b["name"])}</option>' for b in branches)}</select></div>
    <div><label>Verified opening balance (KSh)</label><input type="number" name="amount" min="0" step="0.01" required></div>
    <div><label>Effective date & time</label><input type="datetime-local" name="effective_at" value="{dt.datetime.now(dt.timezone(dt.timedelta(hours=3))).strftime('%Y-%m-%dT%H:%M')}" required></div>
    <div style="grid-column:1/-1"><label>Verification note</label><input name="note" maxlength="250" required></div>
    <div class="actions"><button class="btn gold">SAVE INITIAL BALANCE</button></div></form></section>'''
    manual_form=f'''<section class="card" style="margin-top:14px"><div class="section-head"><h2>Record Other Cash / Paybill Movement</h2><span class="pill">ADMINISTRATOR ONLY</span></div><p class="muted">Use only for a genuine bank/Paybill withdrawal, charge or adjustment not already recorded as a sale, purchase, expense or cash-to-Paybill transfer. Do not duplicate existing transactions.</p><form method="post" action="/financials" class="form-grid">{csrf_input(CURRENT_HANDLER)}<input type="hidden" name="action" value="manual_entry"><div><label>Account</label><select name="account" id="manual_account" onchange="document.getElementById('manual_branch_wrap').style.display=this.value==='CASH'?'block':'none'"><option value="PAYBILL">M-Pesa Paybill</option><option value="CASH">Cash</option></select></div><div><label>Direction</label><select name="direction"><option value="OUT">Money Out / Withdrawal</option><option value="IN">Money In / Adjustment</option></select></div><div id="manual_branch_wrap" style="display:none"><label>Branch (cash only)</label><select name="branch_id"><option value="">SELECT BRANCH</option>{''.join(f'<option value="{int(b["id"])}">{esc(b["name"])}</option>' for b in branches)}</select></div><div><label>Amount (KSh)</label><input type="number" name="amount" min="0.01" step="0.01" required></div><div><label>Transaction reference</label><input name="reference" maxlength="80" required></div><div><label>Date & time</label><input type="datetime-local" name="occurred_at" value="{dt.datetime.now(dt.timezone(dt.timedelta(hours=3))).strftime('%Y-%m-%dT%H:%M')}" required></div><div style="grid-column:1/-1"><label>Description / reason</label><input name="description" maxlength="250" required></div><div class="actions"><button class="btn gold">RECORD FINANCIAL MOVEMENT</button></div></form></section>'''
    opening_rows=[[esc(r['account']),esc(next((b['name'] for b in branches if int(b['id'])==int(r['branch_key'])),'Company / Paybill') if r['account']=='CASH' else 'Company / Paybill'),money(r['amount']),esc(r['effective_at'])] for r in opening]
    opening_section=f'<section class="card" style="margin-top:14px"><h3>Recorded Initial Balances</h3>{html_table(["Account","Branch","Amount","Effective From"],opening_rows,"No initial balances have been entered.")}</section>'
    return layout(u,'Cash & Paybill Statements',notice+account_links+statement+transfer_form+opening_form+manual_form+opening_section,'/financials','Administrator-only cash movement and Paybill reconciliation')


def financials_export(handler,u,query):
    if role_name(u)!='ADMIN': raise PermissionError('Administrator permission required.')
    account=str(_qsval(query,'account','CASH') or 'CASH').upper()
    if account not in ('CASH','PAYBILL'): raise ValueError('Invalid financial account.')
    start=str(_qsval(query,'start','1900-01-01 00:00:00')); end=str(_qsval(query,'end','2999-12-31 23:59:59'))
    raw_branch=str(_qsval(query,'branch_id','') or '').strip()
    branch_id=int(raw_branch) if account=='CASH' and raw_branch.isdigit() else None
    data=financial_statement_data(account,start,end,branch_id)
    headers=['Date & Time','Transaction Code / Reference','Description','Branch','Money In','Money Out','Running Balance']
    c=core.db.c()
    try: branches=[dict(r) for r in c.execute('SELECT id,name FROM branches').fetchall()]
    finally: c.close()
    rows=[['STATEMENT SUMMARY','OPENING BALANCE','','',0,0,data['opening']]]
    for r in data['rows']:
        bn=next((b['name'] for b in branches if r['branch_id'] is not None and int(b['id'])==int(r['branch_id'])),'Company / Paybill')
        rows.append([r['date'],r['reference'],r['description'],bn,r['money_in'],r['money_out'],r['balance']])
    rows.append(['STATEMENT SUMMARY','TOTALS','','',data['money_in'],data['money_out'],data['closing']])
    title='MONTEZ LIQUOR '+('CASH' if account=='CASH' else 'M-PESA PAYBILL')+' STATEMENT'
    out,ctype,filename=_export_rows(title,headers,rows,_qsval(query,'format','xlsx'))
    return send_file(handler,out,ctype,filename)


def financials_post(handler,u,form):
    if role_name(u)!='ADMIN': raise PermissionError('Administrator permission required.')
    action=str(form.get('action','')).lower()
    c=core.db.c()
    try:
        c.execute('BEGIN IMMEDIATE')
        ts=now()
        if action=='transfer':
            raise PermissionError('Direct transfers are disabled. Submit a request for Management or Administrator approval.')
            branch_id=int(form.get('branch_id','0') or 0)
            amount=round(float(form.get('amount','0') or 0),2)
            code=str(form.get('mpesa_code','')).strip().upper()
            deposit_ref=str(form.get('deposit_reference','')).strip()
            note=str(form.get('note','')).strip()
            if branch_id<=0 or amount<=0 or not code or len(note)<3: raise ValueError('Select a branch and enter a positive amount, M-Pesa code and description.')
            branch=c.execute('SELECT name FROM branches WHERE id=? AND active=1',(branch_id,)).fetchone()
            if not branch: raise ValueError('Select an active registered branch.')
            if c.execute('SELECT 1 FROM financial_transfers WHERE UPPER(mpesa_code)=?',(code,)).fetchone(): raise ValueError('This M-Pesa deposit code has already been used for a transfer.')
            if c.execute("SELECT 1 FROM sale_payments WHERE UPPER(COALESCE(mpesa_code,''))=? UNION ALL SELECT 1 FROM credit_payments WHERE UPPER(COALESCE(mpesa_code,''))=? UNION ALL SELECT 1 FROM mpesa_stk_requests WHERE UPPER(COALESCE(mpesa_code,''))=?",(code,code,code)).fetchone(): raise ValueError('This M-Pesa code is already allocated to a sale, credit repayment or STK payment.')
            # Recompute the branch cash balance inside the same write transaction to prevent overdrawing.
            current=financial_statement_data('CASH','1900-01-01 00:00:00','2999-12-31 23:59:59',branch_id,c)['closing']
            if amount>current: raise ValueError(f'Insufficient recorded cash in {branch["name"]}. Available: {money(current)}.')
            transfer_no='TRF-'+dt.datetime.now().strftime('%Y%m%d%H%M%S')+'-'+secrets.token_hex(3).upper()
            c.execute('INSERT INTO financial_transfers(transfer_no,branch_id,amount,mpesa_code,deposit_reference,note,created_at,created_by) VALUES(?,?,?,?,?,?,?,?)',(transfer_no,branch_id,amount,code,deposit_ref,note,ts,u['id']))
            core.db.audit(c,u,'CASH_TO_PAYBILL_TRANSFER','FINANCIAL_TRANSFER',transfer_no,f'branch={branch["name"]}; amount={amount:.2f}; mpesa_code={code}; deposit_reference={deposit_ref}; note={note}; cash_decreased; paybill_increased; no revenue or expense impact')
            c.commit()
            add_flash(session_token(handler),f'Transfer {transfer_no} posted. Cash reduced by {money(amount)} and Paybill increased by the same amount.','success')
            return redirect(handler,'/financials?account=CASH')
        if action=='manual_entry':
            account=str(form.get('account','PAYBILL')).upper()
            direction=str(form.get('direction','OUT')).upper()
            amount=round(float(form.get('amount','0') or 0),2)
            reference=str(form.get('reference','')).strip().upper()
            description=str(form.get('description','')).strip()
            occurred=str(form.get('occurred_at','')).strip().replace('T',' ')
            branch_id=int(form.get('branch_id','0') or 0) if account=='CASH' else None
            if account not in ('CASH','PAYBILL') or direction not in ('IN','OUT') or amount<=0 or not reference or len(description)<5:
                raise ValueError('Enter a valid account, direction, positive amount, reference and description of at least 5 characters.')
            try: dt.datetime.strptime(occurred,'%Y-%m-%d %H:%M')
            except Exception: raise ValueError('Enter a valid transaction date and time.')
            if account=='CASH' and not c.execute('SELECT 1 FROM branches WHERE id=? AND active=1',(branch_id,)).fetchone():
                raise ValueError('Select an active branch for this cash movement.')
            if direction=='OUT':
                available=financial_statement_data(account,'1900-01-01 00:00:00','2999-12-31 23:59:59',branch_id,c)['closing']
                if amount>available: raise ValueError(f'Insufficient recorded {account.lower()} balance. Available: {money(available)}.')
            c.execute('INSERT INTO financial_manual_entries(account,branch_id,direction,amount,reference,description,occurred_at,created_at,created_by) VALUES(?,?,?,?,?,?,?,?,?)',(account,branch_id,direction,amount,reference,description,occurred+':00',ts,u['id']))
            core.db.audit(c,u,'RECORD_FINANCIAL_MOVEMENT','FINANCIAL_ENTRY',reference,f'account={account}; direction={direction}; amount={amount:.2f}; branch_id={branch_id}; description={description}; occurred_at={occurred}')
            c.commit()
            add_flash(session_token(handler),'Financial movement recorded successfully.','success')
            return redirect(handler,'/financials?account='+account)
        if action=='opening':
            account=str(form.get('account','CASH')).upper()
            if account not in ('CASH','PAYBILL'): raise ValueError('Select Cash or Paybill.')
            branch_key=int(form.get('branch_id','0') or 0) if account=='CASH' else 0
            amount=round(float(form.get('amount','-1') or -1),2)
            effective=str(form.get('effective_at','')).strip().replace('T',' ')
            note=str(form.get('note','')).strip()
            if amount<0 or len(note)<5: raise ValueError('Enter a non-negative opening balance and a verification note of at least 5 characters.')
            if account=='CASH' and not c.execute('SELECT 1 FROM branches WHERE id=? AND active=1',(branch_key,)).fetchone(): raise ValueError('Select an active branch for the cash opening balance.')
            try: dt.datetime.strptime(effective,'%Y-%m-%d %H:%M')
            except Exception: raise ValueError('Enter a valid effective date and time.')
            c.execute('INSERT INTO financial_opening_balances(account,branch_key,amount,effective_at,note,created_at,created_by) VALUES(?,?,?,?,?,?,?)',(account,branch_key,amount,effective+':00',note,ts,u['id']))
            core.db.audit(c,u,'SET_FINANCIAL_OPENING_BALANCE','FINANCIAL_OPENING_BALANCE',f'{account}:{branch_key}',f'amount={amount:.2f}; effective_at={effective}; note={note}')
            c.commit()
            add_flash(session_token(handler),'Initial financial balance recorded and locked.','success')
            return redirect(handler,'/financials?account='+account)
        raise ValueError('Unknown financial action.')
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def admin_record_change(u, form):
    entity=(form.get('entity') or '').strip().lower(); action=(form.get('action') or '').strip().lower(); reason=(form.get('reason') or '').strip()
    if role_name(u)!='ADMIN': raise PermissionError('Administrator permission required.')
    if len(reason)<5: raise ValueError('A clear reason of at least 5 characters is required for every edit or deletion.')
    rid=int(form.get('record_id','0') or 0)
    if rid<=0: raise ValueError('Record identifier is invalid.')
    c=core.db.c()
    try:
        c.execute('BEGIN IMMEDIATE'); ts=now()
        if entity=='expense':
            row=c.execute('SELECT * FROM expenses WHERE id=?',(rid,)).fetchone()
            if not row: raise ValueError('Expense record not found.')
            if action=='edit':
                category=form.get('category','').strip(); desc=form.get('description','').strip(); amount=round(float(form.get('amount','0') or 0),2); method=form.get('payment_method','').strip(); ref=form.get('reference','').strip()
                if not category or not desc or amount<=0 or method not in ('Cash','M-Pesa','Bank'): raise ValueError('Enter a valid category, description, amount and payment method.')
                before=f"category={row['category']}; description={row['description']}; amount={row['amount']}; method={row['payment_method']}; reference={row['reference']}"
                c.execute('UPDATE expenses SET category=?,description=?,amount=?,payment_method=?,reference=? WHERE id=?',(category,desc,amount,method,ref,rid)); after=f'category={category}; description={desc}; amount={amount:.2f}; method={method}; reference={ref}'
                core.db.audit(c,u,'ADMIN_EDIT','EXPENSE',rid,f'reason={reason}; before=[{before}]; after=[{after}]')
            elif action=='delete':
                c.execute('DELETE FROM expenses WHERE id=?',(rid,)); core.db.audit(c,u,'ADMIN_DELETE','EXPENSE',rid,f'expense_no={row["expense_no"]}; amount={row["amount"]}; reason={reason}')
            else: raise ValueError('Unknown expense action.')
        elif entity=='purchase':
            row=c.execute('SELECT pu.*,pl.id line_id,pl.product_id,pl.qty,pl.unit_cost,pl.total line_total FROM purchases pu JOIN purchase_lines pl ON pl.purchase_id=pu.id WHERE pu.id=?',(rid,)).fetchone()
            if not row: raise ValueError('Purchase record not found.')
            prod=c.execute('SELECT * FROM products WHERE id=?',(row['product_id'],)).fetchone()
            if not prod: raise ValueError('The product linked to this purchase no longer exists.')
            branch_id=int(row['branch_id'] or 0)
            bs=c.execute('SELECT * FROM branch_stock WHERE branch_id=? AND product_id=?',(branch_id,row['product_id'])).fetchone()
            if not bs: raise ValueError('The branch stock record for this purchase is missing; reconcile the branch stock before editing.')
            old_qty=float(row['qty']); old_cost=float(row['unit_cost']); current_qty=float(bs['qty']); current_cost=float(bs['avg_cost'])
            if action=='edit':
                supplier=form.get('supplier','').strip(); new_qty=int(form.get('qty','0') or 0); new_cost=round(float(form.get('unit_cost','0') or 0),2); method=form.get('payment_method','').strip(); ref=form.get('reference','').strip()
                if not supplier or new_qty<=0 or new_cost<0 or method not in ('Cash','M-Pesa','Credit'): raise ValueError('Enter a valid supplier, quantity, unit cost and payment method.')
                new_stock=current_qty-old_qty+new_qty
                if new_stock<0: raise ValueError('This purchase cannot be edited because its reversal would make stock negative.')
                new_total=round(new_qty*new_cost,2); old_total=old_qty*old_cost
                # Adjust inventory value by the changed purchase value; sale COGS snapshots are deliberately untouched.
                inventory_value=max(0.0,current_qty*current_cost-old_total+new_total)
                new_avg=inventory_value/new_stock if new_stock>0 else 0.0
                before=f'supplier={row["supplier"]}; qty={old_qty:g}; unit_cost={old_cost:.2f}; total={row["total"]:.2f}; method={row["payment_method"]}; reference={row["reference"]}'
                c.execute('UPDATE purchases SET supplier=?,payment_method=?,total=?,reference=? WHERE id=?',(supplier,method,new_total,ref,rid)); c.execute('UPDATE purchase_lines SET qty=?,unit_cost=?,total=? WHERE id=?',(new_qty,new_cost,new_total,row['line_id'])); c.execute('UPDATE branch_stock SET qty=?,avg_cost=?,buy_price=?,updated_at=? WHERE branch_id=? AND product_id=?',(new_stock,new_avg,new_cost,ts,branch_id,row['product_id']))
                c.execute('UPDATE stock_movements SET qty=?,unit_cost=?,after_qty=before_qty+?,reference=? WHERE movement_type="PURCHASE" AND reference=? AND product_id=? AND branch_id=?',(new_qty,new_cost,new_qty,row['purchase_no'],row['purchase_no'],row['product_id'],branch_id))
                after=f'supplier={supplier}; qty={new_qty}; unit_cost={new_cost:.2f}; total={new_total:.2f}; method={method}; reference={ref}; resulting_stock={new_stock:g}; avg_cost={new_avg:.2f}'
                core.db.audit(c,u,'ADMIN_EDIT','PURCHASE',rid,f'reason={reason}; before=[{before}]; after=[{after}]; historical sale COGS snapshots unchanged')
            elif action=='delete':
                # Reverse only the units that are still physically in stock; sold units stay reflected in historical sales.
                qty_reversed=min(current_qty,old_qty)
                new_stock=max(0.0,current_qty-qty_reversed); inventory_value=max(0.0,current_qty*current_cost-qty_reversed*old_cost); new_avg=inventory_value/new_stock if new_stock>0 else 0.0
                c.execute('UPDATE branch_stock SET qty=?,avg_cost=?,updated_at=? WHERE branch_id=? AND product_id=?',(new_stock,new_avg,ts,branch_id,row['product_id']))
                c.execute('DELETE FROM stock_movements WHERE movement_type="PURCHASE" AND reference=? AND product_id=? AND branch_id=?',(row['purchase_no'],row['product_id'],branch_id)); c.execute('DELETE FROM purchases WHERE id=?',(rid,))
                core.db.audit(c,u,'ADMIN_DELETE','PURCHASE',rid,f'purchase_no={row["purchase_no"]}; qty={old_qty:g}; unit_cost={old_cost:.2f}; units_reversed_from_current_stock={qty_reversed:g}; stock_after={new_stock:g}; reason={reason}; historical sale COGS snapshots unchanged')
            else: raise ValueError('Unknown purchase action.')
        elif entity=='sale':
            row=c.execute('SELECT * FROM sales WHERE id=?',(rid,)).fetchone()
            if not row: raise ValueError('Sale record not found.')
            if action=='edit':
                sold_at=form.get('sold_at','').strip().replace('T',' '); sale_type=form.get('sale_type','').upper()
                try: dt.datetime.strptime(sold_at,'%Y-%m-%d %H:%M')
                except Exception: raise ValueError('Enter a valid sale date and time.')
                if sale_type not in ('RETAIL','WHOLESALE'): raise ValueError('Sale channel must be RETAIL or WHOLESALE.')
                c.execute('UPDATE sales SET sold_at=?,sale_type=? WHERE id=?',(sold_at+':00',sale_type,rid)); core.db.audit(c,u,'ADMIN_EDIT','SALE',rid,f'receipt={row["receipt_no"]}; reason={reason}; before_date={row["sold_at"]}; after_date={sold_at}; before_channel={row["sale_type"]}; after_channel={sale_type}; amount and payments unchanged')
            elif action=='delete':
                lines=c.execute('SELECT product_id,qty FROM sale_lines WHERE sale_id=?',(rid,)).fetchall()
                for line in lines:
                    branch_id=int(row['branch_id'] or 0)
                    bs=c.execute('SELECT * FROM branch_stock WHERE branch_id=? AND product_id=?',(branch_id,line['product_id'])).fetchone()
                    if bs:
                        before=float(bs['qty']); after=before+float(line['qty']); c.execute('UPDATE branch_stock SET qty=?,updated_at=? WHERE branch_id=? AND product_id=?',(after,ts,branch_id,line['product_id']))
                        c.execute('INSERT INTO stock_movements(movement_time,user_id,product_id,movement_type,qty,before_qty,after_qty,unit_cost,reference,branch_id) VALUES(?,?,?,?,?,?,?,?,?,?)',(ts,u['id'],line['product_id'],'ADMIN_SALE_REVERSAL',float(line['qty']),before,after,float(bs['avg_cost'] or 0),'DELETE '+row['receipt_no'],branch_id))
                    else:
                        raise ValueError('The branch stock record for this sale is missing; reconcile the branch stock before deleting the sale.')
                c.execute('UPDATE mpesa_stk_requests SET sale_id=NULL WHERE sale_id=?',(rid,)); c.execute('DELETE FROM credit_payments WHERE credit_id IN (SELECT id FROM credit_accounts WHERE sale_id=?)',(rid,)); c.execute('DELETE FROM credit_accounts WHERE sale_id=?',(rid,)); c.execute('DELETE FROM sale_payments WHERE sale_id=?',(rid,)); c.execute('DELETE FROM sale_lines WHERE sale_id=?',(rid,)); c.execute('DELETE FROM sales WHERE id=?',(rid,)); core.db.audit(c,u,'ADMIN_DELETE','SALE',rid,f'receipt={row["receipt_no"]}; amount={row["total"]:.2f}; reason={reason}; sold units returned to stock; payment/credit allocations removed')
            else: raise ValueError('Unknown sale action.')
        else: raise ValueError('Unsupported record type.')
        c.commit()
    except Exception:
        c.rollback(); raise
    finally: c.close()



def subscription_billing_state(today=None):
    today = today or dt.datetime.now(dt.timezone(dt.timedelta(hours=3))).date()
    month = today.strftime('%Y-%m')
    previous = (today.replace(day=1) - dt.timedelta(days=1)).strftime('%Y-%m')
    first = core.get_setting('subscription_first_month','2026-12')
    due_day = max(1,min(28,int(float(core.get_setting('subscription_due_day','5')))))
    if month < first:
        return {'due_month':None,'due_date':None,'suspended':False,'upcoming':True,'paid_month':core.get_setting('subscription_paid_month','')}
    due_month = month if today.day >= due_day else previous
    if due_month < first:
        due_month = month
    due_date = dt.date(int(due_month[:4]),int(due_month[5:]),due_day)
    paid_month = core.get_setting('subscription_paid_month','')
    paid = bool(paid_month and paid_month >= due_month)
    suspended = today >= due_date and not paid
    return {'due_month':due_month,'due_date':due_date,'suspended':suspended,'upcoming':today < due_date and not paid,'paid_month':paid_month,'paid':paid}

def subscription_block_page(state):
    amount = core.get_setting('subscription_monthly_amount','')
    paybill = core.get_setting('subscription_paybill','516600')
    account = core.get_setting('subscription_account','319851')
    due_day = max(1,min(28,int(float(core.get_setting('subscription_due_day','5')))))
    due_day_label=f"{due_day}{'st' if due_day==1 else 'nd' if due_day==2 else 'rd' if due_day==3 else 'th'}"
    due_month = state.get('due_month') or core.get_setting('subscription_first_month','2026-12')
    return f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Subscription overdue · MONTEZ LIQUOR</title><style>{CSS}</style></head><body class="login-page"><main class="login-card"><h1>MONTEZ LIQUOR — ACCESS SUSPENDED</h1><p>Your software subscription payment has not been confirmed. Normal business access is temporarily suspended.</p><section class="card"><h3>Subscription payment details</h3><p><b>Amount:</b> KSh {esc(amount or 'Not configured — contact Administrator')}</p><p><b>Paybill:</b> {esc(paybill)}</p><p><b>Account:</b> {esc(account)}</p><p><b>Billing month:</b> {esc(due_month)}</p><p><b>Due date:</b> {due_day_label} day of the month</p></section><p>After payment, contact your Administrator to verify the M-PESA transaction and restore access.</p><a class="btn" href="/logout">LOG OUT</a><footer class="app-powered">Powered by SHADYTECHSOLUTIONS</footer></main></body></html>'''

def confirm_subscription_payment(u, form):
    if role_name(u) != 'ADMIN':
        raise PermissionError('Only the Administrator can confirm a software subscription payment.')
    reason = str(form.get('change_reason','')).strip()
    code = str(form.get('mpesa_code','')).strip().upper()
    amount_text = str(form.get('amount','')).strip()
    month = str(form.get('billing_month','')).strip()
    if len(reason) < 5: raise ValueError('Enter a reason of at least 5 characters.')
    if not re.fullmatch(r'[A-Z0-9]{8,15}',code): raise ValueError('Enter a valid M-PESA transaction code (8–15 letters or numbers).')
    try: amount = round(float(amount_text),2)
    except Exception: raise ValueError('Enter the amount received.')
    expected = float(core.get_setting('subscription_monthly_amount','0') or 0)
    if expected <= 0: raise ValueError('Set the monthly subscription amount in Administrator Settings first.')
    if abs(amount-expected) > 0.009: raise ValueError(f'Payment amount must match the configured subscription fee of KSh {expected:,.2f}.')
    state=subscription_billing_state()
    if not state.get('due_month') or month != state['due_month']: raise ValueError('The billing month does not match the current subscription cycle.')
    c=core.db.c()
    try:
        c.execute('BEGIN IMMEDIATE')
        duplicate=c.execute('SELECT id FROM mpesa_transactions WHERE upper(trans_id)=?',(code,)).fetchone()
        if duplicate: raise ValueError('This M-PESA transaction code already exists in the business payment records.')
        old=core.get_setting('subscription_paid_month','')
        vals={'subscription_paid_month':month,'subscription_last_mpesa_code':code,'subscription_last_paid_amount':f'{amount:.2f}','subscription_last_paid_at':now(),'subscription_last_paid_by':str(u['id'])}
        for key,value in vals.items():
            c.execute('INSERT INTO settings(key,value,updated_at,updated_by) VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at,updated_by=excluded.updated_by',(key,value,now(),u['id']))
        core.db.audit(c,u,'SUBSCRIPTION_PAYMENT_CONFIRMED','SOFTWARE_SUBSCRIPTION',month,f'amount={amount:.2f}; mpesa_code={code}; previous_paid_month={old}; reason={reason}; manually verified by Administrator')
        c.commit()
    except Exception:
        c.rollback(); raise
    finally: c.close()

class Handler(BaseHTTPRequestHandler):
    server_version='MONTEZ-LIQUOR/1.0'
    def log_message(self, fmt,*args):
        return
    def do_GET(self):
        global CURRENT_HANDLER
        CURRENT_HANDLER=self
        path=urlparse(self.path).path; qs=parse_qs(urlparse(self.path).query)
        try:
            if path in ('/manifest.json','/logo.svg','/business-logo','/sw.js'):
                if path=='/manifest.json': return send_file(self,MANIFEST,'application/manifest+json','manifest.json','inline')
                if path=='/logo.svg': return send_file(self,LOGO,'image/svg+xml','logo.svg','inline')
                if path=='/business-logo':
                    data,ctype=business_logo_bytes(); return send_file(self,data,ctype,'business-logo','inline')
                return send_file(self,SW,'application/javascript','sw.js','inline')
            if path=='/mpesa/recovery/template.csv': return recovery_template_csv(self)
            if path=='/health': return send_file(self,'OK','text/plain; charset=utf-8','health.txt','inline')
            if path=='/login': return send_html(self,login_page())
            if path=='/ping':
                u=session_user(self, touch=True)
                return send_file(self,'OK','text/plain; charset=utf-8','ping.txt','inline') if u else send_html(self,login_page('Session expired.'),401)
            if path=='/api/customers/search':
                u=session_user(self, touch=True)
                if not u: return send_json(self,{'error':'Session expired. Please sign in again.'},401)
                billing=subscription_billing_state()
                if billing.get('suspended') and role_name(u)!='ADMIN': return send_json(self,{'error':'Software subscription overdue. Contact Administrator.'},402)
                require_perm_user(u,'CREATE_SALE'); q=(_qsval(qs,'q','') or '').strip()
                if not q: return send_json(self,{'results':[]})
                branch_id=branch_scope_for(u); customer_scope=' AND branch_id=?' if branch_id is not None else ''; args=(f'%{q}%',f'%{q}%',f'%{q}%',f'%{q}%',q,q,f'{q}%')+((int(branch_id),) if branch_id is not None else ())
                c=core.db.c(); like=f'%{q}%'; rows=c.execute("SELECT id,full_name,phone,id_no,address FROM customers WHERE active=1 AND (id_no LIKE ? OR full_name LIKE ? OR phone LIKE ? OR customer_no LIKE ?)"+customer_scope+" ORDER BY CASE WHEN id_no=? THEN 0 WHEN phone=? THEN 0 WHEN lower(full_name) LIKE lower(?) THEN 0 ELSE 1 END, full_name LIMIT 30",args).fetchall(); c.close(); return send_json(self,{'results':[dict(r) for r in rows]})
            if path=='/logout': return do_logout(self, _qsval(qs,'auto','0')=='1')
            u=session_user(self)
            if not u: return redirect(self,'/login')
            billing=subscription_billing_state()
            if billing.get('suspended') and role_name(u)!='ADMIN': return send_html(self,subscription_block_page(billing),402)
            if path=='/api/mpesa/stk-status': return send_json(self,mpesa_stk_status(_qsval(qs,'checkout_request_id'),u,qs))
            if path=='/api/mpesa/live-messages': return send_json(self,mpesa_live_messages_json(u,qs))
            if path=='/cash-paybill-transfer': return send_html(self,cash_paybill_transfer_page(u))
            if path=='/financials':
                if role_name(u)!='ADMIN': raise PermissionError('Administrator permission required.')
                return send_html(self,financials_page(u,qs))
            if path=='/financials/export': return financials_export(self,u,qs)
            if path=='/mpesa/monitor': return send_html(self,mpesa_monitor_page(u))
            if path=='/mpesa/recovery': return send_html(self,mpesa_recovery_page(u))
            if path in ('/','/sales-dashboard') and core.get_setting('dashboard_enabled','YES')!='YES': raise PermissionError('Dashboard access is disabled by the Administrator.')
            if path=='/sales/receipt': return send_html(self,sale_receipt_page(u,_qsval(qs,'receipt')),200)
            if path=='/sales/export': return export_sale(self,u,qs)
            if path=='/report/export': return export_report_v2(self,u,qs)
            if path=='/ledger/export': return export_ledger_v2(self,u,qs)
            if path=='/pl/export': return export_pl_v2(self,u,qs)
            if path=='/backup/download': return self.download_backup(u)
            routes={
                '/':home_page,'/sales':sales_page,'/sales-dashboard':sales_dashboard,
                '/purchases':purchase_page,'/expenses':expense_page,'/expense-report':expense_report_page,
                '/customers':customers_page,'/reports':reports_page,'/stock-report':stock_report_page,
                '/reorder-report':reorder_report_page,'/stock-transfers':stock_transfers_page,'/live':live_page,'/products':products_page,
                '/users':users_page,'/branches':branches_page,'/branch-performance':branch_performance_page,'/settings':settings_page,'/ledger':ledger_page,
                '/profit-loss':pl_page,'/backup':backup_page,'/audit':audit_page,'/ai':ai_page}
            if path=='/change-password': return send_html(self,change_password_page(u))
            if path in routes:
                if path=='/': return send_html(self,home_page(u,qs))
                if path in ('/reports','/stock-report','/branch-performance','/products','/purchases','/expenses','/stock-transfers','/customers','/sales-dashboard'):
                    return send_html(self, routes[path](u, query=qs))
                if path in ('/expense-report','/ledger','/profit-loss','/reorder-report','/live'):
                    return send_html(self, routes[path](u, qs))
                if path=='/sales': return send_html(self,sales_page(u, selected_customer_id=_qsval(qs,'customer_id','') or None, query=qs))
                return send_html(self,routes[path](u))
            return send_html(self,error_page('Not found',404),404)
        except PermissionError as e:
            if str(e)=='Please sign in.': return redirect(self,'/login')
            return send_html(self,error_page(str(e),403),403)
        except Exception as e:
            print(f'[MONTEZ ERROR] GET {path}: {type(e).__name__}: {e}', flush=True)
            traceback.print_exc()
            return send_html(self,error_page(str(e),500),500)

    def do_POST(self):
        global CURRENT_HANDLER
        CURRENT_HANDLER=self
        path=urlparse(self.path).path; form=parse_form(self)
        try:
            if path=='/login': return self.login_post(form)
            u=require_user(self)
            if not check_csrf(self, form):
                if path == '/customers/credit-payment': return send_json(self, {'error':'Invalid form token. Please refresh the page and try again.'},400)
                return send_html(self, error_page('Invalid form token. Please refresh and try again.',400),400)
            if path=='/cash-paybill-transfer':
                try:
                    ensure_cash_paybill_schema()
                    action=str(form.get('action','request')).lower()
                    role=role_name(u)
                    c=core.db.c()
                    try:
                        c.execute('BEGIN IMMEDIATE')
                        ts=now()
                        if action=='request':
                            submitted_branch=int(form.get('branch_id','0') or 0)
                            branch_id=int(u.get('branch_id') or 0) if role=='SALES' else submitted_branch
                            amount=round(float(form.get('amount','0') or 0),2)
                            deposit_ref=str(form.get('deposit_reference','')).strip().upper()
                            note=str(form.get('note','')).strip()
                            if amount<=0 or len(note)<5:
                                raise ValueError('Enter a positive amount and a reason of at least 5 characters.')
                            if role=='SALES' and submitted_branch!=branch_id:
                                raise PermissionError('Salespeople can request transfers only for their assigned branch.')
                            branch=c.execute('SELECT id,name FROM branches WHERE id=? AND active=1',(branch_id,)).fetchone()
                            if not branch:
                                raise ValueError('Select an active registered branch.')
                            request_no='CPB-REQ-'+dt.datetime.now().strftime('%Y%m%d%H%M%S')+'-'+secrets.token_hex(3).upper()
                            c.execute('''INSERT INTO cash_paybill_requests(request_no,branch_id,amount,deposit_reference,note,status,requested_by,requested_at)
                                VALUES(?,?,?,?,?,'PENDING',?,?)''',(request_no,branch_id,amount,deposit_ref,note,u['id'],ts))
                            core.db.audit(c,u,'CASH_TO_PAYBILL_REQUEST','CASH_PAYBILL_REQUEST',request_no,f'branch={branch["name"]}; amount={amount:.2f}; deposit_reference={deposit_ref}; status=PENDING; balances_unchanged')
                            c.commit()
                            add_flash(session_token(self),f'Request {request_no} submitted as PENDING. No balances have changed.','success')
                        elif action in ('approve','reject'):
                            if role not in ('ADMIN','MANAGEMENT'):
                                raise PermissionError('Only Management or Administrator can approve or reject transfer requests.')
                            request_id=int(form.get('request_id','0') or 0)
                            row=c.execute('SELECT r.*,b.name branch_name FROM cash_paybill_requests r JOIN branches b ON b.id=r.branch_id WHERE r.id=?',(request_id,)).fetchone()
                            if not row:
                                raise ValueError('Transfer request was not found.')
                            if row['status']!='PENDING':
                                raise ValueError('This request has already been reviewed and cannot be posted again.')
                            if int(row['requested_by'])==int(u['id']):
                                raise PermissionError('You cannot approve your own transfer request.')
                            review_note=str(form.get('review_note','')).strip()
                            if action=='reject':
                                if len(review_note)<5:
                                    raise ValueError('Enter a rejection reason of at least 5 characters.')
                                cur=c.execute('UPDATE cash_paybill_requests SET status="REJECTED",reviewed_by=?,reviewed_at=?,review_note=? WHERE id=? AND status="PENDING"',(u['id'],ts,review_note,request_id))
                                if cur.rowcount!=1:
                                    raise ValueError('This request has already been reviewed.')
                                core.db.audit(c,u,'CASH_TO_PAYBILL_REJECT','CASH_PAYBILL_REQUEST',row['request_no'],f'branch={row["branch_name"]}; amount={float(row["amount"]):.2f}; reason={review_note}')
                                c.commit()
                                add_flash(session_token(self),f'Request {row["request_no"]} rejected. No balances changed.','success')
                            else:
                                amount=round(float(row['amount']),2)
                                available=financial_statement_data('CASH','1900-01-01 00:00:00','2999-12-31 23:59:59',int(row['branch_id']),c)['closing']
                                if amount>available:
                                    raise ValueError(f'Insufficient recorded cash in {row["branch_name"]}. Available: {money(available)}. Request remains pending.')
                                deposit_ref=str(row['deposit_reference'] or '').strip().upper()
                                if deposit_ref:
                                    duplicate=c.execute("""SELECT 1 FROM financial_transfers WHERE UPPER(mpesa_code)=?
                                        UNION ALL SELECT 1 FROM sale_payments WHERE UPPER(COALESCE(mpesa_code,''))=?
                                        UNION ALL SELECT 1 FROM credit_payments WHERE UPPER(COALESCE(mpesa_code,''))=?
                                        UNION ALL SELECT 1 FROM mpesa_stk_requests WHERE UPPER(COALESCE(mpesa_code,''))=?""",(deposit_ref,deposit_ref,deposit_ref,deposit_ref)).fetchone()
                                    if duplicate:
                                        raise ValueError('This M-Pesa/deposit reference has already been used. Correct the reference before approval.')
                                transfer_no='TRF-'+dt.datetime.now().strftime('%Y%m%d%H%M%S')+'-'+secrets.token_hex(3).upper()
                                transfer_code=deposit_ref or ('INT'+secrets.token_hex(8).upper())
                                c.execute('''INSERT INTO financial_transfers(transfer_no,branch_id,amount,mpesa_code,deposit_reference,note,created_at,created_by)
                                    VALUES(?,?,?,?,?,?,?,?)''',(transfer_no,int(row['branch_id']),amount,transfer_code,deposit_ref,('Approved request '+row['request_no']+': '+str(row['note']))[:250],ts,u['id']))
                                cur=c.execute('''UPDATE cash_paybill_requests SET status='APPROVED',reviewed_by=?,reviewed_at=?,review_note=?,transfer_no=?,posted_at=?
                                    WHERE id=? AND status='PENDING' ''',(u['id'],ts,review_note,transfer_no,ts,request_id))
                                if cur.rowcount!=1:
                                    raise ValueError('This request was already processed; no duplicate transfer was posted.')
                                core.db.audit(c,u,'CASH_TO_PAYBILL_APPROVE','CASH_PAYBILL_REQUEST',row['request_no'],f'transfer_no={transfer_no}; branch={row["branch_name"]}; amount={amount:.2f}; requested_by={row["requested_by"]}; approved_by={u["id"]}; cash_decreased={amount:.2f}; paybill_increased={amount:.2f}; no_revenue_or_expense_impact')
                                c.commit()
                                add_flash(session_token(self),f'Request {row["request_no"]} approved. Cash decreased and Paybill increased by {money(amount)}.','success')
                        else:
                            raise ValueError('Unknown transfer request action.')
                    except Exception:
                        c.rollback()
                        raise
                    finally:
                        c.close()
                    return redirect(self,'/cash-paybill-transfer')
                except Exception as e:
                    return send_html(self,cash_paybill_transfer_page(u,str(e)),400)
            if path=='/ai':
                try:
                    require_perm_user(u,'AI_ASSISTANT'); q=form.get('question','').strip()
                    if not q: raise ValueError('Enter a question for the AI Assistant.')
                    provider=core.get_setting('ai_provider','LOCAL').upper(); answer=ai_local_answer(u,q) if provider=='LOCAL' else ai_openai_answer(u,q)
                    return send_html(self,ai_page(u,answer),200)
                except Exception as e:
                    return send_html(self,ai_page(u,'AI_ERROR:'+str(e)),400)
            if path=='/api/mpesa/confirm-direct': return send_json(self.mpesa_confirm_direct_post(u,form))
            if path=='/customers/credit-payment': return send_json(self.credit_payment_post(u,form))
            if path=='/sales': return self.sales_post(u,form)
            if path=='/purchases':
                require_perm_user(u,'CREATE_PURCHASE')
                try: no,total=save_purchase(u,form); add_flash(session_token(self),f'Purchase {no} saved successfully. Stock updated in the selected branch.','success'); return redirect(self,'/purchases?branch_id='+str(form.get('branch_id') or u.get('branch_id') or ''))
                except Exception as e: return send_html(self,purchase_page(u,str(e)),400)
            if path=='/expenses':
                try: save_expense(u,form); add_flash(session_token(self),'Expense saved successfully.','success'); return redirect(self,'/expenses?branch_id='+str(form.get('branch_id') or u.get('branch_id') or ''))
                except Exception as e: return send_html(self,expense_page(u,str(e)),400)
            if path=='/stock-transfers':
                if role_name(u) not in ('ADMIN','MANAGEMENT'):
                    raise PermissionError('Stock transfers and cross-branch stock counts are restricted to Management and Administrator.')
                try:
                    action=form.get('action','request')
                    if action=='count':
                        result=stock_count_adjustment(u,form); add_flash(session_token(self),f'Stock count saved for {result["product"]}. System {result["before"]:g}, physical {result["physical"]:g}, variance {result["variance"]:+g}.','success')
                    else: add_flash(session_token(self),stock_transfer_action(u,form),'success')
                    return redirect(self,'/stock-transfers?branch_id='+str(form.get('branch_id') or form.get('source_branch_id') or ''))
                except Exception as e: return send_html(self,stock_transfers_page(u,query={'branch_id':str(form.get('branch_id') or form.get('source_branch_id') or '')},message=str(e)),400)
            if path=='/products':
                if role_name(u) not in ('ADMIN','MANAGEMENT','SALES') or not allowed(u,'MANAGE_PRODUCTS'): raise PermissionError('Product Registration is disabled for your account. Contact Administrator.')
                try:
                    action=form.get('action','create')
                    if action=='create':
                        save_product(u,form); msg='Product saved successfully. Product registered successfully.'
                    elif action=='bulk_save':
                        count=admin_bulk_update_product_costs_prices(u,form); msg=f'Bulk stock update completed. {count} product(s) changed; all edits saved together.'
                    elif action=='edit':
                        admin_update_product(u,form); msg='Product changes saved successfully.'
                    elif action=='stock':
                        admin_set_stock(u,form); msg='Stock correction saved successfully.'
                    elif action=='opening':
                        record_opening_stock(u,form); msg='Branch opening stock recorded successfully.'
                    elif action=='cost':
                        admin_set_cost(u,form); msg='Average unit cost correction saved successfully.'
                    elif action=='delete':
                        admin_product_delete(u,form.get('product_id'),False,form.get('reason','')); msg='Product deleted/archived successfully.'
                    elif action=='restore':
                        admin_product_delete(u,form.get('product_id'),True,form.get('reason','')); msg='Product restored successfully.'
                    else:
                        raise ValueError('Unknown product action.')
                    add_flash(session_token(self),msg,'success'); return redirect(self,'/products?branch_id='+str(form.get('branch_id') or u.get('branch_id') or ''))
                except Exception as e: return send_html(self,products_page(u,str(e),query={'branch_id':str(form.get('branch_id') or '')}),400)
            if path=='/users':
                if role_name(u)!='ADMIN': raise PermissionError('Administrator permission required.')
                try:
                    action=form.get('action','create')
                    if action=='create':
                        save_user(u,form); msg='User created successfully.'
                    elif action=='edit':
                        admin_edit_user(u,form); msg='User changes saved successfully.'
                    elif action=='reset_password':
                        admin_reset_password(u,form.get('user_id'),form.get('new_password','')); msg='Password reset successfully.'
                    elif action=='toggle':
                        admin_toggle_user(u,form.get('user_id'),form.get('active','0')); msg='User access updated successfully.'
                    elif action=='delete':
                        admin_delete_user(u,form.get('user_id')); msg='User deleted or access restricted successfully.'
                    else:
                        raise ValueError('Unknown user action.')
                    add_flash(session_token(self),msg,'success'); return redirect(self,'/users')
                except Exception as e: return send_html(self,users_page(u,str(e)),400)
            if path=='/branches':
                if role_name(u)!='ADMIN': raise PermissionError('Administrator permission required.')
                c=core.db.c()
                try:
                    action=form.get('action','create'); ts=now()
                    if action=='create':
                        name=form.get('name','').strip(); code=form.get('code','').strip().upper() or None; location=form.get('location','').strip(); phone=form.get('phone','').strip()
                        if not name: raise ValueError('Branch name is required.')
                        c.execute('INSERT INTO branches(name,code,location,phone,active,created_at,updated_at) VALUES(?,?,?,?,1,?,?)',(name,code,location,phone,ts,ts)); bid=c.execute('SELECT id FROM branches WHERE name=?',(name,)).fetchone()['id']
                        for pr in c.execute('SELECT id,avg_cost,sell_price,wholesale_price,reorder_level FROM products').fetchall():
                            c.execute('INSERT OR IGNORE INTO branch_stock(branch_id,product_id,qty,avg_cost,buy_price,sell_price,wholesale_price,reorder_level,updated_at) VALUES(?,?,0,?,?,?,?,?,?)',(bid,pr['id'],float(pr['avg_cost'] or 0),float(pr['avg_cost'] or 0),float(pr['sell_price'] or 0),float(pr['wholesale_price'] or pr['sell_price'] or 0),float(pr['reorder_level'] or 0),ts))
                        core.db.audit(c,u,'CREATE','BRANCH',bid,f'{name} code={code or ""}'); c.commit(); msg='Branch registered successfully.'
                    elif action=='toggle':
                        bid=int(form.get('branch_id','0') or 0); active=1 if form.get('active')=='1' else 0
                        br=c.execute('SELECT * FROM branches WHERE id=?',(bid,)).fetchone()
                        if not br: raise ValueError('Branch not found.')
                        if not active and c.execute('SELECT COUNT(*) n FROM users WHERE branch_id=? AND active=1 AND access_role="SALES"',(bid,)).fetchone()['n']:
                            raise ValueError('Reassign or disable all active Sales Persons before deactivating this branch.')
                        c.execute('UPDATE branches SET active=?,updated_at=? WHERE id=?',(active,ts,bid)); core.db.audit(c,u,'UPDATE','BRANCH',bid,f'active={active}'); c.commit(); msg='Branch status updated.'
                    else: raise ValueError('Unknown branch action.')
                    add_flash(session_token(self),msg,'success'); return redirect(self,'/branches')
                except Exception as e:
                    c.rollback(); return send_html(self,branches_page(u,str(e)),400)
                finally: c.close()
            if path=='/settings':
                if role_name(u)!='ADMIN': raise PermissionError('Administrator permission required.')
                try:
                    if form.get('permissions_only')=='1':
                        change_reason=form.get('change_reason','').strip()
                        if len(change_reason)<5: raise ValueError('A reason of at least 5 characters is required for permission changes.')
                        payload=core.permissions_payload(u); roles={'MANAGEMENT':{},'SALES':{}}
                        for d in payload['definitions']:
                            key=d['key']
                            roles['MANAGEMENT'][key]=bool(form.get('perm_m_'+key))
                            roles['SALES'][key]=bool(form.get('perm_s_'+key))
                        core.update_permissions(u,{'roles':roles,'users':{}})
                        c=core.db.c(); core.db.audit(c,u,'UPDATE','PERMISSIONS','',change_reason); c.commit(); c.close()
                        add_flash(session_token(self),'Role permissions saved successfully.','success')
                    else:
                        core.update_settings(u,form); add_flash(session_token(self),'Settings saved successfully.','success')
                    return redirect(self,'/settings')
                except Exception as e: return send_html(self,settings_page(u,str(e)),400)
            if path=='/admin/record-change':
                if role_name(u)!='ADMIN': raise PermissionError('Administrator permission required.')
                admin_record_change(u,form)
                add_flash(session_token(self),'Record updated. The change reason was recorded in the Audit Log.','success')
                target={'sale':'/reports','purchase':'/purchases','expense':'/expenses'}.get(form.get('entity'),'/' )
                return redirect(self,target)
            if path=='/reports': return self.reports_post(u,form)
            return send_html(self,error_page('Not found',404),404)
        except PermissionError as e:
            return send_html(self,error_page(str(e),403),403)
        except Exception as e:
            return send_html(self,error_page(str(e),500),500)
    def login_post(self, form):
        username=form.get('username','').strip(); password=form.get('password','')
        c=core.db.c(); u=c.execute('SELECT * FROM users WHERE username=? AND active=1',(username,)).fetchone(); c.close()
        if not u or core.sha(password)!=u['password_hash']:
            return send_html(self,login_page('Invalid username or password.'),401)
        token=secrets.token_urlsafe(28); SESSIONS[token]={'uid':u['id'],'last_activity':time.time()}; CSRF[token]=secrets.token_urlsafe(18)
        c=core.db.c(); core.db.audit(c,dict(u),'LOGIN','AUTH',u['id'],'Successful login'); c.commit(); c.close()
        self.send_response(303); self.send_header('Location','/'); self.send_header('Set-Cookie',f'kaka_session={token}; Path=/; HttpOnly; SameSite=Lax{COOKIE_SUFFIX}'); self.send_header('Content-Length','0'); self.end_headers()
    def mpesa_confirm_direct_post(self,u,form):
        try:
            return mpesa_confirm_direct_payment(u,form)
        except Exception as e:
            return {'error':str(e)}

    def sales_post(self,u,form):
        require_perm_user(u,'CREATE_SALE')
        try:
            role=role_name(u); branch_id=(int(form.get('branch_id') or 0) if role in ('ADMIN','MANAGEMENT') else int(u.get('branch_id') or 0))
            if not branch_id: raise ValueError('Select an active branch before making a sale.')
            c=core.db.c(); active_branch=c.execute('SELECT 1 FROM branches WHERE id=? AND active=1',(branch_id,)).fetchone(); c.close()
            if not active_branch: raise ValueError('Selected branch is inactive or unavailable.')
            customer_action=form.get('customer_action','').upper()
            if customer_action=='SAVE_NEW_CUSTOMER':
                name=form.get('new_customer_name','').strip(); phone=form.get('new_customer_phone','').strip(); id_no=form.get('new_customer_id_no','').strip(); address=form.get('new_customer_address','').strip()
                if not name or not phone or not id_no: raise ValueError('New credit customer requires full name, phone number and ID number/passport.')
                c=core.db.c(); c.execute('BEGIN')
                try:
                    duplicate=c.execute('SELECT id,full_name,phone,id_no FROM customers WHERE active=1 AND (lower(trim(id_no))=lower(trim(?)) OR replace(replace(replace(phone,' ',''),'-',''),'+','')=replace(replace(replace(?,' ',''),'-',''),'+','')) ORDER BY id LIMIT 1',(id_no,phone)).fetchone()
                    if duplicate:
                        raise ValueError(f'Customer already registered: {duplicate["full_name"]} · Phone {duplicate["phone"]}. Choose EXISTING CUSTOMER and search by ID or phone. No duplicate was created.')
                    ts=now(); no='CUS-'+dt.datetime.now().strftime('%Y%m%d%H%M%S')+'-'+secrets.token_hex(2).upper(); cur=c.execute('INSERT INTO customers(customer_no,full_name,phone,id_no,address,created_at,updated_at,branch_id) VALUES(?,?,?,?,?,?,?,?)',(no,name,phone,id_no,address,ts,ts,branch_id)); cid=cur.lastrowid; core.db.audit(c,u,'CREATE','CUSTOMER',cid,f'{no} {name}'); c.commit()
                except Exception:
                    c.rollback(); raise
                finally: c.close()
                add_flash(session_token(self),f'Customer {name} saved successfully. Continue the credit sale.','success')
                return redirect(self,'/sales?customer_id='+str(cid)+'&branch_id='+str(branch_id))
            if customer_action=='SAVE_EXISTING_CUSTOMER':
                cid=int(form.get('customer_id','0') or 0)
                if not cid: raise ValueError('Search and select an existing customer before saving.')
                c=core.db.c(); found=c.execute('SELECT id,full_name FROM customers WHERE id=? AND active=1'+(' AND branch_id=?' if role_name(u) not in ('ADMIN','MANAGEMENT') else ''),(cid,int(u.get('branch_id') or 0)) if role_name(u) not in ('ADMIN','MANAGEMENT') else (cid,)).fetchone(); c.close()
                if not found: raise ValueError('Selected customer was not found in your branch.')
                add_flash(session_token(self),f'Existing customer {found["full_name"]} saved for this sale.','success')
                return redirect(self,'/sales?customer_id='+str(cid)+'&branch_id='+str(branch_id))
            customer_mode=form.get('customer_mode','WALK_IN').upper(); sale_type=form.get('sale_type','RETAIL').upper()
            if sale_type not in ('RETAIL','WHOLESALE'): raise ValueError('Select Retail or Wholesale sale type.')
            cart=[]; total_expected=0.0; pid=0; qty=0
            line_ids=sorted({int(k.rsplit('_',1)[1]) for k in form.keys() if k.startswith('product_id_') and k.rsplit('_',1)[1].isdigit()})
            if not line_ids: line_ids=[0]
            for ln in line_ids:
                product_key=f'product_id_{ln}' if ln else 'product_id'; qty_key=f'qty_{ln}' if ln else 'qty'
                line_pid=int(form.get(product_key,'0') or 0); line_qty=int(form.get(qty_key,'0') or 0)
                if line_pid<=0 or line_qty<=0: raise ValueError(f'Choose a product and valid quantity on line {ln or 1}.')
                c=core.db.c(); prod=c.execute('SELECT p.*,COALESCE(bs.qty,0) branch_qty,COALESCE(bs.sell_price,p.sell_price) branch_sell_price,COALESCE(bs.wholesale_price,p.wholesale_price) branch_wholesale_price FROM products p LEFT JOIN branch_stock bs ON bs.product_id=p.id AND bs.branch_id=? WHERE p.id=? AND p.active=1',(branch_id,line_pid)).fetchone(); c.close()
                if not prod: raise ValueError(f'Product on line {ln or 1} was not found.')
                if float(prod['branch_qty'])<line_qty: raise ValueError(f'Insufficient stock for {prod["name"]}. Available: {prod["branch_qty"]}.')
                unit_price=float(prod['branch_wholesale_price'] or 0) if sale_type=='WHOLESALE' else float(prod['branch_sell_price'])
                if unit_price<=0: raise ValueError(f'{sale_type.title()} price is not configured for {prod["name"]}.')
                cart.append({'product_id':line_pid,'qty':line_qty}); total_expected+=unit_price*line_qty
                if not pid: pid=line_pid; qty=line_qty
            if customer_mode=='CREDIT':
                if core.get_setting('allow_credit_sales','YES')!='YES': raise ValueError('Credit sales are disabled by Administrator.')
                choice=form.get('credit_customer_choice','NEW').upper(); cid=int(form.get('customer_id','0') or 0) or None
                if choice=='EXISTING':
                    if not cid: raise ValueError('Search by ID number or first name, select the customer, then click SAVE EXISTING CUSTOMER.')
                    customer={'id':cid}
                elif choice=='NEW':
                    name=form.get('new_customer_name','').strip(); phone=form.get('new_customer_phone','').strip(); id_no=form.get('new_customer_id_no','').strip(); address=form.get('new_customer_address','').strip()
                    if not name or not phone or not id_no: raise ValueError('New credit customer requires full name, phone number and ID number/passport.')
                    customer={'full_name':name,'phone':phone,'id_no':id_no,'address':address}
                else: raise ValueError('Select New Credit Customer or Existing Customer.')
                due=form.get('due_date',''); coll=form.get('collateral','').strip(); cv=float(form.get('collateral_value','0') or 0)
                if not due: raise ValueError('Credit due date is required.')
                if not coll: raise ValueError('Collateral held for exchange is required.')
                dt.date.fromisoformat(due)
                d=core.create_sale(u,{'cart':cart,'payments':[],'customer':customer,'credit':{'due_date':due,'collateral':coll,'collateral_value':cv},'sale_type':sale_type,'branch_id':branch_id})
            elif customer_mode=='WALK_IN':
                payment_count=int(form.get('payment_count','1') or 1)
                max_payments=max(1,min(5,int(float(core.get_setting('max_payment_transactions','5')))))
                if not 1<=payment_count<=max_payments: raise ValueError(f'Payment transactions must be between 1 and {max_payments}.')
                payments=[]; all_mpesa=True
                for i in range(1,payment_count+1):
                    amt=float(form.get(f'payment_amount_{i}','0') or 0); method=form.get(f'payment_method_{i}','CASH').upper(); code=form.get(f'mpesa_code_{i}','').strip().upper()
                    if amt<=0: raise ValueError(f'Enter a payment amount for transaction {i}.')
                    if method not in ('CASH','MPESA'): raise ValueError(f'Invalid payment method for transaction {i}.')
                    if method=='CASH': all_mpesa=False
                    payments.append({'amount':amt,'payment_method':method,'mpesa_code':code if method=='MPESA' else ''})
                if abs(sum(x['amount'] for x in payments)-total_expected)>0.009: raise ValueError(f'Payment allocation must equal the full sale total of {money(total_expected)}.')
                # Pure M-Pesa sales can be auto-saved once all STK allocations are confirmed.
                if all_mpesa and core.get_setting('mpesa_enabled','NO')=='YES' and not all(x['mpesa_code'] for x in payments):
                    raise ValueError('For automatic M-Pesa sales, send and confirm every M-Pesa payment first. The sale will save automatically when the full amount is confirmed.')
                d=core.create_sale(u,{'cart':cart,'payments':payments,'customer':{},'credit':None,'sale_type':sale_type,'branch_id':branch_id})
            else:
                raise ValueError('Unknown customer type.')
            if d.get('sale_id'):
                codes=[p.get('mpesa_code') for p in d.get('payments',[]) if p.get('mpesa_code')]
                if codes:
                    c=core.db.c(); c.execute("UPDATE mpesa_stk_requests SET sale_id=?,updated_at=? WHERE mpesa_code IN ({}) AND sale_id IS NULL".format(','.join('?' for _ in codes)),tuple([d['sale_id'],core.now_str()]+codes)); c.commit(); c.close()
            add_flash(session_token(self),f'Sale {d["receipt_no"]} saved successfully. Total {money(d["total"])}','success')
            return redirect(self,'/sales?branch_id='+str(branch_id))
        except Exception as e:
            return send_html(self,sales_page(u,str(e),query={'branch_id':str(branch_id) if 'branch_id' in locals() else ''}),400)
    def credit_payment_post(self,u,form):
        require_perm_user(u,'MANAGE_CUSTOMERS')
        c=None
        try:
            credit_id=int(form.get('credit_id','0') or 0); amount=round(float(form.get('amount','0') or 0),2); method=form.get('payment_method','CASH').upper(); ref=form.get('reference','').strip().upper()
            if credit_id<=0 or amount<=0: raise ValueError('Enter a valid payment amount.')
            if method not in ('CASH','MPESA'): raise ValueError('Invalid payment method.')
            if method=='MPESA' and not ref: raise ValueError('M-Pesa transaction code is required for every M-Pesa credit repayment.')
            c=core.db.c(); row=c.execute('SELECT ca.*,s.branch_id FROM credit_accounts ca JOIN sales s ON s.id=ca.sale_id WHERE ca.id=? AND ca.status="OPEN"',(credit_id,)).fetchone()
            if not row: raise ValueError('Credit account was not found or is already closed.')
            if role_name(u) not in ('ADMIN','MANAGEMENT') and int(row['branch_id'] or 0)!=int(u.get('branch_id') or 0): raise PermissionError('This credit invoice belongs to another branch.')
            balance=round(float(row['principal'])-float(row['paid_amount']),2)
            if amount>balance+0.009: raise ValueError(f'Payment cannot exceed the remaining balance of {money(balance)}.')
            if method=='MPESA' and (c.execute('SELECT 1 FROM credit_payments WHERE UPPER(mpesa_code)=?',(ref,)).fetchone() or c.execute('SELECT 1 FROM sale_payments WHERE UPPER(mpesa_code)=?',(ref,)).fetchone()): raise ValueError('This M-Pesa code has already been used for another payment.')
            ts=core.now_str(); c.execute('BEGIN'); c.execute('INSERT INTO credit_payments(credit_id,payment_date,user_id,amount,payment_method,mpesa_code,reference) VALUES(?,?,?,?,?,?,?)',(credit_id,ts,u['id'],amount,method,ref if method=='MPESA' and ref else None,ref)); new_paid=round(float(row['paid_amount'])+amount,2); new_balance=round(float(row['principal'])-new_paid,2); status='CLOSED' if new_balance<=0.009 else 'OPEN'; c.execute('UPDATE credit_accounts SET paid_amount=?,status=?,updated_at=? WHERE id=?',(new_paid,status,ts,credit_id)); core.db.audit(c,u,'CREATE','CREDIT_PAYMENT',credit_id,f'amount={amount:.2f} method={method} mpesa_code={ref if method == 'MPESA' else ''} balance={new_balance:.2f}; invoice updated'); c.commit(); return {'ok':True,'balance_remaining':max(0,new_balance),'status':status}
        except Exception as e:
            if c:
                try: c.rollback(); c.close()
                except Exception: pass
            return {'error':str(e)}
        finally:
            if c:
                try: c.close()
                except Exception: pass

    def reports_post(self,u,form):
        return send_html(self,reports_page(u,form),200)
    def export_report(self,u,qs):
        kind=qs.get('kind',['report'])[0]; fmt=qs.get('format',['xlsx'])[0]; require_perm_user(u,'PRINT_EXPORT'); start=qs.get('start',[''])[0]; end=qs.get('end',[''])[0]
        if not start or not end: raise ValueError('Report period is missing.')
        if kind=='stock':
            require_perm_user(u,'VIEW_STOCK'); bid=int(qs.get('branch_id',['0'])[0] or 0) or None
            if role_name(u) not in ('ADMIN','MANAGEMENT'): bid=int(u.get('branch_id') or 0)
            rows=core.stock_report(start,end,bid); headers=['SKU','Product Name','Category','Opening Stock','Purchased','Sold','Adjustments','Closing Stock','Retail Units','Wholesale Units','Retail Revenue','Wholesale Revenue','Retail Price','Wholesale Price','Avg Cost','Closing Value','Reorder','Status']; data=[[r['sku'],r['name'],r['category'],r['opening'],r['purchased'],r['sold'],r['adjustments'],r['closing'],r['retail_units'],r['wholesale_units'],r['retail_revenue'],r['wholesale_revenue'],r['retail_price'],r['wholesale_price'],r['avg_cost'],r['closing']*r['avg_cost'],r['reorder_level'],r['status']] for r in rows]; title=f"{core.get_setting('business_name',APP_NAME)} · BRANCH STOCK REPORT"
        else:
            require_perm_user(u,'VIEW_REPORTS'); rows=core.report_data(start,end); headers=['Receipt','Date/Time','Customer','Units','Total','Cash','M-Pesa','Attended By']; data=[[r['receipt_no'],r['sold_at'],r['customer'],r['units'],r['total'],r['cash'],r['mpesa'],r['attended_by']] for r in rows]; title=f"{core.get_setting('business_name',APP_NAME)} · {core.get_setting('business_branch_name','')} · SALES REPORT"
        if fmt=='pdf': return send_file(self,make_pdf(title,headers,data),'application/pdf',re.sub(r'[^A-Za-z0-9]+','_',title)+'.pdf')
        return send_file(self,build_xlsx(title,headers,data),'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',re.sub(r'[^A-Za-z0-9]+','_',title)+'.xlsx')
    def export_ledger(self,u,qs):
        if role_name(u)!='ADMIN': raise PermissionError('Administrator permission required.')
        # Recreate ledger as rows for export.
        ym=qs.get('month',[''])[0] or dt.datetime.now().strftime('%Y-%m'); start=f'{ym}-01'; c=core.db.c(); nextm=c.execute("SELECT date(?, '+1 month') next",(start,)).fetchone()['next']; sales=c.execute('SELECT sold_at,receipt_no,total FROM sales WHERE customer_type<>"CREDIT" AND sold_at>=? AND sold_at<?',(start,nextm)).fetchall(); purs=c.execute('SELECT purchased_at,purchase_no,total,supplier,payment_method FROM purchases WHERE purchased_at>=? AND purchased_at<?',(start,nextm)).fetchall(); ex=c.execute('SELECT expense_date,expense_no,amount,description,payment_method FROM expenses WHERE expense_date>=? AND expense_date<?',(start,nextm)).fetchall(); c.close(); rows=[[r['sold_at'],r['receipt_no'],'Sales Revenue','Cash / M-Pesa',r['total'],'Sales Revenue',r['total']] for r in sales]; rows += [[r['purchased_at'],r['purchase_no'],'Inventory Purchase','Inventory',r['total'],'Accounts Payable / Cash / M-Pesa',r['total']] for r in purs]; rows += [[r['expense_date'],r['expense_no'],r['description'],'Expense',r['amount'],r['payment_method'],r['amount']] for r in ex]; headers=['Date/Time','Reference','Description','Debit Account','Debit','Credit Account','Credit']; title=f"{core.get_setting('business_name',APP_NAME)} · {core.get_setting('business_branch_name','')} · GENERAL LEDGER {ym}"
        if qs.get('format',['xlsx'])[0]=='pdf': return send_file(self,make_pdf(title,headers,rows),'application/pdf',f'MONTEZ_LIQUOR_LEDGER_{ym}.pdf')
        return send_file(self,build_xlsx(title,headers,rows),'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',f'MONTEZ_LIQUOR_LEDGER_{ym}.xlsx')
    def download_backup(self,u):
        if role_name(u)!='ADMIN': raise PermissionError('Administrator permission required.')
        backup_dir=os.path.join(DATA_DIR,'backups'); os.makedirs(backup_dir,exist_ok=True)
        fn=f'MONTEZ_LIQUOR_BACKUP_{dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")}.db'
        dest=os.path.join(backup_dir,fn)
        src=None; target=None
        try:
            # SQLite's backup API includes committed WAL pages in a consistent snapshot.
            src=sqlite3.connect(core.DB_PATH,timeout=30)
            target=sqlite3.connect(dest,timeout=30)
            src.backup(target,pages=256,sleep=0.05)
            integrity=target.execute('PRAGMA integrity_check').fetchone()
            target.commit()
            if not integrity or integrity[0]!='ok':
                raise RuntimeError('Backup integrity check failed; no backup was downloaded.')
        except Exception:
            try:
                if target: target.close()
            except Exception: pass
            try:
                if src: src.close()
            except Exception: pass
            try: os.remove(dest)
            except OSError: pass
            raise
        finally:
            try:
                if target: target.close()
            except Exception: pass
            try:
                if src: src.close()
            except Exception: pass
        with open(dest,'rb') as f: data=f.read()
        if not data.startswith(b'SQLite format 3\x00'):
            try: os.remove(dest)
            except OSError: pass
            raise RuntimeError('The generated backup is not a valid SQLite database.')
        c=core.db.c()
        try:
            core.db.audit(c,u,'DATABASE_BACKUP','DATABASE',fn,f'Created consistent SQLite snapshot; bytes={len(data)}')
            c.commit()
        finally: c.close()
        return send_file(self,data,'application/vnd.sqlite3',fn)


def change_password_page(u,message=''):
    notice=f'<div class="flash error">{esc(message)}</div>' if message else ''
    content=f'''{notice}<section class="card narrow"><h2>Change Password</h2><form method="post" action="/change-password">{csrf_input(CURRENT_HANDLER)}<div><label>Current Password</label><input type="password" name="old_password" required></div><div style="margin-top:10px"><label>New Password</label><input type="password" name="new_password" minlength="8" required></div><div style="margin-top:10px"><label>Confirm New Password</label><input type="password" name="confirm_password" minlength="8" required></div><button class="btn gold" style="margin-top:12px">SAVE PASSWORD</button></form></section>'''
    return layout(u,'Change Password',content,'/change-password')


def error_page(message, code): return f'<section class="card"><div class="flash error">{esc(message)}</div><a class="btn" href="/">BACK TO DASHBOARD</a></section>'

# Additional POST route for password change, injected as method.
def _change_password_post(self,u,form):
    if not check_csrf(self,form): return send_html(self,error_page('Invalid form token.',400),400)
    old=form.get('old_password',''); new=form.get('new_password',''); confirm=form.get('confirm_password',''); c=core.db.c(); r=c.execute('SELECT password_hash FROM users WHERE id=?',(u['id'],)).fetchone();
    if core.sha(old)!=r['password_hash']:
        c.close(); return send_html(self,change_password_page(u,'Current password is incorrect.'),400)
    if len(new)<8 or new!=confirm:
        c.close(); return send_html(self,change_password_page(u,'New password must be at least 8 characters and must match confirmation.'),400)
    c.execute('UPDATE users SET password_hash=?,updated_at=? WHERE id=?',(core.sha(new),now(),u['id'])); core.db.audit(c,u,'PASSWORD_CHANGE','USER',u['id'],'Password changed'); c.commit(); c.close(); add_flash(session_token(self),'Password changed successfully.','success'); return redirect(self,'/')

old_do_post=Handler.do_POST
def do_POST(self):
    global CURRENT_HANDLER; CURRENT_HANDLER=self; path=urlparse(self.path).path
    self._multipart_upload=None
    # Dedicated JSON-only route: every failure, including expired sessions and
    # form parsing errors, must be returned as JSON (never the normal HTML page).
    if path == '/customers/credit-payment':
        try:
            form = parse_form(self)
            u = require_user(self)
            # Prefer the normal CSRF token. If server-side CSRF state was reset
            # during a deployment/restart, permit only an authenticated same-origin
            # AJAX request as a safe fallback; never accept a cross-origin request.
            if not check_csrf(self, form) and not same_origin_request(self):
                return send_json(self, {'error':'This payment request could not be verified. Reload Customers and retry.'}, 400)
            result = self.credit_payment_post(u, form)
            if not isinstance(result, dict):
                return send_json(self, {'error':'Unexpected server result while updating the credit payment.'}, 500)
            if result.get('error'):
                message = str(result.get('error') or 'Unable to update credit payment.')
                status = 403 if 'permission' in message.lower() else (400 if isinstance(result, dict) else 500)
                return send_json(self, {'error':message}, status)
            return send_json(self, result, 200)
        except PermissionError as e:
            return send_json(self, {'error':str(e) or 'Please sign in again.'}, 401)
        except Exception as e:
            return send_json(self, {'error':str(e) or 'Unable to update credit payment. Please check the invoice before retrying.'}, 500)
    try:
        if path in ('/api/mpesa/stk/callback','/api/mpesa/c2b/confirmation','/api/mpesa/c2b/validation'):
            payload=core.json_body(self)
            if path=='/api/mpesa/stk/callback': mpesa_receive_stk(payload)
            else: mpesa_receive_c2b(payload)
            return send_json(self,{'ResultCode':0,'ResultDesc':'Accepted'})
        if path in ('/settings/logo','/mpesa/recovery') and self.headers.get('Content-Type','').startswith('multipart/form-data'):
            form,self._multipart_upload=parse_multipart_form(self)
        else:
            form=parse_form(self)
        if path=='/login': return self.login_post(form)
        u=require_user(self)
        billing=subscription_billing_state()
        if billing.get('suspended') and role_name(u)!='ADMIN': return send_html(self,subscription_block_page(billing),402)
        if path=='/customers/credit-payment':
            if not check_csrf(self,form): return send_json(self,{'error':'Invalid form token. Please refresh the page and try again.'},400)
            try: return send_json(self,self.credit_payment_post(u,form))
            except PermissionError as e: return send_json(self,{'error':str(e)},403)
            except Exception as e: return send_json(self,{'error':str(e) or 'Unable to update credit invoice.'},400)
        if not check_csrf(self,form): return send_html(self,error_page('Invalid form token. Please refresh and try again.',400),400)
        if path=='/ai':
            try:
                require_perm_user(u,'AI_ASSISTANT')
                q=form.get('question','').strip()
                if not q:
                    raise ValueError('Enter a question for the AI Assistant.')
                provider=core.get_setting('ai_provider','LOCAL').upper()
                answer=ai_local_answer(u,q) if provider=='LOCAL' else ai_openai_answer(u,q)
                return send_html(self,ai_page(u,answer),200)
            except Exception as e:
                return send_html(self,ai_page(u,'AI_ERROR:'+str(e)),400)
        if path=='/api/mpesa/stk-request':
            try:
                payload={k:v for k,v in form.items() if k.startswith('payment_amount_')}
                payload.update({'all_mpesa':form.get('all_mpesa'),'product_id':form.get('product_id',''),'qty':form.get('qty',''),'total':form.get('total','0'),'payment_count':form.get('payment_count','1'),'batch_id':form.get('batch_id',''),'sale_type':form.get('sale_type','RETAIL'),'branch_id':form.get('branch_id','')})
                return send_json(self,mpesa_stk_request(u,form.get('amount','0'),form.get('phone',''),payload,int(form.get('sequence_no','1') or 1)))
            except Exception as e:
                return send_json(self,{'error':str(e)},400)
        if path=='/api/mpesa/test-connection':
            try: return send_json(self,mpesa_test_connection(u))
            except Exception as e: return send_json(self,{'error':str(e)},400)
        if path=='/api/mpesa/register-c2b':
            try:
                if role_name(u)!='ADMIN': raise PermissionError('Administrator permission required.')
                return send_json(self,mpesa.register_c2b_urls(core))
            except Exception as e: return send_json(self,{'error':str(e)},400)
        if path=='/change-password': return _change_password_post(self,u,form)
        if path=='/sales': return self.sales_post(u,form)
        if path=='/purchases':
            require_perm_user(u,'CREATE_PURCHASE')
            try: no,total=save_purchase(u,form); add_flash(session_token(self),f'Purchase {no} saved successfully.','success'); return redirect(self,'/purchases')
            except Exception as e: return send_html(self,purchase_page(u,str(e)),400)
        if path=='/expenses':
            require_perm_user(u,'RECORD_EXPENSE')
            try: save_expense(u,form); add_flash(session_token(self),'Expense saved successfully.','success'); return redirect(self,'/expenses')
            except Exception as e: return send_html(self,expense_page(u,str(e)),400)
        if path=='/products':
            if role_name(u) not in ('ADMIN','MANAGEMENT','SALES') or not allowed(u,'MANAGE_PRODUCTS'): raise PermissionError('Product Registration is disabled for your account. Contact Administrator.')
            try:
                action=form.get('action','create')
                if action=='create': save_product(u,form); msg='Product saved successfully. Product registered successfully.'
                elif action=='bulk_save': count=admin_bulk_update_product_costs_prices(u,form); msg=f'Bulk stock update completed. {count} product(s) changed; all edits saved together.'
                elif action=='edit': admin_update_product(u,form); msg='Product changes saved successfully.'
                elif action=='stock': admin_set_stock(u,form); msg='Stock correction saved successfully.'
                elif action=='delete': admin_product_delete(u,form.get('product_id'),False,form.get('reason','')); msg='Product deleted/archived successfully.'
                elif action=='restore': admin_product_delete(u,form.get('product_id'),True,form.get('reason','')); msg='Product restored successfully.'
                else: raise ValueError('Unknown product action.')
                add_flash(session_token(self),msg,'success'); return redirect(self,'/products')
            except Exception as e: return send_html(self,products_page(u,str(e)),400)
        if path=='/users':
            if role_name(u)!='ADMIN': raise PermissionError('Administrator permission required.')
            try:
                action=form.get('action','create')
                if action=='create': save_user(u,form); msg='User created successfully.'
                elif action=='edit': admin_edit_user(u,form); msg='User changes saved successfully.'
                elif action=='reset_password': admin_reset_password(u,form.get('user_id'),form.get('new_password','')); msg='Password reset successfully.'
                elif action=='toggle': admin_toggle_user(u,form.get('user_id'),form.get('active','0')); msg='User access updated successfully.'
                elif action=='delete': admin_delete_user(u,form.get('user_id')); msg='User deleted or access restricted successfully.'
                else: raise ValueError('Unknown user action.')
                add_flash(session_token(self),msg,'success'); return redirect(self,'/users')
            except Exception as e: return send_html(self,users_page(u,str(e)),400)
        if path=='/subscription/confirm':
            try:
                confirm_subscription_payment(u,form)
                add_flash(session_token(self),'Subscription payment confirmed. Normal access has been restored.','success')
                return redirect(self,'/settings')
            except Exception as e:
                return send_html(self,settings_page(u,str(e)),400)
        if path=='/settings':
            if role_name(u)!='ADMIN': raise PermissionError('Administrator permission required.')
            try:
                if form.get('subscription_settings_only')=='1':
                    reason=form.get('change_reason','').strip()
                    if len(reason)<5: raise ValueError('A reason of at least 5 characters is required for subscription settings.')
                    amount=float(form.get('subscription_monthly_amount','0') or 0)
                    if not (0 < amount <= 10000000): raise ValueError('Enter a monthly subscription amount greater than zero and no more than KSh 10,000,000.')
                    core.update_settings(u,{'subscription_monthly_amount':f'{amount:.2f}','change_reason':reason})
                    add_flash(session_token(self),'Monthly subscription settings saved.','success')
                    return redirect(self,'/settings')
                if form.get('permissions_only')=='1':
                    change_reason=form.get('change_reason','').strip()
                    if len(change_reason)<5: raise ValueError('A reason of at least 5 characters is required for permission changes.')
                    payload=core.permissions_payload(u); roles={'MANAGEMENT':{},'SALES':{}}
                    for d in payload['definitions']:
                        key=d['key']; roles['MANAGEMENT'][key]=bool(form.get('perm_m_'+key)); roles['SALES'][key]=bool(form.get('perm_s_'+key))
                    core.update_permissions(u,{'roles':roles,'users':{}}); c=core.db.c(); core.db.audit(c,u,'UPDATE','PERMISSIONS','',change_reason); c.commit(); c.close(); msg='Role permissions saved successfully.'
                else:
                    core.update_settings(u,form); msg='Settings saved successfully.'
                add_flash(session_token(self),msg,'success'); return redirect(self,'/settings')
            except Exception as e: return send_html(self,settings_page(u,str(e)),400)
        if path=='/financials':
            try: return financials_post(self,u,form)
            except Exception as e: return send_html(self,financials_page(u,{'account':'CASH'},str(e)),400)
        if path=='/mpesa/recovery':
            if role_name(u)!='ADMIN': raise PermissionError('Administrator permission required.')
            try:
                action=form.get('action','import')
                if action=='retry':
                    summary=retry_unmatched_recovery(u)
                elif action=='match':
                    summary=manual_match_recovery(u,form.get('trans_id',''),form.get('allocation_id',''))
                elif action=='manual_reconcile':
                    summary=manual_reconcile_recovery(u,form.get('trans_id',''),form.get('customer_id',''),form.get('product_id',''),form.get('qty','1'))
                else:
                    upload=getattr(self,'_multipart_upload',None)
                    if not upload or not upload.get('data'):
                        raise ValueError('Choose an M-Pesa statement file to upload.')
                    summary=import_mpesa_recovery(u,upload)
                add_flash(session_token(self),summary,'success')
                return redirect(self,'/mpesa/recovery')
            except Exception as e:
                return send_html(self,mpesa_recovery_page(u,str(e)),400)
        if path=='/settings/logo':
            if role_name(u)!='ADMIN': raise PermissionError('Administrator permission required.')
            try:
                if form.get('remove_logo')=='1':
                    reason=form.get('change_reason','').strip()
                    if len(reason)<5: raise ValueError('A reason of at least 5 characters is required to remove the logo.')
                    old=business_logo_path()
                    if old:
                        try: os.remove(old)
                        except OSError: pass
                    core.update_settings(u,{'business_logo_filename':'','change_reason':reason}); add_flash(session_token(self),'Custom business logo removed.','success'); return redirect(self,'/settings')
                reason=form.get('change_reason','').strip()
                if len(reason)<5: raise ValueError('A reason of at least 5 characters is required to change the logo.')
                upload=getattr(self,'_multipart_upload',None)
                if not upload or not upload.get('data'): raise ValueError('Choose a business logo image.')
                ext={'image/png':'.png','image/jpeg':'.jpg','image/webp':'.webp'}.get(upload.get('content_type','').lower())
                if not ext: raise ValueError('Logo must be PNG, JPG, JPEG or WEBP.')
                data=upload['data']
                if len(data)>5*1024*1024: raise ValueError('Logo file must be 5 MB or smaller.')
                filename='business_logo'+ext; target=os.path.join(DATA_DIR,filename)
                for old_ext in ('.png','.jpg','.webp'):
                    old_path=os.path.join(DATA_DIR,'business_logo'+old_ext)
                    if os.path.exists(old_path) and old_path!=target:
                        try: os.remove(old_path)
                        except OSError: pass
                with open(target,'wb') as f: f.write(data)
                core.update_settings(u,{'business_logo_filename':filename,'change_reason':reason}); add_flash(session_token(self),'Business logo uploaded successfully.','success'); return redirect(self,'/settings')
            except Exception as e: return send_html(self,settings_page(u,str(e)),400)
        if path=='/admin/record-change':
            if role_name(u)!='ADMIN': raise PermissionError('Administrator permission required.')
            admin_record_change(u,form)
            add_flash(session_token(self),'Record updated. The change reason was recorded in the Audit Log.','success')
            target={'sale':'/reports','purchase':'/purchases','expense':'/expenses'}.get(form.get('entity'),'/' )
            return redirect(self,target)
        if path=='/reports': return self.reports_post(u,form)
        return send_html(self,error_page('Not found',404),404)
    except PermissionError as e:
        if path=='/customers/credit-payment': return send_json(self,{'error':str(e)},403)
        return send_html(self,error_page(str(e),403),403)
    except Exception as e:
        print(f'[MONTEZ ERROR] POST {path}: {type(e).__name__}: {e}', flush=True)
        traceback.print_exc()
        if path=='/customers/credit-payment': return send_json(self,{'error':str(e) or 'Unable to update credit invoice.'},500)
        return send_html(self,error_page(str(e),500),500)
Handler.do_POST=do_POST


def ip_address():
    try:
        s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.connect(('8.8.8.8',80)); ip=s.getsockname()[0]; s.close(); return ip
    except Exception:
        try:return socket.gethostbyname(socket.gethostname())
        except:return 'YOUR-PC-IP'

MANIFEST=json.dumps({'name':APP_NAME,'short_name':'MONTEZ LIQUOR','start_url':'/','display':'standalone','background_color':'#0d3b2f','theme_color':'#0d3b2f','icons':[{'src':'/business-logo','sizes':'any'}]})
LOGO='''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256"><defs><linearGradient id="g" x1="0" x2="1" y1="0" y2="1"><stop stop-color="#f1d28d"/><stop offset="1" stop-color="#c99635"/></linearGradient></defs><rect width="256" height="256" rx="52" fill="#0d3b2f"/><circle cx="128" cy="128" r="82" fill="url(#g)"/><path d="M103 62h50v16h-8v22c0 11 23 30 23 62 0 26-16 42-40 42s-40-16-40-42c0-32 23-51 23-62V78h-8V62z" fill="#0d3b2f"/><text x="128" y="153" text-anchor="middle" font-family="Arial" font-size="31" font-weight="900" fill="#f1d28d">ML</text></svg>'''
SW='''self.addEventListener("install",e=>self.skipWaiting());self.addEventListener("activate",e=>e.waitUntil(self.clients.claim()));'''

if __name__=='__main__':
    print('='*60); print(APP_NAME); print('SERVER IS RUNNING'); print('='*60)
    print(f'Laptop: http://127.0.0.1:{PORT}'); print(f'Phone (same Wi-Fi): http://{ip_address()}:{PORT}'); print('='*60)
    ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()

# cash-paybill approval workflow changes are staged separately.

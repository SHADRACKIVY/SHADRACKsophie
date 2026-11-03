#!/usr/bin/env python3
"""MONTEZ LIQUOR
Zero-third-party-dependency web application.
Uses Python standard library + SQLite; core business logic is reused from the tested core.
"""
import os, io, json, time, secrets, socket, datetime as dt, html, re, zipfile, mimetypes, traceback
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http import cookies
from urllib.parse import urlparse, parse_qs, quote, urlencode
from urllib import request as urlrequest, error as urlerror

# Keep the database outside the app folder so upgrades do not erase data.
DATA_DIR = os.environ.get('KAKA_DATA_DIR', os.path.join(os.path.expanduser('~'), 'KAKA_LIQUOR_DATA'))
os.makedirs(DATA_DIR, exist_ok=True)
os.environ['KAKA_DATA_DIR'] = DATA_DIR
os.environ.setdefault('KAKA_PORT', os.environ.get('PORT', '5000'))

import kaka_core as core
import mpesa_integration as mpesa
import mpesa_recovery as mpesa_recovery

# Cloud safety: require a private admin password on first deployment.
_admin_password = os.environ.get('KAKA_INITIAL_ADMIN_PASSWORD', '').strip()
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
SECURE_COOKIE = os.environ.get('KAKA_SECURE_COOKIE', '0') == '1'
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
        if not fd: fd=today.isoformat()
        if not td: td=fd
        preset='CUSTOM' if (_qsval(q,'from_date','') or _qsval(q,'to_date','')) else 'TODAY'
        if preset=='TODAY' and not _qsval(q,'from_date','') and not _qsval(q,'to_date',''):
            ft='00:00'; tt='23:59'
    try:
        dt.date.fromisoformat(fd); dt.date.fromisoformat(td)
        dt.datetime.strptime(ft, '%H:%M'); dt.datetime.strptime(tt, '%H:%M')
    except ValueError:
        fd=td=today.isoformat(); ft='00:00'; tt='23:59'; preset='TODAY'
    return fd,ft,td,tt,preset

def range_toolbar(action, fd, ft, td, tt):
    return ('<form method="get" action="%s" class="form-grid" style="align-items:end">'
            '<div><label>From Date</label><input type="date" name="from_date" value="%s"></div>'
            '<div><label>From Time</label><input type="time" name="from_time" value="%s"></div>'
            '<div><label>To Date</label><input type="date" name="to_date" value="%s"></div>'
            '<div><label>To Time</label><input type="time" name="to_time" value="%s"></div>'
            '<div><button class="btn gold" type="submit">APPLY</button></div></form>') % (esc(action),esc(fd),esc(ft),esc(td),esc(tt))

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
    nav=([(setting_label('nav_dashboard','Dashboard'),'/')] if dashboard_enabled and allowed(u,'VIEW_DASHBOARD') else [])+[(setting_label('nav_sales','Sales / POS'),'/sales')]+([(setting_label('nav_sales_dashboard','Sales Dashboard'),'/sales-dashboard')] if dashboard_enabled and allowed(u,'VIEW_DASHBOARD') else [])+[(setting_label('nav_purchases','Purchases'),'/purchases'),(setting_label('nav_expenses','Expenses'),'/expenses'),(setting_label('nav_customers','Customers / Credit'),'/customers'),(setting_label('nav_reports','Reports'),'/reports'),(setting_label('nav_stock_report','Stock Report'),'/stock-report'),(setting_label('nav_reorder_report','Reorder Report'),'/reorder-report'),(setting_label('nav_live','Live Monitoring'),'/live'),(setting_label('nav_products','Product Registration'),'/products'),('MONTEZ LIQUOR AI Assistant','/ai')]
    if role=='ADMIN':
        nav += [('Branch Registration','/branches'),('Branch Performance','/branch-performance'),(setting_label('nav_users','User Management'),'/users'),(setting_label('nav_settings','Settings'),'/settings'),('M-Pesa Monitor','/mpesa/monitor'),('Payment Recovery','/mpesa/recovery'),(setting_label('nav_ledger','Ledger & P&L'),'/ledger'),(setting_label('nav_profit_loss','Profit & Loss'),'/profit-loss'),(setting_label('nav_expense_report','Expense Report'),'/expense-report'),(setting_label('nav_backup','Backup'),'/backup'),(setting_label('nav_audit','Audit Log'),'/audit')]
    else:
        perm_map={'/':'VIEW_DASHBOARD','/sales':'CREATE_SALE','/sales-dashboard':'VIEW_DASHBOARD','/purchases':'CREATE_PURCHASE','/expenses':'RECORD_EXPENSE','/customers':'MANAGE_CUSTOMERS','/reports':'VIEW_REPORTS','/stock-report':'VIEW_STOCK','/reorder-report':'VIEW_STOCK','/live':'LIVE_MONITORING','/products':'MANAGE_PRODUCTS','/ai':'AI_ASSISTANT'}
        if role=='MANAGEMENT':
            nav=[(setting_label('nav_dashboard','Dashboard'),'/')] if dashboard_enabled and allowed(u,'VIEW_DASHBOARD') else []
            nav += [('Reports','/reports'),('Stock Report','/stock-report'),('Reorder Report','/reorder-report'),('Expense Report','/expense-report'),('Branch Performance','/branch-performance')]
            if allowed(u,'VIEW_LEDGER'): nav += [('Ledger','/ledger'),('Profit & Loss','/profit-loss')]
            if allowed(u,'AI_ASSISTANT'): nav += [('MONTEZ LIQUOR AI Assistant','/ai')]
        else:
            nav=[(label,href) for label,href in nav if perm_map.get(href) and allowed(u,perm_map[href])]
            if allowed(u,'VIEW_REPORTS'): nav.append(('Expense Report','/expense-report'))
    nav_html=''.join(page_link(label,href,active) for label,href in nav)
    home_href='/' if dashboard_enabled and allowed(u,'VIEW_DASHBOARD') else '/sales'
    role_label={'ADMIN':setting_label('role_admin_label','ADMINISTRATOR / DEVELOPER'),'MANAGEMENT':setting_label('role_management_label','MANAGEMENT'),'SALES':setting_label('role_sales_label','SALES PERSON')}.get(role,role)
    idle_ms=int(float(settings.get('idle_timeout_minutes','2'))*60*1000)
    refresh_s=max(2,int(float(settings.get('live_refresh_seconds','5'))))
    return f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#123c31"><link rel="manifest" href="/manifest.json"><link rel="icon" href="/business-logo"><title>{esc(title)} · {esc(name)}</title><style>{CSS}</style></head><body><header class="mobile-head"><div class="brand-mini"><img src="/business-logo"> <span>{esc(name)}</span></div><button class="menu" type="button" onclick="document.body.classList.toggle('open-nav')">☰</button></header><aside class="sidebar"><div class="brand"><img src="/business-logo"><span><b>{esc(name)}</b><small>{esc(branch or business_subtitle)}</small></span></div><nav>{nav_html}</nav><div class="side-bottom"><div class="userbox"><b>{esc(u.get('full_name',''))}</b><span>@{esc(u.get('username',''))}</span><span>{esc(role_label)}</span></div><a class="nav" href="/change-password">Change Password</a><a class="nav logout" href="/logout">LOG OUT</a></div></aside><main><header class="topbar"><div><h1>{esc(title)}</h1><p class="muted">{esc(name)} · {esc(business_subtitle)} · {esc(subtitle)}</p></div><div class="clock" id="clock"></div></header><div class="actions movement-controls" style="padding:10px 0;position:sticky;top:0;z-index:20;background:var(--page,#f5f6f4)"><a class="btn" href="{home_href}">⌂ HOME</a><button class="btn" type="button" onclick="if(history.length>1){{history.back()}}else{{location.href='{home_href}'}}">← BACK</button><a class="btn" href="{home_href}" onclick="return confirm('Cancel this screen and return to the home screen? Unsaved form changes will be lost.')">✕ CANCEL</a></div>{flash_html(handler=None)}{content}<footer class="app-powered">Powered by SHADYTECHSOLUTIONS</footer></main>{SCANNER_MODAL}<script>{SCANNER_JS}</script><script>(function(){{function tick(){{var e=document.getElementById('clock');if(e)e.textContent=new Date().toLocaleString('en-KE',{{dateStyle:'medium',timeStyle:'medium'}})}}tick();setInterval(tick,1000);var mpesaPollSeconds={refresh_s},currencySymbol={json.dumps(settings.get('currency','KSh'))};function paintMpesaLive(payload){{var box=document.getElementById('mpesa-live-messages');if(!box||!payload)return;var items=payload.messages||[];if(!items.length){{box.innerHTML='<div class="mpesa-live-empty">Waiting for M-Pesa payment messages…</div>';return;}}box.innerHTML=items.map(function(i){{var safeTitle=String(i.title||'M-PESA PAYMENT').replace(/[&<>\"]/g,function(c){{return {{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}}[c]}});return '<div class="mpesa-live-item"><span class="mpesa-live-dot '+(i.source==='C2B'?'pulse':'')+'"></span><div class="mpesa-live-main"><b>'+safeTitle+'</b><span>'+String(i.ts||'')+' · '+String(i.detail||'')+'</span></div><span class="mpesa-live-amt">KSh '+Number(i.amount||0).toLocaleString('en-KE',{{minimumFractionDigits:2,maximumFractionDigits:2}})+'</span></div>'}}).join('')}};function pollMpesaLive(){{var box=document.getElementById('mpesa-live-messages');if(!box)return;fetch('/api/mpesa/live-messages',{{cache:'no-store',credentials:'same-origin'}}).then(function(r){{if(r.status===401){{location.href='/login';return null;}}return r.ok?r.json():null;}}).then(paintMpesaLive).catch(function(){{}})}};if(document.getElementById('mpesa-live-messages')){{pollMpesaLive();setInterval(pollMpesaLive,Math.max(2,mpesaPollSeconds)*1000);}}var idleMs={idle_ms},lastActivity=Date.now();function touch(){{lastActivity=Date.now()}};['click','keydown','touchstart','input','change','scroll'].forEach(function(ev){{document.addEventListener(ev,touch,{{passive:true}})}});setInterval(function(){{if(Date.now()-lastActivity>=idleMs){{location.href='/logout?auto=1'}}}},1000);}})();</script></body></html>'''

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
        products={}
        for product_id,qty,cost,line_total in clean:
            p=c.execute('SELECT * FROM products WHERE id=? AND active=1',(product_id,)).fetchone()
            if not p:
                raise ValueError(f'Product #{product_id} was not found or is inactive.')
            products[product_id]=p
        ts=now()
        no='PUR-'+dt.datetime.now().strftime('%Y%m%d%H%M%S')+'-'+secrets.token_hex(2).upper()
        cur=c.execute('INSERT INTO purchases(purchase_no,purchased_at,user_id,supplier,payment_method,total,reference) VALUES(?,?,?,?,?,?,?)',(no,ts,u['id'],supplier,method,total,ref))
        pid=cur.lastrowid
        # Apply every line in one database transaction. Repeated products are handled sequentially.
        for product_id,qty,cost,line_total in clean:
            p=c.execute('SELECT * FROM products WHERE id=? AND active=1',(product_id,)).fetchone()
            old=float(p['stock_qty']); new=old+qty
            avg=((old*float(p['avg_cost'])+(qty*cost))/new) if new else cost
            c.execute('INSERT INTO purchase_lines(purchase_id,product_id,qty,unit_cost,total) VALUES(?,?,?,?,?)',(pid,product_id,qty,cost,line_total))
            c.execute('UPDATE products SET stock_qty=?,avg_cost=?,updated_at=? WHERE id=?',(new,avg,ts,product_id))
            c.execute('INSERT INTO stock_movements(movement_time,user_id,product_id,movement_type,qty,before_qty,after_qty,unit_cost,reference) VALUES(?,?,?,?,?,?,?,?,?)',(ts,u['id'],product_id,'PURCHASE',qty,old,new,cost,no))
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
        ts=now(); numbers=[]
        for cat,desc,amount in clean:
            no='EXP-'+dt.datetime.now().strftime('%Y%m%d%H%M%S')+'-'+secrets.token_hex(2).upper()
            cur=c.execute('INSERT INTO expenses(expense_no,expense_date,user_id,category,description,amount,payment_method,reference) VALUES(?,?,?,?,?,?,?,?)',(no,ts,u['id'],cat,desc,amount,method,ref))
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
        cur=c.execute('INSERT INTO products(product_no,sku,name,category,unit,sell_price,wholesale_price,avg_cost,stock_qty,reorder_level,active,created_at,updated_at) VALUES(NULL,?,?,?,?,?,?,?,?,?,1,?,?)',(sku,name,cat,unit,sell,wholesale,avg_cost,stock,reorder,ts,ts)); pid=cur.lastrowid; product_no=f'PRD-{pid:05d}'
        c.execute('UPDATE products SET product_no=? WHERE id=?',(product_no,pid))
        if stock>0:
            c.execute('INSERT INTO stock_movements(movement_time,user_id,product_id,movement_type,qty,before_qty,after_qty,unit_cost,reference) VALUES(?,?,?,?,?,?,?,?,?)',(ts,u['id'],pid,'OPENING_STOCK',stock,0,stock,opening_cost,product_no))
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
    """Save all product retail/wholesale prices, average costs and reorder levels in one audited transaction."""
    if role_name(u) != 'ADMIN':
        raise PermissionError('Administrator permission required.')
    # Admin saves should be one-click; record a default audit reason when none is entered.
    reason = str(form.get('bulk_reason', '')).strip() or 'Administrator one-click stock update'
    c = core.db.c()
    changed = 0
    try:
        c.execute('BEGIN IMMEDIATE')
        products = c.execute('SELECT * FROM products ORDER BY id').fetchall()
        ts = now()
        for p in products:
            pid = int(p['id'])
            retail = float(form.get(f'bulk_sell_{pid}', p['sell_price']) or 0)
            wholesale = float(form.get(f'bulk_wholesale_{pid}', p['wholesale_price'] if p['wholesale_price'] is not None else p['sell_price']) or 0)
            cost = float(form.get(f'bulk_cost_{pid}', p['avg_cost']) or 0)
            reorder = float(form.get(f'bulk_reorder_{pid}', p['reorder_level']) or 0)
            if retail <= 0 or wholesale <= 0 or cost < 0 or reorder < 0:
                raise ValueError(f'Invalid retail/wholesale price, COGS or reorder level for {p["name"]}. Prices must be positive; COGS and reorder level cannot be negative.')
            old_retail = float(p['sell_price'] or 0)
            old_wholesale = float(p['wholesale_price'] if p['wholesale_price'] is not None else old_retail)
            old_cost = float(p['avg_cost'] or 0)
            old_reorder = float(p['reorder_level'] or 0)
            if (abs(retail-old_retail) > 1e-9 or abs(wholesale-old_wholesale) > 1e-9 or abs(cost-old_cost) > 1e-9 or abs(reorder-old_reorder) > 1e-9):
                c.execute('UPDATE products SET sell_price=?,wholesale_price=?,avg_cost=?,reorder_level=?,updated_at=? WHERE id=?', (retail, wholesale, cost, reorder, ts, pid))
                if abs(cost-old_cost) > 1e-9:
                    qty = float(p['stock_qty'] or 0)
                    c.execute('INSERT INTO stock_movements(movement_time,user_id,product_id,movement_type,qty,before_qty,after_qty,unit_cost,reference) VALUES(?,?,?,?,?,?,?,?,?)', (ts,u['id'],pid,'COST_ADJUSTMENT',0,qty,qty,cost,reason))
                core.db.audit(c,u,'BULK UPDATE','PRODUCT',pid,
                    f'{p["product_no"] or pid} {p["name"]}; retail {old_retail:.2f}->{retail:.2f}; wholesale {old_wholesale:.2f}->{wholesale:.2f}; avg_cost {old_cost:.2f}->{cost:.2f}; reorder {old_reorder:g}->{reorder:g}; reason={reason}')
                changed += 1
        c.commit()
        return changed
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()

def admin_set_stock(u, form):
    if role_name(u)!='ADMIN': raise PermissionError('Administrator permission required.')
    pid=int(form.get('product_id','0') or 0); new_qty=float(form.get('new_stock','-1') or -1); reason=form.get('reason','').strip()
    if len(reason)<5: raise ValueError('A reason of at least 5 characters is required for stock corrections.')
    if pid<=0 or new_qty<0: raise ValueError('Enter a valid stock quantity.')
    c=core.db.c()
    try:
        c.execute('BEGIN IMMEDIATE'); p=c.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone()
        if not p: raise ValueError('Product not found.')
        before=float(p['stock_qty']); after=float(new_qty); ts=now(); delta=after-before
        c.execute('UPDATE products SET stock_qty=?,updated_at=? WHERE id=?',(after,ts,pid)); c.execute('INSERT INTO stock_movements(movement_time,user_id,product_id,movement_type,qty,before_qty,after_qty,unit_cost,reference) VALUES(?,?,?,?,?,?,?,?,?)',(ts,u['id'],pid,'ADMIN_ADJUSTMENT',delta,before,after,p['avg_cost'],reason)); core.db.audit(c,u,'UPDATE','STOCK',pid,f'{p["product_no"]} {before:g} -> {after:g}; {reason}'); c.commit()
    except Exception:
        c.rollback(); raise
    finally: c.close()


def admin_set_cost(u, form):
    if role_name(u)!='ADMIN': raise PermissionError('Administrator permission required.')
    pid=int(form.get('product_id','0') or 0); new_cost=float(form.get('new_avg_cost','-1') or -1); reason=form.get('reason','').strip()
    if pid<=0 or new_cost<=0 or not reason: raise ValueError('Enter a positive average unit cost and a reason.')
    c=core.db.c()
    try:
        c.execute('BEGIN IMMEDIATE'); p=c.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone()
        if not p: raise ValueError('Product not found.')
        ts=now(); qty=float(p['stock_qty'])
        c.execute('UPDATE products SET avg_cost=?,updated_at=? WHERE id=?',(new_cost,ts,pid))
        c.execute('INSERT INTO stock_movements(movement_time,user_id,product_id,movement_type,qty,before_qty,after_qty,unit_cost,reference) VALUES(?,?,?,?,?,?,?,?,?)',(ts,u['id'],pid,'COST_ADJUSTMENT',0,qty,qty,new_cost,reason))
        core.db.audit(c,u,'UPDATE','PRODUCT COST',pid,f'{p["product_no"]} avg_cost {float(p["avg_cost"]):.2f} -> {new_cost:.2f}; {reason}')
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
    c=core.db.c(); c.execute('BEGIN')
    try:
        if branch_id and not c.execute('SELECT 1 FROM branches WHERE id=? AND active=1',(branch_id,)).fetchone(): raise ValueError('Select an active branch.')
        ts=now(); cur=c.execute('INSERT INTO users(username,full_name,password_hash,role,access_role,active,created_at,updated_at,branch_id) VALUES(?,?,?,?,?,?,?,?,?)',(username,full,core.sha(pw),storage_role,access_role,1,ts,ts,branch_id or None)); core.db.audit(c,u,'CREATE','USER',cur.lastrowid,f'{username} role={access_role}'); c.commit(); return cur.lastrowid
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
        if branch_id and not c.execute('SELECT 1 FROM branches WHERE id=? AND active=1',(branch_id,)).fetchone(): raise ValueError('Select an active branch.')
        if uid==u['id'] and (active==0 or access_role!='ADMIN'): raise ValueError('You cannot remove or restrict your own Administrator access.')
        if target['access_role']=='ADMIN' and (active==0 or access_role!='ADMIN'):
            admins=c.execute("SELECT COUNT(*) n FROM users WHERE access_role='ADMIN' AND active=1").fetchone()['n']
            if admins<=1: raise ValueError('At least one active Administrator must remain.')
        storage_role='ADMIN' if access_role=='ADMIN' else 'SALES'; ts=now(); c.execute('UPDATE users SET full_name=?,role=?,access_role=?,active=?,updated_at=?,branch_id=? WHERE id=?',(full,storage_role,access_role,active,ts,branch_id or None,uid)); core.db.audit(c,u,'UPDATE','USER',uid,f'Edited {target["username"]}: role={access_role}; active={active}'); c.commit()
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
    # Simple, standards-compliant one-page text PDF using only Python stdlib.
    st=core.get_settings()
    lines=[st.get('business_name',APP_NAME), st.get('business_branch_name',''), st.get('business_phone',''), st.get('business_address',''), st.get('business_website',''), ('KRA PIN: '+st.get('business_tax_pin','')) if st.get('business_tax_pin','') else '', title, now(), ' | '.join(map(str,headers))]
    if st.get('report_footer','').strip(): lines.append(st['report_footer'].strip())
    lines += [' | '.join(map(str,r)) for r in rows[:48]]
    def pdf_escape(s): return str(s).replace('\\','\\\\').replace('(','\\(').replace(')','\\)').replace('\n',' ')
    y=760; content=['BT /F1 8 Tf 36 760 Td']
    for line in lines:
        content.append(f'({pdf_escape(line)}) Tj 0 -12 Td')
    content.append('ET'); stream='\n'.join(content).encode('latin-1','replace')
    objs=[b'<< /Type /Catalog /Pages 2 0 R >>',b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',f'<< /Length {len(stream)} >>\nstream\n'.encode()+stream+b'\nendstream']
    out=io.BytesIO(); out.write(b'%PDF-1.4\n'); offsets=[0]
    for i,obj in enumerate(objs,1):
        offsets.append(out.tell()); out.write(f'{i} 0 obj\n'.encode()); out.write(obj); out.write(b'\nendobj\n')
    xref=out.tell(); out.write(f'xref\n0 {len(objs)+1}\n0000000000 65535 f \n'.encode())
    for off in offsets[1:]: out.write(f'{off:010d} 00000 n \n'.encode())
    out.write(f'trailer\n<< /Size {len(objs)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF'.encode())
    return out.getvalue()


def report_rows(start, end): return core.report_data(start,end)


def build_xlsx(title, headers, rows): return core.xlsx_bytes(title,headers,rows)


CSS = r'''
:root{--bg:#f4f7f5;--card:#fff;--ink:#18241f;--muted:#68756f;--line:#dfe8e3;--green:#0d3b2f;--green2:#164f40;--gold:#d7ae5c;--red:#a92f33;--good:#1a6a49}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:Segoe UI,Arial,sans-serif}a{color:inherit;text-decoration:none}button,input,select{font:inherit}button{cursor:pointer}.sidebar{position:fixed;left:0;top:0;bottom:0;width:248px;background:linear-gradient(180deg,#0b3026,#123e31);color:#e9f2ee;padding:18px 14px;display:flex;flex-direction:column;z-index:20}.brand{display:flex;gap:10px;align-items:center;padding:5px 6px 18px}.brand img{width:46px;height:46px}.brand b{display:block;font-size:16px}.brand small{display:block;font-size:9px;letter-spacing:1.5px;color:#c8ddd5}.sidebar nav{display:flex;flex-direction:column;gap:4px;overflow:auto}.nav{padding:10px 12px;border-radius:9px;color:#dcebe5;font-size:13px}.nav:hover,.nav.active{background:#1b5444}.nav.logout{background:#7f292d;margin-top:4px}.side-bottom{margin-top:auto}.userbox{background:#174739;border:1px solid #2c6655;padding:11px;border-radius:10px;margin-bottom:7px;font-size:12px}.userbox b{font-size:13px}.userbox span{display:block;color:#bdd0c9;margin-top:2px}.mobile-head{display:none}main{margin-left:248px;padding:20px;min-height:100vh}.topbar{display:flex;justify-content:space-between;align-items:start;margin-bottom:18px}.topbar h1{margin:0 0 4px;font-size:27px}.clock{font-size:12px;background:#fff;border:1px solid var(--line);padding:9px 11px;border-radius:9px;font-weight:700}.muted{color:var(--muted);font-size:12px}.grid{display:grid;gap:14px}.two{grid-template-columns:repeat(2,1fr)}.three{grid-template-columns:repeat(3,1fr)}.four{grid-template-columns:repeat(4,1fr)}.card{background:var(--card);border:1px solid var(--line);border-radius:15px;padding:16px;box-shadow:0 5px 18px rgba(17,46,36,.05)}.kpis{grid-template-columns:repeat(6,1fr)}.kpi .label{font-size:11px;color:var(--muted)}.kpi strong{display:block;font-size:22px;margin-top:5px}.section-head,.report-head{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:11px}.section-head h2,.section-head h3{margin:0}.pill{font-size:10px;padding:5px 8px;border-radius:99px;background:#edf4f0;color:var(--green)}.btn{display:inline-block;border:1px solid #ccd8d2;background:#fff;color:var(--ink);border-radius:9px;padding:9px 12px;font-weight:700}.btn.primary{background:var(--green);color:#fff;border-color:var(--green)}.btn.gold{background:var(--gold);border-color:var(--gold);color:#221908}.btn.danger{background:var(--red);color:#fff;border-color:var(--red)}.full{width:100%}.form-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.field label,label{display:block;font-size:12px;font-weight:700;margin-bottom:5px}.field input,.field select,input,select{width:100%;padding:10px;border:1px solid #ccd8d2;border-radius:8px;background:#fff}.actions{display:flex;gap:8px;flex-wrap:wrap}.table-wrap{overflow:auto}.table-wrap table{width:100%;border-collapse:collapse;font-size:12px}.table-wrap th,.table-wrap td{padding:9px;border-bottom:1px solid #edf1ef;text-align:left;white-space:nowrap}.table-wrap th{font-size:11px;background:#f8faf9}.flash{padding:10px 12px;border-radius:9px;margin-bottom:12px;font-size:13px}.flash.success{background:#eaf7ef;color:#12603b}.flash.error{background:#fdecec;color:#9c1f25}.notice{padding:10px;border-radius:8px;background:#eef6f1;color:#175c42}.empty{text-align:center;color:var(--muted);padding:20px}.toolbar{display:flex;flex-wrap:wrap;align-items:end;gap:8px}.pos-grid{display:grid;grid-template-columns:1.45fr .9fr;gap:14px}.cart-line,.pay-line{display:grid;gap:7px;align-items:end;margin:8px 0}.cart-line{grid-template-columns:1fr 90px 120px}.pay-line{grid-template-columns:100px 110px 1fr}.total-box{display:flex;justify-content:space-between;align-items:end;border-top:1px solid var(--line);margin-top:12px;padding-top:12px}.total-box strong{font-size:25px}.barchart{display:flex;align-items:end;gap:7px;height:190px}.bar-item{flex:1;height:100%;display:flex;flex-direction:column;justify-content:end;align-items:center}.bar{width:75%;max-width:30px;background:linear-gradient(180deg,var(--gold),var(--green));border-radius:5px 5px 0 0;min-height:2px}.bar-item span{font-size:9px;color:var(--muted);margin-top:5px}.login-page{min-height:100vh;display:grid;place-items:center;background:radial-gradient(circle at top right,#4a7b6b,#11382d 45%,#071b15)}.login-card{width:min(760px,92vw);background:#fff;border-radius:23px;padding:28px;box-shadow:0 22px 70px rgba(0,0,0,.28);text-align:left}.logo-wrap{display:flex;justify-content:center;align-items:center;gap:14px}.login-logo{width:65px;height:65px}.brand-big{font-size:27px;font-weight:900;letter-spacing:.3px}.subbrand{font-size:10px;color:#a2782c;font-weight:900;letter-spacing:2.2px}.bottles{display:flex;justify-content:center;gap:18px;align-items:end;margin:18px 0}.bottle{display:block;width:62px;border-radius:12px 12px 16px 16px;position:relative;box-shadow:0 14px 18px rgba(0,0,0,.12)}.bottle:before{content:'';position:absolute;width:22px;height:26px;left:20px;top:-23px;background:#cfae68;border-radius:4px}.bottle.wine{height:128px;background:linear-gradient(90deg,#cfd9c6,#eef4ea)}.bottle.red{height:115px;background:linear-gradient(90deg,#87332f,#541915)}.bottle.dark{height:105px;background:linear-gradient(90deg,#1d4a40,#0b231e)}.login-card h1{margin:3px 0}.login-card form{max-width:430px;margin:18px auto}.login-card input{margin:6px 0 11px}.login-note{max-width:600px;margin:13px auto 0;padding:11px;background:#fff8e2;border-radius:9px;font-size:11px;color:#6c5722}.quick-report-links{display:flex;flex-wrap:wrap;gap:6px;margin-top:7px}.quick-report-wrap{margin-top:12px}.quick-report-wrap>b{display:block;font-size:12px;margin-bottom:4px}.report-tools{display:flex;flex-wrap:wrap;gap:8px;align-items:end}.metric{padding:13px;border:1px solid var(--line);border-radius:11px}.metric .v{font-size:18px;font-weight:800;margin-top:5px}@media(max-width:1200px){.kpis{grid-template-columns:repeat(3,1fr)}.two{grid-template-columns:1fr}.form-grid{grid-template-columns:repeat(2,1fr)}}@media(max-width:800px){.sidebar{transform:translateX(-100%);transition:.2s;width:275px}.open-nav .sidebar{transform:translateX(0)}.mobile-head{display:flex;position:sticky;top:0;z-index:30;background:var(--green);color:#fff;justify-content:space-between;align-items:center;padding:10px 13px}.brand-mini{display:flex;align-items:center;gap:8px;font-weight:800}.brand-mini img{width:33px}.menu{background:transparent;color:#fff;border:0;font-size:23px}main{margin-left:0;padding:12px}.topbar{display:block}.clock{display:inline-block;margin-top:8px}.kpis{grid-template-columns:repeat(2,1fr)}.three,.four,.form-grid{grid-template-columns:1fr}.pos-grid{grid-template-columns:1fr}.cart-line,.pay-line{grid-template-columns:1fr 90px 110px}.login-card{padding:22px}.login-logo{width:54px;height:54px}.brand-big{font-size:23px}}
@media print{.sidebar,.mobile-head,.topbar,.btn,.actions,.toolbar{display:none!important}main{margin:0;padding:0}.card{box-shadow:none;border:0}.report-print{display:block}}
.inline-actions{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:2px 0}.inline-actions input,.inline-actions select{padding:7px;font-size:11px;width:auto}.inline-actions .btn{padding:7px 9px;font-size:11px}.notice{padding:10px;border-radius:8px;background:#eef6f1;color:#175c42;font-size:12px;margin-bottom:10px}
/* Live M-Pesa feed */
.mpesa-live-card{min-height:170px}.mpesa-live-head{display:flex;justify-content:space-between;align-items:center;gap:10px}.mpesa-live-feed{display:flex;flex-direction:column;gap:7px;margin-top:10px;max-height:250px;overflow:auto}.mpesa-live-item{display:grid;grid-template-columns:auto 1fr auto;gap:9px;align-items:center;padding:9px 10px;border:1px solid var(--line);border-radius:10px;background:#fbfdfc}.mpesa-live-dot{width:8px;height:8px;border-radius:50%;background:#1a6a49;box-shadow:0 0 0 4px #e7f4ee}.mpesa-live-main{min-width:0}.mpesa-live-main b{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.mpesa-live-main span{font-size:11px;color:var(--muted)}.mpesa-live-amt{font-weight:800;white-space:nowrap}.mpesa-live-empty{padding:18px;text-align:center;color:var(--muted);border:1px dashed var(--line);border-radius:10px}.pulse{animation:pulseMontez 1.4s ease-in-out infinite}@keyframes pulseMontez{0%,100%{opacity:1}50%{opacity:.45}}

/* Sales/customer UI controls */
.locked-price{background:#eef3f0;font-weight:700;border:2px solid #d7ae5c}.sale-total-bar{display:flex;justify-content:space-between;align-items:center;margin-top:14px;padding:14px 16px;border-radius:12px;background:#0d3b2f;color:#fff}.sale-total-bar strong{font-size:1.25rem;color:#f1d28d}.segmented{display:flex;gap:10px;flex-wrap:wrap}.segmented label{border:1px solid var(--line);padding:10px 12px;border-radius:10px;background:#fafcfa}.search-results{display:grid;gap:8px;margin-top:10px}.search-result{display:flex;justify-content:space-between;gap:12px;align-items:center;border:1px solid var(--line);border-radius:10px;padding:10px;background:#fafcfa}.selected-customer{margin-top:10px;padding:10px;border-left:4px solid var(--gold);background:#f6f0df;border-radius:8px}


/* AI Assistant, business performance and scanning */
.performance-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:14px}.performance-card{overflow:hidden}.performance-row{display:flex;align-items:center;gap:18px}.performance-ring{width:104px;height:104px;border-radius:50%;display:grid;place-items:center;flex:0 0 auto;background:conic-gradient(var(--good) calc(var(--pct)*1%),#e8eeeb 0)}.performance-ring.red{background:conic-gradient(var(--red) calc(var(--pct)*1%),#f0e5e5 0)}.performance-ring>div{width:76px;height:76px;border-radius:50%;background:#fff;display:grid;place-items:center;font-weight:900;font-size:21px}.performance-status{font-weight:900;letter-spacing:.7px}.performance-status.green{color:var(--good)}.performance-status.red{color:var(--red)}.performance-caption{font-size:11px;color:var(--muted);margin-top:3px}.ai-card{background:linear-gradient(135deg,#0d3b2f,#174f40);color:#fff}.ai-card .muted{color:#d1e4dc}.ai-quick{display:flex;flex-wrap:wrap;gap:7px}.ai-answer{white-space:pre-wrap;line-height:1.55;background:#fbfdfc;border:1px solid var(--line);border-radius:12px;padding:14px}.scan-inline{display:flex;gap:7px;align-items:end}.scan-inline>div{flex:1}.scan-btn{white-space:nowrap;min-width:88px;height:42px}.scanner-modal{position:fixed;inset:0;background:rgba(3,18,13,.72);display:none;align-items:center;justify-content:center;z-index:100}.scanner-modal.open{display:flex}.scanner-box{width:min(560px,94vw);background:#fff;border-radius:18px;padding:16px}.scanner-box video{width:100%;border-radius:12px;background:#101814;aspect-ratio:4/3;object-fit:cover}.scanner-status{margin-top:8px;font-size:12px;color:var(--muted)}@media(max-width:800px){.performance-grid{grid-template-columns:1fr}.performance-row{align-items:flex-start}.scan-inline{align-items:stretch}.scan-btn{height:auto}}
'''


def mpesa_live_messages(limit=8):
    """Return newest M-Pesa payment updates from STK and C2B sources."""
    c=core.db.c(); items=[]
    stk=c.execute("""SELECT updated_at AS ts,amount,phone,status,mpesa_code,client_ref
                       FROM mpesa_stk_requests ORDER BY id DESC LIMIT ?""",(limit,)).fetchall()
    for r in stk:
        status=str(r['status'] or 'PENDING').upper(); code=r['mpesa_code'] or ''
        title='M-PESA PAYMENT CONFIRMED' if status in ('SUCCESS','SUCCEEDED','COMPLETED') else 'M-PESA PAYMENT UPDATE'
        detail=f"{status} · {r['phone'] or 'Phone unavailable'}"
        if code: detail += f" · Code {code}"
        items.append({'ts':r['ts'] or '','amount':float(r['amount'] or 0),'title':title,'detail':detail,'code':code,'source':'STK'})
    c2b=c.execute("""SELECT received_at AS ts,amount,msisdn,first_name,last_name,trans_id,status,bill_ref_number
                       FROM mpesa_transactions ORDER BY id DESC LIMIT ?""",(limit,)).fetchall()
    for r in c2b:
        customer=' '.join(x for x in [(r['first_name'] or '').strip(),(r['last_name'] or '').strip()] if x) or 'Customer'
        status=str(r['status'] or 'RECEIVED').upper(); detail=f"{customer} · {r['msisdn'] or 'Phone unavailable'} · {status}"
        if r['trans_id']: detail += f" · Code {r['trans_id']}"
        if r['bill_ref_number']: detail += f" · Ref {r['bill_ref_number']}"
        items.append({'ts':r['ts'] or '','amount':float(r['amount'] or 0),'title':'M-PESA PAYMENT RECEIVED','detail':detail,'code':r['trans_id'] or '','source':'C2B'})
    c.close(); items.sort(key=lambda x:x.get('ts',''),reverse=True); return items[:limit]


def mpesa_live_messages_html(items):
    if not items: return '<div class="mpesa-live-empty">Waiting for M-Pesa payment messages…</div>'
    out=[]
    for i in items:
        pulse=' pulse' if i.get('source')=='C2B' else ''
        out.append(f'''<div class="mpesa-live-item"><span class="mpesa-live-dot{pulse}"></span><div class="mpesa-live-main"><b>{esc(i['title'])}</b><span>{esc(i['ts'])} · {esc(i['detail'])}</span></div><span class="mpesa-live-amt">{money(i['amount'])}</span></div>''')
    return ''.join(out)


def mpesa_live_messages_json():
    return {'timestamp':now(),'messages':mpesa_live_messages(8),'refresh_seconds':max(2,int(float(core.get_setting('live_refresh_seconds','5'))))}

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

def home_page(u):
    require_perm_user(u,'VIEW_DASHBOARD')
    settings=core.get_settings(); business_name=settings.get('business_name',APP_NAME); branch_name=settings.get('business_branch_name','')
    d=core.dashboard(); live=core.live_monitoring(); live['active_users']=active_user_count(); chart=daily_chart(); mp_live=mpesa_live_messages(8)
    tstart=f"{now()[:10]} 00:00:00"; tend=f"{now()[:10]} 23:59:59"; pt=core.business_performance(tstart,tend)
    mstart=f"{now()[:7]}-01 00:00:00"; mend=f"{now()[:10]} 23:59:59"; pm=core.business_performance(mstart,mend)
    def perf_card(p,label):
        cls='green' if p['traffic']=='GREEN' else 'red'
        return f'<section class="card performance-card"><div class="section-head"><div><h2>{esc(label)}</h2><div class="muted">{esc(p["label"])} · Green threshold {p["threshold"]}%</div></div><span class="pill">{esc(p["basis"])}</span></div><div class="performance-row"><div class="performance-ring {cls}" style="--pct:{max(0,min(100,p["score"])):.1f}"><div>{p["percentage"]:.1f}%</div></div><div><div class="performance-status {cls}">{p["status"]}</div><div class="performance-caption">Revenue {money(p["revenue"])} · Expenses {money(p["expenses"])} · Net {money(p["net_profit"])}</div><div class="performance-caption">{esc(p["start"])} → {esc(p["end"])}</div></div></div></section>'
    performance_html=perf_card(pt,'Today Business Performance')+perf_card(pm,'This Month Business Performance')
    awards_html=monthly_awards_html(mstart,mend) if role_name(u) in ('ADMIN','MANAGEMENT') else ''
    content=f'''<section class="card" style="margin-bottom:14px"><div class="section-head"><div><h2 style="margin:0">{esc(dashboard_greeting(u))}</h2><div class="muted">Welcome to {esc(business_name)}{(' · '+esc(branch_name)) if branch_name else ''}</div></div><span class="pill">{esc(role_display(role_name(u)))}</span></div></section><div class="grid kpis"><div class="card kpi"><div class="label">TODAY SALES</div><strong>{money(d['today_sales'])}</strong><div class="muted">{d['transactions']} transactions</div></div><div class="card kpi"><div class="label">CASH</div><strong>{money(d['cash'])}</strong></div><div class="card kpi"><div class="label">M-PESA</div><strong>{money(d['mpesa'])}</strong></div><div class="card kpi"><div class="label">TODAY EXPENSES</div><strong>{money(d['expenses'])}</strong></div><div class="card kpi"><div class="label">TOTAL STOCK WORTH · COGS / AT COST</div><strong>{money(d['stock_value'])}</strong><div class="muted">{d['stock_units']:,.0f} units in stock · valued at average unit cost</div><a class="muted" href="/stock-report">View stock valuation</a></div><div class="card kpi"><div class="label">POTENTIAL RETAIL VALUE</div><strong>{money(d['stock_retail_value'])}</strong><div class="muted">Estimated value if all current stock sells at listed prices</div></div><div class="card kpi"><div class="label">LOW STOCK</div><strong>{live['low_stock']}</strong></div><div class="card kpi"><div class="label">ACTIVE USERS</div><strong>{live['active_users']}</strong></div></div><div class="performance-grid" style="margin-top:14px">{performance_html}</div>{awards_html}<div class="grid two" style="margin-top:14px"><section class="card"><div class="section-head"><h2>14-Day Sales Trend</h2><span class="pill">LIVE DATA</span></div>{chart}</section><section class="card"><div class="section-head"><h2>System Accountability</h2><span class="pill">SIGNED IN</span></div><div class="metric"><div class="muted">USER</div><div class="v">{esc(u['full_name'])}</div><div class="muted">@{esc(u['username'])} · {esc(role_display(role_name(u)))}</div></div><div class="metric" style="margin-top:9px"><div class="muted">SERVER TIME</div><div class="v">{now()}</div></div></section></div><div class="grid two" style="margin-top:14px"><section class="card mpesa-live-card"><div class="mpesa-live-head"><div><h2 style="margin:0">Live M-Pesa Messages</h2><div class="muted">{esc(settings.get('mpesa_shortcode',''))} · refreshes automatically</div></div><span class="pill">LIVE</span></div><div id="mpesa-live-messages">{mpesa_live_messages_html(mp_live)}</div></section><section class="card"><div class="section-head"><h2>Low Stock</h2><a class="btn" href="/stock-report">VIEW STOCK</a></div>{html_table(['Product','Stock','Reorder Level'],[[esc(p['name']),p['stock_qty'],p['reorder_level']] for p in low_stock()],'No low-stock products.')}</section></div><div class="grid two" style="margin-top:14px"><section class="card"><div class="section-head"><h2>Recent Sales</h2><a class="btn" href="/sales">OPEN POS</a></div>{html_table(['Time','Receipt','Customer','Total','By'],[[esc(r['sold_at']),esc(r['receipt_no']),esc(r['customer']),money(r['total']),esc(r['user_name'])] for r in live['recent_sales']],'No sales yet.')}</section><section class="card"><div class="section-head"><h2>Recent Purchases</h2><a class="btn" href="/purchases">OPEN PURCHASES</a></div>{html_table(['Time','Purchase','Supplier','Total','By'],[[esc(r['purchased_at']),esc(r['purchase_no']),esc(r['supplier']),money(r['total']),esc(r['user_name'])] for r in live['recent_purchases']],'No purchases yet.')}</section></div>'''
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


def low_stock():
    c=core.db.c(); rows=[dict(r) for r in c.execute('SELECT * FROM products WHERE active=1 AND stock_qty<=reorder_level ORDER BY stock_qty,name')]; c.close(); return rows


def daily_chart():
    c=core.db.c(); rows=c.execute("SELECT date(sold_at) day, COALESCE(SUM(total),0) revenue FROM sales WHERE substr(sold_at,1,10)>=date('now','-13 day','localtime') AND customer_type<>'CREDIT' GROUP BY date(sold_at) ORDER BY day").fetchall(); c.close()
    data=[dict(r) for r in rows]; maxv=max([float(r['revenue']) for r in data] or [1]); labels=''
    for r in data:
        h=max(2, int(float(r['revenue'])/maxv*150)); labels+=f'<div class="bar-item"><div class="bar" style="height:{h}px" title="{esc(money(r["revenue"]))}"></div><span>{esc(str(r["day"])[5:])}</span></div>'
    return f'<div class="barchart">{labels or "<div class=\"muted\">No sales data yet.</div>"}</div>'


def sales_dashboard(u):
    c=core.db.c(); staff=c.execute("SELECT u.full_name,SUM(sl.qty) units,SUM(sl.qty*sl.unit_price) revenue,COUNT(DISTINCT s.receipt_no) receipts FROM sales s JOIN sale_lines sl ON sl.sale_id=s.id JOIN users u ON u.id=s.user_id WHERE substr(s.sold_at,1,10)=? AND s.customer_type<>'CREDIT' GROUP BY u.id ORDER BY revenue DESC",(now()[:10],)).fetchall(); products=c.execute("SELECT p.name,SUM(sl.qty) units,SUM(sl.qty*sl.unit_price) revenue FROM sales s JOIN sale_lines sl ON sl.sale_id=s.id JOIN products p ON p.id=sl.product_id WHERE substr(s.sold_at,1,7)=? AND s.customer_type<>'CREDIT' GROUP BY p.id ORDER BY revenue DESC LIMIT 10",(now()[:7],)).fetchall(); c.close()
    content=f'<div class="grid two"><section class="card"><h2>Sales by Person Today</h2>{html_table(["Employee","Units","Revenue","Receipts"],[[esc(r["full_name"]),r["units"],money(r["revenue"]),r["receipts"]] for r in staff])}</section><section class="card"><h2>Top Products This Month</h2>{html_table(["Product","Units","Revenue"],[[esc(r["name"]),r["units"],money(r["revenue"])] for r in products])}</section></div>'
    return layout(u,'Sales Dashboard',content,'/sales-dashboard')


def sales_page(u, message='', search_query='', search_results=None, selected_customer_id=None):
    require_perm_user(u,'CREATE_SALE')
    max_payments=max(1,min(5,int(float(core.get_setting('max_payment_transactions','5')))))
    default_payment_count=max(1,min(max_payments,int(float(core.get_setting('default_payment_transactions','1')))))
    ps=core.products()
    product_opts=''.join(f'<option value="{p["id"]}" data-retail-price="{float(p["sell_price"]):.2f}" data-wholesale-price="{float(p.get('wholesale_price') or p['sell_price']):.2f}" data-sku="{esc(p.get("sku",""))}">{esc(p["name"])} · Retail {money(p["sell_price"])} / Wholesale {money(p.get('wholesale_price') or p['sell_price'])} · Stock {p["stock_qty"]}</option>' for p in ps) or '<option value="">No products registered</option>'
    c=core.db.c()
    rows=c.execute("SELECT s.receipt_no,s.sold_at,s.customer_type,s.sale_type,s.total,COALESCE(cu.full_name,'Walk-in Customer') customer,COALESCE(u.full_name,'') user_name FROM sales s LEFT JOIN customers cu ON cu.id=s.customer_id LEFT JOIN users u ON u.id=s.user_id ORDER BY s.id DESC LIMIT 100").fetchall(); c.close()
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
            c=core.db.c(); sc=c.execute('SELECT id,full_name,phone,id_no,address FROM customers WHERE id=? AND active=1',(scid,)).fetchone(); c.close()
            if sc:
                selected_customer_id_val=str(sc['id']); selected_customer_html=f'<div class="selected-customer"><b>SAVED CUSTOMER</b><div>{esc(sc["full_name"])} · {esc(sc["phone"])}</div><div class="muted">ID: {esc(sc["id_no"] or "—")} · {esc(sc["address"] or "")}</div></div>'
    notice=f'<div class="flash error">{esc(message)}</div>' if message else ''
    content='''__NOTICE__
<form method="post" action="/sales" class="pos-grid" id="sales_form">
__CSRF__
<input type="hidden" name="customer_id" id="selected_customer_id" value="__SELECTED_CUSTOMER_ID__"><input type="hidden" name="mpesa_batch_id" id="mpesa_batch_id" value="">
<div>
<section class="card"><div class="section-head"><h2>New Sale</h2><span class="pill">ATTENDED BY: __FULL_NAME__</span></div>
<div class="form-grid"><div><label>Sale Type</label><select name="sale_type" id="sale_type" onchange="showRegisteredPrice()"><option value="RETAIL">RETAIL</option><option value="WHOLESALE">WHOLESALE</option></select></div></div><div class="section-head" style="margin-top:12px"><h3>Products in this sale</h3><button type="button" class="btn gold" onclick="addSaleLine()">+ ADD LINE</button></div><div id="sale_lines"><div class="form-grid sale-line" style="border:1px solid var(--line);padding:10px;border-radius:10px;margin-bottom:8px"><div><label>Product</label><select name="product_id_1" id="sale_product_1" class="sale-product" required onchange="showRegisteredPrice()">__PRODUCT_OPTS__</select></div><div><label>Quantity</label><input type="number" name="qty_1" id="sale_qty_1" class="sale-qty" min="1" step="1" value="1" required oninput="showRegisteredPrice()"></div><div><label>Registered unit price</label><input id="registered_price_display_1" class="locked-price sale-price" readonly></div><div><label>Line total</label><input id="line_total_1" class="locked-price sale-line-total" readonly><button type="button" class="btn" onclick="removeSaleLine(this)">REMOVE LINE</button></div></div></div><input id="sales_sku_scan" placeholder="Scan/type SKU, then look up" style="margin-top:7px"><button type="button" class="btn" style="margin-top:7px" onclick="lookupProductBySku('sales_sku_scan')">LOOK UP SKU</button><div class="sale-total-bar"><span>SALE TOTAL</span><strong id="sale_total_display">KSh 0.00</strong></div></section>
<section class="card" style="margin-top:14px"><div class="section-head"><h2>Customer</h2><span class="pill">WALK-IN OR CREDIT CUSTOMER</span></div>
<div class="form-grid"><div><label>Customer Type</label><select name="customer_mode" id="customer_mode" onchange="toggleCustomerMode()"><option value="WALK_IN">WALK-IN CUSTOMER</option><option value="CREDIT">CREDIT CUSTOMER</option></select></div></div>
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
function confirmDirectMpesa(i){var phoneEl=document.getElementById('mpesa_phone_'+i),phone=phoneEl?phoneEl.value.trim():'';var status=document.getElementById('mpesa_status_'+i),btn=document.getElementById('mpesa_confirm_'+i),csrf=document.querySelector('input[name=csrf]');var pp=document.getElementById('sale_product'),qq=document.getElementById('sale_qty');if(!phone){alert('Enter the customer M-Pesa phone number.');return;}if(!pp||!pp.value||!qq||!(parseInt(qq.value||'0',10)>0)){alert('Select the product and quantity first.');return;}status.textContent='Searching latest M-Pesa payment…';btn.disabled=true;var body=new URLSearchParams();body.set('csrf',csrf?csrf.value:'');body.set('phone',phone);body.set('product_id',pp.value);body.set('qty',qq.value);var st=document.getElementById('sale_type');body.set('sale_type',st?st.value:'RETAIL');fetch('/api/mpesa/confirm-direct',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:body.toString()}).then(function(r){return r.json().then(function(d){return {ok:r.ok,d:d};});}).then(function(x){if(!x.ok)throw new Error(x.d.error||'Unable to confirm payment.');var d=x.d;if(d.ok){status.textContent='PAYMENT CONFIRMED · SALE SAVED · '+d.receipt_no;window.location='/sales';return;}status.textContent=d.message||('Payment found: KSh '+Number(d.matched||0).toLocaleString('en-KE',{minimumFractionDigits:2})+' · Remaining: KSh '+Number(d.remaining||0).toLocaleString('en-KE',{minimumFractionDigits:2}));btn.disabled=false;}).catch(function(e){status.textContent=e.message;btn.disabled=false;});}
function requestMpesa(i){var amt=parseFloat(document.getElementById('payment_amount_'+i).value||'0')||0;var phone=document.getElementById('mpesa_phone_'+i).value.trim();var status=document.getElementById('mpesa_status_'+i),btn=document.getElementById('mpesa_request_'+i),csrf=document.querySelector('input[name=csrf]');if(amt<=0){alert('Enter the payment amount first.');return;}if(!phone){alert('Enter the customer M-Pesa phone number.');return;}status.textContent='Sending M-Pesa prompt…';btn.disabled=true;var body=new URLSearchParams();body.set('csrf',csrf?csrf.value:'');body.set('amount',amt.toFixed(2));body.set('phone',phone);body.set('sequence_no',String(i));body.set('all_mpesa',Array.prototype.slice.call(document.querySelectorAll('select[name^=payment_method_]')).every(function(x){return x.value==='MPESA'})?'1':'');var pp=document.getElementById('sale_product');var qq=document.getElementById('sale_qty');if(pp&&qq){body.set('product_id',pp.value||'');body.set('qty',qq.value||'');var st=document.getElementById('sale_type');body.set('sale_type',st?st.value:'RETAIL');var n=document.getElementById('payment_count');body.set('payment_count',n?n.value:'1');body.set('batch_id',(document.getElementById('mpesa_batch_id')||{}).value||'');var pprice=selectedProductPrice();body.set('total',(pprice*(parseFloat(qq.value||'0')||0)).toFixed(2));for(var j=1;j<=parseInt(n?n.value:'1',10);j++){var ai=document.getElementById('payment_amount_'+j);body.set('payment_amount_'+j,ai?ai.value:'0');}}fetch('/api/mpesa/stk-request',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:body.toString()}).then(function(r){return r.json().then(function(d){return {ok:r.ok,d:d};});}).then(function(x){if(!x.ok)throw new Error(x.d.error||'Unable to send M-Pesa prompt.');status.textContent='Prompt sent. Waiting for customer payment…';if(x.d.batch_id){var bh=document.getElementById('mpesa_batch_id');if(bh)bh.value=x.d.batch_id;}var checkout=x.d.CheckoutRequestID||x.d.checkout_request_id,tries=0;var timer=setInterval(function(){tries++;fetch('/api/mpesa/stk-status?checkout_request_id='+encodeURIComponent(checkout),{cache:'no-store'}).then(function(r){return r.json();}).then(function(d){if(d.status==='SUCCESS'&&d.mpesa_code){clearInterval(timer);document.getElementById('mpesa_code_'+i).value=d.mpesa_code;status.textContent=d.auto_saved?'PAYMENT RECEIVED · SALE SAVED AUTOMATICALLY · '+d.mpesa_code:'PAYMENT RECEIVED · M-Pesa code '+d.mpesa_code;btn.textContent=d.auto_saved?'SALE SAVED':'PAYMENT RECEIVED';if(d.auto_saved){window.location='/sales';return;}}else if(d.status==='FAILED'){clearInterval(timer);status.textContent='Payment failed: '+(d.message||'Customer did not complete payment.');btn.disabled=false;}else if(tries>=60){clearInterval(timer);status.textContent='Still pending. Check M-Pesa Monitor.';btn.disabled=false;}}).catch(function(){if(tries>=60){clearInterval(timer);status.textContent='Unable to check payment status.';btn.disabled=false;}});},2000);}).catch(function(e){status.textContent=e.message;btn.disabled=false;});}
function escapeHtml(v){return String(v).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
window.addEventListener('DOMContentLoaded',function(){renderPaymentRows();showRegisteredPrice();toggleCustomerMode();if(document.getElementById('selected_customer_id').value){document.getElementById('customer_mode').value='CREDIT';document.querySelector('input[value="EXISTING"]').checked=true;toggleCustomerMode();toggleCreditChoice();}var form=document.getElementById('sales_form');if(form){form.addEventListener('submit',function(ev){var mode=document.getElementById('customer_mode').value;if(mode==='CREDIT'){if(!document.getElementById('due_date').value||!document.getElementById('collateral').value.trim()){ev.preventDefault();alert('Credit invoices require a due date and collateral details.');return;}}else{var count=parseInt(document.getElementById('payment_count').value||'1',10);for(var i=1;i<=count;i++){var m=document.getElementById('payment_method_'+i),c=document.getElementById('mpesa_code_'+i);if(m&&m.value==='MPESA'&&c&&!c.value.trim()){ev.preventDefault();c.required=true;c.focus();alert('Enter the M-Pesa transaction code for every M-Pesa payment.');return;}}}});}});
</script>'''
    recent=html_table(['Date & Time','Receipt','Customer','Type','Total','Recorded By','Actions'],[[esc(r['sold_at']),esc(r['receipt_no']),esc(r['customer']),esc(r['customer_type']+' · '+r['sale_type']),money(r['total']),esc(r['user_name']),f'<div class="inline-actions"><a class="btn" href="/sales/receipt?receipt={quote(r["receipt_no"])}">OPEN INVOICE / VIEW</a><a class="btn" href="/sales/export?receipt={quote(r["receipt_no"])}&format=pdf">PDF</a><a class="btn" href="/sales/export?receipt={quote(r["receipt_no"])}&format=xlsx">EXCEL</a></div>'] for r in rows])
    content=content.replace('__NOTICE__',notice).replace('__CSRF__',csrf_input(CURRENT_HANDLER)).replace('__FULL_NAME__',esc(u['full_name'])).replace('__PRODUCT_OPTS__',product_opts).replace('__SEARCH_QUERY__',esc(search_query)).replace('__RESULT_HTML__',result_html).replace('__PAYMENTS__',payments).replace('__PAYMENT_OPTIONS__',''.join(f'<option value="{i}" {"selected" if i==default_payment_count else ""}>{i}</option>' for i in range(1,max_payments+1))).replace('__MAX_PAYMENTS__',str(max_payments)).replace('__MAX_TX__',str(max_payments)).replace('__RECENT_SALES__',recent).replace('__SELECTED_CUSTOMER_ID__',selected_customer_id_val).replace('__SELECTED_CUSTOMER_HTML__',selected_customer_html)
    return layout(u,'Sales / POS',content,'/sales','Direct sales entry')


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
    sale=d['sale']; lines=d['lines']; pays=d['payments']; credit=d['credit']
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
    q=str(question or '').strip(); ql=q.lower(); d=core.dashboard(); live=core.live_monitoring()
    ds=f"{now()[:10]} 00:00:00"; de=f"{now()[:10]} 23:59:59"; ms=f"{now()[:7]}-01 00:00:00"; me=f"{now()[:10]} 23:59:59"
    pt=core.business_performance(ds,de); pm=core.business_performance(ms,me)
    c=core.db.c(); pc=c.execute('SELECT COUNT(*) n FROM products WHERE active=1').fetchone()['n']; low=c.execute('SELECT name,stock_qty,reorder_level FROM products WHERE active=1 AND stock_qty<=reorder_level ORDER BY stock_qty,name LIMIT 10').fetchall(); c.close()
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
    d=core.dashboard(); live=core.live_monitoring(); perf=core.business_performance(f"{now()[:10]} 00:00:00",f"{now()[:10]} 23:59:59")
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


def purchase_page(u, message=''):
    require_perm_user(u,'CREATE_PURCHASE'); ps=core.products(); opts=''.join(f'<option value="{p["id"]}" data-sku="{esc(p.get("sku",""))}">{esc(p["name"])} · Stock {p["stock_qty"]}</option>' for p in ps) or '<option value="">No products registered</option>'
    c=core.db.c(); rows=c.execute('SELECT pu.id purchase_id,pu.purchase_no,pu.purchased_at,pu.supplier,pu.total,pu.payment_method,pu.reference,u.full_name,p.name product,pl.product_id,pl.qty,pl.unit_cost FROM purchases pu JOIN purchase_lines pl ON pl.purchase_id=pu.id JOIN products p ON p.id=pl.product_id JOIN users u ON u.id=pu.user_id ORDER BY pu.id DESC LIMIT 100').fetchall(); c.close()
    notice=f'<div class="flash error">{esc(message)}</div>' if message else ''
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
    content=f'''{notice}<section class="card"><div class="section-head"><h2>New Purchase / Stock In</h2><span class="pill">RECORDED BY {esc(u['full_name'])}</span></div><form method="post" action="/purchases">{csrf_input(CURRENT_HANDLER)}<input type="hidden" id="purchase_line_count" name="purchase_line_count" value="1"><div class="form-grid"><div><label>Supplier *</label><input name="supplier" required></div><div><label>Payment *</label><select name="payment_method"><option>Cash</option><option>M-Pesa</option><option>Credit</option></select></div><div><label>Reference / M-Pesa Code</label><input name="reference"></div></div><div class="section-head" style="margin-top:14px"><h3>Purchase items</h3><button type="button" class="btn" onclick="addPurchaseLine()">+ ADD LINE</button></div><div id="purchase_lines"><div class="form-grid purchase-line" style="border:1px solid var(--line);padding:10px;border-radius:10px;margin-bottom:8px"><div><label>Product *</label><select name="product_id_1" id="purchase_product_id_1" class="purchase-product" required>{opts}</select><div class="scan-inline" style="margin-top:7px"><div><input id="purchase_sku_scan_1" placeholder="Scan/type SKU"></div><button type="button" class="btn scan-btn" onclick="openScanner('purchase_sku_scan_1')">SCAN</button></div><button type="button" class="btn" style="margin-top:7px" onclick="lookupGenericSku('purchase_sku_scan_1','purchase_product_id_1')">LOOK UP SKU</button></div><div><label>Quantity *</label><input type="number" min="1" step="1" name="qty_1" required></div><div><label>Unit Cost *</label><input type="number" min="0" step="0.01" name="unit_cost_1" required></div><div><label>Line Total</label><input class="purchase-line-total" readonly value="0.00"></div></div></div><div class="section-head"><strong>Total: KES <span id="purchase_grand_total">0.00</span></strong></div><div class="actions" style="margin-top:14px"><button class="btn gold" type="submit">SAVE PURCHASE</button><button class="btn" type="reset">CLEAR</button></div><script>
function purchaseRecalc(){{var total=0;document.querySelectorAll('#purchase_lines .purchase-line').forEach(function(r){{var q=parseFloat(r.querySelector('[name^="qty_"]').value||0)||0,c=parseFloat(r.querySelector('[name^="unit_cost_"]').value||0)||0,t=q*c;total+=t;r.querySelector('.purchase-line-total').value=t.toFixed(2)}});document.getElementById('purchase_grand_total').textContent=total.toFixed(2)}}
function addPurchaseLine(){{var box=document.getElementById('purchase_lines'),first=box.querySelector('.purchase-line'),n=Number(document.getElementById('purchase_line_count').value||1)+1,row=first.cloneNode(true);document.getElementById('purchase_line_count').value=n;row.querySelectorAll('[name],[id]').forEach(function(e){{if(e.name)e.name=e.name.replace(/_\\d+$/,'_'+n);if(e.id)e.id=e.id.replace(/_\\d+$/,'_'+n)}});row.querySelectorAll('input').forEach(function(e){{if(e.name.indexOf('qty_')===0)e.value='1';else if(e.name.indexOf('unit_cost_')===0||e.classList.contains('purchase-line-total')||e.id.indexOf('purchase_sku_scan_')===0)e.value=''}});row.querySelector('select').value='';box.appendChild(row);purchaseRecalc()}}
document.addEventListener('input',function(e){{if(e.target.closest&&e.target.closest('#purchase_lines'))purchaseRecalc()}});
</script></form></section></section><section class="card" style="margin-top:14px"><div class="section-head"><h2>Recent Purchases</h2></div>{purchases_table}</section>'''
    return layout(u,'Purchases / Stock In',content,'/purchases','Stock acquisition and supplier control')


def expense_page(u, message='', query=None):
    require_perm_user(u,'RECORD_EXPENSE')
    fd,ft,td,tt,preset=period_from_query(query or {})
    start=f'{fd} {ft}:00'; end=f'{td} {tt}:59'
    rows=core.expense_report_data(start,end)
    notice=f'<div class="flash error">{esc(message)}</div>' if message else ''
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
    content=f'''{notice}<section class="card"><div class="section-head"><h2>Business Expenses</h2><span class="pill">ACCOUNTABLE: {esc(u['full_name'])}</span></div><form method="post" action="/expenses">{csrf_input(CURRENT_HANDLER)}<input type="hidden" id="expense_line_count" name="expense_line_count" value="1"><div class="form-grid"><div><label>Payment</label><select name="payment_method"><option>Cash</option><option>M-Pesa</option><option>Bank</option></select></div><div><label>Reference / Receipt No.</label><input name="reference"></div></div><div class="section-head" style="margin-top:14px"><h3>Expense items</h3><button type="button" class="btn" onclick="addExpenseLine()">+ ADD LINE</button></div><div id="expense_lines"><div class="form-grid expense-line" style="border:1px solid var(--line);padding:10px;border-radius:10px;margin-bottom:8px"><div><label>Category *</label><select name="category_1" required><option>Rent</option><option>Utilities</option><option>Wages</option><option>Transport</option><option>Licensing</option><option>Repairs</option><option>Security</option><option>Marketing</option><option>Bank Charges</option><option>Other</option></select></div><div><label>Description *</label><input name="description_1" required></div><div><label>Amount (KES) *</label><input type="number" name="amount_1" min="0.01" step="0.01" required></div></div></div><div class="actions" style="margin-top:14px"><button class="btn gold" type="submit">SAVE EXPENSES</button><button class="btn" type="reset">CLEAR</button></div><script>
function addExpenseLine(){{var box=document.getElementById('expense_lines'),first=box.querySelector('.expense-line'),n=Number(document.getElementById('expense_line_count').value||1)+1,row=first.cloneNode(true);document.getElementById('expense_line_count').value=n;row.querySelectorAll('[name],[id]').forEach(function(e){{if(e.name)e.name=e.name.replace(/_\\d+$/,'_'+n);if(e.id)e.id=e.id.replace(/_\\d+$/,'_'+n)}});row.querySelectorAll('input').forEach(function(e){{e.value=''}});box.appendChild(row)}}
</script></form></section></section><section class="card" style="margin-top:14px"><div class="section-head"><h2>Expense Report</h2><span class="pill">{esc(preset)}</span></div>{range_toolbar('/expenses',fd,ft,td,tt)}<div class="actions" style="margin-top:10px"><button type="button" class="btn" onclick="window.print()">PRINT REPORT</button><a class="btn" href="{exp.format(fmt='pdf')}">DOWNLOAD PDF</a><a class="btn" href="{exp.format(fmt='xlsx')}">DOWNLOAD EXCEL</a></div><div class="report-print">{report_table}</div></section>'''
    return layout(u,'Expenses',content,'/expenses','Expense entry and expense reporting')

def expense_report_page(u, query=None):
    require_perm_user(u,'VIEW_REPORTS')
    fd,ft,td,tt,preset=period_from_query(query or {}); start=f'{fd} {ft}:00'; end=f'{td} {tt}:59'
    rows=core.expense_report_data(start,end)
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
    exp=f'/report/export?kind=expenses&start={quote(start)}&end={quote(end)}&format={{fmt}}'
    content=f'''<section class="card"><div class="section-head"><h2>Expense Report</h2><span class="pill">{esc(preset)}</span></div>{range_toolbar('/expense-report',fd,ft,td,tt)}<div class="muted" style="margin-top:8px">Generated by {esc(u['full_name'])} @{esc(u['username'])} at {now()}</div><div class="actions" style="margin-top:10px"><button type="button" class="btn" onclick="window.print()">PRINT</button><a class="btn" href="{exp.format(fmt='pdf')}">DOWNLOAD PDF</a><a class="btn" href="{exp.format(fmt='xlsx')}">DOWNLOAD EXCEL</a></div></section><section class="card report-print" style="margin-top:14px">{table}</section>'''
    return layout(u,'Expense Report',content,'/expense-report','Custom expense reporting')

def customers_page(u):
    require_perm_user(u,'MANAGE_CUSTOMERS')
    c=core.db.c(); rows=c.execute("SELECT c.*,COALESCE(SUM(ca.principal-ca.paid_amount),0) balance FROM customers c LEFT JOIN credit_accounts ca ON ca.customer_id=c.id AND ca.status='OPEN' GROUP BY c.id ORDER BY c.full_name").fetchall(); credits=c.execute("""SELECT ca.id credit_id,ca.principal,ca.paid_amount,MAX(ca.principal-ca.paid_amount,0) balance,ca.due_date,ca.status,c.full_name,c.phone,s.receipt_no FROM credit_accounts ca JOIN customers c ON c.id=ca.customer_id JOIN sales s ON s.id=ca.sale_id ORDER BY CASE WHEN ca.status='OPEN' THEN 0 ELSE 1 END,c.full_name,ca.id DESC""").fetchall(); c.close()
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
    content=f'''<section class="card"><div class="section-head"><h2>Customers & Credit</h2><span class="pill">REGISTERED CUSTOMERS</span></div>{customer_table}</section><section class="card" style="margin-top:14px"><div class="section-head"><h2>Credit Customer Payments</h2><span class="pill">PAYMENT & BALANCE</span></div><p class="muted">Sales staff can record each repayment against its invoice. M-Pesa repayments require a unique transaction code. The invoice balance and paid status update immediately, and settled invoices remain available for review.</p>{credit_html}</section>{js}'''
    return layout(u,'Customers / Credit',content,'/customers')


def reports_page(u, form=None, message='', query=None):
    require_perm_user(u,'VIEW_REPORTS')
    if form:
        fd=form.get('from_date',''); ft=form.get('from_time','00:00'); td=form.get('to_date',''); tt=form.get('to_time','23:59')
        if not fd or not td: fd,ft,td,tt,preset=period_from_query({})
        else: preset='CUSTOM'
    else:
        fd,ft,td,tt,preset=period_from_query(query or {})
    start=f'{fd} {ft}:00'; end=f'{td} {tt}:59'; rows=[]
    try:
        if start>end: raise ValueError('From date/time must be before To date/time.')
        rows=core.report_data(start,end)
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
    exp=f'/report/export?kind=report&start={quote(start)}&end={quote(end)}&format={{fmt}}'
    total=sum(float(r['total']) for r in rows); cash=sum(float(r['cash']) for r in rows); mp=sum(float(r['mpesa']) for r in rows); units=sum(float(r['units']) for r in rows)
    content=f'''{notice}<section class="card"><div class="section-head"><h2>Sales & Monthly Reports</h2><span class="pill">{esc(preset)}</span></div>{range_toolbar('/reports',fd,ft,td,tt)}<div class="grid four" style="margin-top:12px"><div class="metric"><div class="muted">SALES</div><div class="v">{money(total)}</div></div><div class="metric"><div class="muted">CASH</div><div class="v">{money(cash)}</div></div><div class="metric"><div class="muted">M-PESA</div><div class="v">{money(mp)}</div></div><div class="metric"><div class="muted">UNITS</div><div class="v">{units:g}</div></div></div><div class="actions" style="margin-top:10px"><button type="button" class="btn" onclick="window.print()">PRINT REPORT</button><a class="btn" href="{exp.format(fmt='pdf')}">DOWNLOAD PDF</a><a class="btn" href="{exp.format(fmt='xlsx')}">DOWNLOAD EXCEL</a></div></section><section class="card report-print" style="margin-top:14px"><div class="report-head"><div><h2>{esc(APP_NAME)}</h2><div class="muted">Period: {esc(start)} → {esc(end)} · Generated by {esc(u['full_name'])} @{esc(u['username'])} at {now()}</div></div></div>{table}</section>'''
    return layout(u,'Reports',content,'/reports','End day, week, monthly and custom reporting')

def stock_report_page(u, form=None, query=None):
    require_perm_user(u,'VIEW_STOCK')
    src=query if query is not None else (form or {})
    fd,ft,td,tt,preset=period_from_query(src); start=f'{fd} {ft}:00'; end=f'{td} {tt}:59'
    rows=core.stock_report(start,end)
    total_retail_units=sum(float(r['retail_units']) for r in rows); total_wholesale_units=sum(float(r['wholesale_units']) for r in rows); total_units=total_retail_units+total_wholesale_units
    total_retail_revenue=sum(float(r['retail_revenue']) for r in rows); total_wholesale_revenue=sum(float(r['wholesale_revenue']) for r in rows); total_revenue=total_retail_revenue+total_wholesale_revenue
    channel_summary=f'''<div class="grid two" style="margin-top:12px"><section class="card"><h3>Retail Performance</h3><div>Units sold: <b>{total_retail_units:g}</b> ({(100*total_retail_units/total_units if total_units else 0):.1f}%)</div><div>Revenue: <b>{money(total_retail_revenue)}</b> ({(100*total_retail_revenue/total_revenue if total_revenue else 0):.1f}%)</div></section><section class="card"><h3>Wholesale Performance</h3><div>Units sold: <b>{total_wholesale_units:g}</b> ({(100*total_wholesale_units/total_units if total_units else 0):.1f}%)</div><div>Revenue: <b>{money(total_wholesale_revenue)}</b> ({(100*total_wholesale_revenue/total_revenue if total_revenue else 0):.1f}%)</div></section></div>'''
    table=html_table(['Product Name','Category','Shared Stock','Purchased','Total Sold','Retail Sold','Wholesale Sold','Retail Revenue','Wholesale Revenue','Retail Unit %','Wholesale Unit %','Retail Revenue %','Wholesale Revenue %','Retail Price','Wholesale Price','Avg Cost','Stock Value','Reorder','Status'],[[esc(r['name']),esc(r['category']),r['stock'],r['purchased'],r['sold'],r['retail_units'],r['wholesale_units'],money(r['retail_revenue']),money(r['wholesale_revenue']),f"{r['retail_units_pct']:.1f}%",f"{r['wholesale_units_pct']:.1f}%",f"{r['retail_revenue_pct']:.1f}%",f"{r['wholesale_revenue_pct']:.1f}%",money(r['retail_price']),money(r['wholesale_price']),money(r['avg_cost']),money(r['stock_value']),r['reorder_level'],esc(r['status'])] for r in rows])
    exp=f'/report/export?kind=stock&format={{fmt}}&start={esc(start)}&end={esc(end)}'
    content=f'''<section class="card"><div class="section-head"><h2>Stock Report</h2><span class="pill">{esc(preset)}</span></div>{range_toolbar('/stock-report',fd,ft,td,tt)}{channel_summary}<div class="actions" style="margin-top:10px"><button type="button" class="btn" onclick="window.print()">PRINT</button><a class="btn" href="{exp.format(fmt='pdf')}">DOWNLOAD PDF</a><a class="btn" href="{exp.format(fmt='xlsx')}">DOWNLOAD EXCEL</a><a class="btn danger" href="/reorder-report?from_date={quote(fd)}&from_time={quote(ft)}&to_date={quote(td)}&to_time={quote(tt)}">REORDER REPORT</a></div></section><section class="card report-print" style="margin-top:14px">{table}</section>'''
    return layout(u,'Stock Report',content,'/stock-report','Current stock and movement reporting')

def reorder_report_page(u, query=None):
    require_perm_user(u,'VIEW_STOCK')
    rows=[r for r in core.stock_report('1900-01-01 00:00:00','2999-12-31 23:59:59') if r['status'] in ('LOW STOCK','OUT OF STOCK')]
    table=html_table(['Product Name','Category','Current Stock','Reorder Level','Status','Avg Cost','Stock Value'],[[esc(r['name']),esc(r['category']),r['stock'],r['reorder_level'],esc(r['status']),money(r['avg_cost']),money(r['stock_value'])] for r in rows],'No products currently requiring reorder.')
    content=f'''<section class="card report-print"><div class="section-head"><h2>{esc(setting_label('nav_reorder_report','Reorder Report'))}</h2><span class="pill">CURRENT STOCK STATUS</span></div><div class="muted" style="margin-bottom:12px">Generated by {esc(u['full_name'])} @{esc(u['username'])} at {now()}</div><div class="actions" style="margin-bottom:12px"><button type="button" class="btn" onclick="window.print()">PRINT</button><a class="btn" href="/report/export?kind=reorder&format=pdf&start=1900-01-01%2000:00:00&end=2999-12-31%2023:59:59">DOWNLOAD PDF</a><a class="btn" href="/report/export?kind=reorder&format=xlsx&start=1900-01-01%2000:00:00&end=2999-12-31%2023:59:59">DOWNLOAD EXCEL</a></div>{table}</section>'''
    return layout(u,'Reorder Report',content,'/reorder-report','Current low-stock purchasing action list')


def live_page(u):
    require_perm_user(u,'LIVE_MONITORING'); d=core.live_monitoring(); d['active_users']=active_user_count(); mp_live=mpesa_live_messages(12)
    content=f'''<section class="card"><div class="section-head"><h2>Live Monitoring</h2><span class="pill">AUTO REFRESH</span></div><div class="grid four"><div class="card metric"><div class="muted">ACTIVE USERS</div><div class="v">{d['active_users']}</div></div><div class="card metric"><div class="muted">TODAY SALES</div><div class="v">{money(d['today_sales'])}</div></div><div class="card metric"><div class="muted">PURCHASES</div><div class="v">{money(d['today_purchases'])}</div></div><div class="card metric"><div class="muted">EXPENSES</div><div class="v">{money(d['today_expenses'])}</div></div><div class="card metric"><div class="muted">M-PESA</div><div class="v">{money(d['mpesa'])}</div></div></div></section><section class="card mpesa-live-card" style="margin-top:14px"><div class="mpesa-live-head"><div><h3 style="margin:0">Live M-Pesa Messages</h3><div class="muted">Incoming Till 1546252 payments and STK updates · refreshed automatically</div></div><span class="pill">LIVE</span></div><div id="mpesa-live-messages">{mpesa_live_messages_html(mp_live)}</div></section><div class="grid two" style="margin-top:14px"><section class="card"><div class="section-head"><h3>Recent Sales</h3><a class="btn" href="/live">REFRESH</a></div>{html_table(['Time','Receipt','Customer','Total','By'],[[esc(r['sold_at']),esc(r['receipt_no']),esc(r['customer']),money(r['total']),esc(r['user_name'])] for r in d['recent_sales']])}</section><section class="card"><h3>Recent Purchases</h3>{html_table(['Time','Purchase','Supplier','Total','By'],[[esc(r['purchased_at']),esc(r['purchase_no']),esc(r['supplier']),money(r['total']),esc(r['user_name'])] for r in d['recent_purchases']])}</section></div>'''; return layout(u,'Live Monitoring',content,'/live')


def products_page(u, message=''):
    role=role_name(u)
    if role not in ('ADMIN','SALES') or not allowed(u,'MANAGE_PRODUCTS'):
        raise PermissionError('Product Registration is disabled for your account. Contact Administrator.')
    c=core.db.c(); rows=[dict(r) for r in c.execute('SELECT * FROM products ORDER BY active DESC,name').fetchall()]; c.close()
    notice=f'<div class="flash error">{esc(message)}</div>' if message else ''
    if role=='ADMIN':
        price_rows=[]; action_rows=[]
        for r in rows:
            pid=r['id']; wholesale_value=r.get('wholesale_price') if r.get('wholesale_price') is not None else r['sell_price']
            price_rows.append([
                esc(r['name']), esc(r['sku']),
                f'<input aria-label="Retail price for {esc(r["name"])}" name="bulk_sell_{pid}" type="number" min="0.01" step="0.01" value="{float(r["sell_price"] or 0):.2f}" required style="width:120px">',
                f'<input aria-label="Wholesale price for {esc(r["name"])}" name="bulk_wholesale_{pid}" type="number" min="0.01" step="0.01" value="{float(wholesale_value or 0):.2f}" required style="width:120px">',
                f'<input aria-label="COGS unit cost for {esc(r["name"])}" name="bulk_cost_{pid}" type="number" min="0" step="0.01" value="{float(r["avg_cost"] or 0):.2f}" required style="width:120px">',
                r['stock_qty'], money(float(r['stock_qty'] or 0)*float(r['avg_cost'] or 0)),
                f'<input aria-label="Reorder level for {esc(r["name"])}" name="bulk_reorder_{pid}" type="number" min="0" step="1" value="{float(r["reorder_level"] or 0):g}" required style="width:90px">',
                'ACTIVE' if r['active'] else 'ARCHIVED'
            ])
            stock=f'''<form method="post" action="/products" class="inline-actions">{csrf_input(CURRENT_HANDLER)}<input type="hidden" name="action" value="stock"><input type="hidden" name="product_id" value="{pid}"><input name="new_stock" aria-label="New stock quantity" type="number" step="1" min="0" value="{r['stock_qty']}" required style="width:90px"><input name="reason" placeholder="Stock correction reason" required minlength="5" style="width:180px"><button class="btn">ADJUST STOCK</button></form>'''
            action_btn=f'''<form method="post" action="/products" class="inline-actions" onsubmit="return confirm('Archive this product? Historical records will be preserved.');">{csrf_input(CURRENT_HANDLER)}<input type="hidden" name="action" value="delete"><input type="hidden" name="product_id" value="{pid}"><input name="reason" placeholder="Reason required" required minlength="5" style="width:180px"><button class="btn danger">ARCHIVE</button></form>''' if r['active'] else f'''<form method="post" action="/products" class="inline-actions">{csrf_input(CURRENT_HANDLER)}<input type="hidden" name="action" value="restore"><input type="hidden" name="product_id" value="{pid}"><input name="reason" placeholder="Reason required" required minlength="5" style="width:180px"><button class="btn">RESTORE</button></form>'''
            action_rows.append([esc(r['name']),stock,action_btn])
        bulk_table=html_table(['Product','SKU','Retail Price','Wholesale Price','COGS / Unit Cost','Shared Stock','Stock Value at Cost','Reorder Level','Status'],price_rows,'No products registered.')
        action_table=html_table(['Product','Stock Quantity Adjustment','Archive / Restore'],action_rows,'No products registered.')
        extra=f'''<section class="card" style="margin-top:14px"><div class="notice"><b>QUICK STOCK EDIT:</b> Edit any number of retail prices, wholesale prices, COGS/unit costs and reorder levels, then click SAVE ALL STOCK CHANGES once. No confirmation or reason is required; the system records an automatic audit note. Stock quantities are preserved and adjusted separately.</div><form method="post" action="/products" id="bulk-stock-edit">{csrf_input(CURRENT_HANDLER)}<input type="hidden" name="action" value="bulk_save">{bulk_table}<div class="actions" style="margin-top:14px"><button class="btn gold" type="submit">SAVE ALL STOCK CHANGES</button><button class="btn" type="reset">CANCEL UNSAVED CHANGES</button></div></form><h3 style="margin-top:24px">Separate Stock Quantity / Product Status Actions</h3><p class="muted">Use these only when you need to correct physical stock or archive/restore a product. They require their own reason to preserve the stock audit trail.</p>{action_table}</section>'''
        title='Product Registration & Bulk Stock Editor'; subtitle='Edit many products and save once'
    else:
        table=html_table(['Product Name','SKU','Category','Unit','Retail / Wholesale Price','Stock','Reorder','Status'],[[esc(r['name']),esc(r['sku']),esc(r['category']),esc(r['unit']),money(r['sell_price'])+' / '+money(r.get('wholesale_price') or r['sell_price']),r['stock_qty'],r['reorder_level'],'ACTIVE' if r['active'] else 'ARCHIVED'] for r in rows])
        extra=f'<section class="card" style="margin-top:14px"><div class="notice">Sales Person can register products with separate retail and wholesale prices. Both prices draw from the same shared stock; existing prices and stock costs can only be changed by the Administrator.</div>{table}</section>'
        title='Product Registration'; subtitle='Sales Person product registration access'
    content=f'''{notice}<section class="card"><div class="section-head"><h2>{esc(title)}</h2><span class="pill">{esc(u['full_name'])} · {esc(role_display(role))}</span></div><form method="post" action="/products"><div class="form-grid">{csrf_input(CURRENT_HANDLER)}<input type="hidden" name="action" value="create"><div><label>SKU *</label><div class="scan-inline"><div><input name="sku" id="product_sku" required></div><button type="button" class="btn scan-btn" onclick="openScannerFill('product_sku')">SCAN</button></div></div><div><label>Product Name *</label><input name="name" required></div><div><label>Category *</label><input name="category" placeholder="Wine, Spirit, Beer..." required></div><div><label>Unit</label><input name="unit" value="{esc(core.get_setting('default_unit','bottle'))}" required></div><div><label>Retail Selling Price</label><input type="number" name="sell_price" min="0.01" step="0.01" required></div><div><label>Wholesale Selling Price</label><input type="number" name="wholesale_price" min="0.01" step="0.01" required><small class="muted">Both prices use the same shared stock.</small></div><div><label>Opening Stock</label><input type="number" name="stock_qty" min="0" step="1" required></div><div><label>COGS / Unit Cost (KSh) — Cost of Goods Sold *</label><input type="number" name="cogs_unit_cost" min="0" step="0.01" value="0" required><small class="muted">Enter the cost you pay per unit. Required to calculate accurate profit and COGS.</small></div><div><label>Reorder Level</label><input type="number" name="reorder_level" min="0" step="1" value="{esc(core.get_setting('default_reorder_level','5'))}" required></div></div><div class="actions" style="margin-top:14px"><button class="btn gold" type="submit">SAVE PRODUCT</button></div></form></section>{extra}'''
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
        return html_table(['Award','Rank',who,'Transactions','Units Sold','Sales Revenue','Share of Revenue'],[[award_for_rank(r['rank'],r['revenue'],total),f"#{r['rank']}",esc(r['name'] if who=='Branch' else r['full_name'])+((' · '+esc(r['branch_name'])) if who!='Branch' else ''),r['transactions'],f"{float(r['units'] or 0):g}",money(r['revenue']),f"{r['share']:.1f}%"] for r in rows], 'No sales recorded for this period.')
    best_b=branch_rows[0] if branch_rows and float(branch_rows[0]['revenue'] or 0)>0 else None; low_b=branch_rows[-1] if branch_rows else None
    best_e=employee_rows[0] if employee_rows and float(employee_rows[0]['revenue'] or 0)>0 else None; low_e=employee_rows[-1] if employee_rows else None
    summary=f'''<div class="grid two"><section class="card"><h3>🏆 Best Performing Branch</h3><div class="v">{esc(best_b['name']) if best_b else 'No sales yet'}</div><div>{money(best_b['revenue']) if best_b else money(0)}</div></section><section class="card"><h3>⚠ Lowest Performing Branch</h3><div class="v">{esc(low_b['name']) if low_b else 'No branches'}</div><div>{money(low_b['revenue']) if low_b else money(0)}</div></section><section class="card"><h3>🏆 Best Performing Sales Person</h3><div class="v">{esc(best_e['full_name']) if best_e else 'No sales yet'}</div><div>{money(best_e['revenue']) if best_e else money(0)}{(' · '+esc(best_e['branch_name'])) if best_e else ''}</div></section><section class="card"><h3>⚠ Lowest Performing Sales Person</h3><div class="v">{esc(low_e['full_name']) if low_e else 'No sales yet'}</div><div>{money(low_e['revenue']) if low_e else money(0)}{(' · '+esc(low_e['branch_name'])) if low_e else ''}</div></section></div>'''
    content=f'''<section class="card"><div class="section-head"><h2>Branch & Employee Performance</h2><span class="pill">RANKED BY SALES REVENUE</span></div>{range_toolbar('/branch-performance',fd,ft,td,tt)}<div class="muted" style="margin-top:8px">Period: {esc(start)} to {esc(end)}. 🥇 Gold = first, 🥈 Silver = second, 🥉 Bronze = third, Participant = fourth onward. Awards are recalculated from recorded sales for the selected period, so you can revisit any previous month by selecting its dates. Shares are calculated from the total revenue of the listed branches or employees. Sales are attributed to the branch recorded when each sale was made.</div></section><div style="margin-top:14px">{summary}</div><section class="card" style="margin-top:14px"><h2>Branch Performance Ranking</h2>{rank_table(branch_rows,'Branch')}</section><section class="card" style="margin-top:14px"><h2>Sales Person Performance Ranking</h2>{rank_table(employee_rows,'Employee')}</section><section class="card" style="margin-top:14px"><button class="btn" onclick="window.print()" type="button">PRINT PERFORMANCE REPORT</button> <a class="btn" href="/report/export?kind=branch-performance&format=pdf&start={quote(start)}&end={quote(end)}">DOWNLOAD PDF</a> <a class="btn" href="/report/export?kind=branch-performance&format=xlsx&start={quote(start)}&end={quote(end)}">DOWNLOAD EXCEL</a></section>'''
    return layout(u,'Branch Performance',content,'/branch-performance','Best and lowest performing branches and sales staff')


def settings_page(u, message=''):
    if role_name(u)!='ADMIN': raise PermissionError('Administrator permission required.')
    s=core.get_settings(); payload=core.permissions_payload(u); notice=f'<div class="flash error">{esc(message)}</div>' if message else ''
    role_rows=[]
    for d in payload['definitions']:
        k=d['key']; m='checked' if payload['roles']['MANAGEMENT'].get(k) else ''; sa='checked' if payload['roles']['SALES'].get(k) else ''
        role_rows.append([esc(d['label']),esc(d['description']),f'<input type="checkbox" name="perm_m_{esc(k)}" {m}>',f'<input type="checkbox" name="perm_s_{esc(k)}" {sa}>'])
    main_fields=[('business_name','Business Name','text',''),('business_subtitle','Business Subtitle','text',''),('business_phone','Business Phone','text',''),('business_address','Business Address','text',''),('business_website','Business Website','text',''),('business_tax_pin','KRA PIN / Tax PIN','text',''),('business_branch_name','Branch / Seat Name','text',''),('receipt_footer','Receipt Footer Message','text',''),('report_footer','Report Footer Message','text',''),('currency','Currency','text',''),('receipt_prefix','Receipt Prefix','text',''),('idle_timeout_minutes','Auto Logout (Minutes)','number','min="1" max="120"'),('live_refresh_seconds','Live Refresh (Seconds)','number','min="2" max="120"'),('default_reorder_level','Default Reorder Level','number','min="0"'),('default_unit','Default Product Unit','text',''),('default_payment_transactions','Default Payment Transactions','number','min="1" max="5"'),('max_payment_transactions','Maximum Payment Transactions','number','min="1" max="5"'),('report_default_days','Default Report Days','number','min="1" max="365"'),('performance_green_threshold_pct','Business Performance Green Threshold (%)','number','min="1" max="100"')]
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
    content=f'''{notice}<section class="card"><div class="section-head"><h2>Administrator Settings</h2><span class="pill">NO CODING REQUIRED</span></div><form method="post" action="/settings">{csrf_input(CURRENT_HANDLER)}<div><label>Reason for these changes (required)</label><input name="change_reason" required minlength="5" placeholder="Explain why these settings are changing"></div><div class="form-grid">{fields}</div><h3 style="margin-top:18px">Role & Menu Labels</h3><div class="form-grid">{label_html}</div><div class="actions" style="margin-top:14px"><button class="btn gold">SAVE SETTINGS</button></div></form></section><section class="card" style="margin-top:14px"><div class="section-head"><h3>Report Button Labels</h3><span class="pill">EDIT WITHOUT CODING</span></div><form method="post" action="/settings">{csrf_input(CURRENT_HANDLER)}<div><label>Reason for these changes (required)</label><input name="change_reason" required minlength="5" placeholder="Explain why report labels are changing"></div><div class="form-grid">{report_html}</div><div class="actions" style="margin-top:12px"><button class="btn gold">SAVE REPORT LABELS</button></div></form></section><section class="card" style="margin-top:14px"><div class="section-head"><h3>Business Logo / Picture</h3><span class="pill">MAX 5 MB</span></div><p class="muted">{esc(logo_note)}</p><div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap"><img src="/business-logo" style="width:90px;height:90px;object-fit:contain;border:1px solid var(--line);border-radius:12px;background:#fff;padding:8px"><form method="post" action="/settings/logo" enctype="multipart/form-data">{csrf_input(CURRENT_HANDLER)}<input name="change_reason" placeholder="Reason for logo change" required minlength="5"><input type="file" name="logo" accept="image/png,image/jpeg,image/webp" required><div style="margin-top:10px"><button class="btn gold">UPLOAD BUSINESS LOGO</button></div></form><form method="post" action="/settings/logo">{csrf_input(CURRENT_HANDLER)}<input type="hidden" name="remove_logo" value="1"><input name="change_reason" placeholder="Reason for logo removal" required minlength="5"><button class="btn danger">REMOVE CUSTOM LOGO</button></form></div></section>{mpesa_section}<section class="card" style="margin-top:14px"><div class="section-head"><h3>MONTEZ LIQUOR Software Subscription</h3><span class="pill">ADMINISTRATOR ONLY</span></div><p class="muted">Monthly subscription. November 2026 is exempt; the first payment is due 5 December 2026. Thereafter, payment is due on the 5th of each month. Non-administrator access is automatically suspended when the due date passes without confirmation. Administrator access remains available.</p><div class="form-grid"><div><label>Monthly Subscription Amount (KSh)</label><input value="{esc(s.get('subscription_monthly_amount','Not set'))}" readonly></div><div><label>Paybill Number</label><input value="516600" readonly></div><div><label>Account Number</label><input value="319851" readonly></div><div><label>Due Date</label><input value="5th of every month" readonly></div><div><label>Billing Status</label><input value="{ 'SUSPENDED — PAYMENT OVERDUE' if subscription_billing_state().get('suspended') else ('PAID' if subscription_billing_state().get('paid') else 'UPCOMING / NOT YET DUE') }" readonly></div><div><label>Billing Month</label><input value="{esc(subscription_billing_state().get('due_month') or 'Exempt until December 2026')}" readonly></div></div><form method="post" action="/settings" style="margin-top:12px">{csrf_input(CURRENT_HANDLER)}<input type="hidden" name="subscription_settings_only" value="1"><div class="form-grid"><div><label>Reason for subscription setting change (required)</label><input name="change_reason" required minlength="5" placeholder="Explain why the subscription amount is changing"></div><div><label>Monthly Subscription Amount (KSh)</label><input type="number" name="subscription_monthly_amount" min="1" max="10000000" step="0.01" required value="{esc(s.get('subscription_monthly_amount',''))}" placeholder="Enter monthly amount"></div></div><div class="actions" style="margin-top:12px"><button class="btn gold">SAVE SUBSCRIPTION SETTINGS</button></div></form><hr><h3>Confirm Monthly M-PESA Payment</h3><p class="muted">After checking the payment in the Paybill statement, the Administrator enters the transaction code here. This is a manual verification step; it does not yet connect to Paybill 516600 automatically.</p><form method="post" action="/subscription/confirm">{csrf_input(CURRENT_HANDLER)}<div class="form-grid"><div><label>Billing Month</label><input name="billing_month" value="{esc(subscription_billing_state().get('due_month') or '')}" readonly required></div><div><label>Amount Received (KSh)</label><input type="number" name="amount" min="0.01" step="0.01" value="{esc(s.get('subscription_monthly_amount',''))}" required></div><div><label>M-PESA Transaction Code</label><input name="mpesa_code" maxlength="15" minlength="8" required placeholder="Enter receipt code"></div><div><label>Reason / Verification Note</label><input name="change_reason" minlength="5" required placeholder="Verified against Paybill statement"></div></div><div class="actions" style="margin-top:12px"><button class="btn gold">CONFIRM SUBSCRIPTION PAYMENT</button></div></form><p class="muted">Last confirmed: {esc(s.get('subscription_last_paid_at','Not yet paid'))} · M-PESA code: {esc(s.get('subscription_last_mpesa_code','—'))}</p></section><section class="card" style="margin-top:14px"><div class="section-head"><h3>Management & Sales Permissions</h3><span class="pill">TICK TO ALLOW · UNTICK TO RESTRICT</span></div><p class="muted">Administrator / Developer always has full access. Every permission below can be granted or restricted independently for Management and Sales Person.</p><form method="post" action="/settings"><input type="hidden" name="permissions_only" value="1">{csrf_input(CURRENT_HANDLER)}<div><label>Reason for permission changes (required)</label><input name="change_reason" required minlength="5" placeholder="Explain why access is changing"></div>{html_table(['Permission','Purpose','Management','Sales Person'],role_rows)}<div class="actions" style="margin-top:12px"><button class="btn gold">SAVE ROLE PERMISSIONS</button></div></form></section>'''
    return layout(u,'Administrator Settings',content,'/settings','Overall system parameters and permissions')


def ledger_page(u, query=None):
    require_perm_user(u,'VIEW_LEDGER')
    fd,ft,td,tt,preset=period_from_query(query or {}); start=f'{fd} {ft}:00'; end=f'{td} {tt}:59'; rows=core.ledger_data(start,end)
    content=f'''<section class="card"><div class="section-head"><h2>General Ledger</h2><span class="pill">{esc(preset)}</span></div>{range_toolbar('/ledger',fd,ft,td,tt)}<div class="muted" style="margin-top:8px">Generated by {esc(u['full_name'])} @{esc(u['username'])} at {now()}</div><div class="actions" style="margin-top:10px"><button type="button" class="btn" onclick="window.print()">PRINT</button><a class="btn" href="/ledger/export?format=pdf&start={esc(start)}&end={esc(end)}">DOWNLOAD PDF</a><a class="btn" href="/ledger/export?format=xlsx&start={esc(start)}&end={esc(end)}">DOWNLOAD EXCEL</a></div></section><section class="card report-print" style="margin-top:14px">{html_table(['Date & Time','Reference','Description','Debit Account','Debit','Credit Account','Credit'],[[esc(a),esc(b),esc(c),esc(d),money(e),esc(f),money(g)] for a,b,c,d,e,f,g in rows])}</section>'''
    return layout(u,'Ledger',content,'/ledger','Custom accounting ledger')

def pl_page(u, query=None):
    require_perm_user(u,'VIEW_LEDGER')
    fd,ft,td,tt,preset=period_from_query(query or {}); start=f'{fd} {ft}:00'; end=f'{td} {tt}:59'; d=core.pl_summary(start,end)
    cogs_notice=(f'<div class="flash error" style="margin-top:12px">COGS DATA WARNING: {d["cogs_missing_lines"]} sale line(s) have no usable unit cost and {d["cogs_estimated_lines"]} historic line(s) use an estimated current average cost. Profit may be inaccurate for these lines. Correct current stock cost in Product Registration → SET AVG COST; historical missing costs require source records to reconstruct accurately.</div>' if (d['cogs_missing_lines'] or d['cogs_estimated_lines']) else f'<div class="notice" style="margin-top:12px">COGS coverage: {d["cogs_coverage_pct"]:.1f}% of sale lines use a recorded sale-time cost.</div>')
    content=f'''<section class="card"><div class="section-head"><h2>Profit & Loss</h2><span class="pill">{esc(preset)}</span></div>{range_toolbar('/profit-loss',fd,ft,td,tt)}<div class="muted" style="margin-top:8px">Generated by {esc(u['full_name'])} @{esc(u['username'])} at {now()}</div><div class="grid four" style="margin-top:12px"><div class="metric"><div class="muted">SALES REVENUE</div><div class="v">{money(d['revenue'])}</div></div><div class="metric"><div class="muted">COGS</div><div class="v">{money(d['cogs'])}</div></div><div class="metric"><div class="muted">GROSS PROFIT</div><div class="v">{money(d['gross_profit'])}</div></div><div class="metric"><div class="muted">NET PROFIT</div><div class="v">{money(d['net_profit'])}</div></div></div>{cogs_notice}<div class="muted" style="margin-top:8px">COGS is based on the unit-cost snapshot saved with each sale line, not today's selling price.</div><div class="actions" style="margin-top:12px"><button type="button" class="btn" onclick="window.print()">PRINT</button><a class="btn" href="/pl/export?format=pdf&start={esc(start)}&end={esc(end)}">DOWNLOAD PDF</a><a class="btn" href="/pl/export?format=xlsx&start={esc(start)}&end={esc(end)}">DOWNLOAD EXCEL</a></div></section>'''
    return layout(u,'Profit & Loss',content,'/profit-loss','Custom date and time financial performance')

def backup_page(u):
    if role_name(u)!='ADMIN': raise PermissionError('Administrator permission required.')
    content=f'<section class="card"><h2>Backup</h2><p class="muted">Create and download a full SQLite database backup.</p><a class="btn gold" href="/backup/download">DOWNLOAD BACKUP</a></section>'
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
        require_perm_user(u,'VIEW_REPORTS'); rows=core.report_data(start,end)
        headers=['Receipt','Date/Time','Customer','Units','Total','Cash','M-Pesa','Attended By']
        data=[[r['receipt_no'],r['sold_at'],r['customer'],r['units'],r['total'],r['cash'],r['mpesa'],r['attended_by']] for r in rows]
        title=f"{core.get_setting('business_name',APP_NAME)} · {core.get_setting('business_branch_name','')} · SALES REPORT"
    elif kind=='stock':
        require_perm_user(u,'VIEW_STOCK'); rows=core.stock_report(start,end)
        headers=['Product Name','Category','Shared Stock','Purchased','Total Sold','Retail Units','Wholesale Units','Retail Revenue','Wholesale Revenue','Retail Units %','Wholesale Units %','Retail Revenue %','Wholesale Revenue %','Retail Price','Wholesale Price','Avg Cost','Stock Value','Reorder','Status']
        data=[[r['name'],r['category'],r['stock'],r['purchased'],r['sold'],r['retail_units'],r['wholesale_units'],r['retail_revenue'],r['wholesale_revenue'],r['retail_units_pct'],r['wholesale_units_pct'],r['retail_revenue_pct'],r['wholesale_revenue_pct'],r['retail_price'],r['wholesale_price'],r['avg_cost'],r['stock_value'],r['reorder_level'],r['status']] for r in rows]
        title=f"{core.get_setting('business_name',APP_NAME)} · {core.get_setting('business_branch_name','')} · STOCK REPORT"
    elif kind=='reorder':
        require_perm_user(u,'VIEW_STOCK'); rows=[r for r in core.stock_report(start,end) if r['status'] in ('LOW STOCK','OUT OF STOCK')]
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
        require_perm_user(u,'VIEW_REPORTS'); rows=core.expense_report_data(start,end)
        headers=['Date/Time','Expense','Category','Description','Amount','Payment','Reference','Recorded By']
        data=[[r['expense_date'],r['expense_no'],r['category'],r['description'],r['amount'],r['payment_method'],r['reference'] or '',r['attended_by']] for r in rows]
        title='MONTEZ LIQUOR EXPENSE REPORT'
    elif kind=='purchases':
        require_perm_user(u,'VIEW_REPORTS'); rows=core.purchase_report_data(start,end)
        headers=['Date/Time','Purchase','Supplier','Payment','Total','Units','Reference','Recorded By']
        data=[[r['purchased_at'],r['purchase_no'],r['supplier'],r['payment_method'],r['total'],r['units'],r['reference'] or '',r['attended_by']] for r in rows]
        title='MONTEZ LIQUOR PURCHASE REPORT'
    else:
        raise ValueError('Unknown report type.')
    st=core.get_settings(); title=f"{st.get('business_name',APP_NAME)} · {st.get('business_branch_name','')} · {title}"
    out,ctype,fn=_export_rows(title,headers,data,fmt)
    return send_file(handler,out,ctype,fn)

def export_ledger_v2(handler,u,qs):
    require_perm_user(u,'VIEW_LEDGER'); require_perm_user(u,'PRINT_EXPORT')
    start=_qsval(qs,'start'); end=_qsval(qs,'end'); fmt=_qsval(qs,'format','xlsx').lower()
    if not start or not end: raise ValueError('Report period is missing.')
    rows=core.ledger_data(start,end); headers=['Date & Time','Reference','Description','Debit Account','Debit','Credit Account','Credit']
    st=core.get_settings(); out,ctype,fn=_export_rows(f"{st.get('business_name',APP_NAME)} · {st.get('business_branch_name','')} · GENERAL LEDGER",headers,rows,fmt); return send_file(handler,out,ctype,fn)

def export_pl_v2(handler,u,qs):
    require_perm_user(u,'VIEW_LEDGER'); require_perm_user(u,'PRINT_EXPORT')
    start=_qsval(qs,'start'); end=_qsval(qs,'end'); fmt=_qsval(qs,'format','xlsx').lower()
    if not start or not end: raise ValueError('Report period is missing.')
    d=core.pl_summary(start,end); rows=[['Sales Revenue',d['revenue']],['Cost of Goods Sold',d['cogs']],['Gross Profit',d['gross_profit']],['Operating Expenses',d['expenses']],['Net Profit',d['net_profit']],['COGS Reliable Cost Coverage (%)',round(d['cogs_coverage_pct'],2)],['Sale Lines Missing Cost',d['cogs_missing_lines']],['Sale Lines Using Estimated Cost',d['cogs_estimated_lines']]]
    headers=['Profit & Loss Item','Amount / Count']; st=core.get_settings(); out,ctype,fn=_export_rows(f"{st.get('business_name',APP_NAME)} · {st.get('business_branch_name','')} · PROFIT AND LOSS",headers,rows,fmt); return send_file(handler,out,ctype,fn)

def mpesa_test_connection(u):
    if role_name(u)!='ADMIN': raise PermissionError('Administrator permission required.')
    mpesa.get_access_token(core)
    return {'message':'M-Pesa/Daraja authentication successful.'}

def mpesa_stk_request(u, amount, phone, batch_payload=None, sequence_no=1):
    require_perm_user(u,'CREATE_SALE')
    if core.get_setting('mpesa_enabled','NO')!='YES': raise mpesa.MpesaError('M-Pesa integration is disabled in Administrator Settings.')
    amt=float(amount); phone_n=mpesa.normalize_phone(phone)
    if amt<=0: raise mpesa.MpesaError('Payment amount must be greater than zero.')
    c=core.db.c(); ts=core.now_str(); batch_id=None
    try:
        # First STK request can create the pending M-Pesa-only sale batch.
        if batch_payload and batch_payload.get('all_mpesa'):
            pid=int(batch_payload['product_id']); qty=int(batch_payload['qty']); total=float(batch_payload['total']); n=int(batch_payload['payment_count'])
            if sequence_no<1 or sequence_no>n: raise mpesa.MpesaError('Invalid payment transaction number.')
            existing_rows=c.execute("SELECT id,customer_json FROM pending_payment_batches WHERE user_id=? AND status='PENDING' AND product_id=? AND ABS(total-?)<0.01 ORDER BY id DESC LIMIT 20",(u['id'],pid,total)).fetchall()
            requested_sale_type=str(batch_payload.get('sale_type','RETAIL')).upper()
            existing=None
            for candidate in existing_rows:
                try: candidate_type=str(json.loads(candidate['customer_json'] or '{}').get('sale_type','RETAIL')).upper()
                except Exception: candidate_type='RETAIL'
                if candidate_type==requested_sale_type:
                    existing=candidate; break
            if existing: batch_id=existing['id']
            else:
                p=c.execute('SELECT id,sell_price,stock_qty FROM products WHERE id=? AND active=1',(pid,)).fetchone()
                if not p or p['stock_qty']<qty: raise mpesa.MpesaError('Insufficient stock for this pending sale.')
                cur=c.execute('INSERT INTO pending_payment_batches(created_at,updated_at,user_id,total,product_id,qty,customer_json,due_date,collateral,collateral_value,payment_count,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(ts,ts,u['id'],total,pid,qty,json.dumps({'sale_type':str(batch_payload.get('sale_type','RETAIL')).upper()},ensure_ascii=False),'','',0,n,'PENDING')); batch_id=cur.lastrowid
                for j in range(1,n+1):
                    amt_j=float(batch_payload.get(f'payment_amount_{j}',0) or 0)
                    c.execute('INSERT INTO pending_payment_allocations(batch_id,sequence_no,amount,payment_method,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',(batch_id,j,amt_j,'MPESA','PENDING',ts,ts))
        client_ref='K'+secrets.token_hex(6).upper(); account_ref=(core.get_setting('mpesa_account_reference_prefix','MONTEZ')+client_ref)[-12:]
        data=mpesa.stk_push(core,amt,phone_n,account_ref,core.get_setting('mpesa_transaction_desc','MONTEZ LIQUOR'))
        checkout=data.get('CheckoutRequestID'); merchant=data.get('MerchantRequestID')
        if not checkout: raise mpesa.MpesaError(data.get('ResponseDescription') or data.get('errorMessage') or 'No CheckoutRequestID returned by M-Pesa.')
        c.execute('INSERT INTO mpesa_stk_requests(created_at,user_id,checkout_request_id,merchant_request_id,client_ref,amount,phone,status,raw_response,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',(ts,u['id'],checkout,merchant,client_ref,amt,phone_n,'PENDING',json.dumps(data,ensure_ascii=False),ts))
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
    phone_raw=str(form.get('phone','')).strip(); pid=int(form.get('product_id','0') or 0); qty=int(form.get('qty','0') or 0); sale_type=str(form.get('sale_type','RETAIL') or 'RETAIL').upper()
    if sale_type not in ('RETAIL','WHOLESALE'): raise ValueError('Invalid sale type.')
    if not phone_raw: raise ValueError('Enter the customer M-Pesa phone number.')
    if pid<=0 or qty<=0: raise ValueError('Select a product and enter a valid quantity.')
    phone=mpesa.normalize_phone(phone_raw); c=core.db.c()
    try:
        p=c.execute('SELECT * FROM products WHERE id=? AND active=1',(pid,)).fetchone()
        if not p: raise ValueError('Product not found.')
        if float(p['stock_qty'])<qty: raise ValueError(f'Insufficient stock. Available: {p["stock_qty"]}.')
        unit_price=float(p['wholesale_price'] or 0) if sale_type=='WHOLESALE' else float(p['sell_price']); total=round(unit_price*qty,2)
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
        d=core.create_sale(u,{'cart':cart,'payments':payments,'customer':{},'credit':None,'sale_type':sale_type})
        c2=core.db.c()
        try:
            for code in codes:
                c2.execute('UPDATE mpesa_transactions SET status="AUTO_SAVED",matched_sale_payment_id=(SELECT id FROM sale_payments WHERE sale_id=? AND UPPER(mpesa_code)=?) WHERE UPPER(trans_id)=?',(d['sale_id'],code,code))
            c2.commit()
        finally: c2.close()
        return {'ok':True,'sale_id':d['sale_id'],'receipt_no':d['receipt_no'],'matched':accumulated,'remaining':0.0,'transactions':[dict(r) for r in selected]}
    finally: c.close()

def mpesa_stk_status(checkout):
    c=core.db.c(); r=c.execute('SELECT checkout_request_id,status,mpesa_code,amount,phone,transaction_date,sale_id FROM mpesa_stk_requests WHERE checkout_request_id=?',(checkout,)).fetchone(); c.close();
    if not r: return {'status':'NOT_FOUND'}
    d=dict(r); d['auto_saved']=bool(d.get('sale_id'))
    return d

def finalize_pending_mpesa_batch(batch_id):
    c=core.db.c(); b=c.execute('SELECT * FROM pending_payment_batches WHERE id=? AND status="PENDING"',(batch_id,)).fetchone()
    if not b: c.close(); return None
    rows=c.execute('SELECT * FROM pending_payment_allocations WHERE batch_id=? ORDER BY sequence_no',(batch_id,)).fetchall()
    if len(rows)!=b['payment_count'] or any(r['status']!='SUCCESS' or not r['mpesa_code'] for r in rows): c.close(); return None
    total=sum(float(r['amount']) for r in rows)
    if abs(total-float(b['total']))>0.009: c.close(); return None
    p=c.execute('SELECT * FROM products WHERE id=? AND active=1',(b['product_id'],)).fetchone()
    if not p or float(p['stock_qty'])<float(b['qty']): c.close(); return None
    c.close()
    payments=[{'amount':float(r['amount']),'payment_method':'MPESA','mpesa_code':r['mpesa_code']} for r in rows]
    u=core.db.c(); ur=u.execute('SELECT * FROM users WHERE id=? AND active=1',(b['user_id'],)).fetchone(); u.close()
    if not ur: return None
    
    try:
        customer_payload=json.loads(b['customer_json'] or '{}')
    except Exception:
        customer_payload={}
    sale=core.create_sale(dict(ur),{'cart':[{'product_id':b['product_id'],'qty':b['qty']}],'payments':payments,'customer':customer_payload,'credit':None,'sale_type':str(customer_payload.get('sale_type','RETAIL')).upper()})
    c=core.db.c(); ts=core.now_str(); c.execute('UPDATE pending_payment_batches SET status="COMPLETED",created_sale_id=?,updated_at=? WHERE id=?',(sale['sale_id'],ts,batch_id)); c.execute('UPDATE mpesa_stk_requests SET sale_id=?,updated_at=? WHERE checkout_request_id IN (SELECT checkout_request_id FROM pending_payment_allocations WHERE batch_id=?)',(sale['sale_id'],ts,batch_id)); c.commit(); c.close(); return sale

def mpesa_receive_stk(payload):
    d=mpesa.parse_stk_callback(payload); c=core.db.c(); r=c.execute('SELECT id FROM mpesa_stk_requests WHERE checkout_request_id=?',(d['checkout_request_id'],)).fetchone(); status='PENDING'
    if r:
        status='SUCCESS' if d['result_code']==0 and d['mpesa_code'] else ('FAILED' if d['result_code']!=0 else 'PENDING')
        ts=core.now_str(); c.execute('UPDATE mpesa_stk_requests SET status=?,mpesa_code=?,transaction_date=?,raw_response=?,updated_at=? WHERE checkout_request_id=?',(status,d['mpesa_code'],d['transaction_date'],json.dumps(payload,ensure_ascii=False),ts,d['checkout_request_id']))
        if status=='SUCCESS':
            a=c.execute('SELECT id,batch_id FROM pending_payment_allocations WHERE checkout_request_id=?',(d['checkout_request_id'],)).fetchone()
            if a: c.execute('UPDATE pending_payment_allocations SET status="SUCCESS",mpesa_code=?,updated_at=? WHERE id=?',(d['mpesa_code'],ts,a['id']))
    c.commit(); c.close()
    sale=None
    if status=='SUCCESS':
        c2=core.db.c(); a2=c2.execute('SELECT batch_id FROM pending_payment_allocations WHERE checkout_request_id=?',(d['checkout_request_id'],)).fetchone(); c2.close()
        if a2: sale=finalize_pending_mpesa_batch(a2['batch_id'])
    d['auto_saved']=bool(sale); d['sale_id']=sale['sale_id'] if sale else None
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
            old_qty=float(row['qty']); old_cost=float(row['unit_cost']); current_qty=float(prod['stock_qty']); current_cost=float(prod['avg_cost'])
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
                c.execute('UPDATE purchases SET supplier=?,payment_method=?,total=?,reference=? WHERE id=?',(supplier,method,new_total,ref,rid)); c.execute('UPDATE purchase_lines SET qty=?,unit_cost=?,total=? WHERE id=?',(new_qty,new_cost,new_total,row['line_id'])); c.execute('UPDATE products SET stock_qty=?,avg_cost=?,updated_at=? WHERE id=?',(new_stock,new_avg,ts,row['product_id']))
                c.execute('UPDATE stock_movements SET qty=?,unit_cost=?,after_qty=before_qty+?,reference=? WHERE movement_type="PURCHASE" AND reference=?',(new_qty,new_cost,new_qty,row['purchase_no'],row['purchase_no']))
                after=f'supplier={supplier}; qty={new_qty}; unit_cost={new_cost:.2f}; total={new_total:.2f}; method={method}; reference={ref}; resulting_stock={new_stock:g}; avg_cost={new_avg:.2f}'
                core.db.audit(c,u,'ADMIN_EDIT','PURCHASE',rid,f'reason={reason}; before=[{before}]; after=[{after}]; historical sale COGS snapshots unchanged')
            elif action=='delete':
                # Reverse only the units that are still physically in stock; sold units stay reflected in historical sales.
                qty_reversed=min(current_qty,old_qty)
                new_stock=max(0.0,current_qty-qty_reversed); inventory_value=max(0.0,current_qty*current_cost-qty_reversed*old_cost); new_avg=inventory_value/new_stock if new_stock>0 else 0.0
                c.execute('UPDATE products SET stock_qty=?,avg_cost=?,updated_at=? WHERE id=?',(new_stock,new_avg,ts,row['product_id']))
                c.execute('DELETE FROM stock_movements WHERE movement_type="PURCHASE" AND reference=?',(row['purchase_no'],)); c.execute('DELETE FROM purchases WHERE id=?',(rid,))
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
                    prod=c.execute('SELECT stock_qty FROM products WHERE id=?',(line['product_id'],)).fetchone()
                    if prod:
                        before=float(prod['stock_qty']); after=before+float(line['qty']); c.execute('UPDATE products SET stock_qty=?,updated_at=? WHERE id=?',(after,ts,line['product_id']))
                        c.execute('INSERT INTO stock_movements(movement_time,user_id,product_id,movement_type,qty,before_qty,after_qty,unit_cost,reference) VALUES(?,?,?,?,?,?,?,?,?)',(ts,u['id'],line['product_id'],'ADMIN_SALE_REVERSAL',float(line['qty']),before,after,0.0,'DELETE '+row['receipt_no']))
                c.execute('UPDATE mpesa_stk_requests SET sale_id=NULL WHERE sale_id=?',(rid,)); c.execute('DELETE FROM credit_payments WHERE credit_id IN (SELECT id FROM credit_accounts WHERE sale_id=?)',(rid,)); c.execute('DELETE FROM credit_accounts WHERE sale_id=?',(rid,)); c.execute('DELETE FROM sale_payments WHERE sale_id=?',(rid,)); c.execute('DELETE FROM sale_lines WHERE sale_id=?',(rid,)); c.execute('DELETE FROM sales WHERE id=?',(rid,)); core.db.audit(c,u,'ADMIN_DELETE','SALE',rid,f'receipt={row["receipt_no"]}; amount={row["total"]:.2f}; reason={reason}; sold units returned to stock; payment/credit allocations removed')
            else: raise ValueError('Unknown sale action.')
        else: raise ValueError('Unsupported record type.')
        c.commit()
    except Exception:
        c.rollback(); raise
    finally: c.close()



SUBSCRIPTION_FIRST_MONTH = '2026-12'
SUBSCRIPTION_PAYBILL = '516600'
SUBSCRIPTION_ACCOUNT = '319851'

def subscription_billing_state(today=None):
    today = today or dt.datetime.now(dt.timezone(dt.timedelta(hours=3))).date()
    month = today.strftime('%Y-%m')
    previous = (today.replace(day=1) - dt.timedelta(days=1)).strftime('%Y-%m')
    first = SUBSCRIPTION_FIRST_MONTH
    if month < first:
        return {'due_month':None,'due_date':None,'suspended':False,'upcoming':True,'paid_month':core.get_setting('subscription_paid_month','')}
    due_month = month if today.day >= 5 else previous
    if due_month < first:
        due_month = month
    due_date = dt.date(int(due_month[:4]),int(due_month[5:]),5)
    paid_month = core.get_setting('subscription_paid_month','')
    paid = bool(paid_month and paid_month >= due_month)
    suspended = today >= due_date and not paid
    return {'due_month':due_month,'due_date':due_date,'suspended':suspended,'upcoming':today < due_date and not paid,'paid_month':paid_month,'paid':paid}

def subscription_block_page(state):
    amount = core.get_setting('subscription_monthly_amount','')
    due_month = state.get('due_month') or 'December 2026'
    return f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Subscription overdue · MONTEZ LIQUOR</title><style>{CSS}</style></head><body class="login-page"><main class="login-card"><h1>MONTEZ LIQUOR — ACCESS SUSPENDED</h1><p>Your software subscription payment has not been confirmed. Normal business access is temporarily suspended.</p><section class="card"><h3>Subscription payment details</h3><p><b>Amount:</b> KSh {esc(amount or 'Not configured — contact Administrator')}</p><p><b>Paybill:</b> {SUBSCRIPTION_PAYBILL}</p><p><b>Account:</b> {SUBSCRIPTION_ACCOUNT}</p><p><b>Billing month:</b> {esc(due_month)}</p><p><b>Due date:</b> 5th day of the month</p></section><p>After payment, contact your Administrator to verify the M-PESA transaction and restore access.</p><a class="btn" href="/logout">LOG OUT</a><footer class="app-powered">Powered by SHADYTECHSOLUTIONS</footer></main></body></html>'''

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
                c=core.db.c(); like=f'%{q}%'; rows=c.execute("SELECT id,full_name,phone,id_no,address FROM customers WHERE active=1 AND (id_no LIKE ? OR full_name LIKE ? OR phone LIKE ? OR customer_no LIKE ?) ORDER BY CASE WHEN id_no=? THEN 0 WHEN phone=? THEN 0 WHEN lower(full_name) LIKE lower(?) THEN 0 ELSE 1 END, full_name LIMIT 30",(like,like,like,like,q,q,f'{q}%')).fetchall(); c.close(); return send_json(self,{'results':[dict(r) for r in rows]})
            if path=='/logout': return do_logout(self, _qsval(qs,'auto','0')=='1')
            u=session_user(self)
            if not u: return redirect(self,'/login')
            billing=subscription_billing_state()
            if billing.get('suspended') and role_name(u)!='ADMIN': return send_html(self,subscription_block_page(billing),402)
            if path=='/api/mpesa/stk-status': return send_json(self,mpesa_stk_status(_qsval(qs,'checkout_request_id')))
            if path=='/api/mpesa/live-messages': return send_json(self,mpesa_live_messages_json())
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
                '/reorder-report':reorder_report_page,'/live':live_page,'/products':products_page,
                '/users':users_page,'/branches':branches_page,'/branch-performance':branch_performance_page,'/settings':settings_page,'/ledger':ledger_page,
                '/profit-loss':pl_page,'/backup':backup_page,'/audit':audit_page,'/ai':ai_page}
            if path=='/change-password': return send_html(self,change_password_page(u))
            if path in routes:
                if path in ('/reports','/stock-report','/branch-performance'):
                    return send_html(self, routes[path](u, query=qs))
                if path in ('/expense-report','/ledger','/profit-loss','/reorder-report'):
                    return send_html(self, routes[path](u, qs))
                if path=='/sales': return send_html(self,sales_page(u, selected_customer_id=_qsval(qs,'customer_id','') or None))
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
                try: no,total=save_purchase(u,form); add_flash(session_token(self),f'Purchase {no} saved successfully. Stock updated by {form.get("qty")} unit(s).','success'); return redirect(self,'/purchases')
                except Exception as e: return send_html(self,purchase_page(u,str(e)),400)
            if path=='/expenses':
                try: save_expense(u,form); add_flash(session_token(self),'Expense saved successfully.','success'); return redirect(self,'/expenses')
                except Exception as e: return send_html(self,expense_page(u,str(e)),400)
            if path=='/products':
                if role_name(u) not in ('ADMIN','SALES') or not allowed(u,'MANAGE_PRODUCTS'): raise PermissionError('Product Registration is disabled for your account. Contact Administrator.')
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
                    elif action=='cost':
                        admin_set_cost(u,form); msg='Average unit cost correction saved successfully.'
                    elif action=='delete':
                        admin_product_delete(u,form.get('product_id'),False,form.get('reason','')); msg='Product deleted/archived successfully.'
                    elif action=='restore':
                        admin_product_delete(u,form.get('product_id'),True,form.get('reason','')); msg='Product restored successfully.'
                    else:
                        raise ValueError('Unknown product action.')
                    add_flash(session_token(self),msg,'success'); return redirect(self,'/products')
                except Exception as e: return send_html(self,products_page(u,str(e)),400)
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
                        c.execute('INSERT INTO branches(name,code,location,phone,active,created_at,updated_at) VALUES(?,?,?,?,1,?,?)',(name,code,location,phone,ts,ts)); bid=c.execute('SELECT id FROM branches WHERE name=?',(name,)).fetchone()['id']; core.db.audit(c,u,'CREATE','BRANCH',bid,f'{name} code={code or ""}'); c.commit(); msg='Branch registered successfully.'
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
            customer_action=form.get('customer_action','').upper()
            if customer_action=='SAVE_NEW_CUSTOMER':
                name=form.get('new_customer_name','').strip(); phone=form.get('new_customer_phone','').strip(); id_no=form.get('new_customer_id_no','').strip(); address=form.get('new_customer_address','').strip()
                if not name or not phone or not id_no: raise ValueError('New credit customer requires full name, phone number and ID number/passport.')
                c=core.db.c(); c.execute('BEGIN')
                try:
                    duplicate=c.execute('SELECT id,full_name,phone,id_no FROM customers WHERE active=1 AND (lower(trim(id_no))=lower(trim(?)) OR replace(replace(replace(phone,' ',''),'-',''),'+','')=replace(replace(replace(?,' ',''),'-',''),'+','')) ORDER BY id LIMIT 1',(id_no,phone)).fetchone()
                    if duplicate:
                        raise ValueError(f'Customer already registered: {duplicate["full_name"]} · Phone {duplicate["phone"]}. Choose EXISTING CUSTOMER and search by ID or phone. No duplicate was created.')
                    ts=now(); no='CUS-'+dt.datetime.now().strftime('%Y%m%d%H%M%S')+'-'+secrets.token_hex(2).upper(); cur=c.execute('INSERT INTO customers(customer_no,full_name,phone,id_no,address,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',(no,name,phone,id_no,address,ts,ts)); cid=cur.lastrowid; core.db.audit(c,u,'CREATE','CUSTOMER',cid,f'{no} {name}'); c.commit()
                except Exception:
                    c.rollback(); raise
                finally: c.close()
                add_flash(session_token(self),f'Customer {name} saved successfully. Continue the credit sale.','success')
                return redirect(self,'/sales?customer_id='+str(cid))
            if customer_action=='SAVE_EXISTING_CUSTOMER':
                cid=int(form.get('customer_id','0') or 0)
                if not cid: raise ValueError('Search and select an existing customer before saving.')
                c=core.db.c(); found=c.execute('SELECT id,full_name FROM customers WHERE id=? AND active=1',(cid,)).fetchone(); c.close()
                if not found: raise ValueError('Selected customer was not found.')
                add_flash(session_token(self),f'Existing customer {found["full_name"]} saved for this sale.','success')
                return redirect(self,'/sales?customer_id='+str(cid))
            customer_mode=form.get('customer_mode','WALK_IN').upper(); sale_type=form.get('sale_type','RETAIL').upper()
            if sale_type not in ('RETAIL','WHOLESALE'): raise ValueError('Select Retail or Wholesale sale type.')
            cart=[]; total_expected=0.0; pid=0; qty=0
            line_ids=sorted({int(k.rsplit('_',1)[1]) for k in form.keys() if k.startswith('product_id_') and k.rsplit('_',1)[1].isdigit()})
            if not line_ids: line_ids=[0]
            for ln in line_ids:
                product_key=f'product_id_{ln}' if ln else 'product_id'; qty_key=f'qty_{ln}' if ln else 'qty'
                line_pid=int(form.get(product_key,'0') or 0); line_qty=int(form.get(qty_key,'0') or 0)
                if line_pid<=0 or line_qty<=0: raise ValueError(f'Choose a product and valid quantity on line {ln or 1}.')
                c=core.db.c(); prod=c.execute('SELECT * FROM products WHERE id=? AND active=1',(line_pid,)).fetchone(); c.close()
                if not prod: raise ValueError(f'Product on line {ln or 1} was not found.')
                if float(prod['stock_qty'])<line_qty: raise ValueError(f'Insufficient stock for {prod["name"]}. Available: {prod["stock_qty"]}.')
                unit_price=float(prod['wholesale_price'] or 0) if sale_type=='WHOLESALE' else float(prod['sell_price'])
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
                d=core.create_sale(u,{'cart':cart,'payments':[],'customer':customer,'credit':{'due_date':due,'collateral':coll,'collateral_value':cv},'sale_type':sale_type})
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
                d=core.create_sale(u,{'cart':cart,'payments':payments,'customer':{},'credit':None,'sale_type':sale_type})
            else:
                raise ValueError('Unknown customer type.')
            if d.get('sale_id'):
                codes=[p.get('mpesa_code') for p in d.get('payments',[]) if p.get('mpesa_code')]
                if codes:
                    c=core.db.c(); c.execute("UPDATE mpesa_stk_requests SET sale_id=?,updated_at=? WHERE mpesa_code IN ({}) AND sale_id IS NULL".format(','.join('?' for _ in codes)),tuple([d['sale_id'],core.now_str()]+codes)); c.commit(); c.close()
            add_flash(session_token(self),f'Sale {d["receipt_no"]} saved successfully. Total {money(d["total"])}','success')
            return redirect(self,'/sales')
        except Exception as e:
            return send_html(self,sales_page(u,str(e)),400)
    def credit_payment_post(self,u,form):
        require_perm_user(u,'MANAGE_CUSTOMERS')
        c=None
        try:
            credit_id=int(form.get('credit_id','0') or 0); amount=round(float(form.get('amount','0') or 0),2); method=form.get('payment_method','CASH').upper(); ref=form.get('reference','').strip().upper()
            if credit_id<=0 or amount<=0: raise ValueError('Enter a valid payment amount.')
            if method not in ('CASH','MPESA'): raise ValueError('Invalid payment method.')
            if method=='MPESA' and not ref: raise ValueError('M-Pesa transaction code is required for every M-Pesa credit repayment.')
            c=core.db.c(); row=c.execute('SELECT * FROM credit_accounts WHERE id=? AND status="OPEN"',(credit_id,)).fetchone()
            if not row: raise ValueError('Credit account was not found or is already closed.')
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
            require_perm_user(u,'VIEW_STOCK'); rows=core.stock_report(start,end); headers=['SKU','Product Name','Category','Shared Stock','Purchased','Total Sold','Retail Units','Wholesale Units','Retail Revenue','Wholesale Revenue','Retail Units %','Wholesale Units %','Retail Revenue %','Wholesale Revenue %','Retail Price','Wholesale Price','Avg Cost','Stock Value','Reorder','Status']; data=[[r['sku'],r['name'],r['category'],r['stock'],r['purchased'],r['sold'],r['retail_units'],r['wholesale_units'],r['retail_revenue'],r['wholesale_revenue'],r['retail_units_pct'],r['wholesale_units_pct'],r['retail_revenue_pct'],r['wholesale_revenue_pct'],r['retail_price'],r['wholesale_price'],r['avg_cost'],r['stock_value'],r['reorder_level'],r['status']] for r in rows]; title=f"{core.get_setting('business_name',APP_NAME)} · {core.get_setting('business_branch_name','')} · STOCK REPORT"
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
        c=core.db.c(); c.execute('PRAGMA wal_checkpoint(FULL)'); c.close(); fn=f'MONTEZ_LIQUOR_BACKUP_{dt.datetime.now().strftime("%Y%m%d_%H%M%S")}.db'; path=os.path.join(DATA_DIR,'backups'); os.makedirs(path,exist_ok=True); dest=os.path.join(path,fn); import shutil; shutil.copy2(core.DB_PATH,dest); return send_file(self,open(dest,'rb').read(),'application/octet-stream',fn)


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
                payload.update({'all_mpesa':form.get('all_mpesa'),'product_id':form.get('product_id',''),'qty':form.get('qty',''),'total':form.get('total','0'),'payment_count':form.get('payment_count','1'),'batch_id':form.get('batch_id',''),'sale_type':form.get('sale_type','RETAIL')})
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
            if role_name(u) not in ('ADMIN','SALES') or not allowed(u,'MANAGE_PRODUCTS'): raise PermissionError('Product Registration is disabled for your account. Contact Administrator.')
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

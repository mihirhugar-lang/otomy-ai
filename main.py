import os, sys, asyncio, base64, hashlib, hmac, json, secrets, time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from fastapi import FastAPI, Depends, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from database import init_db, get_db, SessionLocal
from routers import sales, expenses, boulders, machines, labour, parts, dashboard, workers, emi
from routers import customers, vendors, bank, exports, erp_sync as erp_sync_router

AUTH_FILE = os.path.join(os.path.dirname(__file__), "data", "access_auth.json")
_AUTH_CACHE: dict = {}
_LOGIN_ATTEMPTS: dict = {}  # ip -> [timestamp, ...]
_LOGIN_MAX_ATTEMPTS = 10
_LOGIN_WINDOW_SECONDS = 60


def _load_access_auth():
    try:
        mtime = os.path.getmtime(AUTH_FILE)
        if _AUTH_CACHE.get("mtime") == mtime:
            return _AUTH_CACHE.get("cfg")
        with open(AUTH_FILE) as f:
            cfg = json.load(f)
        result = cfg if cfg.get("enabled") else None
        _AUTH_CACHE["cfg"] = result
        _AUTH_CACHE["mtime"] = mtime
        return result
    except Exception:
        return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    asyncio.create_task(_start_background_sync())
    yield


app = FastAPI(title="CrusherOps", version="2.0", lifespan=lifespan)


def _unauthorized():
    return Response(
        "Login required",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="CrusherOps"'},
    )


def _verify_password(cfg: dict, username: str, password: str) -> bool:
    return bool(_verify_user(cfg, username, password))


def _auth_users(cfg: dict) -> list:
    users = cfg.get("users")
    if isinstance(users, list) and users:
        return users
    return [
        {
            "username": cfg.get("username", ""),
            "role": "admin",
            "salt": cfg.get("salt", ""),
            "password_hash": cfg.get("password_hash", ""),
            "iterations": cfg.get("iterations", 200000),
        }
    ]


def _find_user(cfg: dict, username: str) -> dict:
    for user in _auth_users(cfg):
        if secrets.compare_digest(username, user.get("username", "")):
            return user
    return {}


def _verify_user(cfg: dict, username: str, password: str) -> dict:
    try:
        user = _find_user(cfg, username)
        if not user:
            return {}
        salt = base64.b64decode(user["salt"])
        expected = base64.b64decode(user["password_hash"])
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            int(user.get("iterations", cfg.get("iterations", 200000))),
        )
        return user if secrets.compare_digest(actual, expected) else {}
    except Exception:
        return {}


def _session_token(cfg: dict, username: str) -> str:
    payload = f"{username}:{int(time.time())}"
    sig = hmac.new(cfg.get("session_secret", "").encode(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}:{sig}".encode()).decode()


def _session_username(cfg: dict, token: str) -> str:
    try:
        decoded = base64.urlsafe_b64decode(token.encode()).decode()
        username, issued, sig = decoded.rsplit(":", 2)
        if not _find_user(cfg, username):
            return ""
        if time.time() - int(issued) > 30 * 24 * 60 * 60:
            return ""
        payload = f"{username}:{issued}"
        expected = hmac.new(cfg.get("session_secret", "").encode(), payload.encode(), hashlib.sha256).hexdigest()
        return username if secrets.compare_digest(sig, expected) else ""
    except Exception:
        return ""


def _valid_session(cfg: dict, token: str) -> bool:
    return bool(_session_username(cfg, token))


def _can_write(cfg: dict, username: str) -> bool:
    user = _find_user(cfg, username)
    return (user.get("role") or "").lower() == "admin"


def _is_write_request(request: Request) -> bool:
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return False
    if request.url.path in ("/api/login", "/api/logout", "/api/report/table-pdf"):
        return False
    return request.url.path.startswith("/api/")


@app.middleware("http")
async def require_basic_auth(request: Request, call_next):
    cfg = _load_access_auth()
    if not cfg:
        return await call_next(request)
    public_paths = (
        "/static/manifest.webmanifest",
        "/static/service-worker.js",
        "/service-worker.js",
    )
    if request.url.path in public_paths or request.url.path.startswith("/static/icons/"):
        return await call_next(request)
    if request.url.path in ("/login", "/api/login", "/api/logout"):
        return await call_next(request)

    session_username = _session_username(cfg, request.cookies.get("crusherops_session", ""))
    if session_username:
        if _is_write_request(request) and not _can_write(cfg, session_username):
            return JSONResponse({"detail": "Read-only access. Changes are allowed only for admin."}, status_code=403)
        return await call_next(request)

    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("basic "):
        if not request.url.path.startswith("/api/"):
            return RedirectResponse("/login", status_code=302)
        return _unauthorized()
    try:
        decoded = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8")
        username, password = decoded.split(":", 1)
    except Exception:
        return _unauthorized()

    if not _verify_user(cfg, username, password):
        return _unauthorized()
    if _is_write_request(request) and not _can_write(cfg, username):
        return JSONResponse({"detail": "Read-only access. Changes are allowed only for admin."}, status_code=403)
    return await call_next(request)

# ── Background ERP sync ────────────────────────────────────────────────────────
async def _start_background_sync():
    """
    1. On first boot: full historical sync from 2026-06-01 → yesterday (once, in background).
    2. Then every 15 minutes: sync yesterday + today (keeps data fresh).
    """
    await asyncio.sleep(10)          # give server a moment to fully start
    loop = asyncio.get_running_loop()

    cfg = erp_sync_router.load_config()
    org      = cfg.get("erp_org", "")
    username = cfg.get("erp_username", "")
    password = cfg.get("erp_password", "")
    erp_base = cfg.get("erp_base", erp_sync_router.ERP_BASE)

    if not username or not password:
        print("[auto_sync] ERP credentials not set — skipping background sync.")
        return

    # ── Historical full sync (June 1 to yesterday) ─────────────────────────────
    if not cfg.get("historical_sync_done"):
        print("[auto_sync] Starting historical sync June 1 → yesterday…")
        try:
            sess = await loop.run_in_executor(None,
                lambda: erp_sync_router.erp_auth(erp_base, org, username, password))
            from_d   = date.fromisoformat(cfg.get("historical_sync_from", "2026-06-01"))
            to_d     = date.today() - timedelta(days=1)
            db       = SessionLocal()
            try:
                result = await loop.run_in_executor(None,
                    lambda: erp_sync_router.run_sync(
                        sess, erp_base, from_d, to_d, db=db))
                print(f"[auto_sync] Historical done: {result}")
                cfg2 = erp_sync_router.load_config()
                cfg2["historical_sync_done"] = True
                erp_sync_router.save_config(cfg2)
            finally:
                db.close()
        except Exception as e:
            print(f"[auto_sync] Historical sync error: {e}")

    # ── Rolling 15-minute sync loop ────────────────────────────────────────────
    while True:
        await asyncio.sleep(15 * 60)       # 15 minutes
        cfg = erp_sync_router.load_config()
        username = cfg.get("erp_username", "")
        password = cfg.get("erp_password", "")
        if not username or not password:
            continue
        try:
            print(f"[auto_sync] Running 15-min sync at {datetime.now().strftime('%H:%M')}…")
            sess  = await loop.run_in_executor(None,
                lambda: erp_sync_router.erp_auth(erp_base, org, username, password))
            today = date.today()
            db    = SessionLocal()
            try:
                result = await loop.run_in_executor(None,
                    lambda: erp_sync_router.run_sync(
                        sess, erp_base,
                        today - timedelta(days=1), today,
                        db=db))
                total = (result["sales_imported"] + result["expenses_imported"] +
                         result["bank_imported"]  + result["cash_imported"] +
                         result["iot_imported"])
                if total > 0:
                    print(f"[auto_sync] {total} new records synced.")
            finally:
                db.close()
        except Exception as e:
            print(f"[auto_sync] 15-min sync error: {e}")

# ── Static + routers ────────────────────────────────────────────────────────────
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

app.include_router(sales.router)
app.include_router(expenses.router)
app.include_router(boulders.router)
app.include_router(machines.router)
app.include_router(labour.router)
app.include_router(parts.router)
app.include_router(dashboard.router)
app.include_router(workers.router)
app.include_router(emi.router)
app.include_router(customers.router)
app.include_router(vendors.router)
app.include_router(bank.router)
app.include_router(exports.router)
app.include_router(erp_sync_router.router)


@app.get("/login", response_class=HTMLResponse)
def login_page():
    return """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
  <meta name="theme-color" content="#38aeea">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-title" content="VMIPL">
  <link rel="manifest" href="/static/manifest.webmanifest">
  <link rel="apple-touch-icon" href="/static/icons/icon-180.png">
  <title>CrusherOps Login</title>
  <style>
    *{box-sizing:border-box} body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;font-family:Georgia,'Times New Roman',serif;background:radial-gradient(circle at 20% 10%,rgba(245,158,11,.28),transparent 30%),linear-gradient(135deg,#17202a,#3f2507 58%,#fff7ed);color:#111827}
    .card{width:min(420px,100%);background:rgba(255,255,255,.94);border:1px solid rgba(255,255,255,.4);border-radius:28px;padding:28px 24px;box-shadow:0 24px 70px rgba(0,0,0,.28)}
    .brand{display:flex;align-items:center;gap:14px;margin-bottom:22px}.brand img{width:58px;height:58px;border-radius:16px}.brand h1{font-size:27px;margin:0;color:#17202a}.brand p{margin:4px 0 0;color:#8a5a18;font:700 12px -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;letter-spacing:.8px;text-transform:uppercase}
    label{display:block;font:800 11px -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;letter-spacing:.6px;text-transform:uppercase;color:#667085;margin:14px 0 6px}
    input{width:100%;font-size:18px;padding:14px 15px;border-radius:14px;border:1.5px solid #e2d7c5;background:#fffaf0}
    button{width:100%;margin-top:20px;padding:15px;border:0;border-radius:16px;background:linear-gradient(135deg,#17202a,#b45309);color:white;font-size:17px;font-weight:900;letter-spacing:.3px}
    .err{display:none;margin-top:14px;color:#b42318;font:700 13px -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}.hint{margin-top:18px;color:#667085;font-size:13px;line-height:1.5}
  </style>
</head>
<body>
  <form class="card" id="loginForm">
    <div class="brand"><img src="/static/icons/icon-180.png" alt=""><div><h1>CrusherOps</h1><p>VMIPL Secure Access</p></div></div>
    <label>User ID</label><input id="username" autocomplete="username" autocapitalize="characters" value="VMIPL">
    <label>Password</label><input id="password" type="password" autocomplete="current-password" placeholder="Enter password">
    <button type="submit">Open Dashboard</button>
    <div class="err" id="err">Wrong user ID or password.</div>
    <div class="hint">After login, use Add to Home Screen. The app icon will open this secure dashboard.</div>
  </form>
  <script>
    try{sessionStorage.removeItem('crusherops_unlocked');}catch(e){}
    document.getElementById('loginForm').addEventListener('submit', async (e)=>{
      e.preventDefault();
      const res=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:username.value,password:password.value})});
      if(res.ok){try{sessionStorage.setItem('crusherops_unlocked','1');}catch(e){} location.href='/';return;}
      document.getElementById('err').style.display='block';
    });
  </script>
</body>
</html>
"""


@app.post("/api/login")
async def login(request: Request):
    cfg = _load_access_auth()
    if not cfg:
        return JSONResponse({"ok": True})
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    attempts = [t for t in _LOGIN_ATTEMPTS.get(ip, []) if now - t < _LOGIN_WINDOW_SECONDS]
    if len(attempts) >= _LOGIN_MAX_ATTEMPTS:
        return JSONResponse({"ok": False, "error": "Too many attempts"}, status_code=429)
    try:
        body = await request.json()
    except Exception:
        body = {}
    username = body.get("username", "")
    user = _verify_user(cfg, username, body.get("password", ""))
    if not user:
        attempts.append(now)
        _LOGIN_ATTEMPTS[ip] = attempts
        return JSONResponse({"ok": False}, status_code=401)
    response = JSONResponse({"ok": True})
    response.set_cookie(
        "crusherops_session",
        _session_token(cfg, user.get("username", username)),
        httponly=True,
        secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "") == "https",
        samesite="lax",
    )
    return response


@app.post("/api/logout")
def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie("crusherops_session", samesite="lax")
    return response


@app.get("/api/me")
def current_user(request: Request):
    cfg = _load_access_auth()
    username = _session_username(cfg, request.cookies.get("crusherops_session", "")) if cfg else ""
    return {
        "authenticated": bool(username),
        "username": username,
        "can_write": bool(username and cfg and _can_write(cfg, username)),
    }


@app.get("/api/reports/audit")
def generate_audit(from_date: date, to_date: date):
    from reports.audit_report import generate_audit_report
    path = generate_audit_report(from_date, to_date)
    return FileResponse(path, media_type="application/pdf",
                        filename=os.path.basename(path))


@app.get("/", response_class=HTMLResponse)
def root():
    with open(os.path.join(STATIC_DIR, "index.html")) as f:
        return f.read()


@app.get("/service-worker.js")
def service_worker():
    return FileResponse(
        os.path.join(STATIC_DIR, "service-worker.js"),
        media_type="application/javascript",
    )


@app.get("/api/report/generate")
def generate_report(date_str: str = None, from_date: date = None, to_date: date = None):
    from reports.pdf_generator import generate_daily_report
    d = date.fromisoformat(date_str) if date_str else date.today()
    path = generate_daily_report(d, from_date=from_date, to_date=to_date)
    return FileResponse(path, media_type="application/pdf",
                        filename=os.path.basename(path))


@app.post("/api/report/table-pdf")
async def generate_table_pdf(request: Request):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Spacer, Table, TableStyle, Paragraph

    body = await request.json()
    title = str(body.get("title") or "CrusherOps Report").replace("₹", "Rs ").replace("\u2014", "-")[:120]
    subtitle = str(body.get("subtitle") or "").replace("₹", "Rs ").replace("\u2014", "-")[:180]
    tables = body.get("tables") or []
    if not tables:
        return JSONResponse({"detail": "No table data to print"}, status_code=400)

    output_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "output_reports", "mobile_prints"))
    os.makedirs(output_dir, exist_ok=True)
    safe_name = "".join(ch if ch.isalnum() else "_" for ch in title)[:60].strip("_") or "CrusherOps_Report"
    path = os.path.join(output_dir, f"{safe_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf")

    doc = SimpleDocTemplate(
        path,
        pagesize=landscape(A4),
        leftMargin=9 * mm,
        rightMargin=9 * mm,
        topMargin=9 * mm,
        bottomMargin=9 * mm,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "mobile_title",
        parent=styles["Heading1"],
        alignment=TA_CENTER,
        fontName="Helvetica-Bold",
        fontSize=16,
        textColor=colors.HexColor("#17202a"),
        spaceAfter=4,
    )
    sub_style = ParagraphStyle(
        "mobile_sub",
        parent=styles["Normal"],
        alignment=TA_CENTER,
        fontSize=9,
        textColor=colors.HexColor("#667085"),
        spaceAfter=8,
    )
    cell_style = ParagraphStyle("mobile_cell", parent=styles["Normal"], fontSize=7.3, leading=8.4)
    head_style = ParagraphStyle(
        "mobile_head",
        parent=cell_style,
        fontName="Helvetica-Bold",
        textColor=colors.white,
    )

    def clean(value):
        return str(value or "").replace("₹", "Rs ").replace("\u2014", "-").strip()

    story = [
        Paragraph(title, title_style),
        Paragraph(subtitle or f"Generated: {datetime.now().strftime('%d %b %Y, %I:%M %p')}", sub_style),
    ]
    page_width = landscape(A4)[0] - (18 * mm)

    for block in tables[:8]:
        rows = block.get("rows") or []
        if not rows:
            continue
        table_title = clean(block.get("title") or "")
        if table_title:
            story.append(Paragraph(table_title, styles["Heading3"]))
        max_cols = min(max(len(r) for r in rows if isinstance(r, list)), 12)
        data = []
        for r_idx, row in enumerate(rows[:350]):
            style = head_style if r_idx == 0 else cell_style
            cells = [Paragraph(clean(v), style) for v in list(row)[:max_cols]]
            cells += [Paragraph("", style)] * (max_cols - len(cells))
            data.append(cells)
        col_width = page_width / max_cols
        table = Table(data, colWidths=[col_width] * max_cols, repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#17202a")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fff7ed")]),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d8dee8")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(table)
        story.append(Spacer(1, 8))

    doc.build(story)
    return FileResponse(path, media_type="application/pdf", filename=os.path.basename(path))


@app.get("/api/report/monthly")
def generate_monthly(year: int, month: int):
    from reports.pdf_generator import generate_monthly_report
    path = generate_monthly_report(year, month)
    return FileResponse(path, media_type="application/pdf",
                        filename=os.path.basename(path))


@app.get("/api/config/machines")
def get_machines():
    return {"machines": ["Jaw Crusher", "Cone Crusher", "VSI", "Wheel Loader", "Hitachi Excavator"]}


@app.get("/api/config/materials")
def get_materials():
    return {"materials": ["40mm", "20mm", "12mm", "6mm", "M-Sand", "P-Sand", "Dust", "Mixed"]}


@app.get("/api/config/expense_categories")
def get_expense_categories():
    return {"categories": ["Fuel", "EMI", "Crusher Repair", "Vehicle Repair", "Blasting",
                           "Wages", "Transport", "Electricity", "Other"]}


@app.get("/api/config/worker_types")
def get_worker_types():
    return {"types": ["Operator", "Helper", "Driver", "Blasting", "Watchman", "Supervisor", "Other"]}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8765, reload=True)

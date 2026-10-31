#!/usr/bin/env python3
"""Router data tracker.
Sources: router LTE interface (SNMP total + NetFlow v5 per-device) and the laptop's own SIM modem
(Linux interface counters + ModemManager for the SIM's network). One bundle per network.
Config: config.toml. Run: python app.py (single process; do not use multiple workers)."""
import asyncio, base64, hashlib, hmac, ipaddress, json, pathlib, re, socket, sqlite3, struct, subprocess, time, tomllib, urllib.request
from contextlib import asynccontextmanager
from datetime import date
from functools import lru_cache
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

BASE = pathlib.Path(__file__).parent
CFG = tomllib.loads((BASE / "config.toml").read_text())
R, F, C = CFG["router"], CFG["flow"], CFG.get("control", {})
LAN = ipaddress.ip_network(F["lan"])
GB = 1024 ** 3

db = sqlite3.connect(BASE / "usage.db", check_same_thread=False)
db.executescript("""
CREATE TABLE IF NOT EXISTS total(ts INTEGER, bytes INTEGER);
CREATE TABLE IF NOT EXISTS dev(hour INTEGER, net TEXT, ip TEXT, down INTEGER, up INTEGER, PRIMARY KEY(hour, net, ip));
CREATE TABLE IF NOT EXISTS bundles(net TEXT PRIMARY KEY, size_gb REAL, expiry TEXT, start REAL);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS rules(mac TEXT PRIMARY KEY, limit_gb REAL, blocked INTEGER DEFAULT 0, auto INTEGER DEFAULT 0);
""")


def kv(k, default=None):
    r = db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


# ---- networks: one bundle per network (MTN, Telecel, AirtelTigo) ----
ALIAS = {"vodafone": "Telecel", "telecel": "Telecel", "mtn": "MTN", "airtel": "AirtelTigo",
         "tigo": "AirtelTigo", "airteltigo": "AirtelTigo", "at": "AirtelTigo"}


def norm(name):
    name = (name or "").strip()
    first = re.split(r"[\s_\-]+", name.lower())[0] if name else ""
    return ALIAS.get(first, name)


RNET = norm(kv("router_net") or R.get("network", "Router SIM"))  # network of the SIM currently in the router
LAPTOP = {"net": None, "connected": False, "iface": ""}          # live state of the laptop's modem


def migrate():
    """Upgrade a database made by an older version (single bundle) to per-network bundles."""
    cols = {r[1] for r in db.execute("PRAGMA table_info(total)")}
    for c in ("net", "src"):
        if c not in cols:
            db.execute(f"ALTER TABLE total ADD COLUMN {c} TEXT")
    db.execute("UPDATE total SET net=?, src='router' WHERE net IS NULL", (RNET,))
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='device'").fetchone():
        db.execute("INSERT OR IGNORE INTO dev SELECT hour, ?, ip, down, up FROM device", (RNET,))
        db.execute("DROP TABLE device")
    if kv("size_gb") and not db.execute("SELECT 1 FROM bundles").fetchone():
        db.execute("INSERT INTO bundles VALUES(?,?,?,?)", (RNET, float(kv("size_gb")), kv("expiry"), float(kv("start", 0))))
        db.execute("DELETE FROM kv WHERE k IN ('size_gb','expiry','start')")
    db.commit()


migrate()


def add_usage(delta, net, src):
    if delta > 0:
        db.execute("INSERT INTO total VALUES(?,?,?,?)", (int(time.time()), delta, net, src))
        db.commit()


def bundle_info(net):
    row = db.execute("SELECT size_gb, expiry, start FROM bundles WHERE net=?", (net,)).fetchone()
    size, exp, start = (row[0] * GB, row[1], row[2]) if row else (0, None, 0)
    by_src = dict(db.execute("SELECT src, SUM(bytes) FROM total WHERE net=? AND ts>=? GROUP BY src", (net, start)))
    used = sum(by_src.values())
    return {"net": net, "configured": bool(row), "size": size, "used": used, "remaining": max(size - used, 0),
            "expiry": exp, "days_left": (date.fromisoformat(exp) - date.today()).days if exp else None,
            "start": start, "by_src": by_src}


def active_net():  # laptop SIM when it is connected, otherwise the SIM in the router
    return LAPTOP["net"] if LAPTOP["connected"] and LAPTOP["net"] else RNET


# ---- source 1: router (SNMP via net-snmp CLI: tiny, stable, works with any router) ----
def _snmp(cmd, *oids, opts="-Oqn"):
    return subprocess.run([cmd, "-v2c", "-c", R["community"], opts, R["host"], *oids],
                          capture_output=True, text=True, timeout=10, check=True).stdout


def find_ifindex():
    for line in _snmp("snmpwalk", "1.3.6.1.2.1.2.2.1.2").splitlines():
        oid, _, val = line.partition(" ")
        if val.strip('"') == R["interface"]:
            return int(oid.rsplit(".", 1)[1])
    raise RuntimeError(f"interface '{R['interface']}' not found via SNMP")


def read_octets(idx):
    out = _snmp("snmpget", f"1.3.6.1.2.1.31.1.1.1.6.{idx}", f"1.3.6.1.2.1.31.1.1.1.10.{idx}", opts="-Oqv").split()
    return int(out[0]) + int(out[1])  # ifHCInOctets + ifHCOutOctets


IFIDX = None


async def poll():
    global IFIDX
    last, failed = None, False
    while True:
        try:
            IFIDX = IFIDX or await asyncio.to_thread(find_ifindex)
            cur = await asyncio.to_thread(read_octets, IFIDX)
            if last is not None:
                add_usage(cur - last if cur >= last else cur, RNET, "router")  # reset on reboot -> cur
            last, failed = cur, False
        except Exception as e:
            IFIDX = None  # ifIndex can change after reboot; re-resolve
            if not failed:
                print("router poll error (laptop SIM in use, or router off?):", e, flush=True)
            failed = True
        await asyncio.sleep(R.get("poll_seconds", 60))


# ---- source 2: laptop SIM (Linux interface counters + ModemManager) ----
def lap_iface():
    name = CFG.get("laptop", {}).get("interface")
    return name or next((p.name for p in sorted(pathlib.Path("/sys/class/net").glob("ww*"))), "")


def lap_bytes(iface):
    d = pathlib.Path("/sys/class/net") / iface / "statistics"
    return int((d / "rx_bytes").read_text()), int((d / "tx_bytes").read_text())  # (down, up)


def _sh(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=True).stdout


def modem_info():
    """{'net': home network of the SIM, 'connected': bool} from ModemManager, or None if no modem."""
    try:
        m = json.loads(_sh("mmcli", "-m", "any", "-J"))["modem"]
    except Exception:
        return None
    net = None
    try:
        sim = m["generic"].get("sim")
        if sim and sim != "--":
            net = json.loads(_sh("mmcli", "-i", sim, "-J"))["sim"]["properties"].get("operator-name")
        net = net if net and net != "--" else m["3gpp"].get("operator-name")
    except Exception:
        pass
    return {"net": norm(net) if net and net != "--" else "Laptop SIM",
            "connected": m["generic"].get("state") == "connected"}


def laptop_tick(last, info):
    iface = LAPTOP["iface"]
    if not iface or not pathlib.Path(f"/sys/class/net/{iface}").exists():
        return None
    cur = lap_bytes(iface)
    if last is not None:
        net = (info and info["net"]) or "Laptop SIM"
        down, up = (c - l if c >= l else c for c, l in zip(cur, last))  # counter reset -> current value
        add_usage(down + up, net, "laptop")
        if down + up:  # the laptop shows up as a device in the table (ip = 'laptop')
            db.execute("INSERT INTO dev VALUES(?,?,?,?,?) ON CONFLICT(hour,net,ip) DO UPDATE SET down=down+?, up=up+?",
                       (int(time.time()) // 3600, net, "laptop", down, up, down, up))
            db.commit()
    return cur


async def laptop_poll():
    last = None
    while True:
        try:
            iface = lap_iface()
            info = await asyncio.to_thread(modem_info) if iface else None
            LAPTOP.update(iface=iface, net=info and info["net"], connected=bool(info and info["connected"]))
            last = laptop_tick(last, info)
        except Exception as e:
            print("laptop poll error:", e, flush=True)
        await asyncio.sleep(30)


# ---- per-device split: NetFlow v5 collector (router only) ----
REC = struct.Struct("!4s4s4sHHIIIIHHxBBBHHBBxx")
assert REC.size == 48
buf = {}  # (hour, net, ip) -> [down, up]
seen = {}  # ip -> last time traffic was seen (in memory)
online = {}  # ip -> answered the last ping
FLOW_LAST_RECEIVED = 0  # unix timestamp of last NetFlow v5 datagram received


class Flow(asyncio.DatagramProtocol):
    def datagram_received(self, data, addr):
        if len(data) < 24 or struct.unpack("!H", data[:2])[0] != 5 or IFIDX is None:
            return
        n = struct.unpack("!H", data[2:4])[0]
        hour = int(time.time()) // 3600
        for i in range(min(n, (len(data) - 24) // 48)):
            s, d, _, inp, out, _, octs, *_ = REC.unpack_from(data, 24 + i * 48)
            s, d = socket.inet_ntoa(s), socket.inet_ntoa(d)
            if out == IFIDX and ipaddress.ip_address(s) in LAN:    # upload
                ip, k = s, 1
            elif inp == IFIDX and ipaddress.ip_address(d) in LAN:  # download
                ip, k = d, 0
            else:
                continue
            buf.setdefault((hour, RNET, ip), [0, 0])[k] += octs
            seen[ip] = time.time()
            global FLOW_LAST_RECEIVED
            FLOW_LAST_RECEIVED = time.time()


async def flush():
    global FLOW_LAST_RECEIVED
    while True:
        await asyncio.sleep(30)
        items = list(buf.items())
        buf.clear()
        if items:
            FLOW_LAST_RECEIVED = time.time()
        for (h, net, ip), (dn, up) in items:
            db.execute("INSERT INTO dev VALUES(?,?,?,?,?) ON CONFLICT(hour,net,ip) "
                       "DO UPDATE SET down=down+?, up=up+?", (h, net, ip, dn, up, dn, up))
        db.commit()


# ---- device names: config -> router DHCP hostname -> reverse DNS -> IP ----
def arp():
    lines = pathlib.Path("/proc/net/arp").read_text().splitlines()[1:]
    return {p[0]: p[3].lower() for p in map(str.split, lines)}


@lru_cache(maxsize=256)
def rdns(ip):
    try:
        return socket.gethostbyaddr(ip)[0].split(".")[0]
    except OSError:
        return ip


def label(ip, macs):
    names = CFG.get("names", {})
    return names.get(macs.get(ip, "")) or names.get(ip) or LEASES.get(ip, ("", ""))[1] or rdns(ip)


def dev_start():  # start hour of the router SIM's current bundle
    return int(bundle_info(RNET)["start"]) // 3600


# ---- online/offline: ping known devices (works on any router) ----
async def ping(ip):
    p = await asyncio.create_subprocess_exec("ping", "-c1", "-W1", ip,
                                             stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    online[ip] = (await p.wait()) == 0


async def probe():
    while True:
        ips = [r[0] for r in db.execute("SELECT DISTINCT ip FROM dev WHERE net=? AND hour>=? AND ip!='laptop'", (RNET, dev_start()))]
        await asyncio.gather(*(ping(ip) for ip in ips), return_exceptions=True)
        await asyncio.sleep(30)


# ---- control: block / unblock / per-device limits (optional; MikroTik RouterOS 7 REST) ----
PW = C.get("password", "")
CTL = bool(PW and C.get("router_user"))
LIST = "tracker-blocked"
LEASES = {}  # ip -> (mac, hostname), refreshed from the router's DHCP server


def my_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((R["host"], 9))
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


def exempt(ip):  # never block the router or the machine running this tracker
    return ip in (R["host"], my_ip())


def token():
    return hmac.new(PW.encode(), b"data-tracker", hashlib.sha256).hexdigest()


def authed(request):
    return bool(PW) and hmac.compare_digest(request.cookies.get("t", ""), token())


def need_auth(request):
    if PW and not authed(request):
        raise HTTPException(401, "Password required")


def rest(method, path, body=None):
    auth = base64.b64encode(f'{C["router_user"]}:{C.get("router_password", "")}'.encode()).decode()
    req = urllib.request.Request(
        C.get("router_url", f'http://{R["host"]}') + "/rest" + path, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": "Basic " + auth, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read() or "null")


def refresh_leases():
    global LEASES
    LEASES = {l["address"]: (l.get("mac-address", "").lower(), l.get("host-name", ""))
              for l in rest("GET", "/ip/dhcp-server/lease") if l.get("address")}


def ip_of(mac):
    for ip, (m, _) in LEASES.items():
        if m == mac:
            return ip
    return next((ip for ip, m in arp().items() if m == mac), None)


def sync_blocks(macs):
    """Make the router's block list match `macs` (by each device's current IP)."""
    want = {}
    for mac in macs:
        ip = ip_of(mac)
        if ip and not exempt(ip):
            want[ip] = mac
    have = {e["address"]: e for e in rest("GET", f"/ip/firewall/address-list?list={LIST}")}
    for ip, mac in want.items():
        if ip not in have:
            rest("PUT", "/ip/firewall/address-list", {"list": LIST, "address": ip, "comment": "tracker:" + mac})
    for ip, e in have.items():
        if ip not in want:
            rest("DELETE", "/ip/firewall/address-list/" + e[".id"])


def evaluate():
    """Auto-block devices over their per-bundle limit. Returns all blocked MACs."""
    default = float(kv("default_limit_gb", 0))
    rules = {m: l for m, l in db.execute("SELECT mac, limit_gb FROM rules")}
    blocked = {m for (m,) in db.execute("SELECT mac FROM rules WHERE blocked=1")}
    a, used = arp(), {}
    for ip, tot in db.execute("SELECT ip, SUM(down+up) FROM dev WHERE net=? AND hour>=? AND ip!='laptop' GROUP BY ip", (RNET, dev_start())):
        mac = LEASES.get(ip, ("",))[0] or a.get(ip)
        if mac and not exempt(ip):
            used[mac] = used.get(mac, 0) + tot
    for mac, u in used.items():
        lim = rules.get(mac)
        lim = default if lim is None else lim  # no rule value -> default; 0 -> unlimited
        if lim and mac not in blocked and u >= lim * GB:
            db.execute("INSERT INTO rules(mac, blocked, auto) VALUES(?,1,1) "
                       "ON CONFLICT(mac) DO UPDATE SET blocked=1, auto=1", (mac,))
            blocked.add(mac)
    db.commit()
    return sorted(blocked)


async def enforce():
    while True:
        try:
            await asyncio.to_thread(refresh_leases)
            await asyncio.to_thread(sync_blocks, evaluate())
        except Exception as e:
            print("control error:", e, flush=True)
        await asyncio.sleep(30)


# ---- API ----
@asynccontextmanager
async def lifespan(_):
    await asyncio.get_running_loop().create_datagram_endpoint(Flow, local_addr=("0.0.0.0", F["port"]))
    tasks = [asyncio.create_task(t()) for t in (poll, laptop_poll, flush, probe)]
    if CTL:
        tasks.append(asyncio.create_task(enforce()))
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(lifespan=lifespan)


class Bundle(BaseModel):
    size_gb: float
    expiry: str  # YYYY-MM-DD
    network: str = ""  # empty = the network currently in use


@app.post("/api/bundle")
def set_bundle(b: Bundle, request: Request):
    need_auth(request)
    date.fromisoformat(b.expiry)
    net = norm(b.network) or active_net()
    db.execute("INSERT OR REPLACE INTO bundles VALUES(?,?,?,?)", (net, b.size_gb, b.expiry, int(time.time())))  # int: same-second usage still counts
    db.execute("UPDATE rules SET blocked=0, auto=0 WHERE auto=1")  # a new bundle releases auto-blocks
    db.commit()
    return {"ok": True}


class RouterNet(BaseModel):
    network: str


@app.post("/api/router-net")  # which network's SIM is in the router
def set_router_net(b: RouterNet, request: Request):
    global RNET
    need_auth(request)
    RNET = norm(b.network) or RNET
    db.execute("INSERT OR REPLACE INTO kv VALUES('router_net', ?)", (RNET,))
    db.commit()
    return {"ok": True}


class Login(BaseModel):
    password: str


@app.post("/api/login")
def login(b: Login, response: Response):
    if not PW or not hmac.compare_digest(b.password.encode(), PW.encode()):
        raise HTTPException(401, "Wrong password")
    response.set_cookie("t", token(), httponly=True, samesite="strict", max_age=30 * 86400)
    return {"ok": True}


class Act(BaseModel):
    mac: str
    action: str  # block | unblock | limit
    limit_gb: Optional[float] = None


@app.post("/api/device")
async def device(a: Act, request: Request):
    if not CTL:
        raise HTTPException(404)
    need_auth(request)
    mac = a.mac.lower()
    if a.action == "block":
        ip = ip_of(mac)
        if not ip or exempt(ip):
            raise HTTPException(400, "This device can't be blocked")
        db.execute("INSERT INTO rules(mac, blocked, auto) VALUES(?,1,0) "
                   "ON CONFLICT(mac) DO UPDATE SET blocked=1, auto=0", (mac,))
    elif a.action == "unblock":  # unblocking an auto-blocked device lifts its limit (else it would re-block)
        db.execute("UPDATE rules SET blocked=0, limit_gb=CASE WHEN auto=1 THEN 0 ELSE limit_gb END, auto=0 "
                   "WHERE mac=?", (mac,))
    elif a.action == "limit":
        db.execute("INSERT INTO rules(mac, limit_gb) VALUES(?,?) ON CONFLICT(mac) DO UPDATE SET limit_gb=?",
                   (mac, a.limit_gb, a.limit_gb))
    else:
        raise HTTPException(400, "Unknown action")
    db.commit()
    try:
        await asyncio.to_thread(sync_blocks, evaluate())
    except Exception as e:
        raise HTTPException(502, f"Router error: {e}")
    return {"ok": True}


class Lim(BaseModel):
    limit_gb: float


@app.post("/api/default-limit")
def default_limit(b: Lim, request: Request):
    if not CTL:
        raise HTTPException(404)
    need_auth(request)
    db.execute("INSERT OR REPLACE INTO kv VALUES('default_limit_gb', ?)", (str(b.limit_gb),))
    db.commit()
    return {"ok": True}


@app.get("/api/status")
def status(request: Request):
    net = active_net()
    b = bundle_info(net)
    macs = arp() | {ip: m for ip, (m, _) in LEASES.items()}
    rules = {m: (l, bl, au) for m, l, bl, au in db.execute("SELECT mac, limit_gb, blocked, auto FROM rules")}
    rows = db.execute("SELECT ip, SUM(down), SUM(up) FROM dev WHERE net=? GROUP BY ip "
                      "ORDER BY SUM(down)+SUM(up) DESC", (net,)).fetchall()
    devices, router_sum = [], 0
    for ip, d, u in rows:
        if ip == "laptop":  # the laptop itself, using its own SIM
            devices.append({"name": "This laptop", "ip": LAPTOP["iface"] or "SIM", "down": d, "up": u, "mac": "",
                            "online": LAPTOP["connected"] and LAPTOP["net"] == net, "blocked": False, "auto": False,
                            "limit_gb": None, "exempt": True})
            continue
        router_sum += d + u
        mac = macs.get(ip, "")
        l, bl, au = rules.get(mac, (None, 0, 0))
        devices.append({"name": label(ip, macs), "ip": ip, "down": d, "up": u, "mac": mac,
                        "online": online.get(ip, False) or time.time() - seen.get(ip, 0) < 300,
                        "blocked": bool(bl), "auto": bool(au), "limit_gb": l, "exempt": exempt(ip)})
    devices.sort(key=lambda d: not d["online"])  # online first, then by usage
    unattributed = max(b["by_src"].get("router", 0) - router_sum, 0)
    flows_stale = FLOW_LAST_RECEIVED == 0 or time.time() - FLOW_LAST_RECEIVED > 120
    return {
        **b, "devices": devices, "unattributed": unattributed,
        "snmp_ok": IFIDX is not None,
        "router_error": IFIDX is None and not LAPTOP["connected"],
        "flows_last_received": FLOW_LAST_RECEIVED,
        "flows_stale": flows_stale,
        "sources": {"router_net": RNET, "laptop": dict(LAPTOP)},
        "bundles": [{**bundle_info(n), "active": n == net} for (n,) in db.execute("SELECT net FROM bundles ORDER BY net")],
        "control": {"enabled": CTL, "authed": authed(request), "password_set": bool(PW),
                    "default_limit_gb": float(kv("default_limit_gb", 0))},
    }


@app.get("/")
def index():
    return FileResponse(BASE / "index.html")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=CFG["web"]["port"])

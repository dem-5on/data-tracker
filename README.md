# Router data tracker

Local, router-agnostic tracker for a metered (SIM/LTE) WAN link. One Python process, one SQLite file, ~40 MB RAM.

- **Total used**: SNMP polls the byte counters of the metered interface every `poll_seconds`.
- **Per device**: a NetFlow v5 collector (UDP) attributes bytes on that interface to LAN IPs. Device name = `[names]` in config, else reverse DNS, else IP.
- **Laptop SIM**: if the SIM is in the laptop's modem, Linux interface counters (`wwan0`, auto-detected) are tracked and ModemManager (`mmcli`) tells which network the SIM belongs to.
- **Bundles are per network** (MTN, Telecel, AirtelTigo...). The dashboard shows the bundle of the SIM in use; usage from router and laptop is added together per network.
- **Balance**: you enter bundle size + expiry per network in the dashboard; remaining = size - tracked usage since entry.
- Switching routers = edit `config.toml` (host, community, interface, LAN). No code changes.

## Setup
1. Router, SNMP: enable SNMP v2c with the community from `config.toml`, readable from this machine.
   MikroTik: `/snmp set enabled=yes` and `/snmp community set [find default=yes] name=public`
2. Router, flow export (NetFlow v5 to this machine's IP, port 2055).
   MikroTik: `/ip traffic-flow set enabled=yes interfaces=all active-flow-timeout=1m` then
   `/ip traffic-flow target add dst-address=<THIS_MACHINE_IP> port=2055 version=5`
3. Edit `config.toml`, then run `./install.sh`. Open `http://<machine-ip>:8080`.

Logs: `journalctl -u data-tracker -f`. Reset data: stop service, delete `usage.db`.

## Notes
- Per-device figures come from flow records and can differ a few % from the carrier; the "unattributed" row shows the gap against the SNMP total.
- If every device shows 0 but the total moves: the router exports NAT-translated addresses. Check flows with `tcpdump -n udp port 2055`.
- 1 GB is treated as 1024^3 bytes.

## GNOME top-bar indicator (optional, GNOME 45+)
Shows remaining data in the top bar with a popup (usage bar, devices, online dots). Reads `http://localhost:8080/api/status`; the tracker must be running.
```bash
mkdir -p ~/.local/share/gnome-shell/extensions/datatracker@local
cp gnome-extension/* ~/.local/share/gnome-shell/extensions/datatracker@local/
```
Log out and back in (Wayland needs this once), then `gnome-extensions enable datatracker@local`.
Debug: `journalctl -f -o cat /usr/bin/gnome-shell`. If the tracker moves to another machine, change `BASE_URL` in `extension.js`.

## Block, unblock and per-device limits (optional, MikroTik RouterOS 7)
Controls live on the web dashboard only (the top-bar popup shows a read-only "blocked" tag). Block = the router drops that device's traffic to the internet (LAN access stays). Limits count over the whole bundle; an over-limit device is blocked automatically within ~30 s, and "Start new bundle" releases automatic blocks (manual blocks stay).

Router setup (replace `<TRACKER_IP>` with the machine running this app, and `10.10.2.0/24` with your LAN):
```
/user group add name=tracker policy=read,write,rest-api
/user add name=tracker group=tracker password=<PASSWORD> address=<TRACKER_IP>/32
/ip service set www disabled=no address=<TRACKER_IP>/32
/ip firewall raw add chain=prerouting action=drop src-address-list=tracker-blocked dst-address=!10.10.2.0/24 place-before=0 comment="data-tracker block"
```
Then fill `[control]` in `config.toml` (dashboard `password`, `router_user`, `router_password`, `router_url`) and `sudo systemctl restart data-tracker`.

Notes: RouterOS cannot limit a user to firewall rules only, so the user and the `www` service are restricted to the tracker's IP. Devices are matched by MAC, but usage is counted under the IP they currently hold. The router and the tracker machine can never be blocked. Unblocking an auto-blocked device lifts its limit until you set a new one. The dashboard runs over plain HTTP on your LAN; do not expose it to the internet.

## Laptop SIM and per-network bundles
- Detection: when the laptop modem is connected, the dashboard switches to that SIM's network (shown as "<Network> | SIM in laptop"). Names are normalised: Vodafone = Telecel, Tigo/Airtel/AT = AirtelTigo, MTN Ghana = MTN.
- Router SIM: the router cannot report its SIM's network portably, so choose it in the "SIM in router" dropdown on the dashboard when you swap SIMs. The default comes from `network` under `[router]` in `config.toml`.
- Add one bundle per network with the form (Network field is pre-filled with the network in use). "Start new bundle" restarts that network's count only.
- Per-device table, block and limits apply to traffic through the router only. While the laptop SIM is active, usage shows under "Laptop".
- Upgrading from the single-bundle version: add `network = "<network of your router SIM>"` under `[router]` before the first start. The old database migrates automatically (old bundle and device history become that network's).
- Troubleshooting: header says "Laptop SIM" instead of a network name -> run `mmcli -m any -J` and check `modem.generic.sim` and `modem.3gpp.operator-name`; `ip -br link` should show a `ww*` interface. Set `[laptop] interface` if auto-detection picks the wrong one.

// Data Tracker: top-bar indicator for the local router data tracker (GNOME 45+).
// Reads http://localhost:<config.json web.port>/api/status every 10 s. No dependencies.
import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Soup from 'gi://Soup?version=3.0';
import St from 'gi://St';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

// Port read from config.json — single source of truth shared with the Python tracker.
// Changing config.json updates the backend, dashboard, and this extension.
let BASE_URL = 'http://localhost:8181';
try {
    const cfgPath = GLib.build_filenamev([
        GLib.get_home_dir(), 'Main', 'PERSONAl_PROJECTS', 'router', 'config.json',
    ]);
    const [ok, contents] = GLib.file_get_contents(cfgPath);
    if (ok) {
        const cfg = JSON.parse(new TextDecoder().decode(contents));
        const port = cfg.web?.port;
        if (port) BASE_URL = `http://localhost:${port}`;
    }
} catch (e) {
    // fall back to the default above
}
const GB = 1073741824;
const GREEN = '#57e389', AMBER = '#e5a50a', RED = '#f66151', GREY = '#77767b';
const DIM = 'color: #9a9996;';
const CENTER = Clutter.ActorAlign.CENTER;

const fmt = b => b >= GB
    ? `${(b / GB).toFixed(2)} GB`
    : `${(b / 1048576).toFixed(b >= 104857600 ? 0 : 1)} MB`;

const Indicator = GObject.registerClass(
class DataTrackerIndicator extends PanelMenu.Button {
    _init() {
        super._init(0.0, 'Data Tracker');
        this._session = new Soup.Session({timeout: 5});
        this._cancel = new Gio.Cancellable();
        this._showOffline = false;
        this._last = null;

        this._label = new St.Label({text: '–', y_align: CENTER});
        this.add_child(this._label);

        this.menu.box.style = 'min-width: 340px;';
        this._head = new St.Label({text: 'Loading…', style: DIM});
        this._row(this._head);

        this._fill = new St.Widget({style: 'width: 0px; height: 8px;'});
        const track = new St.Widget({
            style: 'width: 300px; height: 8px; border-radius: 4px; background-color: rgba(255,255,255,0.14);',
        });
        track.add_child(this._fill);
        this._row(track);

        this._stats = new St.Label({text: ''});
        this._row(this._stats);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._devs = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._devs);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        const sw = new PopupMenu.PopupSwitchMenuItem('Show offline devices', false);
        sw.activate = () => sw.toggle();  // keep the menu open when toggled
        sw.connect('toggled', (_i, on) => {
            this._showOffline = on;
            if (this._last) this._render(this._last);
        });
        this.menu.addMenuItem(sw);

        const open = new PopupMenu.PopupMenuItem('Open dashboard');
        open.connect('activate', () => Gio.AppInfo.launch_default_for_uri(BASE_URL, null));
        this.menu.addMenuItem(open);

        this.menu.connect('open-state-changed', (_m, isOpen) => { if (isOpen) this._refresh(); });
        this._timer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 10, () => {
            this._refresh();
            return GLib.SOURCE_CONTINUE;
        });
        this._refresh();
    }

    _row(actor) {
        const item = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
        item.add_child(actor);
        this.menu.addMenuItem(item);
    }

    _refresh() {
        const msg = Soup.Message.new('GET', `${BASE_URL}/api/status`);
        this._session.send_and_read_async(msg, GLib.PRIORITY_DEFAULT, this._cancel, (s, res) => {
            if (this._destroyed) return;
            try {
                const bytes = s.send_and_read_finish(res);
                this._render(JSON.parse(new TextDecoder().decode(bytes.get_data())));
            } catch (e) {
                if (!this._destroyed) this._fail();
            }
        });
    }

    _fail() {
        this._label.text = 'No data';
        this._label.style = DIM;
        this._head.text = `Tracker not reachable on ${BASE_URL}.`;
    }

    _render(d) {
        this._last = d;
        const pct = d.size > 0 ? Math.min(d.used / d.size * 100, 100) : 0;
        const color = pct > 90 ? RED : pct > 70 ? AMBER : GREEN;

        if (d.configured) {
            this._label.text = `${fmt(d.remaining)} left`;
            this._label.style = pct > 70 ? `color: ${color};` : '';
            this._head.text = `${d.net}: ${pct.toFixed(0)}% of ${fmt(d.size)} used` +
                (d.days_left !== null ? `, ${d.days_left} days to expiry` : '');
            this._fill.style = `width: ${Math.round(pct * 3)}px; height: 8px; border-radius: 4px; background-color: ${color};`;
            this._stats.text = `Used ${fmt(d.used)}   Left ${fmt(d.remaining)}`;
        } else {
            this._label.text = `${fmt(d.used)} used`;
            this._label.style = '';
            this._head.text = `No bundle set for ${d.net}. Add one in the dashboard.`;
            this._fill.style = 'width: 0px; height: 8px;';
            this._stats.text = `Used ${fmt(d.used)}`;
        }
        if (d.router_error) this._head.text = 'Router not reachable over SNMP.';

        this._devs.removeAll();
        const list = d.devices.filter(x => this._showOffline || x.online || x.blocked);
        if (!list.length) {
            const none = new PopupMenu.PopupMenuItem('No devices online right now', {reactive: false});
            none.label.style = DIM;
            this._devs.addMenuItem(none);
        }
        for (const x of list) {
            const style = x.online ? '' : DIM;
            const box = new St.BoxLayout({x_expand: true, style: 'spacing: 8px;'});
            box.add_child(new St.Label({text: '●', y_align: CENTER, style: `color: ${x.blocked ? RED : x.online ? GREEN : GREY};`}));
            box.add_child(new St.Label({text: x.name + (x.blocked ? (x.auto ? '  (blocked, over limit)' : '  (blocked)') : ''), x_expand: true, y_align: CENTER, style}));
            box.add_child(new St.Label({text: fmt(x.down + x.up), y_align: CENTER, style}));
            const item = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
            item.add_child(box);
            this._devs.addMenuItem(item);
        }
    }

    destroy() {
        this._destroyed = true;
        this._cancel.cancel();
        if (this._timer) GLib.Source.remove(this._timer);
        this._session.abort();
        super.destroy();
    }
});

export default class DataTrackerExtension extends Extension {
    enable() {
        this._indicator = new Indicator();
        Main.panel.addToStatusArea(this.uuid, this._indicator);
    }

    disable() {
        this._indicator?.destroy();
        this._indicator = null;
    }
}

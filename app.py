import os
import sys
import io
import json
import math
import re
import hashlib
import threading
import subprocess
import webbrowser
import datetime
import tkinter as tk
from tkinter import ttk, messagebox

if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import regional_printer
import radius_scanner
import subscription
import auth_client
import data_sync
import map_view
from applog import debug

try:
    import tkintermapview
except ImportError:
    tkintermapview = None


def _patch_tkintermapview_tile_retry():
    """tkintermapview's request_image() has no timeout on its tile fetch and
    treats ANY network hiccup (dropped connection, slow response) the same
    as a real "no tile here" — permanently caching a gray placeholder that's
    never retried. A burst of zoom/pan, or just an ordinary network blip,
    leaves the map partially blank forever. This patches in a timeout plus a
    few retries before giving up, and only *caches* a genuine missing-tile
    response — a transient failure can still be retried if that tile
    position comes back into view later."""
    if tkintermapview is None:
        return
    import io as _io
    from tkintermapview.map_widget import TkinterMapView

    def request_image(self, zoom, x, y, db_cursor=None):
        if db_cursor is not None:
            try:
                db_cursor.execute(
                    "SELECT t.tile_image FROM tiles t WHERE t.zoom=? AND t.x=? AND t.y=? AND t.server=?;",
                    (zoom, x, y, self.tile_server))
                result = db_cursor.fetchone()
                if result is not None:
                    image = PIL.Image.open(_io.BytesIO(result[0]))
                    image_tk = PIL.ImageTk.PhotoImage(image)
                    self.tile_image_cache[f"{zoom}{x}{y}"] = image_tk
                    return image_tk
                elif self.use_database_only:
                    return self.empty_tile_image
            except Exception:
                if self.use_database_only:
                    return self.empty_tile_image

        url = self.tile_server.replace("{x}", str(x)).replace("{y}", str(y)).replace("{z}", str(zoom))
        for _attempt in range(3):
            try:
                resp = requests.get(url, stream=True, timeout=8,
                                    headers={"User-Agent": "TkinterMapView"})
                image = PIL.Image.open(resp.raw)
                if not self.running:
                    return self.empty_tile_image
                image_tk = PIL.ImageTk.PhotoImage(image)
                self.tile_image_cache[f"{zoom}{x}{y}"] = image_tk
                return image_tk
            except PIL.UnidentifiedImageError:
                # server responded but there's no tile here (e.g. ocean) —
                # a real, stable "nothing to show", safe to cache.
                self.tile_image_cache[f"{zoom}{x}{y}"] = self.empty_tile_image
                return self.empty_tile_image
            except Exception:
                continue  # transient network issue — retry
        # Exhausted retries: return blank WITHOUT caching, so a later redraw
        # over this tile position gets a fresh attempt instead of being
        # stuck gray for the rest of the session.
        return self.empty_tile_image

    TkinterMapView.request_image = request_image


if tkintermapview is not None:
    import requests
    import PIL.Image
    import PIL.ImageTk
    _patch_tkintermapview_tile_retry()

# ═══════════════════════════════════════════════
#  SETTINGS (text size, first-run flag)
# ═══════════════════════════════════════════════
SETTINGS_PATH = os.path.join(BASE_DIR, "settings.json")

def load_settings():
    try:
        with open(SETTINGS_PATH) as f:
            return json.load(f)
    except Exception:
        return {}

def save_settings(d):
    try:
        with open(SETTINGS_PATH, "w") as f:
            json.dump(d, f, indent=2)
    except Exception:
        pass

SETTINGS = load_settings()
SCALE = float(SETTINGS.get("scale", 1.0))

def fs(n):
    return max(7, int(round(n * SCALE)))

# ═══════════════════════════════════════════════
#  DESIGN TOKENS
# ═══════════════════════════════════════════════
BG       = "#0d0f18"
SURFACE  = "#13162a"
CARD     = "#1c2035"
BORDER   = "#2a2f4a"
ACCENT   = "#f59e0b"
ACCENT_D = "#d97706"
BLUE     = "#3b82f6"
GREEN    = "#22c55e"
RED      = "#ef4444"
TEXT     = "#f1f5f9"
TEXT2    = "#94a3b8"
TEXT3    = "#475569"

UI       = "Segoe UI"
F_HEAD   = (UI, fs(18), "bold")
F_SUB    = (UI, fs(13), "bold")
F_BODY   = (UI, fs(11))
F_SMALL  = (UI, fs(9))
F_BIG    = (UI, fs(28), "bold")
F_NAV    = (UI, fs(12), "bold")
F_MONO   = ("Consolas", fs(10))

# ═══════════════════════════════════════════════
#  SAVED-BIDS PERSISTENCE
# ═══════════════════════════════════════════════
SAVED_PATH = os.path.join(BASE_DIR, "saved_bids.json")

def load_saved():
    try:
        with open(SAVED_PATH) as f:
            return json.load(f)
    except Exception:
        return {}

def write_saved(d):
    try:
        with open(SAVED_PATH, "w") as f:
            json.dump(d, f, indent=2)
    except Exception:
        pass

# Persist the last scan's bids so they stay in Live Bids across restarts
FEED_PATH = os.path.join(BASE_DIR, "last_feed.json")

def load_feed():
    try:
        with open(FEED_PATH) as f:
            return json.load(f)
    except Exception:
        return {}

def write_feed(d):
    try:
        with open(FEED_PATH, "w") as f:
            json.dump(d, f, indent=2)
    except Exception:
        pass

def bid_id(city, bid):
    raw = (str(city) + bid.get("title", "") + bid.get("scope", "")).encode("utf-8")
    return hashlib.md5(raw).hexdigest()[:12]

_DEADLINE_FORMATS = (
    "%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%B %d, %Y", "%b %d, %Y",
    "%B %d %Y", "%b %d %Y", "%m/%d/%y",
)

def _parse_deadline_date(text):
    """Best-effort parse of a free-text deadline into a date. None if unparseable."""
    if not text:
        return None
    t = str(text).strip()
    m = re.search(r"\d{4}-\d{2}-\d{2}", t)
    if m:
        t = m.group(0)
    for fmt in _DEADLINE_FORMATS:
        try:
            return datetime.datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    return None

def days_until(bid):
    """Days from today until the bid's deadline, or None if unparseable."""
    d = _parse_deadline_date(bid.get("deadline"))
    if d is None:
        return None
    return (d - datetime.datetime.now().date()).days

# Persist planned/pre-bid work found by the Upcoming tab across restarts
UPCOMING_PATH = os.path.join(BASE_DIR, "upcoming_feed.json")

def load_upcoming():
    try:
        with open(UPCOMING_PATH) as f:
            return json.load(f)
    except Exception:
        return {}

def write_upcoming(d):
    try:
        with open(UPCOMING_PATH, "w") as f:
            json.dump(d, f, indent=2)
    except Exception:
        pass

def upcoming_id(city, item):
    raw = (str(city) + item.get("title", "") + item.get("scope", "")).encode("utf-8")
    return hashlib.md5(raw).hexdigest()[:12]

# Persist residential permit leads found by the Leads tab across restarts
LEADS_PATH = os.path.join(BASE_DIR, "leads_feed.json")

def load_leads():
    try:
        with open(LEADS_PATH) as f:
            return json.load(f)
    except Exception:
        return {}

def write_leads(d):
    try:
        with open(LEADS_PATH, "w") as f:
            json.dump(d, f, indent=2)
    except Exception:
        pass

# Outreach status per lead (Called/Quoted/Won/Lost) -- local-only for now,
# keyed by permit_id. No Supabase table for leads yet the way there is for
# saved_bids, so this doesn't sync across devices yet.
LEAD_STATUS_PATH = os.path.join(BASE_DIR, "lead_status.json")
LEAD_STATUSES = [("called", "Called"), ("quoted", "Quoted"),
                  ("won", "Won"), ("lost", "Lost")]

def load_lead_status():
    try:
        with open(LEAD_STATUS_PATH) as f:
            return json.load(f)
    except Exception:
        return {}

def write_lead_status(d):
    try:
        with open(LEAD_STATUS_PATH, "w") as f:
            json.dump(d, f, indent=2)
    except Exception:
        pass

# Company info used to personalize AI-drafted proposals — nothing here is
# sent anywhere except as part of a /draft-proposal request.
COMPANY_PATH = os.path.join(BASE_DIR, "company_profile.json")

def load_company_profile():
    try:
        with open(COMPANY_PATH) as f:
            return json.load(f)
    except Exception:
        return {}

def write_company_profile(d):
    try:
        with open(COMPANY_PATH, "w") as f:
            json.dump(d, f, indent=2)
    except Exception:
        pass

# Saved searches for auto-alerts — re-scanned silently on app open
SAVED_SEARCHES_PATH = os.path.join(BASE_DIR, "saved_searches.json")

def load_saved_searches():
    try:
        with open(SAVED_SEARCHES_PATH) as f:
            return json.load(f)
    except Exception:
        return []

def write_saved_searches(d):
    try:
        with open(SAVED_SEARCHES_PATH, "w") as f:
            json.dump(d, f, indent=2)
    except Exception:
        pass

PIPELINE_LABELS = [("submitted", "Submitted"), ("won", "Won"),
                   ("lost", "Lost"), ("passed", "Passed")]

def parse_value(v):
    """'$1.2M' -> 1200000.0, '$85k' -> 85000.0, '' -> -1 (unparseable)."""
    s = str(v or "").lower()
    m = re.search(r"[\d.]+", s.replace(",", ""))
    if not m:
        return -1.0
    n = float(m.group())
    if "m" in s:
        n *= 1_000_000
    elif "k" in s:
        n *= 1_000
    return n

def format_money(n):
    if n >= 1_000_000:
        return f"${n/1_000_000:.{0 if n % 1_000_000 == 0 else 1}f}M"
    if n >= 1_000:
        return f"${n/1_000:.{0 if n % 1_000 == 0 else 1}f}k"
    return f"${round(n)}"

# ═══════════════════════════════════════════════
#  WIDGET HELPERS
# ═══════════════════════════════════════════════
def pill_btn(parent, text, cmd, bg=ACCENT, fg="#000", font=None, px=22, py=9):
    return tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg,
                     font=font or (UI, fs(11), "bold"), relief="flat", bd=0,
                     cursor="hand2", padx=px, pady=py,
                     activebackground=ACCENT_D, activeforeground="#000")

def ghost_btn(parent, text, cmd, fg=TEXT2, font=None):
    return tk.Button(parent, text=text, command=cmd, bg=CARD, fg=fg,
                     font=font or F_SMALL, relief="flat", bd=0, cursor="hand2",
                     padx=14, pady=6, activebackground=BORDER, activeforeground=TEXT)

def divider(parent, color=BORDER, pad=0):
    tk.Frame(parent, bg=color, height=1).pack(fill="x", padx=pad)

def scrollable(parent, bg=BG):
    outer = tk.Frame(parent, bg=bg)
    outer.pack(fill="both", expand=True)
    canvas = tk.Canvas(outer, bg=bg, highlightthickness=0, bd=0)
    sb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview, style="Vertical.TScrollbar")
    inner = tk.Frame(canvas, bg=bg)
    inner.bind("<Configure>",
               lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.create_window((0, 0), window=inner, anchor="nw")
    canvas.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)
    canvas.bind_all("<MouseWheel>",
                    lambda e: canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))
    return inner

STATUS_COLORS = {
    "open":   ("#052e16", GREEN),
    "closed": ("#1c1917", TEXT3),
    "pending":("#1c1a07", ACCENT),
}

# ═══════════════════════════════════════════════
#  MAIN APP
# ═══════════════════════════════════════════════
class BidCaller:
    TRADES = ["All trades", "Roofing", "Concrete / Paving", "Roads / Sitework",
              "Building / Facility", "HVAC", "Demolition", "General"]
    TRADE_KEYWORDS = {
        "Roofing":            ["roof", "shingle", "membrane", "gutter"],
        "Concrete / Paving":  ["concrete", "asphalt", "paving", "sidewalk", "curb", "pavement"],
        "Roads / Sitework":   ["road", "street", "grading", "excavation", "drainage", "storm", "sewer", "water main"],
        "Building / Facility": ["building", "facility", "renovation", "remodel", "construction", "addition", "repair"],
        "HVAC":               ["hvac", "heating", "cooling", "air condition", "ventilation", "furnace", "boiler", "chiller", "rooftop unit", "rtu"],
        "Demolition":         ["demolition", "demo", "removal", "clearing"],
        "General":            [],
    }

    def __init__(self, root):
        self.root = root
        self.root.title("Bid Caller Pro")
        self.root.geometry("1180x780")
        self.root.configure(bg=BG)
        self.root.minsize(1000, 660)

        self.bid_data = load_feed()
        self.saved = load_saved()
        self.upcoming_data = load_upcoming()
        self.leads_data = load_leads()
        self.lead_status = load_lead_status()
        self._leads_status_filter = "All"
        self.company = load_company_profile()
        self.saved_searches = load_saved_searches()
        self.scan_running = False
        self.search_results = []
        self._current = "feed"
        self._auth_mode = "signin"
        self._map_auto_tried = False

        self._sidebar()
        self._main_area()
        self._nav("feed")

        if not SETTINGS.get("seen_welcome"):
            self.root.after(300, self._show_welcome)

        if not subscription.get_status()["active"]:
            self._nav("subscribe")

        self.root.after(1500, self._try_auto_unlock)
        self.root.after(2000, self._sync_pull_after_signin)
        self.root.after(2500, self._check_saved_searches_on_load)
        self.root.after(60000, self._tick_trial_countdown)

    # ────────── SIDEBAR ──────────
    def _sidebar(self):
        sb = tk.Frame(self.root, bg=SURFACE, width=215)
        sb.pack(side="left", fill="y")
        sb.pack_propagate(False)

        logo = tk.Frame(sb, bg=SURFACE, pady=20)
        logo.pack(fill="x")
        tk.Label(logo, text="🔨", font=(UI, fs(22)), bg=SURFACE, fg=ACCENT).pack()
        tk.Label(logo, text="BID CALLER", font=(UI, fs(13), "bold"), bg=SURFACE, fg=TEXT).pack()
        tk.Label(logo, text="PRO", font=(UI, fs(9), "bold"), bg=SURFACE, fg=ACCENT).pack()

        divider(sb, BORDER, 20)

        self._nav_btns = {}
        self._nav_badges = {}
        items = [
            ("feed",      "📋", "Live Bids"),
            ("saved",     "⭐", "Active Bids"),
            ("scan",      "🔍", "Find Bids"),
            ("upcoming",  "📡", "Upcoming"),
            ("leads",     "🏠", "Residential Leads"),
            ("search",    "🌐", "Search City"),
            ("account",   "👤", "Account"),
            ("subscribe", "🛒", "Upgrade"),
            ("billing",   "💳", "Billing"),
            ("support",   "🛟", "Support"),
            ("settings",  "⚙",  "Settings"),
        ]
        for key, icon, label in items:
            f = tk.Frame(sb, bg=SURFACE, cursor="hand2")
            f.pack(fill="x")
            il = tk.Label(f, text=icon, font=(UI, fs(13)), bg=SURFACE, fg=TEXT2,
                          padx=18, pady=12, anchor="w")
            il.pack(side="left")
            tl = tk.Label(f, text=label, font=F_NAV, bg=SURFACE, fg=TEXT2, anchor="w")
            tl.pack(side="left")
            if key == "feed":
                bd = tk.Label(f, text="●", font=(UI, fs(8)), bg=SURFACE, fg=RED)
                self._nav_badges[key] = bd
            for w in (f, il, tl):
                w.bind("<Button-1>", lambda e, k=key: self._nav(k))
            self._nav_btns[key] = (f, il, tl)

        bottom = tk.Frame(sb, bg=SURFACE)
        bottom.pack(side="bottom", fill="x", pady=18, padx=14)
        divider(bottom, BORDER)
        self.sub_lbl = tk.Label(bottom, text="", font=F_SMALL, bg=SURFACE,
                                fg=TEXT3, wraplength=175, justify="left")
        self.sub_lbl.pack(anchor="w", pady=(10, 0))
        self._refresh_sub_lbl()

    def _show_feed_badge(self, show):
        bd = self._nav_badges.get("feed")
        if not bd:
            return
        if show:
            bd.pack(side="left", padx=(2, 0))
        else:
            bd.pack_forget()

    def _refresh_sub_lbl(self):
        s = subscription.get_status()
        if s.get("active") and s.get("trial"):
            label = (subscription.time_left_label(s.get("expires_at"))
                     or f"{s.get('days_left','?')} days left")
            self.sub_lbl.config(text=f"🎁 Free Trial\n{label}", fg=ACCENT)
        elif s.get("active"):
            self.sub_lbl.config(text=f"✅ {s.get('plan','Pro')}\nRenews {s.get('renews','—')}", fg=GREEN)
        else:
            self.sub_lbl.config(text="⚠ No active plan", fg=RED)

    def _tick_trial_countdown(self):
        """Re-renders the trial countdown from the already-cached expiry
        timestamp every minute — no extra network calls just to keep a
        clock ticking, the server is still only re-checked on the normal
        schedule (startup, nav, etc)."""
        cache = subscription._load_cache()
        if cache.get("trial") and cache.get("expires_at"):
            label = subscription.time_left_label(cache["expires_at"])
            if label:
                if hasattr(self, "sub_lbl"):
                    self.sub_lbl.config(text=f"🎁 Free Trial\n{label}", fg=ACCENT)
                if getattr(self, "_current", None) == "subscribe" and hasattr(self, "_trial_countdown_lbl"):
                    try:
                        self._trial_countdown_lbl.config(text=label)
                    except tk.TclError:
                        pass
        self.root.after(60000, self._tick_trial_countdown)

    def _nav(self, key):
        self._current = key
        for k, (f, il, tl) in self._nav_btns.items():
            on = (k == key)
            f.config(bg=CARD if on else SURFACE)
            il.config(bg=CARD if on else SURFACE, fg=ACCENT if on else TEXT2)
            tl.config(bg=CARD if on else SURFACE, fg=TEXT if on else TEXT2)
            if k in self._nav_badges:
                self._nav_badges[k].config(bg=CARD if on else SURFACE)
        for pg in self._pages.values():
            pg.pack_forget()
        self._pages[key].pack(fill="both", expand=True)
        if key == "saved":
            self._render_saved()
        elif key == "feed":
            self._render_feed()
            self._show_feed_badge(False)
        elif key == "upcoming":
            self._render_upcoming()
        elif key == "leads":
            self._render_leads()
        elif key == "account":
            self._render_account()
        elif key == "subscribe":
            self._try_auto_unlock()
        elif key == "billing":
            self._render_billing()

    # ────────── MAIN AREA ──────────
    def _main_area(self):
        self._container = tk.Frame(self.root, bg=BG)
        self._container.pack(side="left", fill="both", expand=True)
        self._pages = {}
        self._page_feed()
        self._page_saved()
        self._page_scan()
        self._page_upcoming()
        self._page_leads()
        self._page_search()
        self._page_account()
        self._page_subscribe()
        self._page_billing()
        self._page_support()
        self._page_settings()

    # ═══════════ BID CARD ═══════════
    def _bid_card(self, parent, bid, city=""):
        bid = dict(bid)
        bid["_city"] = city
        bid["_id"] = bid_id(city, bid)
        is_saved = bid["_id"] in self.saved
        note = self.saved.get(bid["_id"], {}).get("note", "") if is_saved else ""
        # A manually-entered Est. Value lives in self.saved, not in `bid`
        # itself (bid comes from self.bid_data, which _edit_value never
        # touches) -- so this has to be read the same overridden way as
        # note/pipeline above, or a value just saved from this exact card
        # looks like it silently failed to save.
        value = (self.saved.get(bid["_id"], {}).get("value") if is_saved else "") or bid.get("value", "")

        status_key = bid.get("status", "open").lower()
        pill_bg, pill_fg = STATUS_COLORS.get(status_key, ("#1c1917", TEXT3))
        dleft = days_until(bid)
        if status_key == "open" and dleft is not None and dleft <= 2:
            bar = RED
        elif status_key == "open" and dleft is not None and dleft <= 7:
            bar = ACCENT
        else:
            bar = GREEN if status_key == "open" else (ACCENT if status_key == "pending" else TEXT3)

        outer = tk.Frame(parent, bg=CARD)
        outer.pack(fill="x", padx=20, pady=5)
        tk.Frame(outer, bg=bar, width=4).pack(side="left", fill="y")
        body = tk.Frame(outer, bg=CARD, padx=18, pady=14)
        body.pack(side="left", fill="both", expand=True)

        r1 = tk.Frame(body, bg=CARD)
        r1.pack(fill="x")
        title_lbl = tk.Label(r1, text=bid.get("title", "Untitled Project"), font=F_SUB,
                             bg=CARD, fg=TEXT, anchor="w", cursor="hand2")
        title_lbl.pack(side="left")
        title_lbl.bind("<Button-1>", lambda e, c=city, b=dict(bid): self._show_bid_detail(c, b))

        star = tk.Label(r1, text="★" if is_saved else "☆", font=(UI, fs(15)), bg=CARD,
                        fg=ACCENT if is_saved else TEXT3, cursor="hand2")
        star.pack(side="right", padx=(8, 0))
        star.bind("<Button-1>", lambda e, c=city, b=dict(bid): self._toggle_save(c, b))

        if city:
            tk.Label(r1, text=f"  {city}  ", font=F_SMALL, bg=BORDER,
                     fg=TEXT2).pack(side="right", padx=(4, 0))
        tk.Label(r1, text=f"  {bid.get('status','Open').upper()}  ", font=F_SMALL,
                 bg=pill_bg, fg=pill_fg).pack(side="right")

        scope = bid.get("scope", "").strip()
        if scope:
            tk.Label(body, text=scope, font=F_BODY, bg=CARD, fg=TEXT2,
                     anchor="w", wraplength=820, justify="left").pack(fill="x", pady=(5, 8))

        meta = tk.Frame(body, bg=CARD)
        meta.pack(fill="x")

        def chip(text, color=TEXT3):
            tk.Label(meta, text=text, font=F_SMALL, bg=SURFACE, fg=color,
                     padx=10, pady=4).pack(side="left", padx=(0, 6))

        if bid.get("deadline"):
            suffix = ""
            if dleft is not None and dleft >= 0:
                suffix = "  •  today" if dleft == 0 else f"  •  {dleft}d left"
            dl_color = RED if (dleft is not None and dleft <= 2) else ACCENT
            chip(f"📅  {bid['deadline']}{suffix}", dl_color)
        if value:
            chip(f"💲  {value}", GREEN)
        if bid.get("contact"):
            chip(f"👤  {bid['contact']}", TEXT2)
        pipeline = self.saved.get(bid["_id"], {}).get("pipeline", "") if is_saved else ""
        if pipeline:
            pcolor = {"won": GREEN, "lost": RED}.get(pipeline, ACCENT)
            chip(pipeline.upper(), pcolor)

        # "Details" opens the full popup
        det = tk.Label(meta, text="🔎  Details →", font=(UI, fs(9), "bold"),
                       bg=SURFACE, fg=ACCENT, padx=10, pady=4, cursor="hand2")
        det.pack(side="left", padx=(0, 6))
        det.bind("<Button-1>", lambda e, c=city, b=dict(bid): self._show_bid_detail(c, b))

        note_btn = tk.Label(meta, text="📝  Note" if not note else "📝  Edit note",
                            font=F_SMALL, bg=SURFACE, fg=TEXT2,
                            padx=10, pady=4, cursor="hand2")
        note_btn.pack(side="right")
        note_btn.bind("<Button-1>", lambda e, c=city, b=dict(bid): self._edit_note(c, b))

        # Most scanned bids don't state a dollar value at all (the AI
        # extraction deliberately never guesses one) -- this lets a
        # contractor plug in their own estimate once they've actually
        # looked at the bid documents, instead of Est. Value just staying
        # blank forever.
        value_btn = tk.Label(meta, text="💲  Add value" if not value else "💲  Edit value",
                             font=F_SMALL, bg=SURFACE, fg=TEXT2,
                             padx=10, pady=4, cursor="hand2")
        value_btn.pack(side="right", padx=(0, 6))
        value_btn.bind("<Button-1>", lambda e, c=city, b=dict(bid): self._edit_value(c, b))

        # Make the whole title clickable too
        # (re-bind happens after title creation above via _title_lbl if present)

        if note:
            nf = tk.Frame(body, bg="#0f1320")
            nf.pack(fill="x", pady=(8, 0))
            tk.Label(nf, text=f"  📌  {note}", font=F_SMALL, bg="#0f1320",
                     fg=TEXT2, anchor="w", wraplength=800, justify="left",
                     padx=8, pady=6).pack(fill="x")

    def _show_bid_detail(self, city, bid):
        """Full-detail popup for a single bid."""
        bid = dict(bid)
        key = bid_id(city, bid)
        saved = key in self.saved
        note = self.saved.get(key, {}).get("note", "") if saved else ""
        # Same override as _bid_card: a manually-entered value lives in
        # self.saved, not in `bid` (which is untouched raw scan data).
        value = (self.saved.get(key, {}).get("value") if saved else "") or bid.get("value", "")

        win = tk.Toplevel(self.root)
        _apply_icon(win)
        win.title(bid.get("title", "Bid Details"))
        win.configure(bg=SURFACE)
        win.geometry("560x620")
        win.transient(self.root)
        win.grab_set()

        # Header
        head = tk.Frame(win, bg=CARD, padx=22, pady=18)
        head.pack(fill="x")
        status_key = bid.get("status", "open").lower()
        pill_bg, pill_fg = STATUS_COLORS.get(status_key, ("#1c1917", TEXT3))
        toprow = tk.Frame(head, bg=CARD)
        toprow.pack(fill="x")
        tk.Label(toprow, text=f"  {bid.get('status','Open').upper()}  ", font=F_SMALL,
                 bg=pill_bg, fg=pill_fg).pack(side="left")
        if city:
            tk.Label(toprow, text=f"  📍 {city}  ", font=F_SMALL, bg=BORDER,
                     fg=TEXT2).pack(side="left", padx=6)
        tk.Label(head, text=bid.get("title", "Untitled Project"), font=(UI, fs(16), "bold"),
                 bg=CARD, fg=TEXT, anchor="w", wraplength=500, justify="left").pack(
                 anchor="w", pady=(10, 0))

        # Scrollable detail body
        wrap = tk.Frame(win, bg=SURFACE)
        wrap.pack(fill="both", expand=True)
        canvas = tk.Canvas(wrap, bg=SURFACE, highlightthickness=0)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview, style="Vertical.TScrollbar")
        inner = tk.Frame(canvas, bg=SURFACE)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw", width=540)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        def field(label, value, link=None, link_type=None):
            if not value:
                return
            row = tk.Frame(inner, bg=SURFACE)
            row.pack(fill="x", padx=22, pady=(12, 0))
            tk.Label(row, text=label.upper(), font=(UI, fs(8), "bold"),
                     bg=SURFACE, fg=TEXT3, anchor="w").pack(anchor="w")
            if link:
                lbl = tk.Label(row, text=value, font=F_BODY, bg=SURFACE, fg=BLUE,
                               anchor="w", wraplength=480, justify="left", cursor="hand2")
                lbl.pack(anchor="w")
                if link_type == "email":
                    lbl.bind("<Button-1>", lambda e: webbrowser.open(f"mailto:{value}"))
                elif link_type == "phone":
                    lbl.bind("<Button-1>", lambda e: webbrowser.open(f"tel:{value}"))
                else:
                    lbl.bind("<Button-1>", lambda e: webbrowser.open(value))
            else:
                tk.Label(row, text=value, font=F_BODY, bg=SURFACE, fg=TEXT,
                         anchor="w", wraplength=480, justify="left").pack(anchor="w")

        field("Scope of Work", bid.get("scope"))
        field("Est. Value (if listed)", value or "Not listed")
        field("Deadline", bid.get("deadline") or "Not listed")
        field("Contact", bid.get("contact"))
        field("Email", bid.get("email"), link=True, link_type="email")
        field("Phone", bid.get("phone"), link=True, link_type="phone")
        field("Original Posting", bid.get("url"), link=True, link_type="url")
        if note:
            field("Your Note", note)

        pipeline = self.saved.get(key, {}).get("pipeline", "")
        prow = tk.Frame(inner, bg=SURFACE)
        prow.pack(fill="x", padx=22, pady=(14, 0))
        tk.Label(prow, text="BID STATUS", font=(UI, fs(8), "bold"),
                 bg=SURFACE, fg=TEXT3, anchor="w").pack(anchor="w", pady=(0, 4))
        pbtns = tk.Frame(prow, bg=SURFACE)
        pbtns.pack(anchor="w")

        def pick_status(status):
            self._set_pipeline_status(city, bid, status)
            win.destroy()
            self._show_bid_detail(city, bid)

        for skey, slabel in PIPELINE_LABELS:
            active = (pipeline == skey)
            bg_c = {"won": GREEN, "lost": RED}.get(skey, ACCENT) if active else CARD
            fg_c = "#000" if active else TEXT2
            pill_btn(pbtns, slabel, lambda s=skey: pick_status(s), bg=bg_c, fg=fg_c,
                     px=12, py=6, font=F_SMALL).pack(side="left", padx=(0, 6))

        # Action buttons
        actions = tk.Frame(win, bg=SURFACE, pady=14)
        actions.pack(fill="x", padx=22)
        divider(actions, BORDER)
        btnrow = tk.Frame(actions, bg=SURFACE)
        btnrow.pack(fill="x", pady=(12, 0))

        if bid.get("email"):
            pill_btn(btnrow, "✉  Email Contact",
                     lambda: webbrowser.open(f"mailto:{bid['email']}"),
                     px=16, py=8, font=F_SMALL).pack(side="left", padx=(0, 8))
        if bid.get("url"):
            pill_btn(btnrow, "🔗  Open Posting",
                     lambda: webbrowser.open(bid["url"]),
                     bg=CARD, fg=TEXT, px=16, py=8, font=F_SMALL).pack(side="left", padx=(0, 8))
        pill_btn(btnrow, "✍  Draft Proposal",
                 lambda: self._draft_proposal(city, bid),
                 bg=CARD, fg=TEXT, px=16, py=8, font=F_SMALL).pack(side="left", padx=(0, 8))
        pill_btn(btnrow, "📤  Share",
                 lambda: self._share_bid(city, bid),
                 bg=CARD, fg=TEXT, px=16, py=8, font=F_SMALL).pack(side="left", padx=(0, 8))
        if bid.get("deadline"):
            pill_btn(btnrow, "📅  Add to Calendar",
                     lambda: self._add_to_calendar(bid),
                     bg=CARD, fg=TEXT, px=16, py=8, font=F_SMALL).pack(side="left", padx=(0, 8))

        save_txt = "★  Saved" if saved else "☆  Save Lead"
        def do_save():
            self._toggle_save(city, bid)
            win.destroy()
        pill_btn(btnrow, save_txt, do_save, bg=CARD,
                 fg=ACCENT if saved else TEXT, px=16, py=8, font=F_SMALL).pack(side="right")

    def _draft_proposal(self, city, bid):
        if not bid.get("title", "").strip():
            messagebox.showwarning("Draft Proposal", "This bid is missing a title.")
            return

        prog = tk.Toplevel(self.root)
        _apply_icon(prog)
        prog.title("Drafting Proposal...")
        prog.configure(bg=SURFACE)
        prog.geometry("340x120")
        prog.transient(self.root)
        prog.grab_set()
        tk.Label(prog, text="✍  Drafting your proposal...", font=F_SUB,
                 bg=SURFACE, fg=TEXT).pack(pady=(24, 6))
        tk.Label(prog, text="Asking the AI for a ready-to-edit cover letter.",
                 font=F_SMALL, bg=SURFACE, fg=TEXT3).pack()

        def work():
            payload_bid = {"title": bid.get("title", ""), "scope": bid.get("scope", ""),
                           "deadline": bid.get("deadline", ""), "city": city}
            resp = subscription.draft_proposal(payload_bid, self.company)

            def finish():
                prog.destroy()
                if not resp.get("ok"):
                    reasons = {
                        "unreachable": "Couldn't reach the server. Check your internet and try again.",
                        "not_licensed": "Your trial or subscription isn't active.",
                        "ai_unavailable": "Proposal drafting isn't configured yet.",
                        "no_bid": "Missing bid details.",
                        "ai_error": "Couldn't draft a proposal. Try again.",
                    }
                    messagebox.showwarning("Draft Proposal",
                        reasons.get(resp.get("reason"), "Couldn't draft a proposal. Try again."))
                    return
                self._show_proposal(bid, resp.get("draft", ""))
            self.root.after(0, finish)

        threading.Thread(target=work, daemon=True).start()

    def _show_proposal(self, bid, draft):
        win = tk.Toplevel(self.root)
        _apply_icon(win)
        win.title("Proposal Draft")
        win.configure(bg=SURFACE)
        win.geometry("560x560")
        win.transient(self.root)
        win.grab_set()

        tk.Label(win, text="✍️  Proposal Draft", font=(UI, fs(16), "bold"),
                 bg=SURFACE, fg=TEXT).pack(anchor="w", padx=20, pady=(18, 2))
        tk.Label(win, text=f"{bid.get('title','')} — AI-generated starting point. Review before sending.",
                 font=F_SMALL, bg=SURFACE, fg=TEXT3, wraplength=520, justify="left").pack(
                 anchor="w", padx=20, pady=(0, 10))

        box = tk.Text(win, font=F_BODY, bg=CARD, fg=TEXT, relief="flat", bd=0,
                      wrap="word", insertbackground=TEXT, padx=12, pady=10)
        box.pack(fill="both", expand=True, padx=20)
        box.insert("1.0", draft)

        actions = tk.Frame(win, bg=SURFACE, pady=14)
        actions.pack(fill="x", padx=20)

        def do_copy():
            self.root.clipboard_clear()
            self.root.clipboard_append(box.get("1.0", tk.END).strip())
            copy_btn.config(text="✅  Copied")

        copy_btn = pill_btn(actions, "📋  Copy", do_copy, px=18, py=9, font=F_SMALL)
        copy_btn.pack(side="left")
        pill_btn(actions, "Close", win.destroy, bg=CARD, fg=TEXT,
                 px=18, py=9, font=F_SMALL).pack(side="right")

    def _share_bid(self, city, bid):
        """Desktop has no native share sheet, so this copies a formatted
        summary to the clipboard — the same fallback the web app uses when
        the browser Share API isn't available."""
        parts = [bid.get("title", "Bid")]
        meta = [p for p in (city, bid.get("value"),
                            f"Due {bid['deadline']}" if bid.get("deadline") else None) if p]
        if meta:
            parts.append(" • ".join(meta))
        if bid.get("url"):
            parts.append(bid["url"])
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(parts))
        messagebox.showinfo("Share", "Copied a summary of this bid to your clipboard.")

    def _add_to_calendar(self, bid):
        """Builds a minimal .ics for the bid's deadline and hands it to the
        system's default calendar app — same idea as the web app's
        downloadable .ics, just via the OS instead of a browser download."""
        dl = _parse_deadline_date(bid.get("deadline"))
        if not dl:
            messagebox.showwarning("Add to Calendar", "Couldn't read a calendar date from this deadline.")
            return
        uid = bid_id(bid.get("_city", ""), bid)
        stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%SZ")
        def esc(s):
            return str(s or "").replace(",", "\\,").replace(";", "\\;")
        summary = esc("Bid due: " + (bid.get("title") or "Untitled Project"))
        desc_parts = [bid.get("_city", ""), bid.get("scope", ""), bid.get("value", "")]
        description = esc(" - ".join(p for p in desc_parts if p))
        lines = [
            "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Bid Caller Pro//Bid Deadlines//EN",
            "BEGIN:VEVENT",
            f"UID:{uid}@bidcallerpro.app",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{dl.strftime('%Y%m%d')}",
            f"SUMMARY:{summary}",
            f"DESCRIPTION:{description}",
            f"LOCATION:{esc(bid.get('_city', ''))}",
            "END:VEVENT", "END:VCALENDAR",
        ]
        safe_name = re.sub(r"[^a-z0-9]+", "_", (bid.get("title") or "bid").lower())[:40]
        path = os.path.join(BASE_DIR, f"{safe_name}_deadline.ics")
        try:
            with open(path, "w", newline="\r\n", encoding="utf-8") as f:
                f.write("\r\n".join(lines))
            os.startfile(path)
        except Exception as e:
            debug(f"Add to calendar failed: {e}")
            messagebox.showerror("Add to Calendar", "Couldn't create the calendar event.")

    def _sync_saved_bid(self, key):
        """Best-effort push of one saved bid to the cloud — no-op if signed
        out, and any network failure is silently ignored since the local
        JSON file is always the fallback source of truth."""
        token = auth_client.current_access_token()
        rec = self.saved.get(key)
        if not token or rec is None:
            return
        threading.Thread(target=lambda: data_sync.push_saved_bid(token, key, rec),
                         daemon=True).start()

    def _sync_delete_saved_bid(self, key):
        token = auth_client.current_access_token()
        if not token:
            return
        threading.Thread(target=lambda: data_sync.delete_saved_bid(token, key),
                         daemon=True).start()

    def _toggle_save(self, city, bid):
        bid = dict(bid)
        key = bid_id(city, bid)
        if key in self.saved:
            del self.saved[key]
            write_saved(self.saved)
            self._sync_delete_saved_bid(key)
        else:
            rec = dict(bid)
            rec["_city"] = city
            rec["note"] = ""
            rec["saved_at"] = datetime.datetime.now().strftime("%d %b %Y")
            self.saved[key] = rec
            write_saved(self.saved)
            self._sync_saved_bid(key)
        self._refresh_all()

    def _set_pipeline_status(self, city, bid, status):
        """Sets/clears a Submitted/Won/Lost/Passed status. Setting a status
        on a bid that isn't saved yet saves it too — tracking a bid's status
        is itself a reason to keep it."""
        bid = dict(bid)
        key = bid_id(city, bid)
        current = self.saved.get(key, {}).get("pipeline", "")
        new_status = "" if current == status else status
        if key not in self.saved:
            rec = dict(bid)
            rec["_city"] = city
            rec["note"] = ""
            rec["saved_at"] = datetime.datetime.now().strftime("%d %b %Y")
            self.saved[key] = rec
        self.saved[key]["pipeline"] = new_status
        write_saved(self.saved)
        self._sync_saved_bid(key)
        self._refresh_all()

    def _edit_note(self, city, bid):
        bid = dict(bid)
        key = bid_id(city, bid)

        win = tk.Toplevel(self.root)
        _apply_icon(win)
        win.title("Note")
        win.configure(bg=SURFACE)
        win.geometry("440x300")
        win.transient(self.root)
        win.grab_set()

        tk.Label(win, text=bid.get("title", "Bid"), font=F_SUB, bg=SURFACE,
                 fg=ACCENT, wraplength=400).pack(padx=20, pady=(18, 4), anchor="w")
        tk.Label(win, text="Add a note — e.g. 'Called Tuesday, awaiting quote'",
                 font=F_SMALL, bg=SURFACE, fg=TEXT3).pack(padx=20, anchor="w")

        box = tk.Text(win, font=F_BODY, bg=CARD, fg=TEXT, relief="flat", bd=0,
                      height=7, wrap="word", insertbackground=TEXT)
        box.pack(fill="both", expand=True, padx=20, pady=12)
        if self.saved.get(key, {}).get("note"):
            box.insert("1.0", self.saved[key]["note"])

        def do_save():
            text = box.get("1.0", tk.END).strip()
            if key not in self.saved:
                rec = dict(bid)
                rec["_city"] = city
                rec["note"] = text
                rec["saved_at"] = datetime.datetime.now().strftime("%d %b %Y")
                self.saved[key] = rec
            else:
                self.saved[key]["note"] = text
            write_saved(self.saved)
            self._sync_saved_bid(key)
            win.destroy()
            self._refresh_all()

        row = tk.Frame(win, bg=SURFACE)
        row.pack(fill="x", padx=20, pady=(0, 16))
        pill_btn(row, "Save note", do_save, px=18, py=8).pack(side="right")
        ghost_btn(row, "Cancel", win.destroy).pack(side="right", padx=(0, 8))

    def _edit_value(self, city, bid):
        """Manual override for Est. Value -- most scanned bids never state
        one (the AI extraction is deliberately told not to guess), so this
        is the only way it's ever filled in for most bids. Same
        auto-save-if-not-tracked-yet behavior as _edit_note: annotating an
        unsaved bid starts tracking it."""
        bid = dict(bid)
        key = bid_id(city, bid)

        win = tk.Toplevel(self.root)
        _apply_icon(win)
        win.title("Est. Value")
        win.configure(bg=SURFACE)
        win.geometry("380x180")
        win.transient(self.root)
        win.grab_set()

        tk.Label(win, text=bid.get("title", "Bid"), font=F_SUB, bg=SURFACE,
                 fg=ACCENT, wraplength=340, justify="left").pack(padx=20, pady=(18, 4), anchor="w")
        tk.Label(win, text="Enter your own estimate if the bid doesn't list one -- e.g. \"$45,000\" or \"$120k\"",
                 font=F_SMALL, bg=SURFACE, fg=TEXT3, wraplength=340, justify="left").pack(padx=20, anchor="w")

        entry = tk.Entry(win, font=F_BODY, bg=CARD, fg=TEXT, relief="flat", bd=0,
                         insertbackground=TEXT, highlightthickness=1,
                         highlightbackground=BORDER, highlightcolor=ACCENT)
        entry.pack(fill="x", padx=20, pady=12, ipady=6)
        current = self.saved.get(key, {}).get("value") or bid.get("value") or ""
        entry.insert(0, current)
        entry.bind("<Return>", lambda e: do_save())

        def do_save():
            text = entry.get().strip()
            if key not in self.saved:
                rec = dict(bid)
                rec["_city"] = city
                rec["value"] = text
                rec["saved_at"] = datetime.datetime.now().strftime("%d %b %Y")
                self.saved[key] = rec
            else:
                self.saved[key]["value"] = text
            write_saved(self.saved)
            self._sync_saved_bid(key)
            win.destroy()
            self._refresh_all()

        row = tk.Frame(win, bg=SURFACE)
        row.pack(fill="x", padx=20, pady=(0, 16))
        pill_btn(row, "Save", do_save, px=18, py=8).pack(side="right")
        ghost_btn(row, "Cancel", win.destroy).pack(side="right", padx=(0, 8))

    def _refresh_all(self):
        if self._current == "feed":
            self._render_feed()
        if self._current == "saved":
            self._render_saved()
        if self._current == "search":
            self._rerender_search()

    # ═══════════ PAGE: LIVE FEED ═══════════
    def _page_feed(self):
        pg = tk.Frame(self._container, bg=BG)
        self._pages["feed"] = pg

        hdr = tk.Frame(pg, bg=BG, pady=18)
        hdr.pack(fill="x", padx=24)
        tk.Label(hdr, text="Live Bid Feed", font=F_HEAD, bg=BG, fg=TEXT).pack(side="left")
        ghost_btn(hdr, "↺  Refresh", self._render_feed).pack(side="right")
        ghost_btn(hdr, "⬇  Export CSV", self._export_csv).pack(side="right", padx=(0, 8))
        ghost_btn(hdr, "🗑  Clear Bids", self._clear_feed).pack(side="right", padx=(0, 8))

        fbar = tk.Frame(pg, bg=SURFACE, pady=10)
        fbar.pack(fill="x")
        inner = tk.Frame(fbar, bg=SURFACE)
        inner.pack(padx=24)

        tk.Label(inner, text="🔎", font=F_BODY, bg=SURFACE, fg=TEXT3).pack(side="left", padx=(0, 6))
        self._kw_var = tk.StringVar()
        kw = tk.Entry(inner, textvariable=self._kw_var, font=F_BODY, bg=CARD, fg=TEXT,
                      relief="flat", bd=0, insertbackground=TEXT, width=26,
                      highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT)
        kw.pack(side="left", ipady=6, padx=(0, 12))
        kw.bind("<KeyRelease>", lambda e: self._render_feed())

        self._city_var = tk.StringVar(value="All")
        self._city_filter_box = ttk.Combobox(inner, textvariable=self._city_var, state="readonly",
                                              values=["All"], font=F_BODY, width=14)
        self._city_filter_box.pack(side="left", padx=(0, 10))
        self._city_filter_box.bind("<<ComboboxSelected>>", lambda e: self._render_feed())

        self._trade_var = tk.StringVar(value="All trades")
        cb2 = ttk.Combobox(inner, textvariable=self._trade_var, state="readonly",
                           values=self.TRADES, font=F_BODY, width=18)
        cb2.pack(side="left", padx=(0, 10))
        cb2.bind("<<ComboboxSelected>>", lambda e: self._render_feed())

        self._sort_var = tk.StringVar(value="Best Match")
        cb3 = ttk.Combobox(inner, textvariable=self._sort_var, state="readonly",
                           values=["Best Match", "Deadline", "Est. Value"], font=F_BODY, width=12)
        cb3.pack(side="left")
        cb3.bind("<<ComboboxSelected>>", lambda e: self._render_feed())

        self._stats_strip = tk.Frame(pg, bg=SURFACE, pady=10)
        self._stats_strip.pack(fill="x")
        divider(pg, BORDER)

        self._feed_frame = scrollable(pg)

    def _matches_filters(self, bid):
        kw = self._kw_var.get().strip().lower() if hasattr(self, "_kw_var") else ""
        if kw:
            blob = (bid.get("title", "") + " " + bid.get("scope", "")).lower()
            if kw not in blob:
                return False
        trade = self._trade_var.get() if hasattr(self, "_trade_var") else "All trades"
        if trade != "All trades":
            kws = self.TRADE_KEYWORDS.get(trade, [])
            if kws:
                blob = (bid.get("title", "") + " " + bid.get("scope", "")).lower()
                if not any(k in blob for k in kws):
                    return False
        return True

    def _build_stats(self):
        for w in self._stats_strip.winfo_children():
            w.destroy()
        total = sum(len(v) for v in self.bid_data.values())
        open_c = sum(1 for v in self.bid_data.values() for b in v
                     if b.get("status", "").lower() == "open")
        cities = len([c for c, v in self.bid_data.items() if v])

        def stat(label, val, color):
            f = tk.Frame(self._stats_strip, bg=SURFACE, padx=26)
            f.pack(side="left")
            tk.Label(f, text=str(val), font=F_BIG, bg=SURFACE, fg=color).pack()
            tk.Label(f, text=label, font=F_SMALL, bg=SURFACE, fg=TEXT3).pack()
            tk.Frame(self._stats_strip, bg=BORDER, width=1).pack(side="left", fill="y", pady=8)

        stat("Total Bids", total or "—", TEXT)
        stat("Open Now", open_c or "—", GREEN)
        stat("Towns", cities or "—", BLUE)
        stat("Saved", len(self.saved) or "—", ACCENT)

        f = tk.Frame(self._stats_strip, bg=SURFACE, padx=26)
        f.pack(side="left")
        tk.Label(f, text=self._last_scan(), font=(UI, fs(11), "bold"),
                 bg=SURFACE, fg=TEXT3).pack()
        tk.Label(f, text="Last Scan", font=F_SMALL, bg=SURFACE, fg=TEXT3).pack()

    def _render_feed(self):
        self._build_stats()
        for w in self._feed_frame.winfo_children():
            w.destroy()

        sel = self._city_var.get() if hasattr(self, "_city_var") else "All"
        rows = []
        for city, bids in self.bid_data.items():
            if sel == "All" or sel.lower() == city.lower():
                for b in bids:
                    if self._matches_filters(b):
                        rows.append((city, b))

        sort_by = self._sort_var.get() if hasattr(self, "_sort_var") else "Best Match"
        if sort_by == "Deadline":
            def _deadline_key(cb):
                d = days_until(cb[1])
                return d if d is not None else 9999
            rows.sort(key=_deadline_key)
        elif sort_by == "Est. Value":
            # A manually-entered value lives in self.saved, not in the raw
            # bid dict -- without this override, sorting by value ignores
            # every value the user typed in themselves (see _bid_card).
            def _value_key(cb):
                city, b = cb
                override = self.saved.get(bid_id(city, b), {}).get("value")
                return -parse_value(override or b.get("value"))
            rows.sort(key=_value_key)
        # "Best Match" (default): leave rows in the order /scan returned them --
        # the server already ranks each city's bids by fit (deadline urgency,
        # niche keyword strength, contact completeness).

        if not rows:
            self._empty_state(self._feed_frame,
                              "No bids match" if self.bid_data else "No bids loaded yet",
                              "Try clearing filters." if self.bid_data
                              else "Go to Find Bids to pull live bids near you.",
                              show_scan_btn=not self.bid_data)
            return

        # ── City tiles row (click to filter) ──
        if len(self.bid_data) > 1:
            tk.Label(self._feed_frame, text="  CITIES", font=(UI, fs(9), "bold"),
                     bg=BG, fg=TEXT3).pack(anchor="w", padx=20, pady=(12, 2))
            tiles = tk.Frame(self._feed_frame, bg=BG)
            tiles.pack(fill="x", padx=16, pady=(0, 6))

            def make_tile(parent, label, count, active, on_click):
                t = tk.Frame(parent, bg=ACCENT if active else CARD,
                             padx=16, pady=10, cursor="hand2")
                t.pack(side="left", padx=4, pady=4)
                fgc = "#000" if active else TEXT
                cnt = tk.Label(t, text=str(count), font=(UI, fs(16), "bold"),
                               bg=ACCENT if active else CARD, fg=fgc)
                cnt.pack()
                nm = tk.Label(t, text=label, font=F_SMALL,
                              bg=ACCENT if active else CARD,
                              fg="#000" if active else TEXT3)
                nm.pack()
                for w in (t, cnt, nm):
                    w.bind("<Button-1>", lambda e: on_click())

            total_all = sum(len(v) for v in self.bid_data.values())
            make_tile(tiles, "All", total_all, sel == "All",
                      lambda: (self._city_var.set("All"), self._render_feed()))
            for c in sorted(self.bid_data.keys()):
                cnt = len(self.bid_data[c])
                if cnt == 0:
                    continue
                make_tile(tiles, c, cnt, sel.lower() == c.lower(),
                          lambda cc=c: (self._city_var.set(cc), self._render_feed()))

            divider(self._feed_frame, BORDER, pad=20)

        open_rows = [(c, b) for c, b in rows if b.get("status", "").lower() == "open"]
        other_rows = [(c, b) for c, b in rows if b.get("status", "").lower() != "open"]

        if open_rows:
            tk.Label(self._feed_frame, text="  OPEN BIDS", font=(UI, fs(9), "bold"),
                     bg=BG, fg=GREEN).pack(anchor="w", padx=20, pady=(14, 4))
            for c, b in open_rows:
                self._bid_card(self._feed_frame, b, c)
        if other_rows:
            tk.Label(self._feed_frame, text="  OTHER BIDS", font=(UI, fs(9), "bold"),
                     bg=BG, fg=TEXT3).pack(anchor="w", padx=20, pady=(18, 4))
            for c, b in other_rows:
                self._bid_card(self._feed_frame, b, c)
        tk.Frame(self._feed_frame, bg=BG, height=30).pack()

    def _empty_state(self, parent, title, subtitle, show_scan_btn=False):
        e = tk.Frame(parent, bg=BG, pady=70)
        e.pack(fill="x")
        tk.Label(e, text="🏗", font=(UI, fs(36)), bg=BG, fg=BORDER).pack()
        tk.Label(e, text=title, font=(UI, fs(16), "bold"), bg=BG, fg=TEXT2).pack(pady=6)
        tk.Label(e, text=subtitle, font=F_BODY, bg=BG, fg=TEXT3).pack()
        if show_scan_btn:
            pill_btn(e, "Find Bids →", lambda: self._nav("scan"), py=10, px=24).pack(pady=16)

    def _last_scan(self):
        p = os.path.join(BASE_DIR, "last_scan.txt")
        if os.path.exists(p):
            with open(p) as f:
                return f.read().strip()
        return "Never"

    # ═══════════ PAGE: SAVED ═══════════
    def _page_saved(self):
        pg = tk.Frame(self._container, bg=BG)
        self._pages["saved"] = pg
        self._saved_pipeline_filter = "All"
        hdr = tk.Frame(pg, bg=BG, pady=18)
        hdr.pack(fill="x", padx=24)
        tk.Label(hdr, text="Active Bids", font=F_HEAD, bg=BG, fg=TEXT).pack(side="left")
        self._saved_export_btn = ghost_btn(hdr, "  ⬇  Export  ", self._export_saved_csv, font=F_SMALL)
        divider(pg, BORDER)
        self._saved_stats_frame = tk.Frame(pg, bg=BG)
        self._saved_stats_frame.pack(fill="x", padx=20, pady=(14, 0))
        self._saved_tiles_frame = tk.Frame(pg, bg=BG)
        self._saved_tiles_frame.pack(fill="x", padx=20, pady=(10, 4))
        self._saved_frame = scrollable(pg)

    def _pipeline_stats(self):
        """Win rate + $ won + $ tracked across all saved bids — used by the
        Saved tab's scoreboard and the Account tab's stats summary."""
        counts = {"All": len(self.saved), "none": 0, "submitted": 0, "won": 0, "lost": 0, "passed": 0}
        won_value, tracked_value = 0.0, 0.0
        for rec in self.saved.values():
            st = rec.get("pipeline", "")
            if st:
                counts[st] = counts.get(st, 0) + 1
            else:
                counts["none"] += 1
            v = parse_value(rec.get("value"))
            if v >= 0:
                tracked_value += v
                if st == "won":
                    won_value += v
        decided = counts["won"] + counts["lost"]
        win_rate = f"{round(counts['won'] / decided * 100)}%" if decided else "—"
        return counts, win_rate, won_value, tracked_value

    def _render_saved(self):
        for w in self._saved_stats_frame.winfo_children():
            w.destroy()
        for w in self._saved_tiles_frame.winfo_children():
            w.destroy()
        for w in self._saved_frame.winfo_children():
            w.destroy()

        if not self.saved:
            self._saved_export_btn.pack_forget()
            e = tk.Frame(self._saved_frame, bg=BG, pady=70)
            e.pack(fill="x")
            tk.Label(e, text="⭐", font=(UI, fs(36)), bg=BG, fg=BORDER).pack()
            tk.Label(e, text="No active bids yet", font=(UI, fs(16), "bold"),
                     bg=BG, fg=TEXT2).pack(pady=6)
            tk.Label(e, text="Tap the ☆ on any bid to start tracking it here.",
                     font=F_BODY, bg=BG, fg=TEXT3).pack()
            return
        self._saved_export_btn.pack(side="right")

        # ── Pipeline scoreboard: win rate + $ won + $ tracked ──
        counts, win_rate, won_value, tracked_value = self._pipeline_stats()
        for n, label in [(win_rate, "Win Rate"),
                          (str(counts["won"]), f"Won ({format_money(won_value)})"),
                          (format_money(tracked_value), "Tracked Value")]:
            box = tk.Frame(self._saved_stats_frame, bg=CARD, padx=14, pady=10)
            box.pack(side="left", fill="x", expand=True, padx=(0, 8))
            tk.Label(box, text=n, font=(UI, fs(16), "bold"), bg=CARD, fg=TEXT).pack()
            tk.Label(box, text=label, font=F_SMALL, bg=CARD, fg=TEXT3).pack()

        # ── Filter tiles ──
        cats = [("All", "All"), ("none", "Untracked")] + PIPELINE_LABELS
        for key, label in cats:
            if key != "All" and not counts.get(key):
                continue
            active = (self._saved_pipeline_filter == key)
            tile = tk.Frame(self._saved_tiles_frame, bg=CARD if active else SURFACE,
                            highlightbackground=ACCENT if active else BORDER,
                            highlightthickness=1, padx=12, pady=8, cursor="hand2")
            tile.pack(side="left", padx=(0, 8))
            n_lbl = tk.Label(tile, text=str(counts.get(key, 0)), font=(UI, fs(13), "bold"),
                             bg=tile["bg"], fg=TEXT)
            n_lbl.pack()
            l_lbl = tk.Label(tile, text=label, font=F_SMALL, bg=tile["bg"], fg=TEXT2)
            l_lbl.pack()
            def pick(k=key):
                self._saved_pipeline_filter = k
                self._render_saved()
            for w in (tile, n_lbl, l_lbl):
                w.bind("<Button-1>", lambda e, k=key: pick(k))

        # ── Filtered list ──
        f = self._saved_pipeline_filter
        if f == "All":
            filtered = list(self.saved.items())
        elif f == "none":
            filtered = [(k, r) for k, r in self.saved.items() if not r.get("pipeline")]
        else:
            filtered = [(k, r) for k, r in self.saved.items() if r.get("pipeline") == f]

        if not filtered:
            tk.Label(self._saved_frame, text="Nothing here with this status.",
                     font=F_BODY, bg=BG, fg=TEXT3).pack(pady=40)
            return
        tk.Label(self._saved_frame, text=f"  {len(filtered)} saved bid(s)",
                 font=(UI, fs(9), "bold"), bg=BG, fg=ACCENT).pack(
                 anchor="w", padx=20, pady=(14, 4))
        for key, rec in filtered:
            self._bid_card(self._saved_frame, rec, rec.get("_city", ""))
        tk.Frame(self._saved_frame, bg=BG, height=30).pack()

    def _export_saved_csv(self):
        """Exports the currently filtered saved bids, including pipeline status."""
        f = getattr(self, "_saved_pipeline_filter", "All")
        if f == "All":
            rows = list(self.saved.items())
        elif f == "none":
            rows = [(k, r) for k, r in self.saved.items() if not r.get("pipeline")]
        else:
            rows = [(k, r) for k, r in self.saved.items() if r.get("pipeline") == f]
        if not rows:
            messagebox.showinfo("Nothing to Export", "No saved bids match this filter.")
            return
        import csv
        from tkinter import filedialog
        default = f"bid_caller_saved_{datetime.datetime.now().strftime('%Y%m%d')}.csv"
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", initialfile=default,
            filetypes=[("CSV files", "*.csv")], title="Save bids as CSV")
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as f_out:
                w = csv.writer(f_out)
                w.writerow(["City", "Title", "Scope", "Est. Value", "Deadline",
                            "Contact", "Email", "Phone", "Source URL", "Pipeline Status"])
                for _, rec in rows:
                    w.writerow([rec.get("_city", ""), rec.get("title", ""), rec.get("scope", ""),
                                rec.get("value", ""), rec.get("deadline", ""), rec.get("contact", ""),
                                rec.get("email", ""), rec.get("phone", ""), rec.get("url", ""),
                                rec.get("pipeline", "")])
            messagebox.showinfo("Exported", f"Saved your bids to:\n{path}")
        except Exception as e:
            debug(f"Saved CSV export failed: {e}")
            messagebox.showerror("Export Failed", "Couldn't save the file. Try a different location.")

    # ═══════════ PAGE: FIND BIDS (location-based) ═══════════
    def _page_scan(self):
        pg = tk.Frame(self._container, bg=BG)
        self._pages["scan"] = pg

        hdr = tk.Frame(pg, bg=BG, pady=18)
        hdr.pack(fill="x", padx=24)
        tk.Label(hdr, text="Find Bids Near You", font=F_HEAD, bg=BG, fg=TEXT).pack(side="left")
        divider(pg, BORDER)

        body = tk.Frame(pg, bg=BG)
        body.pack(fill="both", expand=True, padx=24, pady=18)

        self._build_loc_map(body)

        form = tk.Frame(body, bg=SURFACE, padx=24, pady=18)
        form.pack(fill="x", pady=(0, 12))

        tk.Label(form, text="Your Location", font=(UI, fs(11), "bold"),
                 bg=SURFACE, fg=ACCENT).pack(anchor="w", pady=(0, 4))

        loc_row = tk.Frame(form, bg=SURFACE)
        loc_row.pack(fill="x", pady=(0, 10))
        self._loc_entry = tk.Entry(loc_row, font=F_BODY, bg=CARD, fg=TEXT,
                                   insertbackground=TEXT, relief="flat", bd=0,
                                   highlightthickness=1, highlightbackground=BORDER,
                                   highlightcolor=ACCENT)
        self._loc_entry.pack(side="left", fill="x", expand=True, ipady=8, padx=(0, 8))
        self._loc_entry.bind("<Return>", self._preview_typed_location)
        ghost_btn(loc_row, "  Auto-Detect  ", self._auto_locate, font=F_SMALL).pack(side="left")
        tk.Label(form, text="Enter a ZIP code or 'City, State'",
                 font=F_SMALL, bg=SURFACE, fg=TEXT3).pack(anchor="w")

        tk.Label(form, text="Search Radius", font=(UI, fs(11), "bold"),
                 bg=SURFACE, fg=ACCENT).pack(anchor="w", pady=(14, 4))
        rad_row = tk.Frame(form, bg=SURFACE)
        rad_row.pack(fill="x")
        self._radius_var = tk.StringVar(value="25 miles")
        self._radius_combo = ttk.Combobox(rad_row, textvariable=self._radius_var, state="readonly",
                     values=["10 miles", "25 miles", "50 miles", "75 miles", "125 miles"],
                     font=F_BODY, width=16)
        self._radius_combo.pack(side="left")
        self._radius_combo.bind("<<ComboboxSelected>>", self._on_radius_change)

        divider(form, BORDER, pad=0)

        btn_row = tk.Frame(form, bg=SURFACE)
        btn_row.pack(anchor="w", pady=(16, 0))
        self._scan_btn = pill_btn(btn_row, "  🔍  Scan for Bids  ",
                                  self._run_scan, px=26, py=12,
                                  font=(UI, fs(12), "bold"))
        self._scan_btn.pack(side="left")
        self._map_btn = ghost_btn(btn_row, "  📍  View Map  ",
                                  self._open_scan_map, font=F_SMALL)
        self._map_btn.pack(side="left", padx=(10, 0))
        self._map_btn.config(state="disabled")
        ghost_btn(btn_row, "  📌  Save Search  ", self._save_current_search,
                 font=F_SMALL).pack(side="left", padx=(10, 0))

        self._saved_search_frame = tk.Frame(body, bg=BG)
        self._saved_search_frame.pack(fill="x")
        self._render_saved_searches()

        self._scan_status = tk.Label(body, text="", font=F_BODY, bg=BG, fg=TEXT3)
        self._scan_status.pack(pady=(20, 6))
        self._prog_frame = tk.Frame(body, bg=BORDER, height=4, width=480)
        self._prog_frame.pack(pady=(0, 6))
        self._prog_bar = tk.Frame(self._prog_frame, bg=ACCENT, height=4, width=0)
        self._prog_bar.place(x=0, y=0)
        self._scan_log = tk.Text(body, font=F_MONO, bg=SURFACE, fg=TEXT2, relief="flat",
                                 bd=0, height=11, state="disabled", wrap="word")
        self._scan_log.pack(fill="both", expand=True, pady=12)

        self.root.after(600, self._maybe_auto_locate_on_load)

    def _build_loc_map(self, parent):
        """Embedded 'you are here' map at the top of the Find tab (no browser tab)."""
        wrap = tk.Frame(parent, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1)
        wrap.pack(fill="x", pady=(0, 12))

        if tkintermapview is None:
            tk.Label(wrap, text="📍  Live map needs one more install: pip install tkintermapview pillow",
                     font=F_SMALL, bg=SURFACE, fg=TEXT3, pady=14).pack()
            self._loc_map = None
            self._loc_marker = None
            return

        self._loc_map = tkintermapview.TkinterMapView(wrap, width=600, height=220, corner_radius=0)
        self._loc_map.pack(fill="x")
        # tkintermapview defaults to a.tile.openstreetmap.org with a generic,
        # library-wide User-Agent — OSM's volunteer-run tile servers tend to
        # throttle that shared identifier. Carto's free basemap CDN is built
        # for embedding and loads noticeably faster (dark tiles to match the UI).
        self._loc_map.set_tile_server(
            "https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png", max_zoom=20)
        self._loc_map.set_position(39.5, -98.35)   # center of the US until we know better
        self._loc_map.set_zoom(4)
        self._loc_marker = None
        self._loc_radius_poly = None
        self._loc_town_markers = []
        self._loc_map.add_left_click_map_command(self._on_map_click)
        # tkintermapview only re-fetches tiles that are newly scrolled into
        # view — a tile that's already on screen but failed to load (see
        # _patch_tkintermapview_tile_retry) never gets nudged to retry on
        # its own. Re-trigger a cache-check-and-refetch pass a few times
        # after any zoom/pan so stuck tiles get another chance without the
        # user having to do anything.
        self._loc_map.canvas.bind("<MouseWheel>", lambda e: self._schedule_tile_retry(), add="+")
        self._loc_map.canvas.bind("<ButtonRelease-1>", lambda e: self._schedule_tile_retry(), add="+")
        tk.Label(wrap, text="Click anywhere on the map — or click a nearby city — to set your search center.",
                 font=F_SMALL, bg=SURFACE, fg=TEXT3, pady=6).pack()

    def _update_loc_map(self, lat, lon, label=""):
        if not getattr(self, "_loc_map", None):
            return
        # Only force the zoom level on the very first location set. Forcing
        # it on every call — including from background callbacks that can
        # land late (auto-locate on startup, a town-marker refresh finishing
        # after the user has since zoomed out to look around) — snaps the
        # view back to a zoom level whose tiles aren't cached yet, which
        # shows as the map briefly rendering, then going blank while it
        # re-fetches.
        first_time = self._loc_marker is None
        self._loc_map.set_position(lat, lon)
        if first_time:
            self._loc_map.set_zoom(11)
        if self._loc_marker:
            self._loc_marker.delete()
        self._loc_marker = self._loc_map.set_marker(lat, lon, text=label or "You are here")
        self._draw_radius_circle(lat, lon)
        self._refresh_town_markers(lat, lon)
        self._schedule_tile_retry()

    def _schedule_tile_retry(self, attempts=3):
        """Debounced: each new zoom/pan bumps a generation counter, so only
        the retry chain from the LATEST interaction actually runs — avoids
        piling up timers while the user is actively scrolling."""
        if not getattr(self, "_loc_map", None):
            return
        self._tile_retry_gen = getattr(self, "_tile_retry_gen", 0) + 1
        gen = self._tile_retry_gen

        def go(n):
            if gen != self._tile_retry_gen or not getattr(self, "_loc_map", None):
                return
            try:
                self._loc_map.draw_zoom()
            except Exception:
                pass
            if n > 1:
                self.root.after(1500, lambda: go(n - 1))
        self.root.after(1200, lambda: go(attempts))

    def _build_simple_map(self, parent, entry_widget, status_setter=None):
        """A lighter 'click to set location' map for tabs that don't need
        Find's radius-circle/nearby-town-marker machinery (Upcoming, Leads)
        -- just click, reverse-geocode, and fill the given entry widget.
        Keeps the map standard across every search tab instead of only Find
        having one, without dragging in logic those tabs don't use."""
        wrap = tk.Frame(parent, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1)
        wrap.pack(fill="x", pady=(0, 12))

        if tkintermapview is None:
            tk.Label(wrap, text="📍  Live map needs one more install: pip install tkintermapview pillow",
                     font=F_SMALL, bg=SURFACE, fg=TEXT3, pady=14).pack()
            return None

        m = tkintermapview.TkinterMapView(wrap, width=600, height=220, corner_radius=0)
        m.pack(fill="x")
        m.set_tile_server("https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png", max_zoom=20)
        m.set_position(39.5, -98.35)
        m.set_zoom(4)
        state = {"marker": None, "retry_gen": 0}

        def schedule_retry(attempts=3):
            state["retry_gen"] += 1
            gen = state["retry_gen"]
            def go(n):
                if gen != state["retry_gen"]:
                    return
                try:
                    m.draw_zoom()
                except Exception:
                    pass
                if n > 1:
                    self.root.after(1500, lambda: go(n - 1))
            self.root.after(1200, lambda: go(attempts))

        m.canvas.bind("<MouseWheel>", lambda e: schedule_retry(), add="+")
        m.canvas.bind("<ButtonRelease-1>", lambda e: schedule_retry(), add="+")

        def set_marker(lat, lon, label):
            first_time = state["marker"] is None
            m.set_position(lat, lon)
            if first_time:
                m.set_zoom(11)
            if state["marker"]:
                state["marker"].delete()
            state["marker"] = m.set_marker(lat, lon, text=label or "Selected location")
            schedule_retry()

        def on_click(coords):
            lat, lon = coords
            fallback = f"{lat:.4f}, {lon:.4f}"
            entry_widget.delete(0, tk.END)
            entry_widget.insert(0, fallback)
            set_marker(lat, lon, None)
            if status_setter:
                status_setter(f"Location set: {fallback}", ACCENT)

            def work():
                label = radius_scanner.reverse_geocode(lat, lon)
                if not label:
                    return
                def apply():
                    entry_widget.delete(0, tk.END)
                    entry_widget.insert(0, label)
                    set_marker(lat, lon, label)
                    if status_setter:
                        status_setter(f"Location set: {label}", GREEN)
                self.root.after(0, apply)
            threading.Thread(target=work, daemon=True).start()

        m.add_left_click_map_command(on_click)
        tk.Label(wrap, text="Click anywhere on the map to set your search location.",
                 font=F_SMALL, bg=SURFACE, fg=TEXT3, pady=6).pack()
        return m

    def _radius_miles(self):
        try:
            return int(self._radius_var.get().split()[0])
        except Exception:
            return 25

    def _draw_radius_circle(self, lat, lon):
        if not getattr(self, "_loc_map", None):
            return
        if self._loc_radius_poly:
            self._loc_radius_poly.delete()
            self._loc_radius_poly = None
        radius_miles = self._radius_miles()
        EARTH_MI = 3958.8
        ang_r = radius_miles / EARTH_MI
        points = []
        for i in range(37):
            theta = math.radians(i * 10)
            dlat = ang_r * math.cos(theta)
            dlon = ang_r * math.sin(theta) / math.cos(math.radians(lat))
            points.append((lat + math.degrees(dlat), lon + math.degrees(dlon)))
        self._loc_radius_poly = self._loc_map.set_polygon(
            points, outline_color=ACCENT, fill_color=None, border_width=2)

    def _on_radius_change(self, event=None):
        latlon = getattr(self, "_auto_latlon", None)
        if latlon:
            self._draw_radius_circle(*latlon)

    def _clear_town_markers(self):
        for m in getattr(self, "_loc_town_markers", []):
            m.delete()
        self._loc_town_markers = []

    def _refresh_town_markers(self, lat, lon):
        """Plots nearby towns as clickable markers so the user can pick one
        instead of an arbitrary point. Runs the lookup off the UI thread."""
        radius_miles = self._radius_miles()

        def work():
            towns = radius_scanner.find_nearby_towns(lat, lon, radius_miles, max_towns=15)
            self.root.after(0, lambda: self._place_town_markers(towns))

        threading.Thread(target=work, daemon=True).start()

    def _place_town_markers(self, towns):
        if not getattr(self, "_loc_map", None):
            return
        self._clear_town_markers()
        for t in towns:
            if t.get("lat") is None or t.get("lon") is None:
                continue
            m = self._loc_map.set_marker(
                t["lat"], t["lon"], text=t["name"],
                marker_color_circle=BLUE, marker_color_outside="#1d4ed8",
                command=self._on_town_marker_click,
                data={"name": t["name"], "state": t.get("state", "")})
            self._loc_town_markers.append(m)

    def _on_town_marker_click(self, marker):
        lat, lon = marker.position
        name = (marker.data or {}).get("name", marker.text or "")
        state = (marker.data or {}).get("state", "")
        label = f"{name}, {state}".strip(", ") if state else name
        self._set_picked_location(lat, lon, label)

    def _on_map_click(self, coords):
        lat, lon = coords
        self._set_picked_location(lat, lon, None)

        def work():
            label = radius_scanner.reverse_geocode(lat, lon)
            if label:
                self.root.after(0, lambda: self._set_picked_location(
                    lat, lon, label, refresh_towns=False))

        threading.Thread(target=work, daemon=True).start()

    def _set_picked_location(self, lat, lon, label, refresh_towns=True):
        self._auto_latlon = (lat, lon)
        display = label or f"{lat:.4f}, {lon:.4f}"
        self._loc_entry.delete(0, tk.END)
        self._loc_entry.insert(0, display)
        self._scan_status.config(text=f"Location set: {display}", fg=GREEN)
        if refresh_towns:
            self._update_loc_map(lat, lon, display)
        else:
            # Just update the label on an already-drawn marker/circle — no
            # need to re-center or re-query nearby towns for the same point.
            self._loc_map.set_position(lat, lon)
            if self._loc_marker:
                self._loc_marker.delete()
            self._loc_marker = self._loc_map.set_marker(lat, lon, text=display)

    def _maybe_auto_locate_on_load(self):
        if self._map_auto_tried:
            return
        self._map_auto_tried = True
        self._auto_locate()

    def _preview_typed_location(self, event=None):
        location = self._loc_entry.get().strip()
        if not location:
            return
        self._scan_status.config(text="Locating...", fg=ACCENT)

        def work():
            geo = radius_scanner.geocode(location)
            if geo:
                lat, lon, label = geo
                self._auto_latlon = (lat, lon)
                self.root.after(0, lambda: (
                    self._scan_status.config(text=f"Location set: {label}", fg=GREEN),
                    self._update_loc_map(lat, lon, label)))
            else:
                self.root.after(0, lambda: self._scan_status.config(
                    text="Couldn't find that location. Try a ZIP code.", fg=RED))
        threading.Thread(target=work, daemon=True).start()

    def _log_scan(self, msg):
        self._scan_log.config(state="normal")
        self._scan_log.insert(tk.END, msg + "\n")
        self._scan_log.see(tk.END)
        self._scan_log.config(state="disabled")

    def _scan_guard(self):
        if not subscription.get_status()["active"]:
            messagebox.showwarning("Subscription Required",
                                   "Start your free trial or subscribe to scan.")
            self._nav("subscribe")
            return False
        if self.scan_running:
            return False
        return True

    def _auto_locate(self):
        self._scan_status.config(text="Detecting your location...", fg=ACCENT)
        def work():
            loc = radius_scanner.auto_locate()
            if loc:
                lat, lon, label = loc
                self._auto_latlon = (lat, lon)
                self.root.after(0, lambda: (
                    self._loc_entry.delete(0, tk.END),
                    self._loc_entry.insert(0, label.strip(", ")),
                    self._scan_status.config(text=f"Location set: {label}", fg=GREEN),
                    self._update_loc_map(lat, lon, label)))
            else:
                self.root.after(0, lambda: self._scan_status.config(
                    text="Couldn't auto-detect. Type a ZIP or city instead.", fg=RED))
        threading.Thread(target=work, daemon=True).start()

    def _run_scan(self):
        if not self._scan_guard():
            return
        location = self._loc_entry.get().strip()
        if not location:
            messagebox.showwarning("Location needed",
                                   "Enter a ZIP code or city, or tap Auto-Detect.")
            return
        radius = int(self._radius_var.get().split()[0])

        self.scan_running = True
        self._scan_btn.config(state="disabled", text="  Scanning...  ")
        self._scan_log.config(state="normal")
        self._scan_log.delete("1.0", tk.END)
        self._scan_log.config(state="disabled")
        self._set_prog(5)

        SCAN_REASONS = {
            "unreachable": "Couldn't reach the bid service. Check your internet and try again.",
            "not_licensed": "Your trial or subscription isn't active. Check the Subscription tab.",
            "location_not_found": "Couldn't find that location. Try a ZIP code.",
            "no_location": "Enter a ZIP code or city.",
        }

        def process():
            done = threading.Event()

            def tick():
                pct = 15
                while not done.wait(1.5):
                    pct = min(90, pct + 3)
                    self.root.after(0, lambda p=pct: self._set_prog(p))

            try:
                self._scan_status.config(text="Searching for bids in your area...", fg=ACCENT)
                self._set_prog(15)
                self._log_scan("Searching procurement sites and SAM.gov for bids near you...")
                threading.Thread(target=tick, daemon=True).start()

                # Runs entirely server-side: real procurement-platform search
                # (BidNet, DemandStar, PlanetBids, etc.) plus SAM.gov federal
                # bids — the same /scan the web app uses.
                resp = subscription.scan(location, radius)
                done.set()
                self._set_prog(100)

                if not resp.get("ok"):
                    msg = SCAN_REASONS.get(resp.get("reason"), "Scan hit a snag. Try again.")
                    self._log_scan(msg)
                    self._scan_status.config(text=msg, fg=RED)
                    self._set_prog(0)
                    return

                results = resp.get("bids", {})
                total = resp.get("total_bids", sum(len(v) for v in results.values()))
                self.bid_data = results
                write_feed(results)

                center = resp.get("center") or {}
                city_coords = resp.get("city_coords") or {}
                self._last_scan_summary = {
                    "center": center,
                    "town_coords": [
                        {"name": city, "state": "", "lat": c.get("lat"), "lon": c.get("lon"),
                         "status": "scanned"}
                        for city, c in city_coords.items()
                    ],
                }
                self._last_scan_label = resp.get("location") or location
                if city_coords:
                    self.root.after(0, lambda: self._map_btn.config(state="normal"))

                ts = datetime.datetime.now().strftime("%d %b %Y  %H:%M")
                with open(os.path.join(BASE_DIR, "last_scan.txt"), "w") as f:
                    f.write(ts)

                friendly = (f"Done — found {total} bid(s) in your area."
                            if total else
                            "Scan complete — no open bids posted here right now. Try a wider radius.")
                self._log_scan(friendly)
                self._scan_status.config(text=friendly, fg=GREEN if total else TEXT3)
                # Update the feed on the main thread so it reliably redraws
                def show_results():
                    self._refresh_feed_cities()
                    if hasattr(self, "_kw_var"):
                        self._kw_var.set("")
                    if hasattr(self, "_city_var"):
                        self._city_var.set("All")
                    if hasattr(self, "_trade_var"):
                        self._trade_var.set("All trades")
                    self._render_feed()
                self.root.after(0, show_results)
            except Exception:
                import traceback
                done.set()
                debug("Scan error:\n" + traceback.format_exc())
                self._log_scan("Something went wrong. Please try again.")
                self._scan_status.config(text="Scan failed — try again", fg=RED)
                self._set_prog(0)
            finally:
                self.scan_running = False
                self._scan_btn.config(state="normal", text="  🔍  Scan for Bids  ")

        threading.Thread(target=process, daemon=True).start()

    def _set_prog(self, pct):
        self._prog_bar.config(width=int(480 * pct / 100))

    # ═══════════ SAVED SEARCHES (auto-alerts) ═══════════
    def _save_current_search(self):
        location = self._loc_entry.get().strip()
        if not location:
            messagebox.showwarning("Save Search", "Enter a ZIP or city first.")
            return
        radius = int(self._radius_var.get().split()[0])
        for s in self.saved_searches:
            if s["location"].strip().lower() == location.lower() and s["radius"] == radius:
                messagebox.showinfo("Save Search", "Already saved.")
                return
        self.saved_searches.append({"location": location, "radius": radius})
        write_saved_searches(self.saved_searches)
        self._render_saved_searches()
        token = auth_client.current_access_token()
        if token:
            threading.Thread(target=lambda: data_sync.push_saved_search(token, location, radius),
                             daemon=True).start()

    def _remove_saved_search(self, idx):
        if 0 <= idx < len(self.saved_searches):
            removed = self.saved_searches[idx]
            del self.saved_searches[idx]
            write_saved_searches(self.saved_searches)
            self._render_saved_searches()
            token = auth_client.current_access_token()
            if token:
                threading.Thread(
                    target=lambda: data_sync.delete_saved_search(
                        token, removed["location"], removed["radius"]),
                    daemon=True).start()

    def _scan_saved_search(self, location, radius):
        self._loc_entry.delete(0, tk.END)
        self._loc_entry.insert(0, location)
        self._radius_var.set(f"{radius} miles")
        self._run_scan()

    def _render_saved_searches(self):
        if not hasattr(self, "_saved_search_frame"):
            return
        for w in self._saved_search_frame.winfo_children():
            w.destroy()
        if not self.saved_searches:
            return
        tk.Label(self._saved_search_frame, text="AUTO-ALERT SEARCHES", font=(UI, fs(9), "bold"),
                 bg=BG, fg=ACCENT).pack(anchor="w", pady=(12, 6))
        for i, s in enumerate(self.saved_searches):
            row = tk.Frame(self._saved_search_frame, bg=SURFACE, padx=14, pady=8)
            row.pack(fill="x", pady=(0, 6))
            tk.Label(row, text=f"{s['location']}  •  {s['radius']} mi radius",
                     font=F_BODY, bg=SURFACE, fg=TEXT).pack(side="left")
            ghost_btn(row, "✕", lambda i=i: self._remove_saved_search(i),
                      font=F_SMALL).pack(side="right")
            ghost_btn(row, "Scan", lambda s=s: self._scan_saved_search(s["location"], s["radius"]),
                      font=F_SMALL).pack(side="right", padx=(0, 6))

    def _merge_bids(self, new_bids):
        """Adds only new bids into self.bid_data (keyed by bid_id), keeping
        whatever's already there — used by the saved-search auto-check so a
        silent background refresh doesn't wipe the current feed."""
        added = 0
        for city, bids in new_bids.items():
            existing = self.bid_data.setdefault(city, [])
            existing_ids = {bid_id(city, b) for b in existing}
            for b in bids:
                bid_key = bid_id(city, b)
                if bid_key not in existing_ids:
                    existing.append(b)
                    existing_ids.add(bid_key)
                    added += 1
        return added

    def _check_saved_searches_on_load(self):
        """Silently re-scans every saved search once on app open — the
        closest thing to a real alert without a paid always-on backend."""
        if not self.saved_searches:
            return

        def work():
            results = []
            for s in self.saved_searches:
                resp = subscription.scan(s["location"], s["radius"])
                if resp.get("ok") and resp.get("bids"):
                    results.append(resp["bids"])
            if not results:
                return

            def apply():
                total_added = sum(self._merge_bids(r) for r in results)
                if total_added > 0:
                    write_feed(self.bid_data)
                    self._show_feed_badge(True)
                    self._refresh_feed_cities()
                    if self._current == "feed":
                        self._render_feed()
                    _windows_toast("Bid Caller Pro",
                                   f"{total_added} new bid(s) from your saved searches!")
            self.root.after(0, apply)

        threading.Thread(target=work, daemon=True).start()

    def _open_scan_map(self):
        summary = getattr(self, "_last_scan_summary", None)
        if not summary or not summary.get("town_coords"):
            messagebox.showinfo("No Scan Yet", "Run a scan first, then view it on the map.")
            return
        center = summary.get("center", {})
        bid_counts = {city: len(bids) for city, bids in self.bid_data.items()}
        path = map_view.build_map_html(
            center.get("lat"), center.get("lon"),
            getattr(self, "_last_scan_label", "Search center"),
            summary["town_coords"], bid_counts=bid_counts, out_dir=BASE_DIR)
        webbrowser.open("file://" + os.path.abspath(path))

    def _refresh_feed_cities(self):
        if hasattr(self, "_city_filter_box"):
            cities = ["All"] + sorted(self.bid_data.keys())
            self._city_filter_box["values"] = cities
            if self._city_var.get() not in cities:
                self._city_var.set("All")

    def _clear_feed(self):
        if not self.bid_data:
            return
        if messagebox.askyesno("Clear Bids", "Clear all bids from the live feed? "
                               "(Saved bids are not affected.)"):
            self.bid_data = {}
            write_feed(self.bid_data)
            self._refresh_feed_cities()
            self._render_feed()

    # ═══════════ PAGE: UPCOMING (planned, pre-bid work) ═══════════
    def _page_upcoming(self):
        pg = tk.Frame(self._container, bg=BG)
        self._pages["upcoming"] = pg

        hdr = tk.Frame(pg, bg=BG, pady=18)
        hdr.pack(fill="x", padx=24)
        tk.Label(hdr, text="Upcoming Work", font=F_HEAD, bg=BG, fg=TEXT).pack(side="left")
        divider(pg, BORDER)

        body = tk.Frame(pg, bg=BG)
        body.pack(fill="both", expand=True, padx=24, pady=18)

        tk.Label(body, text="Concrete projects spotted in council agendas, budgets & "
                 "plans — before they hit the bid boards.", font=F_SMALL,
                 bg=BG, fg=TEXT3, wraplength=820, justify="left").pack(anchor="w", pady=(0, 12))

        form = tk.Frame(body, bg=SURFACE, padx=24, pady=18)
        self._up_loc_entry = tk.Entry(form, font=F_BODY, bg=CARD, fg=TEXT,
                                      insertbackground=TEXT, relief="flat", bd=0,
                                      highlightthickness=1, highlightbackground=BORDER,
                                      highlightcolor=ACCENT)
        self._up_loc_entry.bind("<Return>", lambda e: self._run_upcoming())

        # Map goes above the form card (same ordering as every other search
        # tab) -- built once the entry it fills exists, packed before the
        # form itself so it renders on top.
        self._build_simple_map(body, self._up_loc_entry,
                               status_setter=lambda t, c: self._upcoming_status.config(text=t, fg=c))

        form.pack(fill="x", pady=(0, 12))
        tk.Label(form, text="Your Location", font=(UI, fs(11), "bold"),
                 bg=SURFACE, fg=ACCENT).pack(anchor="w", pady=(0, 4))
        self._up_loc_entry.pack(fill="x", ipady=8, pady=(0, 10))

        tk.Label(form, text="Search Radius", font=(UI, fs(11), "bold"),
                 bg=SURFACE, fg=ACCENT).pack(anchor="w", pady=(4, 4))
        self._up_radius_var = tk.StringVar(value="25 miles")
        ttk.Combobox(form, textvariable=self._up_radius_var, state="readonly",
                     values=["10 miles", "25 miles", "50 miles", "75 miles", "125 miles"],
                     font=F_BODY, width=16).pack(anchor="w")

        divider(form, BORDER, pad=0)

        self._upcoming_btn = pill_btn(form, "  📡  Find Upcoming Projects  ",
                                      self._run_upcoming, px=26, py=12,
                                      font=(UI, fs(12), "bold"))
        self._upcoming_btn.pack(anchor="w", pady=(16, 0))

        self._upcoming_status = tk.Label(body, text="", font=F_BODY, bg=BG, fg=TEXT3)
        self._upcoming_status.pack(pady=(20, 6))
        self._up_prog_frame = tk.Frame(body, bg=BORDER, height=4, width=480)
        self._up_prog_frame.pack(pady=(0, 6))
        self._up_prog_bar = tk.Frame(self._up_prog_frame, bg=ACCENT, height=4, width=0)
        self._up_prog_bar.place(x=0, y=0)

        up_filter = tk.Frame(body, bg=SURFACE, pady=6)
        up_filter.pack(fill="x", pady=(12, 0))
        tk.Label(up_filter, text="🔎", font=F_BODY, bg=SURFACE, fg=TEXT3).pack(side="left", padx=(12, 6))
        self._up_kw_var = tk.StringVar()
        up_kw = tk.Entry(up_filter, textvariable=self._up_kw_var, font=F_BODY, bg=CARD, fg=TEXT,
                         relief="flat", bd=0, insertbackground=TEXT,
                         highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT)
        up_kw.pack(side="left", fill="x", expand=True, ipady=6, padx=(0, 12))
        up_kw.bind("<KeyRelease>", lambda e: self._render_upcoming())

        self._upcoming_list_frame = scrollable(body, bg=BG)
        self._render_upcoming()

    def _set_up_prog(self, pct):
        self._up_prog_bar.config(width=int(480 * pct / 100))

    def _run_upcoming(self):
        if not self._scan_guard():
            return
        location = self._up_loc_entry.get().strip()
        if not location:
            messagebox.showwarning("Location needed", "Enter a ZIP code or city.")
            return
        radius = int(self._up_radius_var.get().split()[0])

        self.scan_running = True
        self._upcoming_btn.config(state="disabled", text="  Searching...  ")
        self._set_up_prog(15)

        REASONS = {
            "unreachable": "Couldn't reach the bid service. Check your internet.",
            "not_licensed": "Your trial or subscription isn't active. Check the Subscription tab.",
            "location_not_found": "Couldn't find that location. Try a ZIP code.",
            "no_location": "Enter a ZIP code or city.",
            "ai_unavailable": "Upcoming search isn't configured yet.",
        }

        def process():
            done = threading.Event()

            def tick():
                pct = 15
                while not done.wait(1.5):
                    pct = min(90, pct + 3)
                    self.root.after(0, lambda p=pct: self._set_up_prog(p))

            try:
                self._upcoming_status.config(text="Scanning agendas, budgets & plans...", fg=ACCENT)
                threading.Thread(target=tick, daemon=True).start()

                resp = subscription.upcoming(location, radius)
                done.set()
                self._set_up_prog(100)

                if not resp.get("ok"):
                    msg = REASONS.get(resp.get("reason"), "Search hit a snag. Try again.")
                    self._upcoming_status.config(text=msg, fg=RED)
                    self._set_up_prog(0)
                    return

                added = 0
                for city, items in (resp.get("items") or {}).items():
                    have = self.upcoming_data.setdefault(city, [])
                    ids = {upcoming_id(city, b) for b in have}
                    for item in items:
                        iid = upcoming_id(city, item)
                        if iid not in ids:
                            have.append(item)
                            ids.add(iid)
                            added += 1
                write_upcoming(self.upcoming_data)

                total = resp.get("total", 0)
                friendly = (f"Done — found {total} planned project(s) near "
                            f"{resp.get('location') or location}."
                            if total else
                            "Nothing planned turned up yet. Try a wider radius.")
                self._upcoming_status.config(text=friendly, fg=GREEN if total else TEXT3)
                self.root.after(0, self._render_upcoming)
            except Exception:
                import traceback
                done.set()
                debug("Upcoming search error:\n" + traceback.format_exc())
                self._upcoming_status.config(text="Search failed — try again", fg=RED)
                self._set_up_prog(0)
            finally:
                self.scan_running = False
                self._upcoming_btn.config(state="normal", text="  📡  Find Upcoming Projects  ")

        threading.Thread(target=process, daemon=True).start()

    def _upcoming_card(self, parent, item, city=""):
        outer = tk.Frame(parent, bg=CARD)
        outer.pack(fill="x", padx=20, pady=5)
        tk.Frame(outer, bg=BLUE, width=4).pack(side="left", fill="y")
        body = tk.Frame(outer, bg=CARD, padx=18, pady=14)
        body.pack(side="left", fill="both", expand=True)

        r1 = tk.Frame(body, bg=CARD)
        r1.pack(fill="x")
        tk.Label(r1, text=item.get("title", "Untitled Project"), font=F_SUB,
                 bg=CARD, fg=TEXT, anchor="w").pack(side="left")
        tk.Label(r1, text="  PLANNED  ", font=F_SMALL, bg="#0c2a4a",
                 fg=BLUE).pack(side="right")
        if city:
            tk.Label(r1, text=f"  {city}  ", font=F_SMALL, bg=BORDER,
                     fg=TEXT2).pack(side="right", padx=(4, 0))

        scope = (item.get("scope") or "").strip()
        if scope:
            tk.Label(body, text=scope, font=F_BODY, bg=CARD, fg=TEXT2,
                     anchor="w", wraplength=820, justify="left").pack(fill="x", pady=(5, 8))

        meta = tk.Frame(body, bg=CARD)
        meta.pack(fill="x")

        def chip(text, color=TEXT3, cmd=None):
            lbl = tk.Label(meta, text=text, font=F_SMALL, bg=SURFACE, fg=color,
                           padx=10, pady=4, cursor="hand2" if cmd else "arrow")
            lbl.pack(side="left", padx=(0, 6))
            if cmd:
                lbl.bind("<Button-1>", lambda e: cmd())

        if item.get("timeframe"):
            chip(f"🗓  {item['timeframe']}", ACCENT)
        if item.get("contact"):
            chip(f"👤  {item['contact']}", TEXT2)
        if item.get("email"):
            chip("✉  Email", GREEN, lambda: webbrowser.open(f"mailto:{item['email']}"))
        if item.get("phone"):
            chip(f"☎  {item['phone']}", TEXT2)
        if item.get("url"):
            chip("🔗  Source", ACCENT, lambda: webbrowser.open(item["url"]))

    def _render_upcoming(self):
        if not hasattr(self, "_upcoming_list_frame"):
            return
        for w in self._upcoming_list_frame.winfo_children():
            w.destroy()
        cities = sorted(c for c, items in self.upcoming_data.items() if items)
        if not cities:
            tk.Label(self._upcoming_list_frame, text="📡", font=(UI, fs(30)),
                     bg=BG, fg=TEXT3).pack(pady=(40, 6))
            tk.Label(self._upcoming_list_frame, text="Nothing yet", font=F_SUB,
                     bg=BG, fg=TEXT).pack()
            tk.Label(self._upcoming_list_frame,
                     text="Search your area to spot planned work before it's a bid.",
                     font=F_SMALL, bg=BG, fg=TEXT3).pack(pady=(4, 40))
            return
        kw = self._up_kw_var.get().strip().lower() if hasattr(self, "_up_kw_var") else ""
        rows = []
        for city in cities:
            for item in self.upcoming_data[city]:
                if kw:
                    blob = (item.get("title", "") + " " + item.get("scope", "") + " " + city).lower()
                    if kw not in blob:
                        continue
                rows.append((city, item))
        if not rows:
            tk.Label(self._upcoming_list_frame, text="🔍", font=(UI, fs(30)),
                     bg=BG, fg=TEXT3).pack(pady=(40, 6))
            tk.Label(self._upcoming_list_frame, text="No matches", font=F_SUB,
                     bg=BG, fg=TEXT).pack()
            tk.Label(self._upcoming_list_frame, text="Try a different search term.",
                     font=F_SMALL, bg=BG, fg=TEXT3).pack(pady=(4, 40))
            return
        tk.Label(self._upcoming_list_frame, text="PLANNED PROJECTS", font=(UI, fs(9), "bold"),
                 bg=BG, fg=ACCENT).pack(anchor="w", padx=20, pady=(4, 6))
        for city, item in rows:
            self._upcoming_card(self._upcoming_list_frame, item, city)
        tk.Frame(self._upcoming_list_frame, bg=BG, height=30).pack()

    # ═══════════ PAGE: RESIDENTIAL LEADS (driveway/sidewalk permits) ═══════════
    def _page_leads(self):
        pg = tk.Frame(self._container, bg=BG)
        self._pages["leads"] = pg

        hdr = tk.Frame(pg, bg=BG, pady=18)
        hdr.pack(fill="x", padx=24)
        tk.Label(hdr, text="Residential Leads", font=F_HEAD, bg=BG, fg=TEXT).pack(side="left")
        self._leads_export_btn = ghost_btn(hdr, "  ⬇  Export  ", self._export_leads_csv, font=F_SMALL)
        divider(pg, BORDER)

        body = tk.Frame(pg, bg=BG)
        body.pack(fill="both", expand=True, padx=24, pady=18)

        tk.Label(body, text="Fresh driveway & sidewalk permits pulled straight from city "
                 "permit records — homeowners and builders who just started this exact "
                 "work. Not a bid: no deadline, no RFP — the move is to call before a "
                 "competitor does.", font=F_SMALL,
                 bg=BG, fg=TEXT3, wraplength=820, justify="left").pack(anchor="w", pady=(0, 12))

        form = tk.Frame(body, bg=SURFACE, padx=24, pady=18)
        self._leads_loc_entry = tk.Entry(form, font=F_BODY, bg=CARD, fg=TEXT,
                                         insertbackground=TEXT, relief="flat", bd=0,
                                         highlightthickness=1, highlightbackground=BORDER,
                                         highlightcolor=ACCENT)
        self._leads_loc_entry.bind("<Return>", lambda e: self._run_leads())

        # Map goes above the form card (same ordering as every other search
        # tab) -- built once the entry it fills exists, packed before the
        # form itself so it renders on top.
        self._build_simple_map(body, self._leads_loc_entry,
                               status_setter=lambda t, c: self._leads_status.config(text=t, fg=c))

        form.pack(fill="x", pady=(0, 12))
        tk.Label(form, text="Your Location", font=(UI, fs(11), "bold"),
                 bg=SURFACE, fg=ACCENT).pack(anchor="w", pady=(0, 4))
        self._leads_loc_entry.pack(fill="x", ipady=8, pady=(0, 10))

        tk.Label(form, text="Search Radius", font=(UI, fs(11), "bold"),
                 bg=SURFACE, fg=ACCENT).pack(anchor="w", pady=(4, 4))
        self._leads_radius_var = tk.StringVar(value="25 miles")
        ttk.Combobox(form, textvariable=self._leads_radius_var, state="readonly",
                     values=["10 miles", "25 miles", "50 miles", "75 miles", "125 miles"],
                     font=F_BODY, width=16).pack(anchor="w")

        divider(form, BORDER, pad=0)

        self._leads_btn = pill_btn(form, "  🏠  Find Residential Leads  ",
                                   self._run_leads, px=26, py=12,
                                   font=(UI, fs(12), "bold"))
        self._leads_btn.pack(anchor="w", pady=(16, 0))

        self._leads_status = tk.Label(body, text="", font=F_BODY, bg=BG, fg=TEXT3)
        self._leads_status.pack(pady=(20, 6))
        self._leads_prog_frame = tk.Frame(body, bg=BORDER, height=4, width=480)
        self._leads_prog_frame.pack(pady=(0, 6))
        self._leads_prog_bar = tk.Frame(self._leads_prog_frame, bg=ACCENT, height=4, width=0)
        self._leads_prog_bar.place(x=0, y=0)

        self._leads_tiles_frame = tk.Frame(body, bg=BG)
        self._leads_tiles_frame.pack(fill="x", pady=(12, 0))

        leads_filter = tk.Frame(body, bg=SURFACE, pady=6)
        leads_filter.pack(fill="x", pady=(8, 0))
        tk.Label(leads_filter, text="🔎", font=F_BODY, bg=SURFACE, fg=TEXT3).pack(side="left", padx=(12, 6))
        self._leads_kw_var = tk.StringVar()
        leads_kw = tk.Entry(leads_filter, textvariable=self._leads_kw_var, font=F_BODY, bg=CARD, fg=TEXT,
                            relief="flat", bd=0, insertbackground=TEXT,
                            highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT)
        leads_kw.pack(side="left", fill="x", expand=True, ipady=6, padx=(0, 12))
        leads_kw.bind("<KeyRelease>", lambda e: self._render_leads())
        self._leads_sort_var = tk.StringVar(value="Best Match")
        leads_sort_cb = ttk.Combobox(leads_filter, textvariable=self._leads_sort_var, state="readonly",
                                     values=["Best Match", "Newest"], font=F_BODY, width=12)
        leads_sort_cb.pack(side="left", padx=(0, 12))
        leads_sort_cb.bind("<<ComboboxSelected>>", lambda e: self._render_leads())

        self._leads_list_frame = scrollable(body, bg=BG)
        self._render_leads()

    def _set_leads_prog(self, pct):
        self._leads_prog_bar.config(width=int(480 * pct / 100))

    def _run_leads(self):
        if not self._scan_guard():
            return
        location = self._leads_loc_entry.get().strip()
        if not location:
            messagebox.showwarning("Location needed", "Enter a ZIP code or city.")
            return
        radius = int(self._leads_radius_var.get().split()[0])

        self.scan_running = True
        self._leads_btn.config(state="disabled", text="  Searching...  ")
        self._set_leads_prog(15)

        REASONS = {
            "unreachable": "Couldn't reach the bid service. Check your internet.",
            "not_licensed": "Your trial or subscription isn't active. Check the Subscription tab.",
            "location_not_found": "Couldn't find that location. Try a ZIP code.",
            "no_location": "Enter a ZIP code or city.",
        }

        def process():
            done = threading.Event()

            def tick():
                pct = 15
                while not done.wait(1.0):
                    pct = min(90, pct + 8)
                    self.root.after(0, lambda p=pct: self._set_leads_prog(p))

            try:
                self._leads_status.config(text="Checking city permit records...", fg=ACCENT)
                threading.Thread(target=tick, daemon=True).start()

                resp = subscription.residential_leads(location, radius)
                done.set()
                self._set_leads_prog(100)

                if not resp.get("ok"):
                    msg = REASONS.get(resp.get("reason"), "Search hit a snag. Try again.")
                    self._leads_status.config(text=msg, fg=RED)
                    self._set_leads_prog(0)
                    return

                if not resp.get("covered"):
                    self._leads_status.config(
                        text=f"Residential leads aren't set up for "
                             f"{resp.get('location') or location} yet — coverage is "
                             f"growing city by city. (Currently: Austin, TX & Cambridge, MA.)",
                        fg=TEXT3)
                    self._set_leads_prog(0)
                    return

                city = resp.get("location") or location
                have = self.leads_data.setdefault(city, [])
                ids = {l.get("permit_id") for l in have}
                added = 0
                for lead in (resp.get("leads") or []):
                    if lead.get("permit_id") not in ids:
                        have.append(lead)
                        ids.add(lead.get("permit_id"))
                        added += 1
                write_leads(self.leads_data)

                total = resp.get("total", 0)
                friendly = (f"Done — found {total} residential lead(s) near {city}."
                            if total else
                            "No recent driveway/sidewalk permits in this area. Try a wider radius.")
                self._leads_status.config(text=friendly, fg=GREEN if total else TEXT3)
                self.root.after(0, self._render_leads)
            except Exception:
                import traceback
                done.set()
                debug("Residential leads search error:\n" + traceback.format_exc())
                self._leads_status.config(text="Search failed — try again", fg=RED)
                self._set_leads_prog(0)
            finally:
                self.scan_running = False
                self._leads_btn.config(state="normal", text="  🏠  Find Residential Leads  ")

        threading.Thread(target=process, daemon=True).start()

    _LEAD_TYPE_STYLE = {
        "open": ("#0f3d24", GREEN, "\U0001F513  Open Lead"),
        "builder": ("#0c2a4a", BLUE, "\U0001F3D7  Builder Lead"),
        "taken": ("#2a2a2a", TEXT3, "✓  Sub Already Listed"),
        "unknown": ("#2a2a2a", TEXT2, "Contact Listed"),
    }

    def _lead_card(self, parent, lead, city=""):
        outer = tk.Frame(parent, bg=CARD)
        outer.pack(fill="x", padx=20, pady=5)
        tk.Frame(outer, bg=ACCENT, width=4).pack(side="left", fill="y")
        body = tk.Frame(outer, bg=CARD, padx=18, pady=14)
        body.pack(side="left", fill="both", expand=True)

        r1 = tk.Frame(body, bg=CARD)
        r1.pack(fill="x")
        tk.Label(r1, text=lead.get("address") or "Address unknown", font=F_SUB,
                 bg=CARD, fg=TEXT, anchor="w").pack(side="left")
        tk.Label(r1, text=f"  {lead.get('permit_type') or 'Residential Permit'}  ",
                 font=F_SMALL, bg="#0c2a4a", fg=BLUE).pack(side="right")
        if city:
            tk.Label(r1, text=f"  {city}  ", font=F_SMALL, bg=BORDER,
                     fg=TEXT2).pack(side="right", padx=(4, 0))

        desc = (lead.get("description") or "").strip()
        if desc:
            tk.Label(body, text=desc, font=F_BODY, bg=CARD, fg=TEXT2,
                     anchor="w", wraplength=820, justify="left").pack(fill="x", pady=(5, 8))

        meta = tk.Frame(body, bg=CARD)
        meta.pack(fill="x")

        def chip(text, color=TEXT3, cmd=None, bg=SURFACE):
            lbl = tk.Label(meta, text=text, font=F_SMALL, bg=bg, fg=color,
                           padx=10, pady=4, cursor="hand2" if cmd else "arrow")
            lbl.pack(side="left", padx=(0, 6))
            if cmd:
                lbl.bind("<Button-1>", lambda e: cmd())

        bg, fg, label = self._LEAD_TYPE_STYLE.get(lead.get("lead_type"), self._LEAD_TYPE_STYLE["unknown"])
        chip(label, fg, bg=bg)

        if lead.get("issued_date"):
            chip(f"🗓  Permitted {lead['issued_date']}", ACCENT)
        if lead.get("contractor_name"):
            trade = f" ({lead['contractor_trade']})" if lead.get("contractor_trade") else ""
            chip(f"👤  {lead['contractor_name']}{trade}", TEXT2)
        if lead.get("contractor_phone"):
            chip(f"☎  {lead['contractor_phone']}", TEXT2)
        if lead.get("url"):
            chip("🔗  Permit Record", ACCENT, lambda: webbrowser.open(lead["url"]))

        permit_id = lead.get("permit_id")
        if permit_id:
            cur = self.lead_status.get(permit_id, "")
            prow = tk.Frame(body, bg=CARD)
            prow.pack(fill="x", pady=(8, 0))
            for skey, slabel in LEAD_STATUSES:
                active = (cur == skey)
                bg_c = {"won": GREEN, "lost": RED}.get(skey, ACCENT) if active else SURFACE
                fg_c = "#000" if active else TEXT2
                pill_btn(prow, slabel,
                         lambda pid=permit_id, s=skey, c=cur: self._set_lead_status(pid, "" if c == s else s),
                         bg=bg_c, fg=fg_c, px=12, py=6, font=F_SMALL).pack(side="left", padx=(0, 6))

    def _set_lead_status(self, permit_id, status):
        if status:
            self.lead_status[permit_id] = status
        else:
            self.lead_status.pop(permit_id, None)
        write_lead_status(self.lead_status)
        self._render_leads()

    def _export_leads_csv(self):
        """Exports the currently filtered/sorted residential leads."""
        rows = getattr(self, "_last_leads_rows", None)
        if not rows:
            messagebox.showinfo("Nothing to Export", "No leads match this filter.")
            return
        import csv
        from tkinter import filedialog
        default = f"bid_caller_leads_{datetime.datetime.now().strftime('%Y%m%d')}.csv"
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", initialfile=default,
            filetypes=[("CSV files", "*.csv")], title="Save leads as CSV")
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as f_out:
                w = csv.writer(f_out)
                w.writerow(["City", "Address", "Permit Type", "Description", "Issued Date",
                            "Lead Type", "Builder/Contact", "Trade", "Phone", "Permit Record", "Status"])
                for city, lead in rows:
                    w.writerow([city, lead.get("address", ""), lead.get("permit_type", ""),
                                lead.get("description", ""), lead.get("issued_date", ""),
                                lead.get("lead_type_label", lead.get("lead_type", "")),
                                lead.get("contractor_name", ""), lead.get("contractor_trade", ""),
                                lead.get("contractor_phone", ""), lead.get("url", ""),
                                self.lead_status.get(lead.get("permit_id"), "")])
            messagebox.showinfo("Exported", f"Saved your leads to:\n{path}")
        except Exception as e:
            debug(f"Leads CSV export failed: {e}")
            messagebox.showerror("Export Failed", "Couldn't save the file. Try a different location.")

    def _render_leads(self):
        if not hasattr(self, "_leads_list_frame"):
            return
        for w in self._leads_list_frame.winfo_children():
            w.destroy()
        for w in self._leads_tiles_frame.winfo_children():
            w.destroy()
        cities = sorted(c for c, items in self.leads_data.items() if items)
        if not cities:
            self._leads_export_btn.pack_forget()
            tk.Label(self._leads_list_frame, text="🏠", font=(UI, fs(30)),
                     bg=BG, fg=TEXT3).pack(pady=(40, 6))
            tk.Label(self._leads_list_frame, text="Nothing yet", font=F_SUB,
                     bg=BG, fg=TEXT).pack()
            tk.Label(self._leads_list_frame,
                     text="Search your area for fresh driveway & sidewalk permits.\n"
                          "Coverage is growing city by city — currently: Austin, TX & Cambridge, MA.",
                     font=F_SMALL, bg=BG, fg=TEXT3, justify="center").pack(pady=(4, 40))
            return
        self._leads_export_btn.pack(side="right")

        all_rows = [(c, lead) for c in cities for lead in self.leads_data[c]]

        # ── Status filter tiles (All/Not Contacted/Called/Quoted/Won/Lost) --
        # same pattern as Active Bids' pipeline tiles ──
        counts = {"All": len(all_rows), "none": 0, "called": 0, "quoted": 0, "won": 0, "lost": 0}
        for _, lead in all_rows:
            s = self.lead_status.get(lead.get("permit_id"), "")
            counts[s if s else "none"] = counts.get(s if s else "none", 0) + 1
        tile_defs = [("All", "All"), ("none", "Not Contacted")] + LEAD_STATUSES
        for key, label in tile_defs:
            if key != "All" and not counts.get(key):
                continue
            active = (self._leads_status_filter == key)
            tile = tk.Frame(self._leads_tiles_frame, bg=CARD if active else SURFACE,
                            highlightbackground=ACCENT if active else BORDER,
                            highlightthickness=1, padx=12, pady=8, cursor="hand2")
            tile.pack(side="left", padx=(0, 8))
            n_lbl = tk.Label(tile, text=str(counts.get(key, 0)), font=(UI, fs(13), "bold"),
                             bg=tile["bg"], fg=TEXT)
            n_lbl.pack()
            l_lbl = tk.Label(tile, text=label, font=F_SMALL, bg=tile["bg"], fg=TEXT2)
            l_lbl.pack()
            def pick(k=key):
                self._leads_status_filter = k
                self._render_leads()
            for w in (tile, n_lbl, l_lbl):
                w.bind("<Button-1>", lambda e, k=key: pick(k))

        f = self._leads_status_filter
        if f == "All":
            rows = list(all_rows)
        elif f == "none":
            rows = [(c, l) for c, l in all_rows if not self.lead_status.get(l.get("permit_id"))]
        else:
            rows = [(c, l) for c, l in all_rows if self.lead_status.get(l.get("permit_id")) == f]

        kw = self._leads_kw_var.get().strip().lower() if hasattr(self, "_leads_kw_var") else ""
        if kw:
            rows = [(c, l) for c, l in rows if kw in " ".join(
                [l.get("address", ""), l.get("description", ""), l.get("contractor_name", ""), c]).lower()]
        sort_mode = self._leads_sort_var.get() if hasattr(self, "_leads_sort_var") else "Best Match"
        if sort_mode == "Newest":
            rows.sort(key=lambda cl: cl[1].get("issued_date") or "", reverse=True)
        # "Best Match" (default): leave rows in the order /residential-leads
        # returned them -- already sorted open-lead-first, freshest within type.
        self._last_leads_rows = rows
        if not rows:
            tk.Label(self._leads_list_frame, text="🔍", font=(UI, fs(30)),
                     bg=BG, fg=TEXT3).pack(pady=(40, 6))
            tk.Label(self._leads_list_frame, text="No matches", font=F_SUB,
                     bg=BG, fg=TEXT).pack()
            tk.Label(self._leads_list_frame, text="Try a different search term.",
                     font=F_SMALL, bg=BG, fg=TEXT3).pack(pady=(4, 40))
            return
        tk.Label(self._leads_list_frame, text="RESIDENTIAL LEADS", font=(UI, fs(9), "bold"),
                 bg=BG, fg=ACCENT).pack(anchor="w", padx=20, pady=(4, 6))
        for city, lead in rows:
            self._lead_card(self._leads_list_frame, lead, city)
        tk.Frame(self._leads_list_frame, bg=BG, height=30).pack()

    def _export_csv(self):
        """Save all current bids to a CSV file the contractor can open in Excel."""
        if not self.bid_data:
            messagebox.showinfo("Nothing to Export", "Run a scan first to find some bids.")
            return
        import csv
        from tkinter import filedialog
        default = f"bid_caller_leads_{datetime.datetime.now().strftime('%Y%m%d')}.csv"
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", initialfile=default,
            filetypes=[("CSV files", "*.csv")], title="Save bids as CSV")
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["City", "Title", "Status", "Scope", "Est. Value",
                            "Deadline", "Contact", "Email", "Phone", "Link"])
                for city, bids in self.bid_data.items():
                    for b in bids:
                        w.writerow([city, b.get("title", ""), b.get("status", ""),
                                    b.get("scope", ""), b.get("value", ""),
                                    b.get("deadline", ""), b.get("contact", ""),
                                    b.get("email", ""), b.get("phone", ""), b.get("url", "")])
            messagebox.showinfo("Exported", f"Saved your bids to:\n{path}")
        except Exception as e:
            debug(f"CSV export failed: {e}")
            messagebox.showerror("Export Failed", "Couldn't save the file. Try a different location.")

    # ═══════════ PAGE: SEARCH CITY ═══════════
    def _page_search(self):
        pg = tk.Frame(self._container, bg=BG)
        self._pages["search"] = pg
        hdr = tk.Frame(pg, bg=BG, pady=18)
        hdr.pack(fill="x", padx=24)
        tk.Label(hdr, text="Search Any Town Hall", font=F_HEAD, bg=BG, fg=TEXT).pack(side="left")
        divider(pg, BORDER)

        form = tk.Frame(pg, bg=SURFACE, padx=30, pady=22)
        form.pack(fill="x", padx=24, pady=18)

        def field(label, default=""):
            tk.Label(form, text=label, font=(UI, fs(10), "bold"), bg=SURFACE,
                     fg=TEXT2, anchor="w").pack(fill="x", pady=(0, 4))
            e = tk.Entry(form, font=F_BODY, bg=CARD, fg=TEXT, relief="flat", bd=0,
                         insertbackground=TEXT, highlightthickness=1,
                         highlightbackground=BORDER, highlightcolor=ACCENT)
            e.pack(fill="x", ipady=9, pady=(0, 14))
            if default:
                e.insert(0, default)
            return e

        self._s_city = field("City or Town Name")
        self._s_url = field("Bids Page URL  (e.g. https://cityname.gov/bids.aspx)", "https://")
        tk.Label(form, text="💡  Tip: search  '[city name] bids aspx'  in Google to find the URL",
                 font=F_SMALL, bg=SURFACE, fg=TEXT3).pack(anchor="w", pady=(0, 14))
        self._s_status = tk.Label(form, text="", font=F_SMALL, bg=SURFACE, fg=TEXT3)
        self._s_status.pack(anchor="w", pady=(0, 6))
        self._s_btn = pill_btn(form, "  🔍  Search This City  ", self._run_search, px=26, py=10)
        self._s_btn.pack(anchor="w")

        divider(pg, BORDER, pad=24)
        self._search_frame = scrollable(pg)
        tk.Label(self._search_frame, text="Results will appear here after a search.",
                 font=F_BODY, bg=BG, fg=TEXT3).pack(pady=40)

    def _run_search(self):
        if not self._scan_guard():
            return
        city = self._s_city.get().strip()
        url = self._s_url.get().strip()
        if not city or url in ("", "https://", "http://"):
            messagebox.showwarning("Missing Info", "Please enter a city name and a valid URL.")
            return
        self._s_btn.config(state="disabled", text="  Searching...  ")
        self._s_status.config(text=f"Scanning {city}...", fg=ACCENT)
        for w in self._search_frame.winfo_children():
            w.destroy()
        tk.Label(self._search_frame, text=f"Scanning {city} — this can take 10–30 seconds...",
                 font=F_BODY, bg=BG, fg=TEXT3).pack(pady=40)

        def process():
            try:
                bids = regional_printer.search_custom_city(city, url)
                self.search_results = [(city, b) for b in bids]
                self.root.after(0, self._rerender_search)
                self._s_status.config(
                    text=f"Found {len(bids)} bid(s) in {city}" if bids else f"No bids found in {city}",
                    fg=GREEN if bids else TEXT3)
            except Exception:
                import traceback
                debug("Search error:\n" + traceback.format_exc())
                self._s_status.config(text="Search failed — check URL and try again", fg=RED)
                self.search_results = []
                self.root.after(0, self._rerender_search)
            finally:
                self._s_btn.config(state="normal", text="  🔍  Search This City  ")

        threading.Thread(target=process, daemon=True).start()

    def _rerender_search(self):
        for w in self._search_frame.winfo_children():
            w.destroy()
        if not self.search_results:
            tk.Label(self._search_frame, text="No bids to show. Try a different URL.",
                     font=F_BODY, bg=BG, fg=TEXT3).pack(pady=50)
            return
        city = self.search_results[0][0]
        tk.Label(self._search_frame, text=f"  📍  {city} — {len(self.search_results)} bid(s)",
                 font=(UI, fs(9), "bold"), bg=BG, fg=GREEN).pack(anchor="w", padx=20, pady=(14, 6))
        for c, b in self.search_results:
            self._bid_card(self._search_frame, b, c)
        tk.Frame(self._search_frame, bg=BG, height=30).pack()

    # ═══════════ PAGE: SUBSCRIPTION ═══════════
    def _page_subscribe(self):
        pg = tk.Frame(self._container, bg=BG)
        self._pages["subscribe"] = pg
        self._build_subscribe_body(pg)

    def _build_subscribe_body(self, pg):
        for w in pg.winfo_children():
            w.destroy()
        inner = tk.Frame(pg, bg=BG)
        inner.place(relx=0.5, rely=0.46, anchor="center")
        s = subscription.get_status()

        self._account_row(inner)

        if s.get("active") and s.get("trial"):
            countdown = (subscription.time_left_label(s.get("expires_at"))
                        or f"{s.get('days_left','?')} days left")
            tk.Label(inner, text="🎁", font=(UI, fs(40)), bg=BG, fg=ACCENT).pack()
            row = tk.Frame(inner, bg=BG)
            row.pack(pady=6)
            tk.Label(row, text="Free Trial — ", font=(UI, fs(22), "bold"),
                     bg=BG, fg=TEXT).pack(side="left")
            self._trial_countdown_lbl = tk.Label(row, text=countdown, font=(UI, fs(22), "bold"),
                                                  bg=BG, fg=ACCENT)
            self._trial_countdown_lbl.pack(side="left")
            tk.Label(inner, text="Subscribe any time to keep access after the trial ends.",
                     font=F_BODY, bg=BG, fg=TEXT2).pack(pady=(0, 22))
            self._plan_cards(inner)
        elif s.get("active"):
            tk.Label(inner, text="✅", font=(UI, fs(40)), bg=BG, fg=GREEN).pack()
            tk.Label(inner, text="You're all set", font=(UI, fs(22), "bold"),
                     bg=BG, fg=TEXT).pack(pady=6)
            tk.Label(inner, text=f"Plan: {s.get('plan','Pro')}   •   Renews: {s.get('renews','—')}",
                     font=F_BODY, bg=BG, fg=TEXT2).pack(pady=4)
            ghost_btn(inner, "Manage Billing on Stripe →",
                      lambda: webbrowser.open(subscription.STRIPE_PORTAL_URL), font=F_BODY).pack(pady=16)
            pill_btn(inner, "Cancel Subscription", self._do_cancel, bg=RED, fg=TEXT,
                     px=20, py=8, font=F_BODY).pack()
        else:
            tk.Label(inner, text="Start Finding Bids Today",
                     font=(UI, fs(22), "bold"), bg=BG, fg=TEXT).pack(pady=(0, 6))
            tk.Label(inner, text="Live bid cards  •  Save & track leads  •  Nationwide  •  Custom search",
                     font=F_BODY, bg=BG, fg=TEXT2).pack(pady=(0, 20))

            if not subscription.trial_used():
                tc = tk.Frame(inner, bg=CARD, padx=30, pady=20,
                              highlightbackground=ACCENT, highlightthickness=2)
                tc.pack(pady=(0, 18))
                tk.Label(tc, text="Try it free for 7 days", font=(UI, fs(15), "bold"),
                         bg=CARD, fg=ACCENT).pack()
                signed_in = bool(auth_client.current_email())
                sub_text = "No charge now. Full access. Cancel anytime." if signed_in \
                    else "Free account required — no charge now. Cancel anytime."
                tk.Label(tc, text=sub_text,
                         font=F_SMALL, bg=CARD, fg=TEXT2).pack(pady=(2, 12))
                btn_text = "  Start Free Trial  " if signed_in else "  Sign In to Start Trial  "
                pill_btn(tc, btn_text, self._do_trial, px=26, py=10,
                         font=(UI, fs(12), "bold")).pack()
                tk.Label(inner, text="— or subscribe now —", font=F_SMALL,
                         bg=BG, fg=TEXT3).pack(pady=10)

            self._plan_cards(inner)

            divider(inner, BORDER)
            tk.Label(inner, text="Already have a license key?", font=F_BODY,
                     bg=BG, fg=TEXT2).pack(pady=(16, 6))
            kr = tk.Frame(inner, bg=BG)
            kr.pack()
            self._key_entry = tk.Entry(kr, font=F_BODY, bg=CARD, fg=TEXT, insertbackground=TEXT,
                                       relief="flat", bd=0, width=30, highlightthickness=1,
                                       highlightbackground=BORDER, highlightcolor=ACCENT)
            self._key_entry.pack(side="left", ipady=8, padx=(0, 8))
            pill_btn(kr, "Activate", self._activate, px=18, py=8, font=F_BODY).pack(side="left")
            self._key_msg = tk.Label(inner, text="", font=F_SMALL, bg=BG, fg=TEXT3)
            self._key_msg.pack(pady=6)

    def _plan_cards(self, parent):
        plans = tk.Frame(parent, bg=BG)
        plans.pack()

        def card(name, price, period, highlight=False, key="monthly"):
            border = ACCENT if highlight else BORDER
            c = tk.Frame(plans, bg=CARD, padx=32, pady=24,
                         highlightbackground=border, highlightthickness=2)
            c.pack(side="left", padx=12)
            if highlight:
                tk.Label(c, text="BEST VALUE", font=(UI, fs(8), "bold"),
                         bg=ACCENT, fg="#000", padx=8, pady=2).pack()
            tk.Label(c, text=name, font=(UI, fs(14), "bold"), bg=CARD,
                     fg=ACCENT if highlight else TEXT).pack(pady=(8, 4))
            tk.Label(c, text=price, font=(UI, fs(30), "bold"), bg=CARD, fg=TEXT).pack()
            tk.Label(c, text=period, font=F_SMALL, bg=CARD, fg=TEXT3).pack(pady=(2, 14))
            pill_btn(c, "Subscribe", lambda k=key: self._checkout(k),
                     bg=ACCENT if highlight else CARD, fg="#000" if highlight else TEXT2,
                     px=20, py=8, font=F_BODY).pack()

        card("Monthly", "$19", "per month", key="monthly")
        card("Annual", "$149", "per year — save $79", highlight=True, key="annual")
        tk.Label(parent, text="Secure payment via Stripe  •  Cancel anytime",
                 font=F_SMALL, bg=BG, fg=TEXT3).pack(pady=14)

    def _do_trial(self):
        if not auth_client.current_email():
            messagebox.showinfo("Sign In Required",
                "Create a free account to start your trial — this keeps it tied "
                "to you instead of this install, so it can't restart itself.")
            self._show_signin()
            return
        ok, msg = subscription.start_trial()
        if ok:
            messagebox.showinfo("Trial Started", msg + "\n\nEnjoy full access!")
            self._refresh_sub_lbl()
            self._build_subscribe_body(self._pages["subscribe"])
            self._nav("scan")
        else:
            messagebox.showwarning("Trial", msg)

    def _checkout(self, plan):
        base = (subscription.STRIPE_MONTHLY_URL if plan == "monthly"
               else subscription.STRIPE_ANNUAL_URL)
        # client_reference_id ties the purchase to this device so the Stripe
        # webhook can record it — that's what lets auto-unlock find the key.
        url = f"{base}?client_reference_id={subscription._device_id()}"
        webbrowser.open(url)
        messagebox.showinfo("Checkout Opened",
            "Complete payment in your browser.\n\nWe'll unlock automatically when you "
            "come back — or enter your license key below if you'd rather.")

    def _try_auto_unlock(self):
        """Checks if this device has a key on file yet (Stripe webhook
        records it via client_reference_id right after checkout) — desktop
        equivalent of the web app's autoUnlock(), so paying doesn't require
        manually copy-pasting a license key."""
        if subscription._load_cache().get("key"):
            return

        def work():
            resp = subscription.my_key()
            if resp.get("ok") and resp.get("key"):
                def apply():
                    cache = subscription._load_cache()
                    cache.update({
                        "key": resp["key"], "active": True, "trial": False,
                        "plan": resp.get("plan", "Pro").capitalize(),
                        "renews": resp.get("expires", "—"),
                        "last_ok": datetime.datetime.now().isoformat(),
                    })
                    subscription._save_cache(cache)
                    self._refresh_sub_lbl()
                    if "subscribe" in self._pages:
                        self._build_subscribe_body(self._pages["subscribe"])
                    messagebox.showinfo("Unlocked",
                        "Thanks for subscribing! Your account is now active.")
                self.root.after(0, apply)
        threading.Thread(target=work, daemon=True).start()

    def _activate(self):
        ok, msg = subscription.activate_key(self._key_entry.get().strip())
        if ok:
            self._key_msg.config(text="✅ " + msg, fg=GREEN)
            self._refresh_sub_lbl()
            self._build_subscribe_body(self._pages["subscribe"])
            self._nav("scan")
        else:
            self._key_msg.config(text="❌ " + msg, fg=RED)

    def _do_cancel(self):
        if messagebox.askyesno("Cancel", "Remove your local license?"):
            subscription.cancel()
            self._refresh_sub_lbl()
            self._build_subscribe_body(self._pages["subscribe"])

    # ═══════════ ACCOUNT / SIGN IN / FORGOT PASSWORD ═══════════
    def _account_row(self, parent):
        row = tk.Frame(parent, bg=BG)
        row.pack(pady=(0, 14))
        email = auth_client.current_email()
        if email:
            tk.Label(row, text=f"👤  Signed in as {email}", font=F_SMALL,
                     bg=BG, fg=TEXT2).pack(side="left", padx=(0, 10))
            ghost_btn(row, "Sign Out", self._do_sign_out, font=F_SMALL).pack(side="left")
        else:
            tk.Label(row, text="👤  Not signed in", font=F_SMALL,
                     bg=BG, fg=TEXT3).pack(side="left", padx=(0, 10))
            ghost_btn(row, "Sign In / Create Account", self._show_signin,
                      font=F_SMALL).pack(side="left")

    def _do_sign_out(self):
        auth_client.sign_out()
        self._build_subscribe_body(self._pages["subscribe"])
        if hasattr(self, "_account_frame"):
            self._render_account()
        self._refresh_sub_lbl()

    def _show_signin(self):
        self._auth_mode = "signin"
        self._build_signin_body(self._pages["subscribe"])

    def _switch_auth_mode(self, mode):
        self._auth_mode = mode
        self._build_signin_body(self._pages["subscribe"])

    def _auth_card(self, pg):
        """Shared frame for the account screens; returns the card to fill in."""
        for w in pg.winfo_children():
            w.destroy()
        inner = tk.Frame(pg, bg=BG)
        inner.place(relx=0.5, rely=0.46, anchor="center")
        card = tk.Frame(inner, bg=CARD, padx=36, pady=28,
                        highlightbackground=BORDER, highlightthickness=1)
        card.pack()
        return inner, card

    def _labeled_entry(self, card, label, show=None, width=32):
        tk.Label(card, text=label, font=F_SMALL, bg=CARD, fg=TEXT2,
                 anchor="w").pack(fill="x")
        e = tk.Entry(card, font=F_BODY, bg=BG, fg=TEXT, insertbackground=TEXT,
                     relief="flat", bd=0, width=width, show=show,
                     highlightthickness=1, highlightbackground=BORDER,
                     highlightcolor=ACCENT)
        e.pack(ipady=8, pady=(2, 14))
        return e

    def _build_signin_body(self, pg):
        inner, card = self._auth_card(pg)
        is_signup = (self._auth_mode == "signup")

        tk.Label(card, text="Create Account" if is_signup else "Sign In",
                 font=(UI, fs(18), "bold"), bg=CARD, fg=TEXT).pack(pady=(0, 4))
        tk.Label(card, text=("Link your license to an account" if is_signup
                             else "Welcome back"),
                 font=F_SMALL, bg=CARD, fg=TEXT2).pack(pady=(0, 18))

        pill_btn(card, "  G  Continue with Google", self._do_google_signin,
                 bg=CARD, fg=TEXT, px=16, py=10, font=F_BODY).pack(fill="x", pady=(0, 4))
        divider(card, BORDER)
        tk.Frame(card, bg=CARD, height=10).pack()

        self._auth_email = self._labeled_entry(card, "Email")
        self._auth_password = self._labeled_entry(card, "Password", show="•")
        self._auth_password.bind("<Return>", lambda e: self._do_signup() if is_signup
                                  else self._do_signin())

        self._auth_msg = tk.Label(card, text="", font=F_SMALL, bg=CARD, fg=TEXT3,
                                  wraplength=280, justify="left")
        self._auth_msg.pack(pady=(0, 10))

        pill_btn(card, "Create Account" if is_signup else "Sign In",
                 self._do_signup if is_signup else self._do_signin,
                 py=10, font=(UI, fs(12), "bold")).pack()

        toggle_row = tk.Frame(card, bg=CARD)
        toggle_row.pack(pady=(14, 0))
        if is_signup:
            tk.Label(toggle_row, text="Already have an account?", font=F_SMALL,
                     bg=CARD, fg=TEXT3).pack(side="left", padx=(0, 6))
            ghost_btn(toggle_row, "Sign In", lambda: self._switch_auth_mode("signin"),
                      font=F_SMALL).pack(side="left")
        else:
            tk.Label(toggle_row, text="New here?", font=F_SMALL,
                     bg=CARD, fg=TEXT3).pack(side="left", padx=(0, 6))
            ghost_btn(toggle_row, "Create Account", lambda: self._switch_auth_mode("signup"),
                      font=F_SMALL).pack(side="left")

        if not is_signup:
            ghost_btn(card, "Forgot password?", self._show_forgot_email,
                      font=F_SMALL).pack(pady=(10, 0))
            ghost_btn(card, "✉  Send magic link instead", self._show_magic_link,
                      font=F_SMALL).pack(pady=(6, 0))

        ghost_btn(inner, "← Back", lambda: self._build_subscribe_body(self._pages["subscribe"]),
                  font=F_SMALL).pack(pady=(16, 0))

    def _show_magic_link(self):
        self._build_magiclink_body(self._pages["subscribe"])

    def _build_magiclink_body(self, pg):
        inner, card = self._auth_card(pg)
        tk.Label(card, text="Sign In with a Code", font=(UI, fs(18), "bold"),
                 bg=CARD, fg=TEXT).pack(pady=(0, 4))
        tk.Label(card, text="We'll email you a one-time code — no password needed.",
                 font=F_SMALL, bg=CARD, fg=TEXT2, wraplength=280, justify="left").pack(pady=(0, 18))

        self._ml_email = self._labeled_entry(card, "Email")
        self._ml_msg = tk.Label(card, text="", font=F_SMALL, bg=CARD, fg=TEXT3,
                                wraplength=280, justify="left")
        self._ml_msg.pack(pady=(0, 10))
        pill_btn(card, "Send Code", self._do_request_magic_link, py=10,
                 font=(UI, fs(12), "bold")).pack()

        ghost_btn(inner, "← Back to Sign In", lambda: self._switch_auth_mode("signin"),
                  font=F_SMALL).pack(pady=(16, 0))

    def _do_request_magic_link(self):
        email = self._ml_email.get().strip()
        self._ml_msg.config(text="Sending code...", fg=TEXT2)
        self.root.update_idletasks()
        ok, msg = auth_client.request_magic_link(email)
        self._ml_msg.config(text=("✅ " if ok else "❌ ") + msg, fg=GREEN if ok else RED)
        if ok:
            self._build_magiclink_code_body(self._pages["subscribe"], email)

    def _build_magiclink_code_body(self, pg, email):
        inner, card = self._auth_card(pg)
        tk.Label(card, text="Enter Your Code", font=(UI, fs(18), "bold"),
                 bg=CARD, fg=TEXT).pack(pady=(0, 4))
        tk.Label(card, text=f"Check {email} for a one-time code.",
                 font=F_SMALL, bg=CARD, fg=TEXT2, wraplength=280, justify="left").pack(pady=(0, 18))

        self._ml_code = self._labeled_entry(card, "Code")
        self._ml_code.bind("<Return>", lambda e: self._do_verify_magic_link(email))
        self._ml_code_msg = tk.Label(card, text="", font=F_SMALL, bg=CARD, fg=TEXT3,
                                     wraplength=280, justify="left")
        self._ml_code_msg.pack(pady=(0, 10))
        pill_btn(card, "Sign In", lambda: self._do_verify_magic_link(email), py=10,
                 font=(UI, fs(12), "bold")).pack()

        ghost_btn(inner, "← Back to Sign In", lambda: self._switch_auth_mode("signin"),
                  font=F_SMALL).pack(pady=(16, 0))

    def _sync_pull_after_signin(self):
        """After any successful sign-in, pull cloud saved bids + company
        profile and merge with whatever's local. For saved bids: cloud wins
        for bids already synced, anything saved locally while signed out
        gets kept and pushed up so it isn't lost. For the company profile
        (a single row): cloud wins if it has one, otherwise the local one
        (if any) gets pushed up to seed the cloud. Every step fails soft if
        the cloud can't be reached or the sync tables don't exist yet."""
        token = auth_client.current_access_token()
        if not token:
            return
        self._last_sync_at = datetime.datetime.now()
        if hasattr(self, "_account_frame") and self._current == "account":
            self._render_account()

        def work_bids():
            cloud = data_sync.pull_saved_bids(token)
            if cloud is None:
                return

            def apply():
                local_only = {k: v for k, v in self.saved.items() if k not in cloud}
                merged = dict(cloud)
                merged.update(local_only)
                self.saved = merged
                write_saved(self.saved)
                self._refresh_all()
                if local_only:
                    threading.Thread(
                        target=lambda: data_sync.push_all_saved_bids(token, local_only),
                        daemon=True).start()
            self.root.after(0, apply)
        threading.Thread(target=work_bids, daemon=True).start()

        def work_company():
            cloud = data_sync.pull_company_profile(token)
            if cloud is None:
                return

            def apply():
                if cloud.get("name"):
                    self.company = cloud
                    write_company_profile(self.company)
                    if hasattr(self, "_co_entries"):
                        for k, e in self._co_entries.items():
                            e.delete(0, tk.END)
                            e.insert(0, self.company.get(k, ""))
                elif self.company.get("name"):
                    threading.Thread(
                        target=lambda: data_sync.push_company_profile(token, self.company),
                        daemon=True).start()
            self.root.after(0, apply)
        threading.Thread(target=work_company, daemon=True).start()

        def work_searches():
            cloud = data_sync.pull_saved_searches(token)
            if cloud is None:
                return

            def apply():
                def sig(s):
                    return (s["location"].strip().lower(), s["radius"])
                cloud_sigs = {sig(s) for s in cloud}
                local_only = [s for s in self.saved_searches if sig(s) not in cloud_sigs]
                self.saved_searches = cloud + local_only
                write_saved_searches(self.saved_searches)
                self._render_saved_searches()
                if local_only:
                    threading.Thread(
                        target=lambda: data_sync.push_all_saved_searches(token, local_only),
                        daemon=True).start()
            self.root.after(0, apply)
        threading.Thread(target=work_searches, daemon=True).start()

    def _do_verify_magic_link(self, email):
        code = self._ml_code.get().strip()
        self._ml_code_msg.config(text="Verifying...", fg=TEXT2)
        self.root.update_idletasks()
        ok, msg = auth_client.sign_in_with_code(email, code)
        if ok:
            self._ml_code_msg.config(text="✅ " + msg, fg=GREEN)
            self._sync_pull_after_signin()
            self.root.after(700, lambda: self._build_subscribe_body(self._pages["subscribe"]))
        else:
            self._ml_code_msg.config(text="❌ " + msg, fg=RED)

    def _do_signup(self):
        email = self._auth_email.get().strip()
        pw = self._auth_password.get()
        self._auth_msg.config(text="Creating account...", fg=TEXT2)
        self.root.update_idletasks()
        ok, msg = auth_client.sign_up(email, pw)
        if ok:
            self._auth_msg.config(text="✅ " + msg, fg=GREEN)
            self._sync_pull_after_signin()
            self.root.after(900, lambda: self._build_subscribe_body(self._pages["subscribe"]))
        else:
            self._auth_msg.config(text="❌ " + msg, fg=RED)

    def _do_signin(self):
        email = self._auth_email.get().strip()
        pw = self._auth_password.get()
        self._auth_msg.config(text="Signing in...", fg=TEXT2)
        self.root.update_idletasks()
        ok, msg = auth_client.sign_in(email, pw)
        if ok:
            self._auth_msg.config(text="✅ " + msg, fg=GREEN)
            self._sync_pull_after_signin()
            self.root.after(700, lambda: self._build_subscribe_body(self._pages["subscribe"]))
        else:
            self._auth_msg.config(text="❌ " + msg, fg=RED)

    def _do_google_signin(self):
        # Blocks on the browser round-trip (up to 2 minutes), so this has
        # to run off the UI thread or the whole app would freeze while
        # waiting for the user to finish signing in.
        self._auth_msg.config(text="Opening your browser for Google sign-in...", fg=TEXT2)
        self.root.update_idletasks()

        def work():
            ok, msg = auth_client.sign_in_with_google()

            def apply():
                if ok:
                    self._auth_msg.config(text="✅ " + msg, fg=GREEN)
                    self._sync_pull_after_signin()
                    self.root.after(700, lambda: self._build_subscribe_body(self._pages["subscribe"]))
                else:
                    self._auth_msg.config(text="❌ " + msg, fg=RED)
            self.root.after(0, apply)

        threading.Thread(target=work, daemon=True).start()

    def _show_forgot_email(self):
        self._build_forgot_email_body(self._pages["subscribe"])

    def _build_forgot_email_body(self, pg):
        inner, card = self._auth_card(pg)
        tk.Label(card, text="Reset Password", font=(UI, fs(18), "bold"),
                 bg=CARD, fg=TEXT).pack(pady=(0, 4))
        tk.Label(card, text="Enter your email and we'll send you a reset code.",
                 font=F_SMALL, bg=CARD, fg=TEXT2, wraplength=280,
                 justify="left").pack(pady=(0, 18))

        self._reset_email = self._labeled_entry(card, "Email")
        prefill = ""
        try:
            prefill = self._auth_email.get().strip()
        except Exception:
            pass
        prefill = prefill or auth_client.current_email() or ""
        if prefill:
            self._reset_email.insert(0, prefill)

        self._reset_msg = tk.Label(card, text="", font=F_SMALL, bg=CARD, fg=TEXT3,
                                   wraplength=280, justify="left")
        self._reset_msg.pack(pady=(0, 10))

        pill_btn(card, "Send Reset Code", self._do_request_reset,
                 py=10, font=(UI, fs(12), "bold")).pack()

        ghost_btn(inner, "← Back to Sign In", lambda: self._switch_auth_mode("signin"),
                  font=F_SMALL).pack(pady=(16, 0))

    def _do_request_reset(self):
        email = self._reset_email.get().strip()
        self._reset_msg.config(text="Sending...", fg=TEXT2)
        self.root.update_idletasks()
        ok, msg = auth_client.request_password_reset(email)
        if ok:
            self._reset_msg.config(text="✅ " + msg, fg=GREEN)
            self.root.after(500, lambda: self._build_forgot_code_body(self._pages["subscribe"], email))
        else:
            self._reset_msg.config(text="❌ " + msg, fg=RED)

    def _build_forgot_code_body(self, pg, email):
        inner, card = self._auth_card(pg)
        tk.Label(card, text="Enter Reset Code", font=(UI, fs(18), "bold"),
                 bg=CARD, fg=TEXT).pack(pady=(0, 4))
        tk.Label(card, text=f"We sent a code to {email}. Enter it below with your new password.",
                 font=F_SMALL, bg=CARD, fg=TEXT2, wraplength=280,
                 justify="left").pack(pady=(0, 18))

        self._reset_code = self._labeled_entry(card, "Reset code (from email)")
        self._reset_new_pw = self._labeled_entry(card, "New password", show="•")
        self._reset_new_pw2 = self._labeled_entry(card, "Confirm new password", show="•")
        self._reset_new_pw2.bind("<Return>", lambda e: self._do_reset_password(email))

        self._reset_code_msg = tk.Label(card, text="", font=F_SMALL, bg=CARD, fg=TEXT3,
                                        wraplength=280, justify="left")
        self._reset_code_msg.pack(pady=(0, 10))

        pill_btn(card, "Reset Password", lambda: self._do_reset_password(email),
                 py=10, font=(UI, fs(12), "bold")).pack()

        row = tk.Frame(card, bg=CARD)
        row.pack(pady=(14, 0))
        ghost_btn(row, "Resend code", lambda: self._do_request_reset_for(email),
                  font=F_SMALL).pack(side="left", padx=(0, 6))
        ghost_btn(row, "Use a different email", self._show_forgot_email,
                  font=F_SMALL).pack(side="left")

        ghost_btn(inner, "← Back to Sign In", lambda: self._switch_auth_mode("signin"),
                  font=F_SMALL).pack(pady=(16, 0))

    def _do_request_reset_for(self, email):
        ok, msg = auth_client.request_password_reset(email)
        if hasattr(self, "_reset_code_msg") and self._reset_code_msg.winfo_exists():
            self._reset_code_msg.config(text=("✅ " if ok else "❌ ") + msg,
                                        fg=GREEN if ok else RED)

    def _do_reset_password(self, email):
        code = self._reset_code.get().strip()
        pw1 = self._reset_new_pw.get()
        pw2 = self._reset_new_pw2.get()
        if pw1 != pw2:
            self._reset_code_msg.config(text="❌ Passwords don't match.", fg=RED)
            return
        self._reset_code_msg.config(text="Resetting...", fg=TEXT2)
        self.root.update_idletasks()
        ok, msg = auth_client.reset_password_with_code(email, code, pw1)
        if ok:
            self._reset_code_msg.config(text="✅ " + msg, fg=GREEN)
            self.root.after(1000, lambda: self._build_subscribe_body(self._pages["subscribe"]))
        else:
            self._reset_code_msg.config(text="❌ " + msg, fg=RED)

    # ═══════════ PAGE: ACCOUNT ═══════════
    def _page_account(self):
        pg = tk.Frame(self._container, bg=BG)
        self._pages["account"] = pg
        hdr = tk.Frame(pg, bg=BG, pady=18)
        hdr.pack(fill="x", padx=24)
        tk.Label(hdr, text="Account", font=F_HEAD, bg=BG, fg=TEXT).pack(side="left")
        divider(pg, BORDER)
        self._account_frame = scrollable(pg)
        self._last_sync_at = None
        self._company_edit_mode = False

    def _render_account(self):
        for w in self._account_frame.winfo_children():
            w.destroy()
        body = tk.Frame(self._account_frame, bg=BG, padx=30, pady=24)
        body.pack(fill="both", expand=True)

        email = auth_client.current_email()
        if not email:
            tk.Label(body, text="👤", font=(UI, fs(36)), bg=BG, fg=BORDER).pack(pady=(30, 6))
            tk.Label(body, text="Not signed in", font=(UI, fs(16), "bold"),
                     bg=BG, fg=TEXT2).pack()
            tk.Label(body, text="Sign in to sync your saved bids, company profile, and "
                     "searches across every device.", font=F_BODY, bg=BG, fg=TEXT3,
                     wraplength=420, justify="center").pack(pady=(6, 16))
            pill_btn(body, "Sign In / Create Account",
                     lambda: (self._nav("subscribe"), self._show_signin()),
                     px=20, py=10, font=(UI, fs(12), "bold")).pack()
            return

        # ── Identity row (avatar + email/meta + sign out) ──
        idrow = tk.Frame(body, bg=CARD, padx=18, pady=14)
        idrow.pack(fill="x")
        self._avatar_lbl = tk.Label(idrow, text="👤", font=(UI, fs(22)), bg=CARD, fg=TEXT3,
                                    width=2, height=1)
        self._avatar_lbl.pack(side="left", padx=(0, 12))
        self._avatar_photo_ref = None  # keep a reference so Tk doesn't garbage-collect it
        left = tk.Frame(idrow, bg=CARD)
        left.pack(side="left", fill="x", expand=True)
        tk.Label(left, text=email, font=(UI, fs(13), "bold"),
                 bg=CARD, fg=TEXT).pack(anchor="w")
        self._account_meta_lbl = tk.Label(left, text="Loading account details...",
                                          font=F_SMALL, bg=CARD, fg=TEXT3)
        self._account_meta_lbl.pack(anchor="w", pady=(2, 0))
        ghost_btn(left, "Change Photo", self._upload_avatar, font=F_SMALL).pack(anchor="w", pady=(4, 0))
        ghost_btn(idrow, "Sign Out", self._do_sign_out, font=F_SMALL).pack(side="right", anchor="n")

        if self.company.get("avatar_url"):
            self._load_avatar_thumbnail(self.company["avatar_url"])

        def fetch_info():
            info = auth_client.get_user_info()
            self._user_id = info.get("id")

            def apply():
                if not hasattr(self, "_account_meta_lbl"):
                    return
                provider = ((info.get("app_metadata") or {}).get("provider") or "email").title()
                created = (info.get("created_at") or "")[:10]
                meta = f"Signed in with {provider}"
                if created:
                    meta += f"   •   Member since {created}"
                self._account_meta_lbl.config(text=meta)
            self.root.after(0, apply)
        threading.Thread(target=fetch_info, daemon=True).start()

        # ── Stats ──
        tk.Label(body, text="Your Stats", font=F_SUB, bg=BG, fg=ACCENT).pack(anchor="w", pady=(20, 8))
        counts, win_rate, won_value, tracked_value = self._pipeline_stats()
        stats_row = tk.Frame(body, bg=BG)
        stats_row.pack(fill="x")
        for n, label in [(str(counts["All"]), "Active Bids"), (win_rate, "Win Rate"),
                          (format_money(won_value), "Won"), (format_money(tracked_value), "Tracked Value")]:
            box = tk.Frame(stats_row, bg=CARD, padx=14, pady=10)
            box.pack(side="left", fill="x", expand=True, padx=(0, 8))
            tk.Label(box, text=n, font=(UI, fs(16), "bold"), bg=CARD, fg=TEXT).pack()
            tk.Label(box, text=label, font=F_SMALL, bg=CARD, fg=TEXT3).pack()

        # ── Sync status ──
        sync_row = tk.Frame(body, bg=BG)
        sync_row.pack(fill="x", pady=(10, 0))
        sync_text = (f"🔄  Last synced {self._last_sync_at.strftime('%-I:%M %p')}"
                    if self._last_sync_at else "🔄  Not synced yet this session")
        tk.Label(sync_row, text=sync_text, font=F_SMALL, bg=BG, fg=TEXT3).pack(side="left")
        ghost_btn(sync_row, "Sync Now", self._sync_pull_after_signin, font=F_SMALL).pack(side="left", padx=(10, 0))

        divider(body, BORDER, pad=0)
        tk.Frame(body, bg=BG, height=20).pack()

        # ── Company Profile ──
        co_hdr = tk.Frame(body, bg=BG)
        co_hdr.pack(fill="x", pady=(0, 4))
        tk.Label(co_hdr, text="Company Profile", font=F_SUB, bg=BG, fg=ACCENT).pack(side="left")
        if not self._company_edit_mode:
            ghost_btn(co_hdr, "✏  Edit", self._start_company_edit, font=F_SMALL).pack(side="left", padx=(10, 0))
        tk.Label(body, text="Used to personalize AI-drafted bid proposals. Nothing here is "
                 "shared beyond that.", font=F_SMALL, bg=BG, fg=TEXT3,
                 wraplength=500, justify="left").pack(anchor="w", pady=(0, 10))

        co_fields = [("name", "Company Name", "Acme Concrete LLC"),
                     ("contact", "Contact Person", "Jane Doe"),
                     ("phone", "Phone", "(555) 555-5555"),
                     ("email", "Email", "you@company.com"),
                     ("specialty", "Specialty (optional)", "sidewalk, ADA ramp & curb concrete work")]

        if self._company_edit_mode:
            self._co_entries = {}
            for key, label, placeholder in co_fields:
                tk.Label(body, text=label, font=F_SMALL, bg=BG, fg=TEXT2).pack(anchor="w", pady=(6, 2))
                e = tk.Entry(body, font=F_BODY, bg=CARD, fg=TEXT, insertbackground=TEXT,
                            relief="flat", bd=0, width=44, highlightthickness=1,
                            highlightbackground=BORDER, highlightcolor=ACCENT)
                e.pack(anchor="w", ipady=6)
                e.insert(0, self.company.get(key, ""))
                self._co_entries[key] = e

            btn_row = tk.Frame(body, bg=BG)
            btn_row.pack(anchor="w", pady=(14, 0))
            pill_btn(btn_row, "✓  Confirm", self._save_company_profile, px=18, py=9,
                     font=F_SMALL).pack(side="left")
            ghost_btn(btn_row, "Cancel", self._cancel_company_edit, font=F_SMALL).pack(side="left", padx=(8, 0))
            self._co_saved_lbl = tk.Label(body, text="", font=F_SMALL, bg=BG, fg=GREEN)
            self._co_saved_lbl.pack(anchor="w", pady=(6, 0))
        else:
            for key, label, _ in co_fields:
                row = tk.Frame(body, bg=BG)
                row.pack(fill="x", pady=(6, 0))
                tk.Label(row, text=label.upper(), font=(UI, fs(8), "bold"),
                        bg=BG, fg=TEXT3, anchor="w").pack(anchor="w")
                tk.Label(row, text=self.company.get(key) or "Not set", font=F_BODY,
                        bg=BG, fg=TEXT, anchor="w").pack(anchor="w")

        divider(body, BORDER, pad=0)
        tk.Frame(body, bg=BG, height=20).pack()

        # ── Quick links ──
        links = tk.Frame(body, bg=BG)
        links.pack(fill="x")
        ghost_btn(links, "💳  Manage Subscription →", lambda: self._nav("subscribe"),
                 font=F_SMALL).pack(side="left")

    def _load_avatar_thumbnail(self, url, size=44):
        """Downloads + resizes the avatar image for display in the identity
        row. Best-effort — silently leaves the placeholder icon on failure
        (offline, bad URL, PIL/requests unavailable)."""
        def work():
            try:
                resp = requests.get(url, timeout=10)
                img = PIL.Image.open(io.BytesIO(resp.content)).convert("RGB")
                img = img.resize((size, size), PIL.Image.LANCZOS)
                photo = PIL.ImageTk.PhotoImage(img)
            except Exception:
                return

            def apply():
                if not hasattr(self, "_avatar_lbl"):
                    return
                self._avatar_photo_ref = photo  # prevent garbage collection
                self._avatar_lbl.config(image=photo, text="", width=size, height=size)
            self.root.after(0, apply)
        threading.Thread(target=work, daemon=True).start()

    def _upload_avatar(self):
        token = auth_client.current_access_token()
        if not token:
            messagebox.showwarning("Profile Photo", "Sign in first to add a profile photo.")
            return
        user_id = getattr(self, "_user_id", None)
        if not user_id:
            messagebox.showinfo("Profile Photo", "Still loading your account — try again in a second.")
            return
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            title="Choose a profile photo",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.gif *.webp")])
        if not path:
            return

        def work():
            url = data_sync.upload_avatar(token, user_id, path)
            if not url:
                self.root.after(0, lambda: messagebox.showerror(
                    "Profile Photo", "Couldn't upload the photo. Check your internet and try again."))
                return
            self.company["avatar_url"] = url
            write_company_profile(self.company)
            data_sync.push_company_profile(token, self.company)
            self.root.after(0, lambda: self._load_avatar_thumbnail(url))
        threading.Thread(target=work, daemon=True).start()

    # ═══════════ PAGE: BILLING ═══════════
    def _page_billing(self):
        pg = tk.Frame(self._container, bg=BG)
        self._pages["billing"] = pg
        hdr = tk.Frame(pg, bg=BG, pady=18)
        hdr.pack(fill="x", padx=24)
        tk.Label(hdr, text="Billing", font=F_HEAD, bg=BG, fg=TEXT).pack(side="left")
        divider(pg, BORDER)
        self._billing_body = tk.Frame(pg, bg=BG, padx=30, pady=24)
        self._billing_body.pack(fill="both", expand=True)

    def _render_billing(self):
        body = self._billing_body
        for w in body.winfo_children():
            w.destroy()
        s = subscription.get_status()

        if s.get("active") and s.get("trial"):
            countdown = (subscription.time_left_label(s.get("expires_at"))
                        or f"{s.get('days_left','?')} days left")
            tk.Label(body, text="🎁", font=(UI, fs(34)), bg=BG, fg=ACCENT).pack(anchor="w")
            tk.Label(body, text=f"Free Trial — {countdown}", font=F_SUB, bg=BG, fg=TEXT).pack(anchor="w", pady=(4, 4))
            tk.Label(body, text="No payment method on file yet. Subscribe any time to keep access after the trial ends.",
                     font=F_SMALL, bg=BG, fg=TEXT3).pack(anchor="w", pady=(0, 16))
            pill_btn(body, "View Plans", lambda: self._nav("subscribe"), px=18, py=9, font=F_SMALL).pack(anchor="w")
        elif s.get("active"):
            card = tk.Frame(body, bg=CARD, padx=22, pady=20)
            card.pack(fill="x", pady=(0, 16))
            tk.Label(card, text="✅ You're subscribed", font=F_SUB, bg=CARD, fg=GREEN).pack(anchor="w")
            tk.Label(card, text=f"Plan: {s.get('plan','Pro')}", font=F_BODY, bg=CARD, fg=TEXT).pack(anchor="w", pady=(10, 2))
            tk.Label(card, text=f"Renews: {s.get('renews','—')}", font=F_BODY, bg=CARD, fg=TEXT).pack(anchor="w")
            cache = subscription._load_cache()
            key = cache.get("key", "")
            if key:
                masked = key[:4] + "•" * max(0, len(key) - 8) + key[-4:] if len(key) > 8 else key
                tk.Label(card, text=f"License key: {masked}", font=F_SMALL, bg=CARD, fg=TEXT3).pack(anchor="w", pady=(8, 0))

            tk.Label(body, text="Manage your payment method and view invoices in Stripe's secure billing portal.",
                     font=F_SMALL, bg=BG, fg=TEXT3, wraplength=420, justify="left").pack(anchor="w", pady=(0, 10))
            pill_btn(body, "💳 Manage Billing on Stripe →",
                     lambda: webbrowser.open(subscription.STRIPE_PORTAL_URL), px=18, py=9, font=F_SMALL).pack(anchor="w")
            ghost_btn(body, "Cancel Subscription", self._do_cancel, font=F_SMALL).pack(anchor="w", pady=(12, 0))
        else:
            tk.Label(body, text="⚠ No active plan", font=F_SUB, bg=BG, fg=RED).pack(anchor="w")
            tk.Label(body, text="Subscribe to unlock unlimited bid scanning and tracking.",
                     font=F_SMALL, bg=BG, fg=TEXT3).pack(anchor="w", pady=(4, 16))
            pill_btn(body, "View Plans", lambda: self._nav("subscribe"), px=18, py=9, font=F_SMALL).pack(anchor="w")

    # ═══════════ PAGE: SUPPORT ═══════════
    def _page_support(self):
        pg = tk.Frame(self._container, bg=BG)
        self._pages["support"] = pg
        hdr = tk.Frame(pg, bg=BG, pady=18)
        hdr.pack(fill="x", padx=24)
        tk.Label(hdr, text="Support", font=F_HEAD, bg=BG, fg=TEXT).pack(side="left")
        divider(pg, BORDER)

        body = tk.Frame(pg, bg=BG, padx=30, pady=24)
        body.pack(fill="both", expand=True)

        tk.Label(body, text="Need a hand?", font=F_SUB, bg=BG, fg=ACCENT).pack(anchor="w")
        tk.Label(body, text="Send us a message and we'll get back to you, or email us directly.",
                 font=F_SMALL, bg=BG, fg=TEXT3).pack(anchor="w", pady=(0, 14))

        email_row = tk.Frame(body, bg=CARD, padx=16, pady=12)
        email_row.pack(fill="x", pady=(0, 20))
        tk.Label(email_row, text=f"✉  {subscription.SUPPORT_EMAIL}",
                 font=F_BODY, bg=CARD, fg=TEXT, cursor="hand2").pack(side="left")
        ghost_btn(email_row, "Open Email", self._email_support, font=F_SMALL).pack(side="right")

        tk.Label(body, text="Your Email (so we can reply)", font=F_SMALL, bg=BG, fg=TEXT2).pack(anchor="w", pady=(0, 4))
        self._support_email_entry = tk.Entry(body, font=F_BODY, bg=CARD, fg=TEXT, insertbackground=TEXT,
                                             relief="flat", bd=0, width=44, highlightthickness=1,
                                             highlightbackground=BORDER, highlightcolor=ACCENT)
        self._support_email_entry.pack(anchor="w", ipady=6, pady=(0, 12))
        self._support_email_entry.insert(0, auth_client.current_email() or "")

        tk.Label(body, text="Message", font=F_SMALL, bg=BG, fg=TEXT2).pack(anchor="w", pady=(0, 4))
        self._support_msg_box = tk.Text(body, font=F_BODY, bg=CARD, fg=TEXT, relief="flat", bd=0,
                                        height=8, wrap="word", insertbackground=TEXT)
        self._support_msg_box.pack(fill="x", pady=(0, 12))

        pill_btn(body, "Send Message", self._send_support_message, px=18, py=9,
                 font=F_SMALL).pack(anchor="w")
        self._support_status_lbl = tk.Label(body, text="", font=F_SMALL, bg=BG, fg=TEXT3)
        self._support_status_lbl.pack(anchor="w", pady=(8, 0))

    def _email_support(self):
        addr = getattr(subscription, "SUPPORT_EMAIL", "")
        if addr:
            webbrowser.open(f"mailto:{addr}")

    def _send_support_message(self):
        message = self._support_msg_box.get("1.0", tk.END).strip()
        if not message:
            messagebox.showwarning("Support", "Enter a message first.")
            return
        email = self._support_email_entry.get().strip()
        self._support_status_lbl.config(text="Sending...", fg=TEXT2)

        def work():
            ok, msg = subscription.send_support_message(email, message)

            def apply():
                self._support_status_lbl.config(text=msg, fg=GREEN if ok else RED)
                if ok:
                    self._support_msg_box.delete("1.0", tk.END)
            self.root.after(0, apply)
        threading.Thread(target=work, daemon=True).start()

    # ═══════════ PAGE: SETTINGS ═══════════
    def _page_settings(self):
        pg = tk.Frame(self._container, bg=BG)
        self._pages["settings"] = pg
        hdr = tk.Frame(pg, bg=BG, pady=18)
        hdr.pack(fill="x", padx=24)
        tk.Label(hdr, text="Settings", font=F_HEAD, bg=BG, fg=TEXT).pack(side="left")
        divider(pg, BORDER)

        body = tk.Frame(pg, bg=BG)
        body.pack(fill="both", expand=True, padx=30, pady=24)

        tk.Label(body, text="Text Size", font=F_SUB, bg=BG, fg=ACCENT).pack(anchor="w")
        tk.Label(body, text="Bigger text is easier to read on a job site or for tired eyes.",
                 font=F_SMALL, bg=BG, fg=TEXT3).pack(anchor="w", pady=(0, 10))

        size_row = tk.Frame(body, bg=BG)
        size_row.pack(anchor="w", pady=(0, 6))
        for label, val in [("Small", 0.9), ("Normal", 1.0), ("Large", 1.15), ("Extra Large", 1.3)]:
            active = abs(SCALE - val) < 0.001
            tk.Button(size_row, text=label, font=F_BODY,
                      bg=ACCENT if active else CARD, fg="#000" if active else TEXT,
                      relief="flat", bd=0, cursor="hand2", padx=18, pady=10,
                      activebackground=ACCENT_D,
                      command=lambda v=val: self._set_scale(v)).pack(side="left", padx=(0, 8))

        tk.Label(body, text="Changing text size restarts the app.",
                 font=F_SMALL, bg=BG, fg=TEXT3).pack(anchor="w", pady=(6, 24))
        divider(body, BORDER)

        tk.Label(body, text="Your Data", font=F_SUB, bg=BG, fg=ACCENT).pack(anchor="w", pady=(20, 4))
        tk.Label(body, text=f"Saved bids: {len(self.saved)}   •   Stored on this computer only.",
                 font=F_SMALL, bg=BG, fg=TEXT3).pack(anchor="w", pady=(0, 10))
        ghost_btn(body, "Clear all saved bids", self._clear_saved, font=F_BODY).pack(anchor="w")

        divider(body, BORDER)
        tk.Label(body, text="About", font=F_SUB, bg=BG, fg=ACCENT).pack(anchor="w", pady=(20, 4))
        tk.Label(body, text="Bid Caller Pro — construction bid intelligence.",
                 font=F_SMALL, bg=BG, fg=TEXT3).pack(anchor="w")

    def _set_scale(self, val):
        SETTINGS["scale"] = val
        save_settings(SETTINGS)
        if messagebox.askyesno("Restart", "Text size saved. Restart now to apply?"):
            try:
                self.root.destroy()
                os.execl(sys.executable, sys.executable, *sys.argv)
            except Exception:
                messagebox.showinfo("Restart", "Please close and reopen the app to apply.")

    def _start_company_edit(self):
        self._company_edit_mode = True
        self._render_account()

    def _cancel_company_edit(self):
        self._company_edit_mode = False
        self._render_account()

    def _save_company_profile(self):
        self.company = {k: e.get().strip() for k, e in self._co_entries.items()}
        write_company_profile(self.company)
        token = auth_client.current_access_token()
        if token:
            threading.Thread(target=lambda: data_sync.push_company_profile(token, self.company),
                             daemon=True).start()
        self._company_edit_mode = False
        self._render_account()

    def _clear_saved(self):
        token = auth_client.current_access_token()
        prompt = ("Remove ALL saved bids and notes? This clears them on every "
                  "signed-in device too." if token else
                  "Remove ALL saved bids and notes?")
        if messagebox.askyesno("Clear Saved", prompt):
            self.saved = {}
            write_saved(self.saved)
            if token:
                threading.Thread(target=lambda: data_sync.clear_all_saved_bids(token),
                                 daemon=True).start()
            messagebox.showinfo("Done", "All saved bids cleared.")
            self._nav("settings")

    # ═══════════ WELCOME ═══════════
    def _show_welcome(self):
        win = tk.Toplevel(self.root)
        _apply_icon(win)
        win.title("Welcome")
        win.configure(bg=SURFACE)
        win.geometry("520x420")
        win.transient(self.root)
        win.grab_set()

        tk.Label(win, text="🔨", font=(UI, fs(34)), bg=SURFACE, fg=ACCENT).pack(pady=(24, 4))
        tk.Label(win, text="Welcome to Bid Caller Pro", font=(UI, fs(18), "bold"),
                 bg=SURFACE, fg=TEXT).pack()
        tk.Label(win, text="Find construction bids near you in three simple steps:",
                 font=F_BODY, bg=SURFACE, fg=TEXT2).pack(pady=(6, 18))

        for icon, title, desc in [
            ("🔍", "Find Bids", "Enter your ZIP or auto-detect, pick a radius, and scan."),
            ("📋", "Browse the Feed", "Open bids show first. Tap links to contact directly."),
            ("⭐", "Save what matters", "Star bids and add notes to track your leads."),
        ]:
            row = tk.Frame(win, bg=SURFACE)
            row.pack(fill="x", padx=36, pady=6)
            tk.Label(row, text=icon, font=(UI, fs(18)), bg=SURFACE, fg=ACCENT,
                     width=3).pack(side="left")
            col = tk.Frame(row, bg=SURFACE)
            col.pack(side="left", fill="x")
            tk.Label(col, text=title, font=(UI, fs(12), "bold"), bg=SURFACE,
                     fg=TEXT, anchor="w").pack(anchor="w")
            tk.Label(col, text=desc, font=F_SMALL, bg=SURFACE, fg=TEXT3,
                     anchor="w", wraplength=380, justify="left").pack(anchor="w")

        def done():
            SETTINGS["seen_welcome"] = True
            save_settings(SETTINGS)
            win.destroy()

        pill_btn(win, "  Get Started  ", done, px=28, py=10,
                 font=(UI, fs(12), "bold")).pack(pady=20)


# ═══════════════════════════════════════════════
#  LAUNCH
# ═══════════════════════════════════════════════
def _set_windows_app_id():
    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("bidcaller.pro.app.1")
        except Exception:
            pass

def _apply_icon(window):
    icon_path = os.path.join(BASE_DIR, "icon.ico")
    if os.path.exists(icon_path):
        try:
            window.iconbitmap(default=icon_path)
        except Exception:
            try:
                window.iconbitmap(icon_path)
            except Exception:
                pass

def _dark_title_bar(window):
    """Make the Windows title bar dark instead of solid white (Win10 2004+ / Win11)."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        # DWMWA_USE_IMMERSIVE_DARK_MODE = 20 (or 19 on older builds)
        value = ctypes.c_int(1)
        for attr in (20, 19):
            try:
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(value), ctypes.sizeof(value))
            except Exception:
                pass
    except Exception:
        pass

def _windows_toast(title, message):
    """Best-effort native Windows notification for new bids from saved
    searches — desktop equivalent of the web app's browser Notification API.
    Fails silently (non-Windows, PowerShell blocked, etc.) since this is a
    nice-to-have, not something scans should ever depend on."""
    if not sys.platform.startswith("win"):
        return
    safe_title = title.replace('"', "'").replace("`", "'")
    safe_msg = message.replace('"', "'").replace("`", "'")
    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
        "ContentType=WindowsRuntime] | Out-Null; "
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, "
        "ContentType=WindowsRuntime] | Out-Null; "
        "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
        "$n = $t.GetElementsByTagName('text'); "
        f"$n.Item(0).AppendChild($t.CreateTextNode(\"{safe_title}\")) | Out-Null; "
        f"$n.Item(1).AppendChild($t.CreateTextNode(\"{safe_msg}\")) | Out-Null; "
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($t); "
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier"
        "('Bid Caller Pro').Show($toast)"
    )
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                         creationflags=subprocess.CREATE_NO_WINDOW,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

if __name__ == "__main__":
    _set_windows_app_id()
    root = tk.Tk()
    _apply_icon(root)

    style = ttk.Style()
    style.theme_use("clam")
    # Dark, slim scrollbar that matches the app
    style.configure("Vertical.TScrollbar",
                    background=BORDER, troughcolor=BG, bordercolor=BG,
                    arrowcolor=BG, relief="flat", borderwidth=0,
                    width=10)
    style.map("Vertical.TScrollbar",
              background=[("active", ACCENT), ("!active", BORDER)],
              arrowcolor=[("disabled", BG)])
    style.layout("Vertical.TScrollbar",
                 [("Vertical.Scrollbar.trough",
                   {"children": [("Vertical.Scrollbar.thumb",
                                  {"expand": "1", "sticky": "nswe"})],
                    "sticky": "ns"})])  # hide the up/down arrow buttons for a clean look
    style.configure("TScrollbar", background=BORDER, troughcolor=BG,
                    bordercolor=BG, arrowcolor=BG, relief="flat", borderwidth=0)
    style.configure("TCombobox", fieldbackground=CARD, background=CARD, foreground=TEXT,
                    selectbackground=CARD, selectforeground=TEXT, arrowcolor=TEXT2)
    style.map("TCombobox", fieldbackground=[("readonly", CARD)])

    _dark_title_bar(root)

    app = BidCaller(root)
    root.mainloop()

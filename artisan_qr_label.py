#!/usr/bin/env python3
"""
artisan_qr_label.py - a QR-code roast label. The label shows "Bean Me Up", the roast time in UTC
(to the second, monospace), the bag weight, and a QR code holding the whole roast (details + bean-temp curve) from an
Artisan .alog file. Companion to artisan_labels.py (keep both in the same folder, with fonts/).

Usage:
    python3 artisan_qr_label.py ROAST_FOLDER --weight 340            # newest roast in the folder
    python3 artisan_qr_label.py ROAST_FOLDER --weight 340 --print -n 6
    python3 artisan_qr_label.py roast.alog --notes "Passion Fruit, Floral" --shift-x 10
    python3 artisan_qr_label.py ROAST_FOLDER --viewer-url ""        # plain-text QR instead of a link to the viewer
    python3 artisan_qr_label.py --decode "eNpV..."                   # read back a scanned code

What is inside the code (compressed text, a link to the viewer plus about 400 characters): name, roast time (UTC), batch,
tasting notes, bag weight, weights in/out, roast level, charge/drop temps, event times (dry end, first/second
crack, drop) and the bean-temperature curve (charge to drop, whole degrees C). Rate of rise (RoR) is not stored separately:
Artisan computes it from the bean-temp curve, and so does the roast viewer page.

Requires: pip install pillow segno ; for --print: npm i -g @mmote/niimblue-node
"""
import argparse, base64, bisect, json, math, os, struct, sys, zlib
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import artisan_labels as al                      # shared parsing, fonts, layout helpers, printing
from PIL import Image, ImageDraw

try:
    import segno
except ImportError:
    segno = None

DEFAULT_VIEWER_URL = "https://qr-roast-viewer.wrschwartz.workers.dev/"
PREFIX = "BB1:"                                  # old codes had this; new ones are plain text (phones reject "xxx:" as an unknown link type)
QUIET = 2                                         # quiet-zone modules around the QR code
STEPS = (5,)                                      # bean-temp curve sampled every 5 seconds
NOTE_CAPS = (None, 120, 60, 0)                    # tasting-note lengths to try, longest first


# ---------- payload: encode / decode ----------
def to_c(v, unit):
    """Temperature as whole degrees Celsius (Artisan files may be in Fahrenheit)."""
    return round(v if unit != "F" else (v - 32) * 5 / 9)


def curve_samples(d, step, unit="C"):
    """Bean-temp curve from charge to drop, resampled every `step` s, in whole deg C -> (start_temp, [deltas])."""
    tx, bt = d.get("timex") or [], d.get("temp2") or []
    ti = d.get("timeindex") or [0] * 8
    n = min(len(tx), len(bt))
    i0 = ti[0] if ti[0] else 0
    i1 = min(ti[6] if ti[6] else n - 1, n - 1)
    pts = [(tx[i], bt[i]) for i in range(i0, i1 + 1) if bt[i] and bt[i] > 0]
    if len(pts) < 2:
        return None
    ts = [p[0] for p in pts]
    t0 = tx[i0]
    out, k = [], 0
    while True:
        t = t0 + k * step
        if t > ts[-1] + 1e-6:
            break
        j = bisect.bisect_left(ts, t)
        if j <= 0:
            v = pts[0][1]
        elif j >= len(pts):
            v = pts[-1][1]
        else:
            (ta, va), (tb, vb) = pts[j - 1], pts[j]
            v = va + (vb - va) * (t - ta) / (tb - ta) if tb > ta else vb
        out.append(to_c(v, unit))
        k += 1
    deltas = [out[i + 1] - out[i] for i in range(len(out) - 1)]
    return out[0], deltas


MONO = "IBMPlexMono-Medium"
MONO_BOLD = "IBMPlexMono-Bold" if os.path.exists(os.path.join(al.FONT_DIR, "IBMPlexMono-Bold.ttf")) else MONO


def roast_utc(d, r):
    """Roast start time as a UTC datetime. Artisan stores an epoch; older files only local date + time."""
    e = al.num(d.get("roastepoch"))
    if e:
        return datetime.fromtimestamp(e, timezone.utc)
    try:
        local = datetime.strptime(f"{r['date']} {d.get('roasttime') or '00:00:00'}", "%Y-%m-%d %H:%M:%S")
        return (local + timedelta(seconds=al.num(d.get("roasttzoffset"), 0) or 0)).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def ror_samples(d, step, unit="C"):
    """The roaster's own recorded BT RoR (an Artisan extra-device curve named like 'BT RoR'), sampled on the same
    grid as the bean-temp curve, in whole deg C per minute -> [first value, deltas...] as a list, or None."""
    names = list(d.get("extraname1") or [])
    xs, ys = d.get("extratimex") or [], d.get("extratemp1") or []
    idx = next((i for i, nm in enumerate(names) if isinstance(nm, str) and "ror" in nm.lower() and "bt" in nm.lower()
                and "d-" not in nm.lower() and i < len(xs) and i < len(ys)), None)
    if idx is None:
        return None
    tx, ty = xs[idx], ys[idx]
    ti, bx = d.get("timeindex") or [0] * 8, d.get("timex") or []
    n = min(len(tx), len(ty))
    if n < 2 or not bx:
        return None
    i0 = ti[0] if ti[0] else 0
    t0 = bx[i0]
    t1 = bx[min(ti[6] if ti[6] else len(bx) - 1, len(bx) - 1)]
    scale = 5 / 9 if unit == "F" else 1.0
    out, k = [], 0
    while t0 + k * step <= t1 + 1e-6:
        t = t0 + k * step
        j = bisect.bisect_left(tx, t, 0, n)
        if j <= 0:
            v = ty[0]
        elif j >= n:
            v = ty[n - 1]
        else:
            v = ty[j - 1] + (ty[j] - ty[j - 1]) * (t - tx[j - 1]) / (tx[j] - tx[j - 1]) if tx[j] > tx[j - 1] else ty[j]
        out.append(max(-40, min(127, round(v * scale))))      # the charge-time plunge is clipped; it is off the chart anyway
        k += 1
    if len(out) < 2:
        return None
    return [out[0]] + [out[i] - out[i - 1] for i in range(1, len(out))]


def build_meta(d, r, step, notes, bag_weight, level, start, count, utc=None):
    """Roast details for the QR. Kept lean: totals the viewer can work out itself (roast time = drop time,
    development = drop - first crack, point count) are not stored."""
    c = d.get("computed", {}) or {}
    ti = d.get("timeindex") or [0] * 8
    tx = d.get("timex") or []
    t0 = tx[ti[0]] if ti[0] and ti[0] < len(tx) else 0
    ev = []
    for k in (1, 2, 3, 4, 5, 6):                  # dry end, FC start, FC end, SC start, SC end, drop
        ev.append(round(tx[ti[k]] - t0) if ti[k] and ti[k] < len(tx) else 0)
    m = {"v": 2, "n": r["name"], "s": step, "e": ev}        # v2: all temperatures are whole deg C
    if start is not None:
        m["b0"] = start
    if utc:
        m["ut"] = int(utc.timestamp())                      # roast start, UTC epoch seconds
    else:
        m["d"] = r["date"]
    if r["batch"]:
        m["b"] = str(r["batch"]).lstrip("#")
    if notes:
        m["nt"] = notes
    if bag_weight:
        m["bw"] = bag_weight
    if r["w_in"]:
        m["w"] = [round(r["w_in"], 1), round(r["w_out"], 1)] + ([r["unit"]] if r["unit"] != "g" else [])
    if level:
        m["lv"] = level
    for key, ck in (("cb", "CHARGE_BT"), ("ce", "CHARGE_ET"), ("db", "DROP_BT"), ("de", "DROP_ET"), ("fb", "FCs_BT")):
        v = al.num(c.get(ck))
        if v:
            m[key] = to_c(v, r["tunit"])
    return m


def pack_deltas(deltas):
    """One signed byte per step; a big jump (e.g. the charge drop) is the byte -128 followed by an int16."""
    out = bytearray()
    for dl in deltas:
        if -127 <= dl <= 127:
            out += struct.pack("b", dl)
        else:
            out += struct.pack("<bh", -128, dl)
    return bytes(out)


def unpack_deltas(buf):
    out, i = [], 0
    while i < len(buf):
        b = struct.unpack_from("b", buf, i)[0]
        if b == -128:
            out.append(struct.unpack_from("<h", buf, i + 1)[0])
            i += 3
        else:
            out.append(b)
            i += 1
    return out


# ---------- binary format (version 3) ----------
# Everything with a fixed shape is packed as numbers; only the free text (name, notes, ...) is stored as text.
# Layout, big-endian, then zlib + base64url:
#   u8   format version (3; the older JSON codes start with "{")
#   u8   flags: bit0 has weights, bits1-2 weight unit, bit3 has bag weight, bits4-5 bag-weight unit
#   u8   curve step s (seconds)      u8   RoR step rt (seconds, 0 = no recorded RoR)
#   u32  roast start, UTC epoch secs (0 = unknown)
#   6 x u16  dry end, FC start, FC end, SC start, SC end, drop (seconds from charge, 0 = none)
#   u16  batch number (0 = none)     u16 x2  charge / drop weight in tenths   u16  bag weight in tenths
#   u8   roast level in half steps (0 = none)
#   8 bytes: six 10-bit temps (curve start, charge bean, charge env, drop bean, drop env, FC bean; 1023 = none)
#   u16  text length   u16  bean-curve bytes   u16  RoR bytes
#   text (utf-8, NUL-separated: name, notes, bag weight if not a number, batch if not a number, date if no UTC time)
#   bean-temp deltas, RoR values
FORMAT_V3 = 3
TEMP_KEYS = ("b0", "cb", "ce", "db", "de", "fb")
UNITS = ("g", "kg", "lb", "oz")
FIXED = ">BBBBI6HHHHHB8sHHH"


def _tenths(x):
    return max(0, min(65535, int(round(float(x) * 10))))


def _bag_parse(text):
    """'100g' / '100' / '1.5 kg' -> (tenths, unit code), or None if it is free text."""
    import re
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(g|kg|lb|oz)?\s*", str(text or ""), re.I)
    if not m:
        return None
    return _tenths(m.group(1)), UNITS.index((m.group(2) or "g").lower())


def encode(meta, deltas, ror=None):
    meta = dict(meta)
    flags, w_in, w_out, wu = 0, 0, 0, 0
    w = meta.get("w")
    if w and len(w) >= 2 and (w[2] if len(w) > 2 else "g") in UNITS:
        flags |= 1
        w_in, w_out, wu = _tenths(w[0]), _tenths(w[1]), UNITS.index(w[2] if len(w) > 2 else "g")
    bag, bu, bag_text = 0, 0, ""
    if meta.get("bw"):
        p = _bag_parse(meta["bw"])
        if p:
            flags |= 8
            bag, bu = p
        else:
            bag_text = str(meta["bw"])
    batch, batch_text = 0, ""
    if meta.get("b"):
        b = str(meta["b"])
        if b.isdigit() and int(b) < 65536:
            batch = int(b)
        else:
            batch_text = b
    ut = int(meta.get("ut") or 0)
    texts = [meta.get("n", ""), meta.get("nt", ""), bag_text, batch_text, "" if ut else meta.get("d", "")]
    text = "\0".join(str(t) for t in texts).rstrip("\0").encode("utf-8")
    ev = [int(x) for x in (meta.get("e") or [])][:6]
    ev += [0] * (6 - len(ev))
    bits = 0
    for k in TEMP_KEYS:
        v = meta.get(k)
        bits = (bits << 10) | (1023 if v is None else max(0, min(1022, int(round(v)))))
    temps = (bits << 4).to_bytes(8, "big")
    bt, rr = pack_deltas(deltas), (pack_deltas(ror) if ror else b"")
    head = struct.pack(FIXED, FORMAT_V3, flags | (wu << 1) | (bu << 4), int(meta.get("s", 5)), int(meta.get("rt", 0)) if ror else 0,
                       ut, *ev, batch, w_in, w_out, bag, int(round(float(meta.get("lv") or 0) * 2)), temps,
                       len(text), len(bt), len(rr))
    return base64.urlsafe_b64encode(zlib.compress(head + text + bt + rr, 9)).decode().rstrip("=")


def decode_v3(raw):
    n = struct.calcsize(FIXED)
    f = struct.unpack_from(FIXED, raw, 0)
    flags, s, rt, ut = f[1], f[2], f[3], f[4]
    ev, batch, w_in, w_out, bag, lv, temps, lt, lb, lr = f[5:11], f[11], f[12], f[13], f[14], f[15], f[16], f[17], f[18], f[19]
    text = raw[n:n + lt].decode("utf-8").split("\0") + [""] * 5
    bt_bytes, ror_bytes = raw[n + lt:n + lt + lb], raw[n + lt + lb:n + lt + lb + lr]
    meta = {"v": 3, "n": text[0], "s": s, "e": list(ev)}
    if text[1]:
        meta["nt"] = text[1]
    if ut:
        meta["ut"] = ut
    elif text[4]:
        meta["d"] = text[4]
    if batch:
        meta["b"] = str(batch)
    elif text[3]:
        meta["b"] = text[3]
    if flags & 1:
        wu = UNITS[(flags >> 1) & 3]
        meta["w"] = [w_in / 10, w_out / 10] + ([wu] if wu != "g" else [])
    if flags & 8:
        meta["bw"] = f"{bag / 10:g}{UNITS[(flags >> 4) & 3]}"
    elif text[2]:
        meta["bw"] = text[2]
    if lv:
        meta["lv"] = lv / 2
    bits = int.from_bytes(temps, "big") >> 4
    for i, k in enumerate(TEMP_KEYS):
        v = (bits >> (10 * (len(TEMP_KEYS) - 1 - i))) & 1023
        if v != 1023:
            meta[k] = v
    if rt and ror_bytes:
        meta["rt"] = rt
        r = unpack_deltas(ror_bytes)
        for i in range(1, len(r)):
            r[i] += r[i - 1]
        meta["ror"] = r
    if "b0" not in meta:
        return meta, []
    q = [meta["b0"]]
    for dl in unpack_deltas(bt_bytes):
        q.append(q[-1] + dl)
    return meta, q


def unpack_temps(block, meta):
    bits = int.from_bytes(block, "big")
    for i, k in enumerate(("cb", "ce", "db", "de", "fb")):
        v = (bits >> (10 * (4 - i))) & 1023
        if v != 1023:
            meta[k] = v


def decode(text):
    """Scanned text -> (meta dict, bean-temp list in the file's temperature unit). Accepts an old BB1: prefix."""
    text = "".join(text.split())
    if "#" in text:                                          # a scanned viewer link: the data is after the #
        text = text.rsplit("#", 1)[1]
    if text.startswith(PREFIX):
        text = text[len(PREFIX):]
    raw = zlib.decompress(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))
    if raw[:1] == bytes([FORMAT_V3]):
        return decode_v3(raw)
    head, _, tail = raw.partition(b"\n")                    # older JSON-based codes
    meta = json.loads(head.decode("utf-8"))
    if meta.get("h"):                                        # newest codes: the five temps are a 7-byte block at the very end
        tail, block = tail[:-7], tail[-7:]
        unpack_temps(block, meta)
    rl = int(meta.get("rl", 0) or 0)
    if rl:
        tail, rtail = tail[:-rl], tail[-rl:]
        r = unpack_deltas(rtail)
        for i in range(1, len(r)):
            r[i] += r[i - 1]
        meta["ror"] = r                                      # recorded BT RoR, whole deg C/min, same grid as the curve
    deltas = unpack_deltas(tail)
    if "b0" not in meta:
        return meta, []
    q = [meta["b0"]]
    for dl in deltas:
        q.append(q[-1] + dl)
    return meta, q if meta.get("v", 1) >= 2 else [v / 2 for v in q]   # v1 codes stored half-degrees


def make_qr(d, r, notes, bag_weight, level, max_modules_by_m, utc=None, viewer_url=None):
    """Pick the finest curve detail (and longest notes) whose QR still fits at the largest module size."""
    notes_full = al.pretty_notes(notes)
    for m, max_mod in max_modules_by_m:
        for cap in NOTE_CAPS:
            nt = notes_full if cap is None else notes_full[:cap].rstrip(" -,")
            for step in STEPS:
                cs = curve_samples(d, step, r["tunit"])
                start, deltas = cs if cs else (None, [])        # a roast without a bean-temp curve still gets a label
                rstep = step * 2                               # the recorded RoR is smooth: half as many points are plenty
                ror = ror_samples(d, rstep, r["tunit"]) if cs else None
                meta = build_meta(d, r, step, nt, bag_weight, level, start, len(deltas) + 1, utc)
                if ror:
                    meta["rt"] = rstep                         # rt: seconds between RoR points
                s = encode(meta, deltas, ror)
                if viewer_url:                               # scanning opens the viewer; the data rides after the #
                    s = viewer_url.split("#")[0] + "#" + s
                qr = segno.make(s, error="L", micro=False, boost_error=False)
                if qr.symbol_size(border=0)[0] <= max_mod:
                    return qr, m, step, nt != notes_full, s
    raise SystemExit("Could not fit the roast data into a QR code at a scannable size.")


# ---------- label ----------
def render(d, r, w_mm=50, h_mm=30, bag_weight=None, notes=None, level=None, shift_x=0, shift_y=0, viewer_url=None,
           title="Bean Me Up"):
    W, H = min(round(w_mm / 25.4 * al.DPI), 384), round(h_mm / 25.4 * al.DPI)
    ox, oy = max(shift_x, 0), max(shift_y, 0)               # shifting just shrinks the usable region,
    RW, RH = W - abs(shift_x), H - abs(shift_y)             # so the QR module size stays exact
    lvl = level or al.roast_level(r["loss"])
    notes = r.get("notes") if notes is None else notes
    utc = roast_utc(d, r)

    side = RH - 4
    cand = [(m, side // m - 2 * QUIET) for m in (3, 2)]
    qr, m, step, truncated, payload = make_qr(d, r, notes, bag_weight, lvl, cand, utc, viewer_url)
    N = qr.symbol_size(border=0)[0]
    total = (N + 2 * QUIET) * m
    qx1 = ox + RW - 3
    qx0, qy0 = qx1 - total, oy + (RH - total) // 2
    lx0, lx1 = ox + 6, qx0

    # left column: title, roast time in UTC (monospace), bag weight - stacked, centred, shrunk to fit
    Z = 2
    big = Image.new("L", (W * Z, H * Z), 255)
    dr = ImageDraw.Draw(big)
    stamp = (utc.strftime("%Y-%m-%d"), utc.strftime("%H:%M:%S"), "UTC") if utc else (r["date"], "--:--:--", "UTC")
    colw, colh = (lx1 - lx0) * Z - 4, RH * Z - 12
    mono = 80
    texts = ([title] if title else []) + list(stamp) + ([bag_weight] if bag_weight else [])
    while mono > 12:                                       # largest monospace size at which everything fits
        mf, bf = al.tfont(MONO, mono), al.tfont(MONO_BOLD, mono)
        lh = int(mono * 1.35)
        extra = int(mono * 0.5) * ((1 if title else 0) + (1 if bag_weight else 0))   # gaps around the time block
        parts_h = lh * len(texts) + extra
        wide = max(dr.textlength(t, font=(bf if t == title else mf)) for t in texts)
        if wide <= colw and parts_h <= colh:
            break
        mono -= 2
    cx = (lx0 + lx1) * Z / 2
    y = (RH * Z - parts_h) / 2 + oy * Z
    if title:
        dr.text((cx, y), title, font=bf, fill=0, anchor="ma")
        y += lh + int(mono * 0.5)
    for t in stamp:
        dr.text((cx, y), t, font=mf, fill=0, anchor="ma")
        y += lh
    if bag_weight:
        y += int(mono * 0.5)
        dr.text((cx, y), bag_weight, font=bf, fill=0, anchor="ma")

    img = big.resize((W, H), Image.LANCZOS).point(lambda p: 255 if p > 150 else 0)

    # QR code, drawn pixel-exact at final resolution
    qd = ImageDraw.Draw(img)
    qd.rectangle((qx0, qy0, qx0 + total - 1, qy0 + total - 1), fill=255)
    for ry, row in enumerate(qr.matrix):
        for rx, v in enumerate(row):
            if v:
                x = qx0 + (rx + QUIET) * m
                y = qy0 + (ry + QUIET) * m
                qd.rectangle((x, y, x + m - 1, y + m - 1), fill=0)
    info = {"version": qr.version, "modules": N, "module_px": m, "step": step,
            "notes_truncated": truncated, "chars": len(payload)}
    return img.convert("1"), info, payload


def main():
    if segno is None and "--decode" not in sys.argv:
        sys.exit("This script needs the segno package:  pip install segno")
    env = os.environ.get
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", help=".alog files and/or folders (folder = its newest .alog); default $ROAST_DIR")
    ap.add_argument("--decode", metavar="TEXT_OR_FILE", help="decode a scanned code (or a text file holding it) and exit")
    ap.add_argument("--print", dest="do_print", action="store_true", help="print the label on the Niimbot")
    ap.add_argument("-n", "--copies", type=int, default=1, help="number of labels to print (with --print)")
    ap.add_argument("--density", type=int, default=al.MAX_DENSITY, choices=range(1, 6), help="print darkness 1-5 (default: 5)")
    ap.add_argument("--printer", default=env("PRINTER", "B1-H809121559"), help="Niimbot Bluetooth name")
    ap.add_argument("--out", default="labels", help="output folder")
    ap.add_argument("--width", type=float, default=50, help="label width mm")
    ap.add_argument("--height", type=float, default=30, help="label height mm")
    ap.add_argument("--name", help="override coffee name stored in the code")
    ap.add_argument("--weight", default=env("BAG_WEIGHT"), help='bag weight stored in the code, e.g. 340 or "12oz"')
    ap.add_argument("--notes", help="tasting notes stored in the code (default: Artisan cupping notes / Beans box)")
    ap.add_argument("--level", type=float, choices=[1, 1.5, 2, 2.5, 3, 3.5, 4], help="force roast level")
    ap.add_argument("--viewer-url", default=env("VIEWER_URL", DEFAULT_VIEWER_URL), help="address of your hosted roast viewer; the QR then opens it "
                    "with the roast data after the # (default: $VIEWER_URL, else the NAS viewer; use \"\" for plain text)")
    ap.add_argument("--title", default="Bean Me Up", help="text at the top of the label (empty string to hide)")
    ap.add_argument("--shift-x", type=int, default=int(env("SHIFT_X", 0)), help="move print right (+) / left (-) in pixels")
    ap.add_argument("--shift-y", type=int, default=int(env("SHIFT_Y", 0)), help="move print down (+) / up (-) in pixels")
    a = ap.parse_args()

    if a.decode:
        txt = open(a.decode).read() if os.path.isfile(a.decode) else a.decode
        meta, bt = decode(txt)
        print(json.dumps(meta, indent=1, ensure_ascii=False))
        if bt:
            print(f"bean-temp curve: {len(bt)} points every {meta['s']} s, {bt[0]:g} -> {bt[-1]:g} degC")
        return

    paths = []
    for p in (a.paths or [env("ROAST_DIR", ".")]):
        p = os.path.expanduser(p)
        if os.path.isdir(p):
            n = al.newest_alog(p)
            if not n:
                sys.exit(f"No .alog files found in {p}")
            print("Most recent roast:", n)
            paths.append(n)
        else:
            paths.append(p)

    os.makedirs(a.out, exist_ok=True)
    bag_w = a.weight
    if bag_w and bag_w.replace(".", "", 1).isdigit():
        bag_w += "g"
    for p in paths:
        try:
            d = al.load_alog(p)
            r = al.extract(d, a.name)
        except Exception as e:
            print(f"SKIP {p}: {e}", file=sys.stderr)
            continue
        im, info, payload = render(d, r, a.width, a.height, bag_w, a.notes, a.level, a.shift_x, a.shift_y, a.viewer_url, a.title)
        out = os.path.join(a.out, os.path.splitext(os.path.basename(p))[0] + "_qr.png")
        im.save(out, dpi=(al.DPI, al.DPI))
        print(f"wrote {out}")
        print(f"  QR version {info['version']} ({info['modules']} modules at {info['module_px']}px), "
              f"curve every {info['step']} s, {info['chars']} characters"
              + (", tasting notes shortened to fit" if info["notes_truncated"] else ""))
        if info["module_px"] < 3:
            print("  note: modules are only 2px, which may be hard for a phone to scan; shorten the notes or title")
        if a.do_print:
            al.print_label(out, a.printer, im.width, im.height, max(1, a.copies), a.density)


if __name__ == "__main__":
    main()

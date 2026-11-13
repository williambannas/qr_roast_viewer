#!/usr/bin/env python3
"""
artisan_labels.py - make Buddy Beans roast labels from Artisan .alog files, and print them on a Niimbot B1.

Usage:
    python3 artisan_labels.py ROAST_FOLDER --weight 340            # label for the newest roast in the folder
    python3 artisan_labels.py ROAST_FOLDER --weight 340 --print    # ...and print it
    python3 artisan_labels.py ROAST_FOLDER --weight 340 --print -n 6   # print 6 copies (one per bag)
    python3 artisan_labels.py roast.alog --notes "Passion Fruit, Floral" --shift-x 10
    python3 artisan_labels.py a.alog b.alog --out labels           # several specific roasts
    python3 artisan_labels.py roast.alog --level 3                 # override auto roast level

Each path is an .alog file or a folder (a folder means "its newest .alog").
Settings can also come from environment variables so you don't retype them:
    ROAST_DIR  (default path), PRINTER (BLE name), BAG_WEIGHT, SHIFT_X, SHIFT_Y

Requires: pip install pillow ; for --print:  npm i -g @mmote/niimblue-node
Keep the fonts/ folder next to this script. Edit the SETTINGS / TEMPLATE sections to change the design.
"""
import argparse, ast, glob, json, math, os, re, shutil, subprocess, sys
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont

DPI = 203  # Niimbot B1/B21/B3S. Use 300 for B18? (B18 is 203 too); D-series is 203.

FONT_DIRS = [
    "/usr/share/fonts/truetype/dejavu", "/Library/Fonts", "/System/Library/Fonts/Supplemental",
    "C:/Windows/Fonts", os.path.expanduser("~/Library/Fonts"),
]
BOLD = ["Arial Bold.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf", "Helvetica.ttc"]
REG = ["Arial.ttf", "arial.ttf", "DejaVuSans.ttf", "Helvetica.ttc"]


def font(size, bold=False):
    for d in FONT_DIRS:
        for n in (BOLD if bold else REG):
            p = os.path.join(d, n)
            if os.path.exists(p):
                return ImageFont.truetype(p, size)
    return ImageFont.load_default()


# ---------- reading Artisan data ----------
def load_alog(path):
    """.alog is a Python-literal dict (older files) or JSON (newer). Handle both."""
    with open(path, "r", encoding="utf-8") as f:
        txt = f.read()
    try:
        return json.loads(txt)
    except ValueError:
        return ast.literal_eval(txt)


def fmt_time(sec):
    if sec is None:
        return "--"
    sec = int(round(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def num(v, default=None):
    try:
        v = float(v)
        return v if v == v else default
    except (TypeError, ValueError):
        return default


def extract(d, name_override=None):
    c = d.get("computed", {}) or {}
    ti = d.get("timeindex", [0] * 8)
    timex = d.get("timex", [])

    def tidx(i):
        try:
            return timex[ti[i]] if ti[i] else None
        except (IndexError, TypeError):
            return None

    charge_t = tidx(0) or 0
    drop_t = num(c.get("DROP_time")) or tidx(6)
    fcs_t = num(c.get("FCs_time")) or tidx(2)
    total = num(c.get("totaltime")) or (drop_t - charge_t if drop_t is not None else None)
    dev = num(c.get("finishphasetime"))
    if dev is None and drop_t is not None and fcs_t is not None:
        dev = drop_t - fcs_t
    dtr = (dev / total * 100) if dev and total else None

    w = d.get("weight", [0, 0, "g"])
    w_in, w_out, unit = num(w[0], 0), num(w[1], 0), (w[2] if len(w) > 2 else "g")
    loss = (1 - w_out / w_in) * 100 if w_in and w_out else None

    # roast date
    date_str = ""
    iso = d.get("roastisodate")
    if iso:
        date_str = iso
    elif d.get("roastdate"):
        date_str = str(d["roastdate"])
    try:
        date_str = datetime.fromisoformat(date_str).strftime("%Y-%m-%d")
    except ValueError:
        pass

    batch = ""
    if d.get("roastbatchnr"):
        batch = f"{d.get('roastbatchprefix', '')}{d['roastbatchnr']}"

    return {
        "name": name_override or d.get("title") or "Untitled",
        "beans": (d.get("beans") or "").split("\n")[0],
        "date": date_str,
        "batch": batch,
        "w_in": w_in, "w_out": w_out, "unit": unit, "loss": loss,
        "drop_bt": num(c.get("DROP_BT")),
        "total": total, "dev": dev, "dtr": dtr,
        "color": num(d.get("whole_color")),
        "tunit": (d.get("mode") or "C").upper()[:1],
        "fc": fcs_t - charge_t if fcs_t else None,
        "uuid": d.get("roastUUID", ""),
        "notes": (d.get("cuppingnotes") or d.get("beans") or "").strip(),   # Artisan: Cupping Notes, else the Beans box
    }


# ---------- SETTINGS ----------
# Roast level (1 = light ... 4 = dark, in half steps) is inferred from the weight loss %:
# LOSS_AT_LEVEL_1 maps to level 1 and LOSS_AT_LEVEL_4 to level 4, linearly in between,
# then rounded to the nearest 0.5. Edit these two numbers to match how your roaster behaves.
LOSS_AT_LEVEL_1 = 12.0
LOSS_AT_LEVEL_4 = 18.0
LEVEL_NAMES = ("Light", "Medium", "Medium-dark", "Dark")
FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")


def roast_level(loss):
    """Weight-loss % -> roast level 1..4 in 0.5 steps (None if no weight data)."""
    if loss is None:
        return None
    lv = 1 + 3 * (loss - LOSS_AT_LEVEL_1) / (LOSS_AT_LEVEL_4 - LOSS_AT_LEVEL_1)
    return min(4.0, max(1.0, round(lv * 2) / 2))


def level_name(lv):
    if not lv:
        return ""
    lo = int(lv)
    return LEVEL_NAMES[lo - 1] if lv == lo else f"{LEVEL_NAMES[lo - 1]} to {LEVEL_NAMES[lo]}"


# ---------- TEMPLATE ----------
def tfont(name, size, weight=None):
    """Load a bundled font (fonts/ folder next to this script); fall back to a system font."""
    p = os.path.join(FONT_DIR, name + ".ttf")
    if os.path.exists(p):
        f = ImageFont.truetype(p, size)
        if weight:
            try:
                f.set_variation_by_axes([weight])
            except Exception:
                pass
        return f
    return font(size, bold=bool(weight and weight >= 600))


def spaced_width(dr, text, f, sp):
    return sum(dr.textlength(c, font=f) for c in text) + sp * (len(text) - 1)


def draw_spaced(dr, x, y, text, f, sp, fill=0):
    for c in text:
        dr.text((x, y), c, font=f, fill=fill)
        x += dr.textlength(c, font=f) + sp


def wrap_spaced(dr, text, f, sp, maxw):
    lines, cur = [], ""
    for wd in text.split():
        t = (cur + " " + wd).strip()
        if spaced_width(dr, t, f, sp) <= maxw or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = wd
    if cur:
        lines.append(cur)
    return lines


def text_on_circle(img, text, cx, cy, r, f, start, span, fill=0):
    """Place characters along a circle, clockwise from 12 o'clock (angles in degrees)."""
    n = len(text)
    for i, ch in enumerate(text):
        if ch == " ":
            continue
        ang = start + (i + 0.5) * span / n
        g = Image.new("L", (80, 80), 255)
        gd = ImageDraw.Draw(g)
        gd.text((40, 40), ch, font=f, fill=0, anchor="mm")
        g = g.rotate(-ang, resample=Image.BICUBIC, fillcolor=255)
        px = cx + r * math.sin(math.radians(ang))
        py = cy - r * math.cos(math.radians(ang))
        img.paste(0, (int(px - 40), int(py - 40)), Image.eval(g, lambda v: 255 - v))


def pretty_notes(txt):
    parts = [p.strip() for p in re.split(r"(?:\\n|\n|\r|,|;|\s+-\s+)+", txt or "") if p.strip()]
    return " - ".join(p.strip(" !.") for p in parts)


# ---- layout engine: every element lives in a box and its font is shrunk until it fits ----
def line_w(dr, t, f, sp):
    return dr.textlength(t, font=f) if sp == 0 else spaced_width(dr, t, f, sp)


def fit_lines(dr, text, name, weight, bw, bh, max_size, min_size, max_lines, spaced=False, lh=1.25):
    """Largest font (and line wrap) that fits text inside a bw x bh box."""
    words = text.split()
    for size in range(max_size, min_size - 1, -2):
        f = tfont(name, size, weight)
        sp = max(1, round(size * 0.14)) if spaced else 0
        lhp = int(size * lh)
        lines = wrap_spaced(dr, text, f, sp, bw) if spaced else _wrap(dr, words, f, bw)
        if len(lines) == 2:                      # rebalance two-line splits
            best = min(range(1, len(words)),
                       key=lambda i: max(line_w(dr, " ".join(words[:i]), f, sp), line_w(dr, " ".join(words[i:]), f, sp)),
                       default=None)
            if best:
                cand = [" ".join(words[:best]), " ".join(words[best:])]
                if max(line_w(dr, t, f, sp) for t in cand) <= bw:
                    lines = cand
        if len(lines) <= max_lines and len(lines) * lhp <= bh and all(line_w(dr, t, f, sp) <= bw for t in lines):
            return f, sp, lines, lhp
    f = tfont(name, min_size, weight)
    sp = max(1, round(min_size * 0.14)) if spaced else 0
    lhp = int(min_size * lh)
    allw = wrap_spaced(dr, text, f, sp, bw) if spaced else _wrap(dr, words, f, bw)
    keep = max(1, min(max_lines, bh // lhp))
    lines = allw[:keep]
    if len(allw) > keep:                                   # too much text: cut with an ellipsis
        last = lines[-1]
        while last and line_w(dr, last + "\u2026", f, sp) > bw:
            last = last[:-1].rstrip()
        lines[-1] = last + "\u2026"
    return f, sp, lines, lhp


def _wrap(dr, words, f, bw):
    lines, cur = [], ""
    for wd in words:
        t = (cur + " " + wd).strip()
        if dr.textlength(t, font=f) <= bw or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = wd
    if cur:
        lines.append(cur)
    return lines


def draw_block(dr, box, f, sp, lines, lhp):
    x0, y0, x1, y1 = box
    y = y0 + (y1 - y0 - len(lines) * lhp) / 2
    for t in lines:
        w = line_w(dr, t, f, sp)
        if sp:
            draw_spaced(dr, (x0 + x1 - w) / 2, y, t, f, sp)
        else:
            dr.text(((x0 + x1 - w) / 2, y), t, font=f, fill=0)
        y += lhp


def render(r, w_mm=50, h_mm=30, company="Buddy beans", bag_weight=None, notes=None, level=None, debug=False, shift_x=0, shift_y=0):
    W_out, H_out = min(round(w_mm / 25.4 * DPI), 384), round(h_mm / 25.4 * DPI)
    W, H = 768, 480                       # drawn at 2x, scaled down for smooth edges
    img = Image.new("L", (W, H), 255)
    dr = ImageDraw.Draw(img)

    # ---------- zones (x0, y0, x1, y1) ----------
    LEFT_X0, LEFT_X1 = 30, 516            # left column; the stamp owns everything right of it
    Z_HEADER = (30, 20, 738, 118)
    RULE_Y = 126
    Z_NAME = (30, 136, 738, 238)
    Z_NOTES = (LEFT_X0, 244, LEFT_X1, 326)
    Z_ROW = (LEFT_X0, 332, LEFT_X1, 380)          # roast level dots | weight | batch
    STATS_RULE_Y = 388
    Z_STATS = (LEFT_X0, 394, LEFT_X1, 448)        # drop | time | dev
    STAMP = (644, 348, 94)                          # cx, cy, radius

    dr.rounded_rectangle((8, 8, W - 8, H - 8), radius=40, outline=0, width=7)

    # ---------- header ----------
    if company:
        x0, y0, x1, y1 = Z_HEADER
        size = 100
        while True:
            f = tfont("Merienda", size, 600)
            bb = dr.textbbox((0, 0), company, font=f)
            if (bb[2] - bb[0] <= x1 - x0 and bb[3] - bb[1] <= y1 - y0) or size <= 36:
                break
            size -= 2
        dr.text(((x0 + x1 - (bb[2] - bb[0])) / 2 - bb[0], (y0 + y1 - (bb[3] - bb[1])) / 2 - bb[1]), company, font=f, fill=0)
        dr.line((W * 0.17, RULE_Y, W * 0.83, RULE_Y), fill=0, width=5)

    # ---------- coffee name ----------
    zx0, zy0, zx1, zy1 = Z_NAME
    f, sp, lines, lhp = fit_lines(dr, r["name"], "Lora", 700, zx1 - zx0, zy1 - zy0, 58, 26, 2, lh=1.15)
    draw_block(dr, Z_NAME, f, sp, lines, lhp)

    # ---------- tasting notes ----------
    notes_txt = pretty_notes(notes if notes is not None else r.get("notes"))
    if notes_txt:
        zx0, zy0, zx1, zy1 = Z_NOTES
        f, sp, lines, lhp = fit_lines(dr, notes_txt, "Lora", 600, zx1 - zx0, zy1 - zy0, 28, 18, 4, spaced=True, lh=1.4)
        draw_block(dr, Z_NOTES, f, sp, lines, lhp)

    # ---------- row: roast level dots | bag weight | batch ----------
    lvl = level or roast_level(r["loss"])
    zx0, zy0, zx1, zy1 = Z_ROW
    texts = ([bag_weight] if bag_weight else []) + ([f"batch: {str(r['batch']).lstrip('#')}"] if r["batch"] else [])
    for ts in range(38, 17, -2):
        tf_ = tfont("Lora", ts, 500)
        dd, dgap, tgap = int(ts * 1.0), int(ts * 0.3), int(ts * 0.8)
        twid = [dr.textbbox((0, 0), t, font=tf_)[2] for t in texts]
        total = 4 * dd + 3 * dgap + sum(tgap + w for w in twid)
        if total <= zx1 - zx0 - 12 and dd <= zy1 - zy0:
            break
    cy = (zy0 + zy1) / 2
    x = (zx0 + zx1 - total) / 2
    lw = max(3, dd // 9)
    for i in range(4):
        box = (x, cy - dd / 2, x + dd, cy + dd / 2)
        dr.ellipse(box, outline=0, width=lw, fill=0 if lvl == i + 1 else 255)
        if lvl == i + 1.5:                                  # x.5 = halfway between this dot and the next
            dr.pieslice(box, -90, 90, fill=0)
        elif lvl == i + 0.5:
            dr.pieslice(box, 90, 270, fill=0)
        dr.ellipse(box, outline=0, width=lw)
        x += dd + dgap
    for t, w in zip(texts, twid):
        x += tgap
        dr.text((x, cy), t, font=tf_, fill=0, anchor="lm")
        x += w

    # ---------- stats: drop temp | roast time | development % ----------
    dr.line((LEFT_X0, STATS_RULE_Y, LEFT_X1, STATS_RULE_Y), fill=0, width=3)
    parts = [("Drop", f"{r['drop_bt']:.0f}\u00b0{r['tunit']}" if r["drop_bt"] else "--"),
             ("Time", fmt_time(r["total"])),
             ("Dev", f"{r['dtr']:.0f}%" if r["dtr"] else "--")]
    zx0, zy0, zx1, zy1 = Z_STATS
    for vs in range(38, 17, -2):
        vf, sf = tfont("Lora", vs, 700), tfont("Lora", int(vs * 0.72), 500)
        wids = [dr.textlength(k + " ", font=sf) + dr.textlength(v, font=vf) for k, v in parts]
        sep = (zx1 - zx0 - sum(wids)) / 3
        if sep >= vs * 0.7 and vs <= zy1 - zy0:
            break
    cy = (zy0 + zy1) / 2
    x = zx0 + sep / 2
    for i, ((k, v), w) in enumerate(zip(parts, wids)):
        dr.text((x, cy), k + " ", font=sf, fill=0, anchor="lm")
        dr.text((x + dr.textlength(k + " ", font=sf), cy), v, font=vf, fill=0, anchor="lm")
        x += w
        if i < 2:
            dr.line((x + sep / 2, cy - vs * 0.5, x + sep / 2, cy + vs * 0.5), fill=0, width=3)
            x += sep

    # ---------- roast-date stamp ----------
    sx, sy, sr = STAMP
    dr.ellipse((sx - sr, sy - sr, sx + sr, sy + sr), outline=0, width=5)
    rf = tfont("Lora", 24, 700)
    for start in (-80, 100):
        text_on_circle(img, "roast date", sx, sy, sr - 20, rf, start, 150)
    try:
        dt = datetime.strptime(r["date"], "%Y-%m-%d")
        ds = f"{dt.month}/{dt.day}"
    except ValueError:
        ds = r["date"][-5:]
    df = tfont("Merienda", 34, 500)
    dr.text((sx, sy), ds, font=df, fill=0, anchor="mm")

    if debug:                                           # show the layout zones
        for z in (Z_HEADER, Z_NAME, Z_NOTES, Z_ROW, Z_STATS):
            dr.rectangle(z, outline=0, width=1)

    render.preview = img.copy()                          # full-res grayscale, for --preview
    # Print alignment: shift the label (px at 203 dpi; +x = right, +y = down). The design is scaled
    # down just enough to stay fully on the label, so nothing gets clipped.
    sc = min((W_out - abs(shift_x)) / W_out, (H_out - abs(shift_y)) / H_out)
    cw, ch = round(W_out * sc), round(H_out * sc)
    img = img.resize((cw, ch), Image.LANCZOS).point(lambda p: 255 if p > 150 else 0)
    out = Image.new("L", (W_out, H_out), 255)
    px = max(shift_x, 0) + (W_out - abs(shift_x) - cw) // 2
    py = max(shift_y, 0) + (H_out - abs(shift_y) - ch) // 2
    out.paste(img, (px, py))
    return out.convert("1")


def newest_alog(folder):
    files = glob.glob(os.path.join(os.path.expanduser(folder), "*.alog"))
    return max(files, key=os.path.getmtime) if files else None


MAX_DENSITY = 5   # Niimbot B1 density range is 1-5; the CLI default is 3


def print_label(png, printer, width, height, copies=1, density=MAX_DENSITY):
    exe = shutil.which("niimblue-cli")
    if not exe:
        sys.exit("niimblue-cli not found. Install it with: npm i -g @mmote/niimblue-node")
    cmd = [exe, "print", "-t", "ble", "-a", printer, "-p", "B1", "-o", "top",
           "-w", str(width), "-h", str(height), "-q", str(density), "-n", str(copies), png]
    print(f"Printing {png} -> {printer}  (density {density}, {copies} cop{'y' if copies == 1 else 'ies'})")
    if subprocess.run(cmd).returncode != 0:
        sys.exit("Print failed (is the printer on, in range, and is Bluetooth allowed for Terminal?)")


def main():
    env = os.environ.get
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", help=".alog files and/or folders (folder = its newest .alog); default $ROAST_DIR")
    ap.add_argument("--print", dest="do_print", action="store_true", help="print the label on the Niimbot")
    ap.add_argument("-n", "--copies", type=int, default=1, help="number of labels to print (with --print)")
    ap.add_argument("--density", type=int, default=MAX_DENSITY, choices=range(1, 6), help="print darkness 1-5 (default: 5, the maximum)")
    ap.add_argument("--printer", default=env("PRINTER", "B1-H809121559"), help="Niimbot Bluetooth name (see: niimblue-cli scan -t ble)")
    ap.add_argument("--out", default="labels", help="output folder")
    ap.add_argument("--width", type=float, default=50, help="label width mm")
    ap.add_argument("--height", type=float, default=30, help="label height mm")
    ap.add_argument("--name", help="override coffee name on label")
    ap.add_argument("--company", default="Buddy beans", help="header text (empty string to hide)")
    ap.add_argument("--weight", default=env("BAG_WEIGHT"), help='bag weight, e.g. 340 or "12oz" (plain numbers get "g")')
    ap.add_argument("--notes", help='tasting notes, e.g. "Passion Fruit, Blue Raspberry, Floral" '
                                    "(default: read from Artisan - Cupping Notes, else the Beans box)")
    ap.add_argument("--preview", action="store_true", help="also save a 2x smooth grayscale preview (NOT for printing)")
    ap.add_argument("--shift-x", type=int, default=int(env("SHIFT_X", 0)), help="move print right (+) or left (-) by N pixels")
    ap.add_argument("--shift-y", type=int, default=int(env("SHIFT_Y", 0)), help="move print down (+) or up (-) by N pixels")
    ap.add_argument("--debug", action="store_true", help="draw the layout boxes (for checking fit)")
    ap.add_argument("--level", type=float, choices=[1, 1.5, 2, 2.5, 3, 3.5, 4], help="force roast level (default: from weight loss)")
    a = ap.parse_args()

    paths = []
    for p in (a.paths or [env("ROAST_DIR", ".")]):
        p = os.path.expanduser(p)
        if os.path.isdir(p):
            n = newest_alog(p)
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
            r = extract(load_alog(p), a.name)
        except Exception as e:
            print(f"SKIP {p}: {e}", file=sys.stderr)
            continue
        lv = a.level or roast_level(r["loss"])
        if lv:
            print(f"  roast level {lv:g} ({level_name(lv)})" + (f", weight loss {r['loss']:.1f}%" if r["loss"] else ""))
        out = os.path.join(a.out, os.path.splitext(os.path.basename(p))[0] + ".png")
        im = render(r, a.width, a.height, a.company, bag_w, a.notes, a.level, a.debug, a.shift_x, a.shift_y)
        im.save(out, dpi=(DPI, DPI))
        print("wrote", out)
        if a.preview:
            pv = out[:-4] + "_preview.png"
            render.preview.save(pv)
            print("wrote", pv, "(preview only - print the other file)")
        if a.do_print:
            print_label(out, a.printer, im.width, im.height, max(1, a.copies), a.density)


if __name__ == "__main__":
    main()

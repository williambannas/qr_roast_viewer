# Bean Me Up roast labels

Make Niimbot B1 labels from [Artisan](https://artisan-scope.org) `.alog` roast files, and view a roast by
scanning the label's QR code.

- `artisan_qr_label.py` - label with the roast time (UTC), bag weight and a QR code holding the whole roast
  (details + bean-temperature curve). Scanning it opens the viewer page.
- `artisan_labels.py` - the original text label (name, notes, roast level, stats), and shared helpers.
- `docs/index.html` - the viewer page (static, runs entirely in the browser, hosted on Cloudflare Pages).
- `fonts/` - fonts used by the labels. Keep this folder next to the scripts.

## Set up

```
pip install -r requirements.txt
npm i -g @mmote/niimblue-node      # only needed for --print
```

## Make and print labels

```
python3 artisan_qr_label.py ~/Documents/Roasts/Bullet --weight 340            # newest roast in the folder
python3 artisan_qr_label.py ~/Documents/Roasts/Bullet --weight 340 --print -n 6
python3 artisan_labels.py   ~/Documents/Roasts/Bullet --weight 340 --notes "Passion Fruit, Floral"
```

Useful options: `--shift-x/--shift-y` (nudge the print), `--density` (default 5), `--printer` (Bluetooth name),
`--viewer-url` (where the QR points), `--title`, `--decode "<scanned text>"`. Run with `--help` for all of them.
Settings can also come from the environment: `VIEWER_URL`, `PRINTER`, `BAG_WEIGHT`, `ROAST_DIR`, `SHIFT_X`, `SHIFT_Y`.

## The viewer page (Cloudflare Pages)

The QR code holds a link like `https://<your-project>.pages.dev/#<compressed roast data>`. The data sits after the
`#`, so the browser reads it and the server never receives it. The page draws the roast and the bean-temp and
rate-of-rise curves. It is plain static files in `docs/`: no build step.

To publish it: in the Cloudflare dashboard open **Workers & Pages > Create application > Pages > Connect to Git**,
pick this repository, leave the **build command** empty and set the **build output directory** to `docs`.
Every push to `main` then redeploys the page. `docs/_headers` adds a few security headers.

The address ends up on every printed label, so choose it with care: the project name decides the `*.pages.dev`
hostname (and cannot be renamed later), or attach a custom domain of your own in the project's settings.
Then point the labels at it:

```
export VIEWER_URL=https://<your-project>.pages.dev/
```

or change `DEFAULT_VIEWER_URL` in `artisan_qr_label.py`.

## What is in the QR code

Compressed text: coffee name, roast start (UTC), batch, tasting notes, bag weight, weights in and out, roast level,
charge / drop / first-crack temperatures, event times, and the bean temperature every 5 seconds in whole degrees C.
Roast time, development and rate of rise are worked out by the viewer.

## Fonts

IBM Plex Mono, Lora and Merienda, all from Google Fonts, under the SIL Open Font License. See the upstream
projects for the license text.

# color_palette_creation

Watcher that takes paired images (original + marked), extracts color swatches from the marked differences, generates mix suggestions for a limited paint palette, and writes a per-image PDF report.

## How it works (high level)
- Drop two files into the input folder:
  - `NAME.jpg`
  - `NAME_x.jpg` (marked)
- The watcher detects the pair and processes it.
- Outputs go to `processed/NAME/NAME_report.pdf` plus charts/palettes.

## Configuration
This project uses an INI file (mounted into the containers):
- Container path: `/config/color_palette_config.ini`

Example config is at:
- `config/color_palette_config.ini.example`

### Important settings
- `[watch] marked_suffix=_x`
- `[outputs] palette_include_variants=false|true`
- `[mix] step_pct=2.5`, `max_pigments=4`, black fallback controls
- `[paints]` / `[paints.enabled]` to control what paints the solver uses
- `[drive] enabled=true` to upload outputs via rclone (optional)

## Docker compose (portable)
The compose file is written to be portable and not tied to any specific NAS paths.

1) Copy env example:
- `cp .env.example .env`

2) Start:
- `cd docker && docker compose up -d --build`

By default it will create a local `./data/` folder for inputs/outputs/config.

### Security note (rclone)
If you enable Drive uploads, `rclone.conf` contains OAuth tokens. **Do not commit it.**
Mount it into the container as read-only (see `.env.example`).

## Output layout
For a base name `example`:
- `/mnt/nas2/Color_Palette_Creation/processed/example/example_report.pdf`
- `/mnt/nas2/Color_Palette_Creation/processed/example/charts/*.png`
- `/mnt/nas2/Color_Palette_Creation/processed/example/palettes/*_palette-swatches.jpeg`

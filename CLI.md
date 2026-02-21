# CLI Usage Guide

## Setup

Always run commands from the **project root** (where `pyproject.toml` lives), not from `src/`:

```bash
cd ~/Desktop/emya/projects/bom-manager
python3 -m bom_manager.interfaces.main --help
```

Or if installed via pip:

```bash
bom --help
```

---

## Command structure

Every command follows the same pattern:

```
bom  <group>  <subcommand>  <arguments>
      ↑           ↑              ↑
   project      create        "my-name"
   version       list
   bom            add
                delete
```

**The project and version name always go AFTER the subcommand**, not before it.

```bash
# ✓ Correct
bom bom list blight v1

# ✗ Wrong
bom bom blight list v1
```

---

## Typical workflow

### 1 — Create a project

```bash
bom project create blight
bom project create blight --description "RGBW LED controller"
```

### 2 — Create a version

```bash
bom version create blight v1
bom version create blight v1 --notes "First prototype"
```

### 3 — Add parts to the BOM

```bash
bom bom add blight v1 "ESP32-S3-WROOM" --qty 1 --ref U1
```

This will:
1. Search LCSC and show a table of results
2. Ask you to type a number to pick one
3. Show the price breaks for that part
4. Ask "Add to BOM? [y/n]"

The `--ref` flag is the PCB reference designator (e.g. `U1`, `C3`, `R12`).
If you skip it, the part name is used instead.

```bash
bom bom add blight v1 "100nF 0402" --qty 10 --ref "C1,C2,C3"
bom bom add blight v1 "AMS1117-3.3" --qty 1 --ref U2
```

### 4 — See the BOM

```bash
bom bom list blight v1
```

### 5 — Calculate cost for a production run

```bash
# Cost for 1 board (default)
bom bom cost blight v1

# Cost for 50 boards (automatically uses bulk price tiers)
bom bom cost blight v1 --boards 50
```

### 6 — Export

```bash
bom bom export blight v1                    # → exports/blight_v1.csv
bom bom export blight v1 --format xlsx     # → exports/blight_v1.xlsx
bom bom export blight v1 --output-dir ~/Desktop
```

### 7 — Fix a quantity

Copy the 8-character item ID from `bom bom list`, then:

```bash
bom bom update-qty blight v1 a1b2c3d4 5
```

### 8 — Remove a part

```bash
bom bom remove blight v1 a1b2c3d4
```

---

## All commands at a glance

```
bom project create  <name> [--description "..."]
bom project list
bom project delete  <name>

bom version create  <project>  <version>  [--notes "..."]
bom version list    <project>

bom bom add         <project>  <version>  "<part name>"  --qty N  [--ref D]
bom bom list        <project>  <version>
bom bom remove      <project>  <version>  <item-id>
bom bom update-qty  <project>  <version>  <item-id>  <new-qty>
bom bom cost        <project>  <version>  [--boards N]
bom bom export      <project>  <version>  [--format csv|xlsx]  [--output-dir DIR]
```

> **Item ID** — the 8-character code shown in the rightmost column of `bom bom list`.
> You only need to type enough characters to be unique (usually 4–5 is fine).

---

## Example session

```bash
$ bom project create blight --description "RGBW LED controller"
✓ Created project blight  id=9b0c3473

$ bom version create blight v1 --notes "First spin"
✓ Created version v1 for project blight  id=a3f1...

$ bom bom add blight v1 "ESP32-S3-WROOM" --qty 1 --ref U1
Searching LCSC for "ESP32-S3-WROOM"...
╭───┬──────────────────────┬──────────┬──────────────┬────────────────╮
│ # │ MPN                  │ LCSC #   │ Manufacturer │ Description    │
├───┼──────────────────────┼──────────┼──────────────┼────────────────┤
│ 1 │ ESP32-S3-WROOM-1-N8  │ C2913198 │ ESPRESSIF    │ 2.4GHz Wi-Fi…  │
│ 2 │ ESP32-S3-WROOM-1-N4  │ C2913197 │ ESPRESSIF    │ 2.4GHz Wi-Fi…  │
╰───┴──────────────────────┴──────────┴──────────────┴────────────────╯
Select part [1/2]: 1
...
✓ Added ESP32-S3-WROOM-1-N8 × 1 @ $5.4203  (C2913198)

$ bom bom list blight v1
╭─────┬────────────────────┬──────────────────────┬──────────┬─────┬──────────┬──────────┬──────────╮
│ Ref │ Part Name          │ MPN                  │ LCSC #   │ Qty │ Unit     │ Total    │ Item ID  │
├─────┼────────────────────┼──────────────────────┼──────────┼─────┼──────────┼──────────┼──────────┤
│ U1  │ ESP32-S3-WROOM     │ ESP32-S3-WROOM-1-N8  │ C2913198 │   1 │ $5.4203  │ $5.4203  │ f8a2c1d0 │
╰─────┴────────────────────┴──────────────────────┴──────────┴─────┴──────────┴──────────┴──────────╯
  1 item  ·  Total: $5.4203

$ bom bom cost blight v1 --boards 100
...
  Per-board cost:  $5.4203
  Total (100 boards):  $542.03

$ bom bom export blight v1 --format csv
✓ Exported blight / v1  →  /home/.../exports/blight_v1.csv
```

---

## Common mistakes

| What you typed | What you meant |
|---|---|
| `bom bom blight list v1` | `bom bom list blight v1` |
| `bom bom add blight ESP32 --qty 1` | `bom bom add blight v1 "ESP32" --qty 1` |
| `bom project bom add ...` | `bom bom add ...` |
| `-d white led controller` (no quotes) | `-d "white led controller"` |

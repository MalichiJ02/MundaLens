"""Check how well MundaLens tracks real yields. Reports only; never edits parameters.

Usage:  python calibrate.py data/yield_observations.csv

CSV columns: lat, lon, season_start, season_end, crop, yield_t_ha
(ISO dates; one row per field-season you trust. District statistics also work.)

For each row the water-balance model is run for that season at that point and
the modelled relative yield is compared with the observed yield, scaled to the
best observed yield for the same crop. The Spearman rank correlation per crop
says whether the model orders good and bad seasons correctly. If it does, you
may nudge a crop's ky in data/calibration.json, for example
{"ky_scale": {"Maize": 0.9}, "rotation_scale": 1.0}, within 0.5 to 1.5.
"""

import csv
import sys
from collections import defaultdict
from datetime import date, timedelta

import engine
from app import daily_climate


def spearman(xs, ys):
    def ranks(values):
        order = sorted(range(len(values)), key=lambda i: values[i])
        result = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            for k in range(i, j + 1):
                result[order[k]] = (i + j) / 2 + 1
            i = j + 1
        return result

    rx, ry = ranks(xs), ranks(ys)
    n = len(xs)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den else None


def model_yield(row):
    start, end = date.fromisoformat(row["season_start"]), date.fromisoformat(row["season_end"])
    spin = start - timedelta(days=engine.SPINUP_DAYS)
    days = daily_climate(float(row["lat"]), float(row["lon"]), spin, end, False, 800)["days"]
    window_start = engine.SPINUP_DAYS
    onset = engine.find_onset(days[window_start:])
    plant = window_start + (onset or 0)
    sim = engine.simulate_crop(row["crop"], days, float(row["lat"]), plant, "unknown", False, window_start=window_start,
                               apply_soil_observation=False)
    return sim["rel_yield"]


def main(path):
    by_crop = defaultdict(list)
    with open(path, newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            if row["crop"] not in engine.CROP_DB:
                print("Skipping unknown crop", row["crop"])
                continue
            by_crop[row["crop"]].append((model_yield(row), float(row["yield_t_ha"])))
    for crop, pairs in sorted(by_crop.items()):
        if len(pairs) < 6:
            print(f"{crop}: {len(pairs)} seasons, need at least 6 for a meaningful check.")
            continue
        rho = spearman([m for m, _ in pairs], [o for _, o in pairs])
        verdict = "orders seasons well" if rho is not None and rho >= 0.6 else "weak, review the inputs before trusting the ranking"
        print(f"{crop}: {len(pairs)} seasons, rank correlation {rho:.2f} ({verdict})")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])

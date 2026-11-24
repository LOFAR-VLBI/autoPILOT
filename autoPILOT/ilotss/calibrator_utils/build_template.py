"""
Build a template of median calibration solutions across all LOFAR calibrator
observations, using the combined cal_solutions.h5 file produced by LINC.

Saves results to an HDF5 cache file for use by assess_calibrators.py.

Usage:
    python -m autoPILOT.ilotss.calibrator_utils.build_template \
        --input-glob '/data/calibrators/L*/cal_solutions.h5' --output-dir /data/template

-----------------------------------------------------------------------
Structure of cal_solutions.h5
-----------------------------------------------------------------------
All solution tables live under the top-level group  calibrator/

  calibrator/source          (4,)  structured dtype {name: S128, dir: 2×f4}
      The calibrator source name (e.g. b'3C196') is in source['name'][0].

  calibrator/antenna         (288,) structured dtype {name: S16, position: 3×f4}
      Full antenna table (all stations, including international).  Not used
      directly; per-solution antenna lists are stored alongside each table.

  ── calibrator/bandpass ──────────────────────────────────────────────
  ant    (72,)  |S9          station names for this table
  freq   (245,) float64      frequency axis, Hz
  pol    (2,)   |S2          [b'XX', b'YY']
  time   (150,) float64      MJD seconds (150 time slots)
  val    (150, 245, 72, 2)   float64  axes: time × freq × ant × pol
  weight (150, 245, 72, 2)   float64
      The bandpass amplitude solutions.  Values vary meaningfully with
      frequency (spectral shape) and are stable in time within one obs
      (std of time-median ~ 0).  Template: median over time+pol → (245,)
      spectrum per antenna.

  ── calibrator/clock ─────────────────────────────────────────────────
  ant    (72,)  |S9
  time   (150,) float64
  val    (150, 72) float64   axes: time × ant
      Clock delay in seconds.  CONSTANT IN TIME — all 150 time slots
      contain the identical value for each antenna (verified: std = 0).
      Template: val[0, ai] × 1e9  (convert to nanoseconds), then subtract
      the global median across all CS/RS stations to get relative offsets.

  ── calibrator/polalign ──────────────────────────────────────────────
  ant    (72,)  |S9
  freq   (924,) float64      finer frequency grid than bandpass
  pol    (2,)   |S2          [b'XX', b'YY']
  time   (150,) float64
  val    (150, 72, 924, 2)   float64  axes: time × ant × freq × pol
      Polarisation alignment solutions.  Two very different behaviours:
        XX pol (index 0):  REFERENCE — fixed at zero for all 150 time slots
                           and all 924 freq channels.  Not a physical solution;
                           simply the reference polarisation.  IGNORED.
        YY pol (index 1):  the actual X-Y polarisation misalignment.  Varies
                           with frequency as a PERFECT LINEAR GRADIENT
                           (fit residual std ~ 1e-16 rad, i.e. machine precision):
                               YY(ν) = c₀ + c₁ × ν
                           c₁ (rad/Hz) encodes the differential delay between
                           the X and Y signal paths, a hardware property of
                           each station.  Template: τ = c₁ / (2π) × 1e9  (ns),
                           extracted via np.unwrap + np.polyfit on val[0, ai, :, 1].

  ── calibrator/faraday ───────────────────────────────────────────────
  ant    (72,)  |S9
  time   (150,) float64
  val    (72, 150) float64   axes: ant × time   (note transposed vs clock!)
      Ionospheric Faraday rotation in rad/m².  Values are tiny (~0.001 rad/m²)
      and vary slowly in time.  IGNORED in this template.
-----------------------------------------------------------------------
"""
import os
import re
import h5py
import numpy as np
import matplotlib
import glob
import math
import argparse
import json
from datetime import datetime
from pathlib import Path

matplotlib.use("Agg")
import matplotlib.pyplot as plt

CALIBRATORS_3C = {
    '3C48':  (24.4221,  33.1598),
    '3C147': (85.6500,  49.8520),
    '3C196': (123.4009, 48.2174),
    '3C295': (212.8355, 52.2028),
}

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def savefig(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    plt.tight_layout()
    plt.savefig(path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"wrote {path}")


def is_station_of_interest(ant_name):
    return ant_name.startswith('RS') or ant_name.startswith('CS')


def get_h5_files(pattern):
    """Return sorted list of cal_solutions.h5 paths."""
    return sorted(path for path in glob.glob(pattern, recursive=True) if os.path.isfile(path))


def get_obs_id(h5_path):
    """Extract the observation ID from a run directory."""
    path = Path(h5_path)
    for parent in path.parents:
        if match := re.search(r'(L\d{6,})', parent.name):
            return match.group(1)
    return path.parent.name


def get_calibrator_from_h5(h5_path):
    """
    Read calibrator name directly from calibrator/source in the h5 file.
    Falls back to parsing the JSON summary filename in the same directory.
    """
    try:
        with h5py.File(h5_path, 'r') as h:
            name = h['calibrator/source']['name'][0]
            if isinstance(name, bytes):
                name = name.decode()
            return name.strip().upper()
    except Exception:
        pass
    # fallback: JSON summary in the same directory
    parent = os.path.dirname(h5_path)
    json_files = glob.glob(os.path.join(parent, '*_LINC_calibrator_summary.json'))
    if json_files:
        fname = os.path.basename(json_files[0])
        return fname.split('_LINC_calibrator_summary.json')[0].upper()
    return 'Unknown'


def get_obs_time(h5_path):
    """Extract observation datetime from directory name timestamp."""
    check = os.path.abspath(h5_path)
    for _ in range(6):
        check = os.path.dirname(check)
        m = re.search(r'(\d{4}_\d{2}_\d{2}-\d{2}_\d{2}_\d{2})', os.path.basename(check))
        if m:
            try:
                return datetime.strptime(m.group(1), '%Y_%m_%d-%H_%M_%S')
            except ValueError:
                pass
    return None


def calculate_elevation(ra_deg, dec_deg, obs_time, lon_deg=6.87, lat_deg=52.92):
    try:
        from astropy.coordinates import EarthLocation, AltAz, SkyCoord
        from astropy.time import Time
        import astropy.units as u
        location = EarthLocation(lon=lon_deg*u.deg, lat=lat_deg*u.deg)
        target = SkyCoord(ra=ra_deg*u.deg, dec=dec_deg*u.deg)
        altaz = target.transform_to(AltAz(obstime=Time(obs_time), location=location))
        return float(altaz.alt.deg)
    except Exception:
        return None

def yy_to_delay_ns(freq_hz, yy_phase):
    """
    Convert a YY polalign phase spectrum to a differential X-Y delay in nanoseconds.

    Masks non-finite samples channel-by-channel, unwraps the phase, then fits
    a line:  φ(ν) = c₀ + c₁ν.  The delay follows the radio convention
    φ(ν) = −2π τ ν, so  τ = −c₁ / (2π).

    Returns np.nan if fewer than 2 finite channels are available.
    """
    mask = np.isfinite(freq_hz) & np.isfinite(yy_phase)
    f, p = freq_hz[mask], yy_phase[mask]
    if len(f) < 2:
        return np.nan
    slope, _intercept = np.polyfit(f, np.unwrap(p), 1)
    return -slope / (2.0 * np.pi) * 1e9   # nanoseconds


# ---------------------------------------------------------------------------
# Per-solution-type builders  (all read from cal_solutions.h5)
# ---------------------------------------------------------------------------

def build_median_bandpass(h5_files, obs_to_calibrator):
    """
    Bandpass: calibrator/bandpass/val  shape (time, freq, ant, pol)
    Median over time axis and both pols → one spectrum (245 channels) per antenna.
    """
    print("Processing bandpass...")
    ant_data = {}
    for h5_path in h5_files:
        obs_id     = get_obs_id(h5_path)
        calibrator = obs_to_calibrator.get(obs_id, 'Unknown')
        try:
            with h5py.File(h5_path, 'r') as h:
                ant_names = [a.decode() if isinstance(a, bytes) else a
                             for a in h['calibrator/bandpass/ant'][...]]
                freq = h['calibrator/bandpass/freq'][...]       # (245,)
                val  = h['calibrator/bandpass/val'][...]        # (time, freq, ant, pol)
            # median over time (axis 0) and pol (axis 3) → (freq, ant)
            sp_all = np.nanmedian(val, axis=(0, 3))             # (245, 72)
            for ai, an in enumerate(ant_names):
                if not is_station_of_interest(an): continue
                sp = sp_all[:, ai]
                if np.all(np.isnan(sp)): continue
                ant_data.setdefault(an, []).append((freq, sp, calibrator))
        except Exception as e:
            print(f"  Error reading bandpass from {h5_path}: {e}")

    median_bandpass = {}
    for ant_name, entries in ant_data.items():
        common_freq = entries[0][0]
        cal_spectra = {}
        for f, sp, cal in entries:
            if len(sp) == len(common_freq) and not np.all(np.isnan(sp)):
                cal_spectra.setdefault(cal, []).append(sp)
        cal_results = {}
        for cal, spectra in cal_spectra.items():
            arr = np.array(spectra)
            med = np.nanmedian(arr, axis=0)
            mad = np.nanmedian(np.abs(arr - med), axis=0)
            cal_results[cal] = (common_freq, med, mad, len(spectra))
            print(f"  {ant_name} [{cal}]: {len(spectra)} obs")
        if cal_results:
            median_bandpass[ant_name] = cal_results
    return median_bandpass


def build_median_polalign(h5_files, obs_to_calibrator):
    """
    Polalign: calibrator/polalign/val  shape (time, ant, freq, pol)
    XX pol (index 0) is a reference fixed at zero — not a physical solution.
    YY pol (index 1) encodes the actual X-Y polarisation misalignment as a
    function of frequency.  It follows a perfect linear trend:

        YY(ν) = c₀ + c₁ × ν

    where c₁ (rad/Hz) is the differential delay between the X and Y signal
    paths.  Converting: τ = c₁ / (2π)  →  stored in nanoseconds.
    Template stores one delay value (ns) per antenna per calibrator.
    """
    print("Processing polalign...")
    ant_data = {}
    for h5_path in h5_files:
        obs_id     = get_obs_id(h5_path)
        calibrator = obs_to_calibrator.get(obs_id, 'Unknown')
        try:
            with h5py.File(h5_path, 'r') as h:
                ant_names = [a.decode() if isinstance(a, bytes) else a
                             for a in h['calibrator/polalign/ant'][...]]
                val  = h['calibrator/polalign/val'][...]   # (time, ant, freq, pol)
                freq = h['calibrator/polalign/freq'][...]  # (freq,) Hz
            for ai, an in enumerate(ant_names):
                if not is_station_of_interest(an): continue
                yy = val[0, ai, :, 1].astype(float)   # YY at first time slice
                delay_ns = yy_to_delay_ns(freq, yy)
                if np.isnan(delay_ns): continue
                ant_data.setdefault(an, []).append((delay_ns, calibrator))
        except Exception as e:
            print(f"  Error reading polalign from {h5_path}: {e}")

    median_polalign = {}
    for ant_name, entries in ant_data.items():
        cal_vals = {}
        for v, cal in entries:
            cal_vals.setdefault(cal, []).append(v)
        cal_results = {}
        for cal, vals in cal_vals.items():
            arr = np.array(vals)
            med = float(np.nanmedian(arr))
            mad = float(np.nanmedian(np.abs(arr - med)))
            cal_results[cal] = (np.arange(len(arr)), arr, np.full(len(arr), mad))
            print(f"  {ant_name} [{cal}]: {len(vals)} obs  median={med:.4f} ns  MAD={mad:.4f} ns")
        if cal_results:
            median_polalign[ant_name] = cal_results
    return median_polalign


def build_median_clock(h5_files, obs_to_calibrator):
    """
    Clock: calibrator/clock/val  shape (time, ant)
    Constant in time (all 150 slots identical, verified std = 0).
    Template stores one value per antenna per calibrator in nanoseconds,
    relative to the global median across all CS/RS stations (to remove
    any common-mode offset between observations).
    """
    print("Processing clock...")
    ant_data = {}
    for h5_path in h5_files:
        obs_id     = get_obs_id(h5_path)
        calibrator = obs_to_calibrator.get(obs_id, 'Unknown')
        try:
            with h5py.File(h5_path, 'r') as h:
                ant_names = [a.decode() if isinstance(a, bytes) else a
                             for a in h['calibrator/clock/ant'][...]]
                val = h['calibrator/clock/val'][...]             # (time, ant)
            # constant in time — use first slot, convert s → ns
            clk_ns = val[0, :] * 1e9
            station_meds = [clk_ns[i] for i, a in enumerate(ant_names)
                            if is_station_of_interest(a) and np.isfinite(clk_ns[i])]
            global_offset = float(np.nanmedian(station_meds)) if station_meds else 0.0
            for ai, an in enumerate(ant_names):
                if not is_station_of_interest(an): continue
                v = clk_ns[ai] - global_offset
                if not np.isfinite(v): continue
                ant_data.setdefault(an, []).append((v, calibrator))
        except Exception as e:
            print(f"  Error reading clock from {h5_path}: {e}")

    median_clock = {}
    for ant_name, entries in ant_data.items():
        cal_vals = {}
        for v, cal in entries:
            cal_vals.setdefault(cal, []).append(v)
        cal_results = {}
        for cal, vals in cal_vals.items():
            arr = np.array(vals)
            mad = float(np.nanmedian(np.abs(arr - np.nanmedian(arr))))
            cal_results[cal] = (np.arange(len(arr)), arr, np.full(len(arr), mad))
        if cal_results:
            median_clock[ant_name] = cal_results
    return median_clock


def build_median_flagging(h5_files, obs_to_calibrator):
    """
    Overall flagging fraction from the JSON summary files.
    Uses percentage_flagged['final'] per station — the fraction of data
    flagged after the full LINC calibrator pipeline.
    Returns a single-key dict: {'final': {ant_name: {calibrator: (x, vals, mads)}}}
    """
    print("Processing flagging from JSON summaries...")
    raw = {}  # ant -> calibrator -> [final_%]
    for h5_path in h5_files:
        obs_id     = get_obs_id(h5_path)
        calibrator = obs_to_calibrator.get(obs_id, 'Unknown')
        parent = os.path.dirname(h5_path)
        json_files = glob.glob(os.path.join(parent, '*_LINC_calibrator_summary.json'))
        if not json_files:
            continue
        try:
            with open(json_files[0]) as f:
                d = json.load(f)
            for entry in d['metrics']['LINC']['stations']:
                an = entry.get('station', '')
                if not is_station_of_interest(an): continue
                v = entry.get('percentage_flagged', {}).get('final', np.nan)
                if np.isnan(v): continue
                raw.setdefault(an, {}).setdefault(calibrator, []).append(v)
        except Exception as e:
            print(f"  Error reading {json_files[0]}: {e}")

    ant_cal_data = {}
    for ant_name, cal_dict in raw.items():
        for cal, vals in cal_dict.items():
            arr = np.array(vals)
            mad = float(np.nanmedian(np.abs(arr - np.nanmedian(arr))))
            ant_cal_data.setdefault(ant_name, {})[cal] = (
                np.arange(len(arr)), arr, np.full(len(arr), mad))
    return {'final': ant_cal_data}

# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------

def _plot_scalar_solutions(median_data, ylabel, title, outpath):
    if not median_data: return
    ant_names = sorted(median_data.keys())
    all_calibrators = sorted({cal for d in median_data.values() for cal in d})
    ncols = 2
    nrows = math.ceil(len(all_calibrators) / ncols)
    fig, axes = plt.subplots(nrows, ncols, figsize=(14, 5 * nrows), squeeze=False)
    for cal_idx, cal in enumerate(all_calibrators):
        ax = axes[cal_idx // ncols][cal_idx % ncols]
        plot_names, plot_medians, plot_mads = [], [], []
        for an in ant_names:
            if cal not in median_data[an]: continue
            _, vals, mads = median_data[an][cal]
            plot_names.append(an)
            plot_medians.append(np.nanmedian(vals))
            plot_mads.append(np.nanmedian(mads))
        n_obs = max((len(median_data[an][cal][1]) for an in ant_names if cal in median_data[an]), default=0)
        x = np.arange(len(plot_names))
        ax.errorbar(x, plot_medians, yerr=plot_mads, fmt='o', markersize=5,
                    color='steelblue', ecolor='gray', elinewidth=1.5, capsize=3, zorder=3)
        ax.set_xticks(x)
        ax.set_xticklabels(plot_names, rotation=45, ha='right', fontsize=8)
        ax.set_ylabel(ylabel)
        ax.set_title(f'Calibrator: {cal}  (N={n_obs} obs)')
    fig.tight_layout()
    savefig(outpath)


def _plot_freq_solutions(median_data, ylabel, title, xlabel, outpath):
    if not median_data: return
    ant_names = sorted(median_data.keys())
    all_calibrators = sorted({cal for d in median_data.values() for cal in d})
    ncols = 2
    nrows = math.ceil(len(all_calibrators) / ncols)
    fig, axes = plt.subplots(nrows, ncols, figsize=(14, 5 * nrows), squeeze=False)
    cmap = plt.get_cmap('tab20', len(ant_names))
    for cal_idx, cal in enumerate(all_calibrators):
        ax = axes[cal_idx // ncols][cal_idx % ncols]
        n_obs = None
        for i, an in enumerate(ant_names):
            if cal not in median_data[an]: continue
            entry = median_data[an][cal]
            x, med, mad = entry[0], entry[1], entry[2]
            if len(entry) > 3 and n_obs is None:
                n_obs = entry[3]
            color = cmap(i)
            ax.plot(x / 1e6, med, label=an, color=color, linewidth=1.2)
            ax.fill_between(x / 1e6, med - mad, med + mad, color=color, alpha=0.15)
        count_str = f'N={n_obs} obs' if n_obs is not None else f'N={sum(1 for an in ant_names if cal in median_data[an])} stations'
        ax.set_xlabel(xlabel)
        ax.set_ylabel(ylabel)
        ax.set_title(f'Calibrator: {cal}  ({count_str})')
        ax.legend(fontsize=7, loc='best', ncol=2)
        ax.grid(True, alpha=0.3)
    for cal_idx in range(len(all_calibrators), nrows * ncols):
        axes[cal_idx // ncols][cal_idx % ncols].set_visible(False)
    fig.suptitle(f'{title} (±1 MAD shaded)', fontsize=13)
    savefig(outpath)


def _plot_clock_solutions(median_data, outpath):
    """
    Clock-specific plot: one row per calibrator, two panels per row.
    Left panel: CS stations whose clock is consistent with the bulk CS population.
    Right panel: RS stations PLUS any CS station whose median clock is an outlier
    relative to the CS bulk (> 5 × bulk_MAD from the CS median, floored at 10 ns).
    Each panel is independently y-scaled so fine structure is visible in both.
    Title of each panel shows N = number of observations (not stations).
    """
    if not median_data: return
    all_calibrators = sorted({cal for d in median_data.values() for cal in d})
    nrows = len(all_calibrators)
    fig, axes = plt.subplots(nrows, 2, figsize=(16, 4 * nrows), squeeze=False)

    for row, cal in enumerate(all_calibrators):
        # --- collect per-station medians for all CS stations ---
        cs_ants = sorted(an for an in median_data
                         if an.startswith('CS') and cal in median_data[an])
        cs_meds = {an: float(np.nanmedian(median_data[an][cal][1])) for an in cs_ants}

        # Identify outlier CS stations: |median - bulk_cs_median| > threshold
        if cs_meds:
            bulk_cs_med = float(np.nanmedian(list(cs_meds.values())))
            bulk_cs_mad = float(np.nanmedian(
                [abs(v - bulk_cs_med) for v in cs_meds.values()])) or 1.0
            cs_threshold = max(5.0 * bulk_cs_mad, 10.0)  # at least 10 ns
            outlier_cs = {an for an, v in cs_meds.items()
                          if abs(v - bulk_cs_med) > cs_threshold}
        else:
            outlier_cs = set()

        # Panel definitions: (label, station filter function)
        panels = [
            ('CS (typical)',  lambda an: an.startswith('CS') and an not in outlier_cs),
            ('RS + outlier CS', lambda an: an.startswith('RS') or an in outlier_cs),
        ]

        for col, (panel_label, keep) in enumerate(panels):
            ax = axes[row][col]
            names, meds, mads, n_obs_list = [], [], [], []
            for an in sorted(median_data.keys()):
                if not keep(an): continue
                if cal not in median_data[an]: continue
                _, vals, madv = median_data[an][cal]
                names.append(an)
                meds.append(float(np.nanmedian(vals)))
                mads.append(float(np.nanmedian(madv)))
                n_obs_list.append(len(vals))
            if not names:
                ax.set_visible(False)
                continue
            # Mark outlier CS stations with a different colour
            colors = ['tomato' if an in outlier_cs else 'steelblue' for an in names]
            x = np.arange(len(names))
            for xi, (med, mad, color) in enumerate(zip(meds, mads, colors)):
                ax.errorbar(xi, med, yerr=mad, fmt='o', markersize=5,
                            color=color, ecolor='gray',
                            elinewidth=1.5, capsize=3, zorder=3)
            ax.set_xticks(x)
            ax.set_xticklabels(names, rotation=45, ha='right', fontsize=8)
            for tick, an in zip(ax.get_xticklabels(), names):
                if an in outlier_cs:
                    tick.set_color('tomato')
                    tick.set_fontweight('bold')
            ax.set_ylabel('Clock (ns)')
            n_obs = max(n_obs_list)
            ax.set_title(f'{cal} — {panel_label}  (N={n_obs} obs)')
            ax.axhline(0, color='k', linewidth=0.7, linestyle='--', alpha=0.5)
            ax.grid(True, axis='y', alpha=0.3)
            # y-limits based on the spread *across station medians* (inter-station
            # scatter), not the per-station observation MAD.  This ensures all
            # stations fit in view regardless of how spread out they are.
            centre       = float(np.nanmedian(meds))
            inter_mad    = float(np.nanmedian([abs(m - centre) for m in meds])) or 1.0
            obs_mad_floor = max(float(np.nanmedian(mads)), 1.0)
            half_range   = max(5.0 * inter_mad, 3.0 * obs_mad_floor, 2.0)
            ax.set_ylim(centre - half_range, centre + half_range)

    fig.suptitle('Clock Offset per Station — median ± MAD  '
                 '(CS and RS scaled independently; outlier CS shown in red)',
                 fontsize=13)
    fig.tight_layout()
    savefig(outpath)


def plot_all(median_bandpass, median_polalign, median_clock, median_flagging, output_dir):
    _plot_freq_solutions(median_bandpass, ylabel='Bandpass amplitude', title='Median Bandpass per Station',
                         xlabel='Frequency (MHz)',
                         outpath=output_dir / 'bandpass_plots' / 'bandpass_median_all_stations.png')
    _plot_scalar_solutions(median_polalign, ylabel='Polalign YY delay (ns)',
                           title='Polarisation Alignment — YY differential delay per Station',
                           outpath=output_dir / 'polalign_plots' / 'polalign_median_all_stations.png')
    _plot_clock_solutions(median_clock,
                          outpath=output_dir / 'clock_plots' / 'clock_median_all_stations.png')
    if median_flagging.get('final'):
        _plot_scalar_solutions(
            median_flagging['final'],
            ylabel='Final flagging (%)',
            title='Final Data Flagging',
            outpath=output_dir / 'flagging_plots' / 'flagging_final_all_stations.png')

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _save_template_h5(path, obs_to_calibrator, median_bandpass, median_polalign,
                      median_clock, median_flagging):
    """Write the HDF5 cache used for calibrator assessment."""
    with h5py.File(path, 'w') as f:
        # obs_to_calibrator: stored as a JSON string attribute on the root group
        f.attrs['obs_to_calibrator'] = json.dumps(obs_to_calibrator)

        # median_bandpass: ant -> cal -> (freq, med, mad, count)
        grp = f.create_group('median_bandpass')
        for ant, cal_dict in median_bandpass.items():
            ag = grp.create_group(ant)
            for cal, (freq, med, mad, count) in cal_dict.items():
                cg = ag.create_group(cal)
                cg.create_dataset('freq',  data=np.asarray(freq,  dtype=np.float64))
                cg.create_dataset('med',   data=np.asarray(med,   dtype=np.float64))
                cg.create_dataset('mad',   data=np.asarray(mad,   dtype=np.float64))
                cg.attrs['count'] = int(count)

        # median_polalign / median_clock: ant -> cal -> (x, vals, mads)
        for section, data in [('median_polalign', median_polalign),
                               ('median_clock',    median_clock)]:
            grp = f.create_group(section)
            for ant, cal_dict in data.items():
                ag = grp.create_group(ant)
                for cal, (x, vals, mads) in cal_dict.items():
                    cg = ag.create_group(cal)
                    cg.create_dataset('x',    data=np.asarray(x,    dtype=np.float64))
                    cg.create_dataset('vals', data=np.asarray(vals, dtype=np.float64))
                    cg.create_dataset('mads', data=np.asarray(mads, dtype=np.float64))

        # median_flagging: {'final': {ant -> cal -> (x, vals, mads)}}
        fgrp = f.create_group('median_flagging')
        for stage, ant_dict in median_flagging.items():
            sgrp = fgrp.create_group(stage)
            for ant, cal_dict in ant_dict.items():
                ag = sgrp.create_group(ant)
                for cal, (x, vals, mads) in cal_dict.items():
                    cg = ag.create_group(cal)
                    cg.create_dataset('x',    data=np.asarray(x,    dtype=np.float64))
                    cg.create_dataset('vals', data=np.asarray(vals, dtype=np.float64))
                    cg.create_dataset('mads', data=np.asarray(mads, dtype=np.float64))


def save_as_linc_h5(h5_files, median_bandpass, median_polalign, median_clock, out_path):
    """
    Write a single LINC-format cal_solutions.h5 containing the overall median
    solutions (across all observations and all calibrators) for every antenna.

    Strategy: copy the first input file verbatim (so every group, dataset,
    dtype, string encoding, attribute and chunk layout is identical to a real
    LINC file), then open the copy in read-write mode and overwrite only the
    solution arrays:
      calibrator/bandpass/val      (time, freq, ant, pol)
      calibrator/bandpass/weight   (time, freq, ant, pol)
      calibrator/clock/val         (time, ant)
      calibrator/polalign/val      (time, ant, freq, pol)
      calibrator/faraday/val       (ant, time)  — set to zeros

    Everything else (axes, source, antenna table, attributes …) is left
    exactly as it was in the reference file.
    """
    import shutil

    # Pick the input file that has the most antennas across all solution types,
    # so the output axes cover as many stations as possible.
    def _count_antennas(path):
        try:
            with h5py.File(path, 'r') as h:
                return sum(
                    len(h[k][...]) for k in
                    ('calibrator/bandpass/ant',
                     'calibrator/clock/ant',
                     'calibrator/polalign/ant')
                    if k in h
                )
        except Exception:
            return 0

    ref_path = max(h5_files, key=_count_antennas)
    print(f"\nWriting LINC-format median template → {out_path}")
    print(f"  Copying reference file (most antennas): {ref_path}")

    # Start from an exact copy of a real input file
    shutil.copy2(ref_path, out_path)

    # Read the axis/shape information we need to build the new arrays
    try:
        with h5py.File(out_path, 'r') as ref:
            bp_ant_names  = [a.decode() if isinstance(a, bytes) else a
                             for a in ref['calibrator/bandpass/ant'][...]]
            clk_ant_names = [a.decode() if isinstance(a, bytes) else a
                             for a in ref['calibrator/clock/ant'][...]]
            pa_ant_names  = [a.decode() if isinstance(a, bytes) else a
                             for a in ref['calibrator/polalign/ant'][...]]

            bp_freq  = ref['calibrator/bandpass/freq'][...]
            bp_time  = ref['calibrator/bandpass/time'][...]

            clk_time = ref['calibrator/clock/time'][...]

            pa_freq  = ref['calibrator/polalign/freq'][...]
            pa_time  = ref['calibrator/polalign/time'][...]

            far_shape = ref['calibrator/faraday/val'].shape   # (ant, time)

            # Preserve the exact dtype of each val dataset
            bp_dtype  = ref['calibrator/bandpass/val'].dtype
            clk_dtype = ref['calibrator/clock/val'].dtype
            pa_dtype  = ref['calibrator/polalign/val'].dtype
            far_dtype = ref['calibrator/faraday/val'].dtype
    except Exception as e:
        print(f"  ERROR reading reference file: {e}")
        return

    n_time_bp  = len(bp_time)
    n_freq_bp  = len(bp_freq)
    n_ant_bp   = len(bp_ant_names)
    n_time_clk = len(clk_time)
    n_ant_clk  = len(clk_ant_names)
    n_time_pa  = len(pa_time)
    n_freq_pa  = len(pa_freq)
    n_ant_pa   = len(pa_ant_names)

    # ── Bandpass: (n_time, n_freq, n_ant, 2) ────────────────────────────────
    bp_val    = np.full((n_time_bp, n_freq_bp, n_ant_bp, 2), np.nan, dtype=bp_dtype)
    bp_weight = np.zeros((n_time_bp, n_freq_bp, n_ant_bp, 2), dtype=bp_dtype)

    for ai, an in enumerate(bp_ant_names):
        ant_dict = median_bandpass.get(an)
        if ant_dict is None:
            print(f"    bandpass: no data for {an!r} — filling NaN")
            continue
        # Collect spectra across all calibrators and take the overall median
        all_spectra = []
        for cal, (ref_freq, med, _mad, _count) in ant_dict.items():
            if len(ref_freq) == n_freq_bp and np.allclose(ref_freq, bp_freq, rtol=1e-6):
                all_spectra.append(med)
            else:
                interp = np.interp(bp_freq, ref_freq, med, left=np.nan, right=np.nan)
                all_spectra.append(interp)
        if not all_spectra:
            continue
        spectrum = np.nanmedian(np.array(all_spectra), axis=0)
        bp_val[:, :, ai, 0] = spectrum[np.newaxis, :]
        bp_val[:, :, ai, 1] = spectrum[np.newaxis, :]
        w = np.where(np.isfinite(spectrum), 1.0, 0.0)
        bp_weight[:, :, ai, 0] = w[np.newaxis, :]
        bp_weight[:, :, ai, 1] = w[np.newaxis, :]

    # ── Clock: (n_time, n_ant) ───────────────────────────────────────────────
    clk_val = np.full((n_time_clk, n_ant_clk), np.nan, dtype=clk_dtype)

    for ai, an in enumerate(clk_ant_names):
        ant_dict = median_clock.get(an)
        if ant_dict is None:
            print(f"    clock: no data for {an!r} — filling NaN")
            continue
        all_vals = np.concatenate([vals for _x, vals, _mads in ant_dict.values()])
        med_ns = float(np.nanmedian(all_vals))
        clk_val[:, ai] = med_ns * 1e-9   # ns → seconds, constant across time

    # ── Polalign: (n_time, n_ant, n_freq, 2) ────────────────────────────────
    pa_val = np.zeros((n_time_pa, n_ant_pa, n_freq_pa, 2), dtype=pa_dtype)

    for ai, an in enumerate(pa_ant_names):
        ant_dict = median_polalign.get(an)
        if ant_dict is None:
            print(f"    polalign: no data for {an!r} — filling zeros")
            continue
        all_vals = np.concatenate([vals for _x, vals, _mads in ant_dict.values()])
        med_ns = float(np.nanmedian(all_vals))
        tau_s  = med_ns * 1e-9
        yy_phase = -2.0 * np.pi * tau_s * pa_freq
        pa_val[:, ai, :, 1] = yy_phase[np.newaxis, :]   # YY; XX stays zero

    # ── Faraday: (n_ant, n_time) — zeros (ignored) ──────────────────────────
    far_val = np.zeros(far_shape, dtype=far_dtype)

    # ── Overwrite only the solution datasets in the copied file ─────────────
    with h5py.File(out_path, 'r+') as f:
        # delete and recreate each val/weight dataset so shape changes are safe
        # (if the shape happens to be identical we could use [...] assignment,
        # but delete+recreate works in all cases)
        def overwrite_ds(group_path, ds_name, new_data):
            full = f'{group_path}/{ds_name}'
            old_attrs = dict(f[full].attrs)
            del f[full]
            ds = f[group_path].create_dataset(ds_name, data=new_data)
            for k, v in old_attrs.items():
                ds.attrs[k] = v

        overwrite_ds('calibrator/bandpass', 'val',    bp_val)
        overwrite_ds('calibrator/bandpass', 'weight', bp_weight)
        overwrite_ds('calibrator/clock',    'val',    clk_val)
        overwrite_ds('calibrator/polalign', 'val',    pa_val)
        overwrite_ds('calibrator/faraday',  'val',    far_val)

    print(f"  Written: {out_path}")


def main():
    parser = argparse.ArgumentParser(
        description='Build a template of median LOFAR calibration solutions and save to cache.')
    parser.add_argument('--input-glob', required=True,
                        help='Quoted glob matching the input cal_solutions.h5 files.')
    parser.add_argument('--output-dir', type=Path, required=True,
                        help='Directory for the cache and diagnostic plots.')
    parser.add_argument('--recompute', action='store_true',
                        help='Force recomputation even if a cache file already exists.')
    parser.add_argument('--write-linc-template', action='store_true',
                        help='Also write the LINC-format template_median.h5 file.')
    args = parser.parse_args()

    cache = args.output_dir / 'calibrator_medians_cache.h5'
    if not args.recompute and cache.exists():
        print(f"Cache already exists at {cache}. Use --recompute to rebuild.")
        return

    h5_files = get_h5_files(args.input_glob)
    if not h5_files:
        parser.error(f"No input files match {args.input_glob}")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    print(f"Found {len(h5_files)} cal_solutions.h5 files\n")

    obs_to_calibrator = {}
    obs_times = {}
    calibrator_map = {}
    for h5_path in h5_files:
        obs_id     = get_obs_id(h5_path)
        calibrator = get_calibrator_from_h5(h5_path)
        obs_time   = get_obs_time(h5_path)
        obs_times[obs_id] = obs_time
        if calibrator:
            obs_to_calibrator[obs_id] = calibrator
            calibrator_map.setdefault(calibrator, []).append(obs_id)

    print("Calibrator sources identified:")
    print("=" * 60)
    for cal_name in sorted(calibrator_map.keys()):
        obs_list = calibrator_map[cal_name]
        if cal_name in CALIBRATORS_3C:
            ra, dec = CALIBRATORS_3C[cal_name]
            elevs = [calculate_elevation(ra, dec, obs_times[o])
                     for o in obs_list if obs_times.get(o)]
            elevs = [e for e in elevs if e is not None]
            elev_str = f"{min(elevs):.1f}° – {max(elevs):.1f}°" if elevs else "N/A"
        else:
            elev_str = "Unknown source"
        print(f"  {cal_name:<12} {len(obs_list)} obs   elevation: {elev_str}")
    print("=" * 60 + "\n")

    median_bandpass = build_median_bandpass(h5_files, obs_to_calibrator)
    median_polalign = build_median_polalign(h5_files, obs_to_calibrator)
    median_clock    = build_median_clock(h5_files, obs_to_calibrator)
    median_flagging = build_median_flagging(h5_files, obs_to_calibrator)

    plot_all(median_bandpass, median_polalign, median_clock, median_flagging, args.output_dir)

    _save_template_h5(
        cache,
        obs_to_calibrator=obs_to_calibrator,
        median_bandpass=median_bandpass,
        median_polalign=median_polalign,
        median_clock=median_clock,
        median_flagging=median_flagging,
    )
    print(f"\nSaved template cache to {cache}")

    if args.write_linc_template:
        save_as_linc_h5(
            h5_files=h5_files,
            median_bandpass=median_bandpass,
            median_polalign=median_polalign,
            median_clock=median_clock,
            out_path=args.output_dir / "template_median.h5",
        )


if __name__ == "__main__":
    main()

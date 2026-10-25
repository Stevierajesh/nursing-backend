# Simulator baseline calibration

The simulator now has a fitted **joint empirical baseline distribution**. It samples whole
six-vital combinations from bounded reservoirs, conditional on source age/gender groups.
The original authored deterioration/recovery trajectories and time-dependent fluctuations remain.
Nurse concern and escalation rules are unchanged. There is no risk classifier or learned nurse intuition.

## Source and inspection

- Source: [Human Vital Sign Dataset](https://www.kaggle.com/datasets/nasirayub2/human-vital-sign-dataset)
  by nasirayub2; Kaggle metadata lists **CC0: Public Domain**.
- Source file: `human_vital_signs_dataset_2024.csv`.
- CSV SHA-256: `d70375bce658249ff7b7b01de08af6e19ad6bb103527ef66b10be0c0065ec588`.
- Observed size: **200,020 rows**, not the rounded 200,000 in the dataset description.
- All 200,020 rows pass the simulator's baseline-value bounds, finite-number checks, adult
  demographic grouping, and systolic > diastolic validation.
- All 200,020 patient IDs are unique. Consequently this file cannot establish within-patient
  temporal variation, autocorrelation, deterioration rates, or treatment response. Its timestamps
  do not make it a longitudinal patient dataset.
- The dataset description does not establish clinical collection or genuine patient provenance.
  Values lie in narrow ranges and measured cross-vital correlations are very small. Matching this
  dataset is not clinical validation or evidence of meaningful demographic differences.
- `Risk Category` is explicitly described as threshold-derived. Neither it nor `Derived_HRV`,
  BMI, timestamps, or patient IDs is used as a fitting feature. IDs are counted for the audit only.
- Source pulse pressure and estimated MAP match SBP−DBP and DBP+(SBP−DBP)/3 in every accepted row.
  Sampling recomputes these derived quantities from the rounded baseline; it never samples them independently.

## Method

`scripts/calibrate_simulator.py` makes eight adult strata: ages 18–39, 40–64, 65–79, 80+,
each with the source's Female/Male categories (mapped to F/M in the API). It computes
counts, joint means/covariances, minima/maxima, and a reproducible uniform reservoir of
256 complete six-vital tuples per stratum. Source observations are quantized to existing
simulator precision: integer HR/RR/SpO₂/BP and temperature to one decimal place.

Runtime sampling selects one complete tuple. A pooled request weights eligible strata by
all accepted source counts, rather than treating equally sized reservoirs as equal populations.
This preserves observed combinations without assuming Gaussian distributions or inventing
cross-vital relationships. The reservoir is a compact approximation to the full empirical
joint distribution, with 2,048 candidate combinations overall. It is not a generative neural network.

SHA-256 selection from the model version, simulation seed, and requested groups makes results
stable across processes and Python versions. The approximately 234 KB versioned JSON artifact is
checked in. No CSV, network download, pandas, numpy, or external ML service is needed at runtime.
The artifact includes licensing/source information, the source hash, quality audit, fitting
parameters, and a complete version identifier.

## Use

In **Dashboard → Admin → Patient simulations → Dataset-calibrated baseline**, choose optional
reference groups and click **Sample from dataset**, then preview and save as usual.
The chosen reference group does not alter the patient's recorded demographics.

The saved `simulation_runs.config` contains the exact baseline, seed, requested groups,
selected source group, and model version. It uses the existing JSON configuration column;
**no new database migration is needed**. Existing manual simulations remain manual and unchanged.
Editing a baseline value or seed switches to explicit manual mode. Sample again to restore
calibrated provenance. Preview, save, and playback use the same baseline snapshot.

The backend validates provenance when parsing configurations, rejecting a changed baseline or
seed labelled as if it were the original sample. Model files are registered by version; when
adding a newer fitted artifact, retain older registered artifacts so saved runs stay reproducible.

## Reproduce

Download the CSV under the source dataset's license and keep it outside the repository, then run:

```bash
cd nursing-backend
.venv/bin/python scripts/calibrate_simulator.py /path/to/human_vital_signs_dataset_2024.csv
```

The default reservoir size is 256 per group; the fitting seed is 2026. Identical input bytes and
parameters produce a byte-identical artifact. `--output`, `--reservoir-size`, and `--seed` are
available. Changing fitting parameters or source bytes creates a new version; register that
version explicitly in `app/services/calibration.py` before using it.

The simulator's seed chooses a baseline tuple; it does not train or tune the model. Scenario
shifts and temporal amplitudes remain developer-authored because this source cannot fit them.

## Verification

`make test` covers source audit, valid joint samples, group conditioning, source-mean approximation,
derived pressure consistency, deterministic sampling, preserved manual behavior, provenance
validation, fitted covariance on a controlled fixture, invalid data handling, API role access,
and preview/playback consistency. The source-mean comparison is a sampling regression check,
not an evaluation of clinical validity or deterioration prediction.

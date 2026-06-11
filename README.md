# shared-ct

A reusable Flask blueprint for camera trap–based wildlife monitoring. It handles the full workflow from photo upload to species identification, analytics, and data export. Designed to run as a Git submodule inside a host Flask application (see [biomon](https://github.com/yurastrus/biomon)).

## Architecture

The module is a self-contained Flask blueprint registered at `/<lang>/camera-traps`. It owns its own PostgreSQL database (`ct_db`), SQLAlchemy session, and all templates and static assets.

```
shared-ct/
├── __init__.py              ← Blueprint definition and database init hook
├── models.py                ← 27 SQLAlchemy ORM models (ct_db)
├── routes.py                ← 52 Flask endpoints
├── database.py              ← Engine and scoped-session singletons
├── decorators.py            ← @role_required (viewer → manager → admin)
├── forms.py                 ← WTForms: UploadForm, IdentificationForm
├── utils.py                 ← Photo processing, consensus calculation
├── fast_upload.py           ← Async grouping for large batches (10 k–100 k+)
├── background_tasks.py      ← Scheduled cleanup and batch statistics
├── cleanup.py               ← Two-phase orphan cleanup (analyse → execute)
├── data_export.py           ← Occurrence data export with QC filters
├── analytics_calculator.py  ← Monthly activity and yearly trends (background)
├── daily_analytics.py       ← Activity curves (scipy KDE), overlap matrix
├── service_analytics.py     ← Per-location statistics (LocationStats)
├── ai_runner.py             ← AI model registry, queue, feature-flag helpers
├── classification_import.py ← Import external DeepFaune CSV results
├── deployment_import.py     ← Idempotent Excel import for deployments
├── notifications.py         ← Email reminders to verifiers
├── templates/               ← 24 Jinja2 templates
└── static/                  ← JS, CSS, images for the module
```

### Database

The module uses a dedicated PostgreSQL database configured via the `CT_DATABASE_URL` environment variable:

| Env var | Database | Managed by |
|---|---|---|
| `CT_DATABASE_URL` | `ct_db` | `models.py` (`Base.metadata.create_all`) |

The engine and session are initialised once at app startup via `init_ct_database()`, called from `__init__.py`. A scoped session (`get_ct_session()`) is used by routes; `data_export.py` uses `engine.connect()` directly for bulk reads.

### Role hierarchy

Access control is enforced by the `@role_required` decorator. Higher roles inherit all permissions of lower ones:

```
admin
└── manager → ct_verifier, analyst, viewer
    ├── ct_verifier → viewer
    └── analyst     → ct_verifier, viewer
```

## Database models

### Core tables

| Model | Purpose |
|---|---|
| `Location` | Camera trap site: name, coordinates, biotopes, visibility level |
| `Deployment` | Active camera season at a location: dates, camera ID, 22 QC flags |
| `Observation` | Photo series: start/end time, consensus result, flagged status |
| `Photo` | Individual photo: capture time, status, upload batch link |
| `Species` | Taxonomy: scientific name, Ukrainian/English common names, kingdom→genus |
| `Identification` | User's species vote on an observation: species, quantity, behaviours |
| `BehaviorType` | Bilingual behaviour tags |
| `UploadBatch` | Batch metadata: status, total files, processed files |

### Reference and lookup tables

| Model | Purpose |
|---|---|
| `Biotope` | Habitat type (bilingual) |
| `BatteryType` | Battery type with `is_rechargeable` flag (bilingual) |
| `VisitPurpose` | Service visit reason (bilingual) |
| `UserProfile` | Per-user CT stats: identification count, accuracy score |

### Analytics tables (background-populated)

| Model | Purpose |
|---|---|
| `LocationMonthlyActivity` | Species × location × year × month (detection count, trap days) |
| `SpeciesYearlyTrend` | Annual trend + CI per scope (global / institution / ecoregion) |
| `LocationStats` | Per-location totals: photos, species, observation counts by type |
| `CalculationLog` | Status tracker (idle / running / completed / failed) for async jobs |

### Service tables

| Model | Purpose |
|---|---|
| `ServiceVisit` | Maintenance log: location, user, datetime, purpose, battery, SD card |
| `LocationMergeLog` | Audit log for location merges |

### AI classification tables (optional)

| Model | Purpose |
|---|---|
| `AIModel` | Registry: name, version, config, active flag, level |
| `AIModelLevel` | Detector ensemble level (code: `DF` / `MDS` / `DF+MDS` / `MDR`, accuracy rank) |
| `AIPrediction` | Per-photo prediction: label, score, bounding box, `was_correct` |
| `AILabelMap` | Raw classifier label → `species_id` mapping (editable at runtime) |
| `AIRunQueue` | Manual AI run requests: n observations, status, duration |

### Cleanup and association tables

| Table | Purpose |
|---|---|
| `CleanupLog` | Audit log for orphan-cleanup operations (analysis and execution phases) |
| `identification_behaviors` | Identification ↔ BehaviorType (M2M) |
| `location_biotopes` | Location ↔ Biotope (M2M) |
| `location_institutions` | Location ↔ Institution (cross-db, no FK) |

## Flask routes

All routes are prefixed with `/<lang>/camera-traps`. `<lang>` is a two-letter locale code (`uk` or `en`).

### Navigation and dashboard

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Module overview (card hub) |
| `GET` | `/dashboard` | Main dashboard: pending observations, summary stats |
| `GET` | `/ct-static/<filename>` | Serve module static files |

### Photo upload

| Method | Path | Description |
|---|---|---|
| `GET` | `/upload` | Upload form |
| `POST` | `/upload` | Submit upload form |
| `GET` | `/upload-fast` | Fast async upload UI (for large batches) |
| `POST` | `/api/create-batch` | Create a new upload batch |
| `POST` | `/upload/process-single` | Process one photo (called by the JS pool) |
| `POST` | `/api/finalize-batch` | Synchronous batch grouping |
| `POST` | `/api/finalize-batch-async` | Start background grouping (202) |
| `GET` | `/api/batch-status/<id>` | Poll grouping status |
| `GET` | `/api/batch/<id>/uploaded-files` | List uploaded files (for resumable upload) |

### Identification and review

| Method | Path | Description |
|---|---|---|
| `GET` | `/identify` | List of pending observation series |
| `GET` | `/api/identify/ai-species` | Filter observations by AI prediction |
| `POST` | `/api/submit-identification` | Submit a species vote |
| `GET` | `/api/next-observation-for-identification` | Fetch the next pending series |
| `POST` | `/observation/<id>/flag` | Flag a series for re-review |
| `POST` | `/observation/<id>/unflag` | Unflag a series |

### Photos and gallery

| Method | Path | Description |
|---|---|---|
| `GET` | `/photo/<photo_id>` | Single photo detail |
| `GET` | `/observation/<obs_id>/photo/<index>` | Photo by index within a series |
| `GET` | `/gallery` | Photo gallery with filtering |
| `GET` | `/thumbnails/<path>` | Serve cached thumbnails |
| `GET` | `/photos/raw/<path>` | Serve original images |

### Analytics and data

| Method | Path | Description |
|---|---|---|
| `GET` | `/analysis/species-dashboard` | Species-level trends and heatmaps |
| `GET` | `/analysis/species-detailed` | Per-species detail page |
| `GET` | `/analysis/comparison` | Multi-species comparison |
| `GET` | `/analysis/behavior` | Behaviour tag analysis |
| `GET` | `/analysis/daily-activity` | Daily activity curves (KDE) |
| `GET` | `/data-export` | Occurrence data export with QC filters |
| `GET` | `/data-quality` | QC metrics and deployment health |
| `POST` | `/api/stats/top-species` | Species list with detection counts |
| `POST` | `/api/stats/locations` | Per-location statistics |
| `POST` | `/api/stats/species-dynamics` | Time-series detection data |
| `POST` | `/api/stats/comparison` | Comparison dataset |
| `POST` | `/api/stats/distribution-map` | Geographic distribution |
| `POST` | `/api/stats/daily-activity` | Daily activity raw data |
| `POST` | `/api/data-preview` | Export preview |
| `POST` | `/api/data-download` | Export download |
| `POST` | `/api/run-stats-calculation` | Trigger per-location stats recalculation |

### Location and deployment management

| Method | Path | Description |
|---|---|---|
| `GET` | `/manage-locations` | Location CRUD page |
| `GET` | `/manage-deployments` | Deployment CRUD page |
| `GET` | `/location/<id>/coverage` | Temporal coverage chart |
| `POST` | `/api/location/<id>` | Location detail (JSON) |
| `POST` | `/api/update-location/<id>` | Update location |
| `POST` | `/api/location/create` | Create location |
| `POST` | `/api/deployment/<id>` | Deployment detail (JSON) |
| `POST` | `/api/update-deployment/<id>` | Update deployment |
| `POST` | `/api/deployment/create` | Create deployment |
| `POST` | `/api/deployment/<id>/delete` | Delete deployment |
| `GET` | `/export-deployments` | Export deployments as CSV |

### Service log

| Method | Path | Description |
|---|---|---|
| `GET` | `/service-log` | Maintenance visit log |
| `POST` | `/api/locations-with-status` | Locations with last service date |
| `POST` | `/api/location/<id>/service-history` | Visit timeline for a location |
| `POST` | `/api/service-log/create` | Record a new visit |
| `POST` | `/api/service-visit/<id>/update` | Update a visit record |

### Import

| Method | Path | Description |
|---|---|---|
| `GET` | `/import-classification` | Import external classification results (CSV) |
| `POST` | `/import-classification/preview` | Preview import |
| `POST` | `/import-classification/run` | Execute import |

### Admin

| Method | Path | Description |
|---|---|---|
| `GET` | `/admin` | Admin panel (cleanup, analytics, AI queue) |
| `POST` | `/admin/ai/run` | Queue an AI classification run |
| `GET` | `/admin/ai/accuracy` | AI accuracy per species |
| `GET` | `/admin/flagged` | Flagged series list |
| `POST` | `/admin/cleanup/analyze` | Dry-run orphan cleanup |
| `POST` | `/admin/cleanup/execute/<id>` | Execute orphan cleanup |
| `GET` | `/admin/cleanup/task/<id>` | Cleanup task status |
| `POST` | `/admin/run-analytics` | Trigger analytics recalculation |
| `GET` | `/admin/analytics/status` | Analytics job status |
| `POST` | `/admin/recalculate-consensus` | Re-run consensus calculation |

## Integration with biomon

This repository is used as a Git submodule inside [biomon](https://github.com/yurastrus/biomon) at `app/camera_traps/`. The host application registers the blueprint and provides:

- **Auth and roles** — `current_user`, role checks, and `User` / `Institution` from the main database.
- **Extensions** — the `Mail` instance used by `notifications.py` comes from the host app's `extensions.py`.
- **Environment variables** — `CT_DATABASE_URL` and `CAMERA_TRAP_UPLOAD_PATH` are read from the host's `.env`.
- **Templates** — `ct_base.html` extends the host app's `base.html`.

For full installation steps, environment variable reference, and deployment instructions, see the [biomon README](https://github.com/yurastrus/biomon#readme).

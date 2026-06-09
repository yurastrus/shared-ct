from sqlalchemy import Column, Integer, String, DateTime, Date, Time, Boolean, Text, Numeric, Float, ForeignKey, Index, Table, Interval
from sqlalchemy import CheckConstraint, Computed, UniqueConstraint, func
from sqlalchemy.orm import relationship, backref
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from datetime import datetime

from .database import CTBase

# Association table for the many-to-many relationship between Identification and BehaviorType
identification_behaviors = Table(
    'identification_behaviors',
    CTBase.metadata,
    Column('identification_id', Integer, ForeignKey('identifications.id'), primary_key=True),
    Column('behavior_type_id', Integer, ForeignKey('behavior_types.id'), primary_key=True)
)

location_biotopes = Table(
    'location_biotopes',
    CTBase.metadata,
    Column('location_id', Integer, ForeignKey('locations.id'), primary_key=True),
    Column('biotope_id', Integer, ForeignKey('biotopes.id'), primary_key=True)
)

location_institutions = Table(
    'location_institutions',
    CTBase.metadata,
    Column('location_id', Integer, ForeignKey('locations.id', ondelete='CASCADE'), primary_key=True),
    Column('institution_id', Integer, primary_key=True)
)

class Species(CTBase):
    __tablename__ = 'species'

    id = Column(Integer, primary_key=True)
    scientific_name = Column(String(200), unique=True, nullable=False)
    common_name_ua = Column(String(200))
    common_name_en = Column(String(200))
    category = Column(String(50), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)

    kingdom = Column(String(100))
    phylum = Column(String(100))
    class_ = Column("class", String(100)) # "class" is the column name in the database
    order_rank = Column(String(100))
    family = Column(String(100))
    genus = Column(String(100))
    establishment_means = Column(String(100))

    # Relationships
    identifications = relationship('Identification', back_populates='species')

    def __repr__(self):
        return f'<Species {self.scientific_name}>'

class Location(CTBase):
    __tablename__ = 'locations'

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    latitude = Column(Numeric(10, 5), nullable=False)
    longitude = Column(Numeric(10, 5), nullable=False)
    state_province = Column(String(150))
    description = Column(Text)
    photo_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=func.now())
    created_by_id = Column(Integer, nullable=True)  # User ID from the main database
    visibility_level = Column(Integer, default=1, nullable=False)

    # Relationships
    observations = relationship('Observation', back_populates='location')
    biotopes = relationship('Biotope', secondary=location_biotopes, backref='locations')
    service_visits = relationship('ServiceVisit', back_populates='location', order_by=lambda: ServiceVisit.visit_datetime.desc())
    deployments = relationship('Deployment', back_populates='location', order_by=lambda: Deployment.start_date)

    # Index for fast lookup by rounded coordinates
    __table_args__ = (
        Index('idx_locations_rounded', func.round(latitude, 5), func.round(longitude, 5)),
    )

    def __repr__(self):
        return f'<Location {self.name}>'

class Biotope(CTBase):
    __tablename__ = 'biotopes'

    id = Column(Integer, primary_key=True)
    name_ua = Column(String(100), nullable=False, unique=True)
    name_en = Column(String(100), nullable=False, unique=True)

    def get_name(self, lang_code):
        return self.name_ua if lang_code == 'uk' else self.name_en

    def __repr__(self):
        return f'<Biotope {self.name_en}>'

class Deployment(CTBase):
    """Camera trap deployment at a location for a specific period (camera-season).

    A single physical ``Location`` may have many deployments over time. A deployment
    carries temporal fields and a quality-control section (qc_*) from the ARD
    deployments table. The link between observations/photos and a deployment is NOT
    a foreign key — it is resolved on-the-fly by date overlap:
    ``observation.captured_at ∈ [start_date, end_date]`` for the same
    ``location_id``. Institution and region are not duplicated here — they are
    accessible via ``location``.
    """
    __tablename__ = 'deployments'

    id = Column(Integer, primary_key=True)
    # Can be NULL for deployments without GPS coordinates (included in QC analysis as
    # qc_no_gps_coordinates=TRUE; no Location is created, not shown on the map).
    location_id = Column(Integer, ForeignKey('locations.id'), nullable=True, index=True)
    name = Column(String(200), nullable=False)  # deployment_id from the Excel sheet

    # Deployment time interval
    start_date = Column(Date, nullable=True)
    end_date = Column(Date, nullable=True)
    start_time = Column(Time, nullable=True)  # retained to match the Excel source
    end_time = Column(Time, nullable=True)

    # Descriptive deployment fields
    study_year = Column(Integer, nullable=True)
    study_season = Column(String(20), nullable=True)   # Summer / Winter
    study_design = Column(String(100), nullable=True)
    camera_id = Column(String(10), nullable=True)      # String: leading zeros (e.g. '0405'); 5-digit values are valid
    n_days_working = Column(Integer, nullable=True)     # from Excel as-is, NOT end-start
    # Computed by the DB as the calendar interval (end-start); NULL when dates are absent.
    # Separate from n_days_working because actual working days may differ.
    n_days_calc = Column(Integer, Computed('end_date - start_date'), nullable=True)
    n_photos = Column(Integer, nullable=True)
    camera_model = Column(String(100), nullable=True)
    serial_number = Column(String(100), nullable=True)

    # Quality control (NULL = unknown → does not exclude under the "orphans are valid" rule)
    qc_non_functional = Column(Boolean, nullable=True)
    qc_stolen = Column(Boolean, nullable=True)
    qc_hardware_issue = Column(Boolean, nullable=True)
    qc_firmware_issue = Column(Boolean, nullable=True)
    qc_settings_issue = Column(Boolean, nullable=True)
    qc_battery_issue = Column(Boolean, nullable=True)
    qc_sd_issue = Column(Boolean, nullable=True)
    qc_no_data_uploaded_by_pa = Column(Boolean, nullable=True)
    qc_uploaded_data_is_not_raw = Column(Boolean, nullable=True)
    qc_no_gps_coordinates = Column(Boolean, nullable=True)
    qc_no_species_captured = Column(Boolean, nullable=True)
    qc_placement_incorrect = Column(Boolean, nullable=True)
    qc_poor_placement = Column(Boolean, nullable=True)
    qc_feeding_location = Column(Boolean, nullable=True)
    qc_installation_incorrect = Column(Boolean, nullable=True)
    qc_lapse_photos_missed = Column(Boolean, nullable=True)
    qc_installation_photos_missed = Column(Boolean, nullable=True)
    qc_deinstallation_photos_missed = Column(Boolean, nullable=True)
    qc_distance_reference_photos_missed = Column(Boolean, nullable=True)
    qc_datetime_photos_missed = Column(Boolean, nullable=True)
    qc_local_datetime_not_set = Column(Boolean, nullable=True)
    qc_local_datetime_issue = Column(Text, nullable=True)
    qc_data_not_usable = Column(Boolean, nullable=True)  # master filter flag
    qc_used_brf = Column(Boolean, nullable=True)
    qc_comment = Column(Text, nullable=True)

    # Administrative fields
    history_unknown = Column(Boolean, default=False, nullable=False)  # synthetic backfill flag
    created_at = Column(DateTime, default=func.now())
    created_by_id = Column(Integer, nullable=True)

    location = relationship('Location', back_populates='deployments')

    __table_args__ = (
        # Interval matching for observations: WHERE location_id=:x AND captured_at BETWEEN start AND end
        Index('idx_deployments_loc_dates', 'location_id', 'start_date', 'end_date'),
    )

    def is_usable(self):
        """Return whether the deployment is usable for analysis. NULL is treated as usable."""
        return not bool(self.qc_data_not_usable)

    def count_photos(self, session):
        """Return the number of grouped photos in the deployment interval (on-the-fly).

        Counts photos via observations for the same location with captured_at in
        [start_date, end_date]. Separate from the imported n_photos (authoritative from
        Excel). Ungrouped photos (observation_id IS NULL) are not counted.
        """
        q = (session.query(func.count(Photo.id))
             .join(Observation, Photo.observation_id == Observation.id)
             .filter(Observation.location_id == self.location_id))
        if self.start_date is not None:
            q = q.filter(func.date(Photo.captured_at) >= self.start_date)
        if self.end_date is not None:
            q = q.filter(func.date(Photo.captured_at) <= self.end_date)
        return q.scalar() or 0

    def __repr__(self):
        return f'<Deployment {self.name} (loc {self.location_id})>'

class Observation(CTBase):
    __tablename__ = 'observations'

    id = Column(Integer, primary_key=True)
    location_id = Column(Integer, ForeignKey('locations.id'), nullable=False)
    series_start_time = Column(DateTime, nullable=False)
    series_end_time = Column(DateTime, nullable=False)
    photo_count = Column(Integer, default=0)
    status = Column(String(20), default='pending', nullable=False)
    uploaded_by_id = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=func.now())

    # "Needs re-review" flag (Idea 6) — an organisational marker for
    # verifiers/admins. Does NOT change status and does NOT exclude the series
    # from analytics (this is a separate, debated decision — not applied yet).
    flagged = Column(Boolean, nullable=False, default=False, server_default='false')
    flag_note = Column(Text)

    # Relationships
    location = relationship('Location', back_populates='observations')

    # Legacy relationship (kept for backwards compatibility)
    photos = relationship('Photo', back_populates='observation')

    # New relationship in chronological order
    photos_chronological = relationship(
        'Photo',
        back_populates='observation',
        order_by='Photo.captured_at',
        viewonly=True
    )

    def __repr__(self):
        return f'<Observation {self.id} at {self.location.name}>'

class UploadBatch(CTBase):
    __tablename__ = 'upload_batches'

    id = Column(String(36), primary_key=True)  # UUID
    location_id = Column(Integer, ForeignKey('locations.id'), nullable=False)
    uploaded_by_id = Column(Integer, nullable=False)
    status = Column(String(20), default='uploading', nullable=False)  # uploading, processing, completed, failed
    total_files = Column(Integer, default=0)
    processed_files = Column(Integer, default=0)
    created_at = Column(DateTime, default=func.now())
    completed_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)

    # Relationships
    location = relationship('Location')
    photos = relationship('Photo', back_populates='upload_batch')

    def __repr__(self):
        return f'<UploadBatch {self.id[:8]}...>'

class Photo(CTBase):
    __tablename__ = 'photos'

    id = Column(Integer, primary_key=True)
    observation_id = Column(Integer, ForeignKey('observations.id'), nullable=True)  # Can now be NULL
    upload_batch_id = Column(String(36), ForeignKey('upload_batches.id'), nullable=True)  # New relationship
    original_filename = Column(String(500), nullable=False)
    system_filename = Column(String(500), unique=True, nullable=False)
    sequence_number = Column(Integer, nullable=True)  # Can now be NULL until grouping
    captured_at = Column(DateTime, nullable=False)
    status = Column(String(20), default='uploaded', nullable=False)  # uploaded, grouped, pending, completed, archived, needs_review
    identification_count = Column(Integer, default=0)
    is_favorite = Column(Boolean, default=False, nullable=False)

    # Relationships
    observation = relationship('Observation', back_populates='photos')
    upload_batch = relationship('UploadBatch', back_populates='photos')
    identifications = relationship('Identification', back_populates='photo')

    __table_args__ = (
        # Index for CTE-based grouping in /upload-fast: LAG(captured_at)
        # OVER (ORDER BY captured_at, id) for photos of a specific batch.
        # Covers WHERE upload_batch_id=:b AND status='uploaded' + ORDER BY.
        Index('idx_photos_batch_captured', 'upload_batch_id', 'captured_at', 'id'),
        # Status filter: cleanup (status='completed'/'pending') and
        # dashboard. Index already exists on prod — declared here so that create_all
        # on new/dev installations also creates it (metadata = real DB).
        Index('idx_photos_status', 'status'),
    )

    def __repr__(self):
        return f'<Photo {self.system_filename}>'

class BehaviorType(CTBase):
    __tablename__ = 'behavior_types'

    id = Column(Integer, primary_key=True)
    name_ua = Column(String(100), nullable=False, unique=True)
    name_en = Column(String(100), nullable=False, unique=True)

    # Relationships
    identifications = relationship('Identification', secondary=identification_behaviors, back_populates='behaviors')

    def get_name(self, lang_code):
        return self.name_ua if lang_code == 'uk' else self.name_en

    def __repr__(self):
        return f'<BehaviorType {self.name_en}>'

class Identification(CTBase):
    __tablename__ = 'identifications'

    id = Column(Integer, primary_key=True)
    photo_id = Column(Integer, ForeignKey('photos.id'), nullable=False)
    user_id = Column(Integer, nullable=False)  # User ID from the main database
    species_id = Column(Integer, ForeignKey('species.id'), nullable=True)  # None for "Other species"
    # confidence_level removed (#46): the column was always empty (the form never wrote to it) —
    # an architectural leftover. DROP COLUMN applied on prod.
    quantity = Column(Integer, default=1)
    comment = Column(Text)
    created_at = Column(DateTime, default=func.now())

    # Relationships
    photo = relationship('Photo', back_populates='identifications')
    species = relationship('Species', back_populates='identifications')
    behaviors = relationship('BehaviorType', secondary=identification_behaviors, back_populates='identifications')

    # Unique constraint: one user can identify a single photo only once
    __table_args__ = (
        UniqueConstraint('photo_id', 'user_id', name='_photo_user_uc'),
        # Filter/grouping by author: dashboard top-contributors,
        # contribution page. Index already exists on prod — declared here for
        # consistency with create_all on new/dev installations.
        Index('idx_identifications_user_id', 'user_id'),
    )

    def __repr__(self):
        return f'<Identification {self.id} by user {self.user_id}>'

class UserProfile(CTBase):
    __tablename__ = 'user_profiles'

    user_id = Column(Integer, primary_key=True)  # User ID from the main database
    camera_trap_role = Column(String(20), default='viewer', nullable=False)
    identifications_count = Column(Integer, default=0, nullable=False)
    accuracy_score = Column(Numeric(5, 2), default=0.0, nullable=False)

    def __repr__(self):
        return f'<UserProfile {self.user_id}>'

class LocationMergeLog(CTBase):
    __tablename__ = 'location_merge_log'

    id = Column(Integer, primary_key=True)
    merged_by_id = Column(Integer, nullable=False)  # User ID from the main database
    main_location_id = Column(Integer, ForeignKey('locations.id'), nullable=False)
    merged_location_ids = Column(ARRAY(Integer), nullable=False)
    merged_location_names = Column(ARRAY(String), nullable=False)
    merge_reason = Column(Text)
    created_at = Column(DateTime, default=func.now())

    # Relationships
    main_location = relationship('Location')

    def __repr__(self):
        return f'<LocationMergeLog {self.id}>'

class LocationMonthlyActivity(CTBase):
    """Intermediate table for storing monthly activity per location.

    Populated by a background process.
    """
    __tablename__ = 'location_monthly_activity'

    # Composite primary key for uniqueness and fast lookup
    species_id = Column(Integer, ForeignKey('species.id'), primary_key=True)
    location_id = Column(Integer, ForeignKey('locations.id'), primary_key=True)
    year = Column(Integer, primary_key=True)
    month = Column(Integer, primary_key=True)

    detection_count = Column(Integer, nullable=False, default=0)
    trap_days = Column(Integer, nullable=False, default=0)

    # Relationships for potential future queries (not strictly required now)
    species = relationship('Species')
    location = relationship('Location')

    def __repr__(self):
        return f'<Activity: SpID {self.species_id}, LocID {self.location_id}, {self.year}-{self.month}>'

class SpeciesYearlyTrend(CTBase):
    """Final table with computed annual trends and confidence intervals.

    scope_type: 'global' | 'institution' | 'ecoregion'
    scope_id:   '' for global, str(institution.id) for institution, ecoregion_uk for ecoregion
    """
    __tablename__ = 'species_yearly_trends'

    species_id = Column(Integer, ForeignKey('species.id'), primary_key=True)
    year = Column(Integer, primary_key=True)
    scope_type = Column(String(20), primary_key=True)
    scope_id = Column(String(100), primary_key=True)

    mean_dr_index = Column(Numeric(10, 4), nullable=False)
    lower_ci = Column(Numeric(10, 4), nullable=False)
    upper_ci = Column(Numeric(10, 4), nullable=False)

    species = relationship('Species')

    def __repr__(self):
        return f'<Trend: SpID {self.species_id}, Year {self.year}, {self.scope_type}:{self.scope_id}>'

class CalculationLog(CTBase):
    """Service table for tracking data state and recalculation needs."""
    __tablename__ = 'calculation_log'

    id = Column(Integer, primary_key=True)
    source_name = Column(String(100), unique=True, nullable=False) # e.g. 'completed_observations'
    last_count = Column(Integer, nullable=False, default=0)
    last_calculated_at = Column(DateTime, nullable=True)

    # State of the async recalculation (analytics_calculator.start_async_analytics):
    #   'idle'      — no recalculation in progress; last_calculated_at is the time of last success
    #   'running'   — background thread is executing update_analytics_tables
    #   'completed' — last run finished successfully
    #   'failed'    — last run crashed (details in error_message)
    # NB: on prod the columns are added via scripts/init_analytics_status.py
    # (create_all does not add columns to an existing table). Declaration here
    # is for new/dev installations.
    status = Column(String(20), nullable=False, default='idle')
    started_at = Column(DateTime, nullable=True)       # when the current/last run started
    error_message = Column(Text, nullable=True)        # error text from the last failed run

    def __repr__(self):
        return f'<Log: {self.source_name}, Count: {self.last_count}, Status: {self.status}>'

class BatteryType(CTBase):
    """Lookup table: battery types."""
    __tablename__ = 'battery_types'

    id = Column(Integer, primary_key=True)
    name_ua = Column(String(100), nullable=False, unique=True)
    name_en = Column(String(100), nullable=False, unique=True)
    is_rechargeable = Column(Boolean, nullable=False, default=False)

    def get_name(self, lang_code):
        return self.name_ua if lang_code == 'uk' else self.name_en

    def __repr__(self):
        return f'<BatteryType {self.name_en}>'

class VisitPurpose(CTBase):
    """Lookup table: visit purposes."""
    __tablename__ = 'visit_purposes'

    id = Column(Integer, primary_key=True)
    name_ua = Column(String(100), nullable=False, unique=True)
    name_en = Column(String(100), nullable=False, unique=True)

    def get_name(self, lang_code):
        return self.name_ua if lang_code == 'uk' else self.name_en

    def __repr__(self):
        return f'<VisitPurpose {self.name_en}>'

class ServiceVisit(CTBase):
    """Main table: camera trap service visit log."""
    __tablename__ = 'service_visits'

    id = Column(Integer, primary_key=True)
    location_id = Column(Integer, ForeignKey('locations.id'), nullable=False, index=True)
    user_id = Column(Integer, nullable=False)  # User ID from the main database
    visit_datetime = Column(DateTime, nullable=False, default=func.now())

    visit_purpose_id = Column(Integer, ForeignKey('visit_purposes.id'), nullable=False)
    battery_type_id = Column(Integer, ForeignKey('battery_types.id'), nullable=True) # Can be NULL if no battery was replaced

    is_camera_operational = Column(Boolean, nullable=True) # True/False/NULL (unknown)
    sd_card_changed = Column(Boolean, nullable=False, default=False)
    photos_on_card = Column(Integer, nullable=True) # Optional field
    comments = Column(Text, nullable=True)
    created_at = Column(DateTime, default=func.now())

    # Relationships
    location = relationship('Location', back_populates='service_visits')
    visit_purpose = relationship('VisitPurpose')
    battery_type = relationship('BatteryType')

    def __repr__(self):
        return f'<ServiceVisit LocID {self.location_id} at {self.visit_datetime}>'

class LocationStats(CTBase):
    """Table for storing computed statistics per location.

    Updated by a background process for fast access.
    """
    __tablename__ = 'location_stats'

    location_id = Column(Integer, ForeignKey('locations.id'), primary_key=True)
    total_photos = Column(Integer, nullable=False, default=0)
    avg_photos_per_day = Column(Numeric(10, 2), nullable=False, default=0.0)
    total_species = Column(Integer, nullable=False, default=0)
    animal_observations = Column(Integer, nullable=False, default=0) # species_id > 0
    empty_observations = Column(Integer, nullable=False, default=0) # species_id = -1
    other_observations = Column(Integer, nullable=False, default=0) # species_id < -1
    last_calculated_at = Column(DateTime, nullable=True)

    # One-to-one relationship with Location
    location = relationship('Location', backref=backref('stats', uselist=False))

    def __repr__(self):
        return f'<LocationStats for LocID {self.location_id}>'


# ════════════════════════════════════════════════════════════════════════════
# AI RUNNER: automatic image classification by a neural network
# ════════════════════════════════════════════════════════════════════════════
# Separate auxiliary sub-module. Predictions do not go into the final
# `identifications` table — they only suggest a species to the verifier.
# The worker (`services/biomon_ai/`) lives in a separate process with its
# own venv (torch + ultralytics) so the web app is not burdened. If the
# worker or model are not installed, Flask simply ignores this
# (a feature-flag checks for table presence + config).
# ════════════════════════════════════════════════════════════════════════════

class AIModelLevel(CTBase):
    """Lookup table for DeepFaune detector levels (normalisation to avoid
    duplicating the detector string in every ai_models / ai_predictions row).

    DeepFaune v1.4.1 has three base detectors that can be combined into an
    ensemble. ``accuracy_rank`` orders them by accuracy (higher = more accurate) —
    the identification page may prefer predictions from a higher-ranked level.

        DF       deepfaune-yolov8s_960            fast
        MDS      md_v1000.0.0-sorrel              medium (MegaDetector Sorrel)
        DF+MDS   deepfaune-yolov8s_960 + sorrel   ensemble (current prod)
        MDR      md_v1000.0.0-redwood             accurate (MegaDetector Redwood, 1280px)
    """
    __tablename__ = 'ai_model_levels'

    id            = Column(Integer, primary_key=True)
    code          = Column(String(32), nullable=False, unique=True)   # 'DF' | 'MDS' | 'DF+MDS' | 'MDR'
    name          = Column(String(128), nullable=False)               # human-readable name
    detector      = Column(String(128), nullable=True)                # detector string as in config_json
    accuracy_rank = Column(Integer, nullable=False, default=0)        # higher = more accurate
    description   = Column(Text, nullable=True)
    created_at    = Column(DateTime, default=func.now(), nullable=False)

    models = relationship('AIModel', back_populates='level')

    def __repr__(self):
        return f'<AIModelLevel {self.code} rank={self.accuracy_rank}>'


class AIModel(CTBase):
    """Registry of AI models used for classification.

    One row per (name, version) pair. is_active=True for the model currently
    used by the worker. Enables tracking which model produced a specific
    prediction and seamless migration to a new model/classifier.
    """
    __tablename__ = 'ai_models'

    id          = Column(Integer, primary_key=True)
    name        = Column(String(64), nullable=False)   # 'DeepFaune'
    version     = Column(String(32), nullable=False)   # '1.4.1'
    config_json = Column(JSONB, nullable=True)         # {detector, threshold, ...}
    is_active   = Column(Boolean, default=True, nullable=False)
    level_id    = Column(Integer, ForeignKey('ai_model_levels.id'), nullable=True)  # detector level (lookup)
    created_at  = Column(DateTime, default=func.now(), nullable=False)

    predictions = relationship('AIPrediction', back_populates='model')
    level       = relationship('AIModelLevel', back_populates='models')

    __table_args__ = (
        # Models are also distinguished by detector level: the same DeepFaune version
        # may have been run at different levels (DF+MDS on prod, MDR via import).
        UniqueConstraint('name', 'version', 'level_id', name='uq_ai_models_name_version_level'),
    )

    def __repr__(self):
        return f'<AIModel {self.name} {self.version}>'


class AIPrediction(CTBase):
    """AI prediction for a single photo. One model = one row per photo.

    Stores three prediction variants simultaneously (sequence-aware, per-photo,
    top1) so that filters can be rebuilt in the future (e.g. changing the
    confidence threshold) without re-running the model.

    ``photo_id`` deliberately has no ``ondelete=CASCADE``: CT cleanup tasks only
    archive Photo records (status='archived') without deleting the row itself —
    so the FK remains valid. If Photo records are ever physically deleted, the
    schema will need to be denormalised (store path/captured_at directly in
    ``ai_predictions``).
    """
    __tablename__ = 'ai_predictions'

    id                    = Column(Integer, primary_key=True)
    photo_id              = Column(Integer, ForeignKey('photos.id'), nullable=False)
    observation_id        = Column(Integer, ForeignKey('observations.id'), nullable=False)  # denormalized for fast filtering
    model_id              = Column(Integer, ForeignKey('ai_models.id'), nullable=False)

    # Sequence-aware prediction (DeepFaune aggregates over the series)
    prediction_label      = Column(String(64), nullable=True)   # raw label from the model, e.g. 'roe deer'
    prediction_species_id = Column(Integer, ForeignKey('species.id'), nullable=True)  # nullable: when no mapping exists
    prediction_score      = Column(Float, nullable=True)        # 0..1

    # Per-photo (no aggregation over the series)
    base_label            = Column(String(64), nullable=True)
    base_score            = Column(Float, nullable=True)

    # Top-1 always, regardless of threshold — for future metrics
    top1_label            = Column(String(64), nullable=True)
    top1_score            = Column(Float, nullable=True)

    # Auxiliary fields
    animal_count          = Column(Integer, nullable=True)
    human_count           = Column(Integer, nullable=True)
    bbox_json             = Column(JSONB, nullable=True)        # best bounding box from the detector

    # Whether the prediction matched the consensus species — filled when consensus
    # is reached (Idea 4). nullable: None = not yet evaluated
    # (pending series) or AI did not identify a species (prediction_species_id IS NULL).
    was_correct           = Column(Boolean, nullable=True)

    processed_at          = Column(DateTime, default=func.now(), nullable=False)
    error_msg             = Column(Text, nullable=True)

    # Relationships
    photo            = relationship('Photo')
    observation      = relationship('Observation')
    model            = relationship('AIModel', back_populates='predictions')
    species          = relationship('Species', foreign_keys=[prediction_species_id])

    __table_args__ = (
        UniqueConstraint('photo_id', 'model_id', name='uq_ai_predictions_photo_model'),
        # Most important index — filter on /identify: "find series where AI says X"
        Index('idx_ai_pred_filter',
              'model_id', 'prediction_species_id', 'observation_id'),
        Index('idx_ai_pred_observation', 'observation_id'),
    )

    def __repr__(self):
        return f'<AIPrediction photo={self.photo_id} → {self.prediction_label} ({self.prediction_score})>'


class AILabelMap(CTBase):
    """Lookup table: raw DeepFaune label → biomon Species.id.

    The SINGLE source of truth for label mapping, shared by:
      • the worker (services/biomon_ai) — read at startup, falls back to
        the hard-coded DEEPFAUNE_TO_SPECIES_ID if the table is empty/unavailable;
      • the classification import page (app/camera_traps).

    species_id = NULL means the species is not in Species — the raw label is
    still stored in ai_predictions.prediction_label, so a back-fill is possible
    once the species is added. Editable without a code redeploy.
    """
    __tablename__ = 'ai_label_map'

    label      = Column(String(64), primary_key=True)               # 'roe deer', 'bird raptor', 'empty', ...
    species_id = Column(Integer, ForeignKey('species.id'), nullable=True)
    note       = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)

    species = relationship('Species', foreign_keys=[species_id])

    def __repr__(self):
        return f'<AILabelMap {self.label!r} → {self.species_id}>'


class AIRunQueue(CTBase):
    """Queue for manual AI worker runs (admin button).

    The worker (cron or systemd-timer) periodically scans this table — if
    there are pending records it processes the specified number of observations
    and writes the result. For the nightly automatic batch the worker does NOT
    use the queue — it simply picks the N=AI_MAX_PER_RUN oldest pending
    observations.

    ``requested_by`` is a user id from the MAIN database (users), not ct_db.
    No FK is set because this is a cross-DB reference; stored as a plain INTEGER.
    """
    __tablename__ = 'ai_run_queue'

    id              = Column(Integer, primary_key=True)
    requested_by    = Column(Integer, nullable=False)            # users.id from the main database
    requested_at    = Column(DateTime, default=func.now(), nullable=False)
    n_observations  = Column(Integer, nullable=False)            # number of observations the worker will process
    status          = Column(String(16), default='pending', nullable=False)  # pending|running|done|failed
    started_at      = Column(DateTime, nullable=True)
    finished_at     = Column(DateTime, nullable=True)
    processed_count = Column(Integer, nullable=True)             # actual number processed
    error_msg       = Column(Text, nullable=True)

    # Generated STORED column in PostgreSQL: finished_at - started_at.
    # The DB fills this on every UPDATE; Python does NOT write here.
    # Computed(..., persisted=True) → STORED (not VIRTUAL).
    duration        = Column(Interval, Computed('finished_at - started_at', persisted=True), nullable=True)

    __table_args__ = (
        Index('idx_ai_queue_status', 'status', 'requested_at'),
    )

    def __repr__(self):
        return f'<AIRunQueue {self.id} {self.status} n={self.n_observations}>'


# ════════════════════════════════════════════════════════════════════════════
# CLEANUP LOG: audit log for dry-run/execute orphan and failed-batch cleanup
# ════════════════════════════════════════════════════════════════════════════
# Cleanup consists of two steps (analyze → execute). One row in this table
# accompanies both: the full report is stored in report_json first, then
# the deleted_* counters are filled after execute.
# Retention: rows older than CLEANUP_LOG_RETENTION_DAYS are removed on the
# next analyze call (see cleanup.purge_old_logs).
# ════════════════════════════════════════════════════════════════════════════

class CleanupLog(CTBase):
    """Cleanup operations log."""
    __tablename__ = 'cleanup_log'

    id              = Column(String(36), primary_key=True)  # UUID
    kind            = Column(String(20), nullable=False)    # 'analysis' | 'execution'
    status          = Column(String(20), nullable=False)    # 'analyzing'|'analyzed'|'executing'|'completed'|'failed'
    triggered_by    = Column(Integer, nullable=False)       # users.id from the main database
    started_at      = Column(DateTime, default=func.now(), nullable=False)
    finished_at     = Column(DateTime, nullable=True)

    # Launch parameters
    threshold_hours = Column(Integer, nullable=False, default=0)

    # Report after analyze (lists + aggregates)
    report_json     = Column(JSONB, nullable=True)

    # Execution summary
    batches_examined = Column(Integer, nullable=True)
    batches_marked_failed = Column(Integer, nullable=True)
    photos_deleted  = Column(Integer, nullable=True)
    files_deleted   = Column(Integer, nullable=True)
    bytes_freed     = Column(Integer, nullable=True)

    error_message   = Column(Text, nullable=True)

    __table_args__ = (
        Index('idx_cleanup_log_started', 'started_at'),
        Index('idx_cleanup_log_status', 'status'),
    )

    def __repr__(self):
        return f'<CleanupLog {self.id[:8]} {self.kind}/{self.status}>'

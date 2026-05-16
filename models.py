# myproject/app/camera_traps/models.py

from sqlalchemy import Column, Integer, String, DateTime, Boolean, Text, Numeric, Float, ForeignKey, Index, Table, Interval
from sqlalchemy import CheckConstraint, Computed, UniqueConstraint, func
from sqlalchemy.orm import relationship, backref
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from datetime import datetime

from .database import CTBase

# Проміжна таблиця для зв'язку many-to-many між Identification та BehaviorType
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
    class_ = Column("class", String(100)) # "class" - це назва колонки в БД
    order_rank = Column(String(100))
    family = Column(String(100))
    genus = Column(String(100))
    establishment_means = Column(String(100))

    # Зв'язки
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
    created_by_id = Column(Integer, nullable=True)  # ID користувача з основної БД
    visibility_level = Column(Integer, default=1, nullable=False)

    # Зв'язки
    observations = relationship('Observation', back_populates='location')
    biotopes = relationship('Biotope', secondary=location_biotopes, backref='locations')
    service_visits = relationship('ServiceVisit', back_populates='location', order_by=lambda: ServiceVisit.visit_datetime.desc())
    
    # Індекс для швидкого пошуку за округленими координатами
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

    # Зв'язки
    location = relationship('Location', back_populates='observations')
    
    # Старий relationship (залишаємо для зворотної сумісності)
    photos = relationship('Photo', back_populates='observation')
    
    # Новий relationship для хронологічного порядку
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

    # Зв'язки
    location = relationship('Location')
    photos = relationship('Photo', back_populates='upload_batch')

    def __repr__(self):
        return f'<UploadBatch {self.id[:8]}...>'

class Photo(CTBase):
    __tablename__ = 'photos'
    
    id = Column(Integer, primary_key=True)
    observation_id = Column(Integer, ForeignKey('observations.id'), nullable=True)  # Тепер може бути NULL
    upload_batch_id = Column(String(36), ForeignKey('upload_batches.id'), nullable=True)  # Новий зв'язок
    original_filename = Column(String(500), nullable=False)
    system_filename = Column(String(500), unique=True, nullable=False)
    sequence_number = Column(Integer, nullable=True)  # Тепер може бути NULL до групування
    captured_at = Column(DateTime, nullable=False)
    status = Column(String(20), default='uploaded', nullable=False)  # uploaded, grouped, pending, completed, archived, needs_review
    identification_count = Column(Integer, default=0)
    is_favorite = Column(Boolean, default=False, nullable=False)

    # Зв'язки
    observation = relationship('Observation', back_populates='photos')
    upload_batch = relationship('UploadBatch', back_populates='photos')
    identifications = relationship('Identification', back_populates='photo')

    def __repr__(self):
        return f'<Photo {self.system_filename}>'

class BehaviorType(CTBase):
    __tablename__ = 'behavior_types'
    
    id = Column(Integer, primary_key=True)
    name_ua = Column(String(100), nullable=False, unique=True)
    name_en = Column(String(100), nullable=False, unique=True)

    # Зв'язки
    identifications = relationship('Identification', secondary=identification_behaviors, back_populates='behaviors')
    
    def get_name(self, lang_code):
        return self.name_ua if lang_code == 'uk' else self.name_en

    def __repr__(self):
        return f'<BehaviorType {self.name_en}>'

class Identification(CTBase):
    __tablename__ = 'identifications'
    
    id = Column(Integer, primary_key=True)
    photo_id = Column(Integer, ForeignKey('photos.id'), nullable=False)
    user_id = Column(Integer, nullable=False)  # ID користувача з основної БД
    species_id = Column(Integer, ForeignKey('species.id'), nullable=True)  # None для "Інший вид"
    confidence_level = Column(Integer, CheckConstraint('confidence_level >= 1 AND confidence_level <= 5'))
    quantity = Column(Integer, default=1)
    comment = Column(Text)
    created_at = Column(DateTime, default=func.now())

    # Зв'язки
    photo = relationship('Photo', back_populates='identifications')
    species = relationship('Species', back_populates='identifications')
    behaviors = relationship('BehaviorType', secondary=identification_behaviors, back_populates='identifications')

    # Унікальне обмеження: один користувач може ідентифікувати одне фото тільки раз
    __table_args__ = (
        UniqueConstraint('photo_id', 'user_id', name='_photo_user_uc'),
    )

    def __repr__(self):
        return f'<Identification {self.id} by user {self.user_id}>'

class UserProfile(CTBase):
    __tablename__ = 'user_profiles'
    
    user_id = Column(Integer, primary_key=True)  # ID користувача з основної БД
    camera_trap_role = Column(String(20), default='viewer', nullable=False)
    identifications_count = Column(Integer, default=0, nullable=False)
    accuracy_score = Column(Numeric(5, 2), default=0.0, nullable=False)

    def __repr__(self):
        return f'<UserProfile {self.user_id}>'

class LocationMergeLog(CTBase):
    __tablename__ = 'location_merge_log'
    
    id = Column(Integer, primary_key=True)
    merged_by_id = Column(Integer, nullable=False)  # ID користувача з основної БД
    main_location_id = Column(Integer, ForeignKey('locations.id'), nullable=False)
    merged_location_ids = Column(ARRAY(Integer), nullable=False)
    merged_location_names = Column(ARRAY(String), nullable=False)
    merge_reason = Column(Text)
    created_at = Column(DateTime, default=func.now())

    # Зв'язки
    main_location = relationship('Location')

    def __repr__(self):
        return f'<LocationMergeLog {self.id}>'
    
class LocationMonthlyActivity(CTBase):
    """
    Проміжна таблиця для зберігання щомісячної активності по локаціях.
    Розраховується фоновим процесом.
    """
    __tablename__ = 'location_monthly_activity'
    
    # Використовуємо складений первинний ключ для унікальності та швидкості пошуку
    species_id = Column(Integer, ForeignKey('species.id'), primary_key=True)
    location_id = Column(Integer, ForeignKey('locations.id'), primary_key=True)
    year = Column(Integer, primary_key=True)
    month = Column(Integer, primary_key=True)
    
    detection_count = Column(Integer, nullable=False, default=0)
    trap_days = Column(Integer, nullable=False, default=0)

    # Зв'язки для можливих майбутніх запитів (не обов'язкові зараз)
    species = relationship('Species')
    location = relationship('Location')

    def __repr__(self):
        return f'<Activity: SpID {self.species_id}, LocID {self.location_id}, {self.year}-{self.month}>'

class SpeciesYearlyTrend(CTBase):
    """
    Фінальна таблиця з розрахованими річними трендами та довірчими інтервалами.
    scope_type: 'global' | 'institution' | 'ecoregion'
    scope_id:   '' для global, str(institution.id) для institution, ecoregion_uk для ecoregion
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
    """
    Сервісна таблиця для відстеження стану даних та необхідності перерахунку.
    """
    __tablename__ = 'calculation_log'
    
    id = Column(Integer, primary_key=True)
    source_name = Column(String(100), unique=True, nullable=False) # Напр. 'completed_observations'
    last_count = Column(Integer, nullable=False, default=0)
    last_calculated_at = Column(DateTime, nullable=True)

    def __repr__(self):
        return f'<Log: {self.source_name}, Count: {self.last_count}>'
    
class BatteryType(CTBase):
    """Довідкова таблиця: Типи елементів живлення."""
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
    """Довідкова таблиця: Цілі візиту."""
    __tablename__ = 'visit_purposes'
    
    id = Column(Integer, primary_key=True)
    name_ua = Column(String(100), nullable=False, unique=True)
    name_en = Column(String(100), nullable=False, unique=True)

    def get_name(self, lang_code):
        return self.name_ua if lang_code == 'uk' else self.name_en

    def __repr__(self):
        return f'<VisitPurpose {self.name_en}>'

class ServiceVisit(CTBase):
    """Основна таблиця: Журнал обслуговування фотопасток."""
    __tablename__ = 'service_visits'
    
    id = Column(Integer, primary_key=True)
    location_id = Column(Integer, ForeignKey('locations.id'), nullable=False, index=True)
    user_id = Column(Integer, nullable=False)  # ID користувача з основної БД
    visit_datetime = Column(DateTime, nullable=False, default=func.now())
    
    visit_purpose_id = Column(Integer, ForeignKey('visit_purposes.id'), nullable=False)
    battery_type_id = Column(Integer, ForeignKey('battery_types.id'), nullable=True) # Може бути NULL, якщо заміна не проводилась
    
    is_camera_operational = Column(Boolean, nullable=True) # True/False/NULL (невідомо)
    sd_card_changed = Column(Boolean, nullable=False, default=False)
    photos_on_card = Column(Integer, nullable=True) # Необов'язкове поле
    comments = Column(Text, nullable=True)
    created_at = Column(DateTime, default=func.now())

    # Зв'язки
    location = relationship('Location', back_populates='service_visits')
    visit_purpose = relationship('VisitPurpose')
    battery_type = relationship('BatteryType')

    def __repr__(self):
        return f'<ServiceVisit LocID {self.location_id} at {self.visit_datetime}>'

class LocationStats(CTBase):
    """
    Таблиця для зберігання розрахованої статистики по кожній локації.
    Оновлюється фоновим процесом для швидкого доступу.
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

    # Зв'язок "один-до-одного" з локацією
    location = relationship('Location', backref=backref('stats', uselist=False))

    def __repr__(self):
        return f'<LocationStats for LocID {self.location_id}>'


# ════════════════════════════════════════════════════════════════════════════
# AI-РУНЕР: автоматична класифікація зображень нейромережею
# ════════════════════════════════════════════════════════════════════════════
# Окремий допоміжний підмодуль. Прогнози не йдуть у фінальні `identifications`,
# а лише підказують верифікатору вид. Worker (`services/biomon_ai/`) живе в
# окремому процесі з власним venv (torch + ultralytics), щоб не вантажити
# веб-додаток. Якщо worker або модель не встановлені — Flask цього просто
# не помічає (feature-flag перевіряє наявність таблиць + кофіг).
# ════════════════════════════════════════════════════════════════════════════

class AIModel(CTBase):
    """Реєстр AI-моделей, що використовувалися для класифікації.

    Один рядок на пару (name, version). is_active=True для тієї, яку
    зараз використовує worker. Дозволяє трекати, яка модель видала
    конкретний прогноз, і безболісно мігрувати на нову модель/класифікатор.
    """
    __tablename__ = 'ai_models'

    id          = Column(Integer, primary_key=True)
    name        = Column(String(64), nullable=False)   # 'DeepFaune'
    version     = Column(String(32), nullable=False)   # '1.4.1'
    config_json = Column(JSONB, nullable=True)         # {detector, threshold, ...}
    is_active   = Column(Boolean, default=True, nullable=False)
    created_at  = Column(DateTime, default=func.now(), nullable=False)

    predictions = relationship('AIPrediction', back_populates='model')

    __table_args__ = (
        UniqueConstraint('name', 'version', name='uq_ai_models_name_version'),
    )

    def __repr__(self):
        return f'<AIModel {self.name} {self.version}>'


class AIPrediction(CTBase):
    """Прогноз AI по одному фото. Одна модель = один рядок на фото.

    Зберігаємо одразу 3 варіанти прогнозу (sequence-aware, per-photo, top1)
    щоб у майбутньому можна було перебудовувати фільтри (наприклад,
    змінити поріг впевненості) без перепрогону моделі.

    `photo_id` — без `ondelete=CASCADE` навмисно: cleanup-таски CT лише
    архівують Photo (status='archived'), сам запис не видаляють — тож
    FK залишається валідним. Якщо колись Photo все-таки буде видалятись —
    схему доведеться денормалізувати (зберігати path/captured_at у самому
    `ai_predictions`).
    """
    __tablename__ = 'ai_predictions'

    id                    = Column(Integer, primary_key=True)
    photo_id              = Column(Integer, ForeignKey('photos.id'), nullable=False)
    observation_id        = Column(Integer, ForeignKey('observations.id'), nullable=False)  # денормалізовано для швидких фільтрів
    model_id              = Column(Integer, ForeignKey('ai_models.id'), nullable=False)

    # Sequence-aware прогноз (DeepFaune агрегує по серії)
    prediction_label      = Column(String(64), nullable=True)   # сирий label від моделі, напр. 'roe deer'
    prediction_species_id = Column(Integer, ForeignKey('species.id'), nullable=True)  # nullable: коли мапінга немає
    prediction_score      = Column(Float, nullable=True)        # 0..1

    # Per-photo (без агрегації по серії)
    base_label            = Column(String(64), nullable=True)
    base_score            = Column(Float, nullable=True)

    # Топ-1 завжди, незалежно від threshold — для майбутніх метрик
    top1_label            = Column(String(64), nullable=True)
    top1_score            = Column(Float, nullable=True)

    # Допоміжне
    animal_count          = Column(Integer, nullable=True)
    human_count           = Column(Integer, nullable=True)
    bbox_json             = Column(JSONB, nullable=True)        # найкращий bbox від детектора

    processed_at          = Column(DateTime, default=func.now(), nullable=False)
    error_msg             = Column(Text, nullable=True)

    # Зв'язки
    photo            = relationship('Photo')
    observation      = relationship('Observation')
    model            = relationship('AIModel', back_populates='predictions')
    species          = relationship('Species', foreign_keys=[prediction_species_id])

    __table_args__ = (
        UniqueConstraint('photo_id', 'model_id', name='uq_ai_predictions_photo_model'),
        # Найважливіший індекс — фільтр на /identify: "знайти серії, де AI каже X"
        Index('idx_ai_pred_filter',
              'model_id', 'prediction_species_id', 'observation_id'),
        Index('idx_ai_pred_observation', 'observation_id'),
    )

    def __repr__(self):
        return f'<AIPrediction photo={self.photo_id} → {self.prediction_label} ({self.prediction_score})>'


class AIRunQueue(CTBase):
    """Черга ручних запусків AI-воркера (адмін-кнопка).

    Worker (cron або systemd-timer) періодично сканує цю таблицю — якщо
    є pending записи, обробляє вказану кількість observation і записує
    результат. Для нічного автоматичного batch worker не використовує
    чергу — просто бере N=AI_MAX_PER_RUN найстаріших pending observation.

    `requested_by` — id користувача з ОСНОВНОЇ БД (users), а не з ct_db.
    FK не ставимо, бо це cross-DB; зберігаємо як простий INTEGER.
    """
    __tablename__ = 'ai_run_queue'

    id              = Column(Integer, primary_key=True)
    requested_by    = Column(Integer, nullable=False)            # users.id з основної БД
    requested_at    = Column(DateTime, default=func.now(), nullable=False)
    n_observations  = Column(Integer, nullable=False)            # скільки серій оброблятиме worker
    status          = Column(String(16), default='pending', nullable=False)  # pending|running|done|failed
    started_at      = Column(DateTime, nullable=True)
    finished_at     = Column(DateTime, nullable=True)
    processed_count = Column(Integer, nullable=True)             # фактично оброблено
    error_msg       = Column(Text, nullable=True)

    # Згенерована STORED колонка в PostgreSQL: finished_at - started_at.
    # БД заповнює сама при кожному UPDATE; Python НЕ пише сюди.
    # Computed(..., persisted=True) → STORED (не VIRTUAL).
    duration        = Column(Interval, Computed('finished_at - started_at', persisted=True), nullable=True)

    __table_args__ = (
        Index('idx_ai_queue_status', 'status', 'requested_at'),
    )

    def __repr__(self):
        return f'<AIRunQueue {self.id} {self.status} n={self.n_observations}>'
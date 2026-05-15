"""Flask-side helpers для AI-runner'а.

Flask нічого з services/biomon_ai/ не імпортує — той живе в окремому venv.
Тут — лише читання/запис в ai_* таблиці через основний CT-engine.

Призначення:
    is_ai_available()      — feature flag для template (показ кнопки і фільтру)
    request_run()          — створити запит у ai_run_queue (адмін-кнопка)
    get_recent_requests()  — показати останні запити з статусами
    get_active_model()     — який класифікатор зараз активний
"""

from typing import Optional

from flask import current_app
from sqlalchemy import inspect

from .database import get_ct_engine, get_ct_session
from .models import AIModel, AIRunQueue


_AI_TABLES = ('ai_models', 'ai_predictions', 'ai_run_queue')

# Кеш на час життя процесу — щоб не тягати information_schema на кожен запит
_tables_checked: bool = False
_tables_exist: bool = False


def is_ai_available() -> bool:
    """True якщо AI-runner налаштований і доступний з боку Flask.

    Перевіряє:
      1. config AI_RUNNER.ENABLED (на dev-машині завжди False)
      2. Усі 3 ai_* таблиці існують у ct_db (на серверах де модуль
         фотопасток встановлений, але AI-схему не накатили — теж False)

    Перевірка таблиць кешується (виконується один раз на запуск процесу).
    """
    cfg = (current_app.config.get('CAMERA_TRAP_CONFIG') or {}).get('AI_RUNNER') or {}
    if not cfg.get('ENABLED', False):
        return False

    return _ai_tables_exist()


def _ai_tables_exist() -> bool:
    global _tables_checked, _tables_exist
    if _tables_checked:
        return _tables_exist

    try:
        engine = get_ct_engine()
        insp = inspect(engine)
        existing = set(insp.get_table_names())
        _tables_exist = all(t in existing for t in _AI_TABLES)
    except Exception as e:
        current_app.logger.warning(f"AI: cannot inspect ct_db schema: {e}")
        _tables_exist = False

    _tables_checked = True
    return _tables_exist


def _reset_cache():
    """Для тестів: примусово перевірити таблиці знову."""
    global _tables_checked, _tables_exist
    _tables_checked = False
    _tables_exist = False


def request_run(user_id: int, n_observations: int) -> AIRunQueue:
    """Створює запис у ai_run_queue зі статусом 'pending'.

    Worker (cron) підхопить його у наступному прогоні. Повертає створений
    об'єкт (не від'єднаний від сесії — викликач має зробити commit АБО
    використати окремий context manager).
    """
    sess = get_ct_session()
    req = AIRunQueue(
        requested_by=user_id,
        n_observations=n_observations,
        status='pending',
    )
    sess.add(req)
    sess.commit()
    sess.refresh(req)
    return req


def get_recent_requests(limit: int = 5) -> list:
    """Останні запити для відображення статусу на адмін-сторінці."""
    sess = get_ct_session()
    return (
        sess.query(AIRunQueue)
        .order_by(AIRunQueue.requested_at.desc())
        .limit(limit)
        .all()
    )


def get_active_model() -> Optional[AIModel]:
    """Поточна активна AI-модель або None.

    Якщо None — worker ще не запускався або жодна модель не зареєстрована.
    """
    sess = get_ct_session()
    return sess.query(AIModel).filter_by(is_active=True).first()


def get_species_with_ai_predictions(
    lang_code: str = 'uk',
    user_id: Optional[int] = None,
    user_inst_ids: Optional[list] = None,
    is_admin: bool = False,
) -> list:
    """Повертає [(species_id, display_name)] видів які мають **pending для
    цього юзера** AI-прогнози від активної моделі. Тобто:
      - observation.status='pending'
      - користувач ще не визначав жодного фото цієї серії
      - локація доступна юзеру (admin — всі; інакше visibility_level=0
        АБО локація належить інституціям юзера)

    display_name містить кількість таких серій у дужках, наприклад
    "Козуля (Capreolus capreolus) (42)".

    Якщо user_id=None — повертаємо всі види без фільтра по юзеру
    (для тестів / debug).
    """
    from sqlalchemy import bindparam, text as sql_text
    sess = get_ct_session()
    active = sess.query(AIModel).filter_by(is_active=True).first()
    if active is None:
        return []

    # Будуємо access-clause (та сама логіка що в next_observation_for_identification)
    if is_admin:
        access_clause = ""
        access_params = {}
    elif user_inst_ids:
        access_clause = """
            AND (l.visibility_level = 0 OR EXISTS (
                SELECT 1 FROM location_institutions li
                WHERE li.location_id = l.id
                  AND li.institution_id IN :inst_ids
            ))
        """
        access_params = {'inst_ids': tuple(user_inst_ids)}
    else:
        access_clause = "AND l.visibility_level = 0"
        access_params = {}

    user_clause = ""
    user_params = {}
    if user_id is not None:
        user_clause = """
            AND NOT EXISTS (
                SELECT 1 FROM photos pu
                JOIN identifications i ON i.photo_id = pu.id
                WHERE pu.observation_id = o.id AND i.user_id = :uid
            )
        """
        user_params = {'uid': user_id}

    sql = sql_text(f"""
        SELECT s.id,
               s.common_name_ua,
               s.common_name_en,
               s.scientific_name,
               COUNT(DISTINCT ap.observation_id) AS pending_count
        FROM species s
        JOIN ai_predictions ap ON ap.prediction_species_id = s.id
        JOIN observations o ON o.id = ap.observation_id
        JOIN locations l ON l.id = o.location_id
        WHERE ap.model_id = :model_id
          AND o.status = 'pending'
          {user_clause}
          {access_clause}
        GROUP BY s.id, s.common_name_ua, s.common_name_en, s.scientific_name
        HAVING COUNT(DISTINCT ap.observation_id) > 0
        ORDER BY s.common_name_ua
    """)

    # IN-clause з тимчасовим списком потребує expanding-bindparam.
    if 'inst_ids' in access_params:
        sql = sql.bindparams(bindparam('inst_ids', expanding=True))

    rows = sess.execute(sql, {
        'model_id': active.id,
        **user_params,
        **access_params,
    }).fetchall()

    result = []
    for s in rows:
        if lang_code == 'en':
            name = s.common_name_en or s.common_name_ua or s.scientific_name
        else:
            name = s.common_name_ua or s.common_name_en or s.scientific_name
        if s.id > 0 and s.scientific_name:
            name = f"{name} ({s.scientific_name})"
        # Лічильник у дужках в кінці
        name = f"{name} ({s.pending_count})"
        result.append({'id': s.id, 'text': name})
    return result


def get_observation_ai_prediction(observation_id: int) -> Optional[dict]:
    """Повертає прогноз AI для observation (від активної моделі) або None.

    Оскільки worker зберігає прогноз на кожне фото, але для серії всі фото
    мають однаковий sequence-aware prediction, беремо просто перший рядок.
    Якщо prediction_species_id IS NULL (немає мапінга на наш Species) —
    повертаємо тільки сирий label без species_id.

    Структура повернення:
        {
            'species_id':       int або None,
            'species_label':    str (DeepFaune raw label, напр. 'roe deer'),
            'score':            float (0..1),
            'animal_count':     int,
        }
    """
    from .models import AIPrediction

    sess = get_ct_session()
    active = sess.query(AIModel).filter_by(is_active=True).first()
    if active is None:
        return None

    row = (
        sess.query(AIPrediction)
        .filter(
            AIPrediction.observation_id == observation_id,
            AIPrediction.model_id == active.id,
        )
        .order_by(AIPrediction.prediction_score.desc().nullslast())
        .first()
    )
    if row is None:
        return None

    return {
        'species_id':    row.prediction_species_id,
        'species_label': row.prediction_label,
        'score':         row.prediction_score,
        'animal_count':  row.animal_count,
    }

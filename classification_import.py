"""Імпорт результатів зовнішньої класифікації DeepFaune (CSV) у ct_db.

Призначення
-----------
Локально (на CUDA, точнішим детектором MDR — MegaDetector Redwood) користувач
проганяє DeepFaune і отримує CSV. Сервер biomon класифікує слабшим ensemble
DF+MDS. Цей модуль дозволяє накласти кращі локальні результати на ВЖЕ
завантажені у biomon фото — по одній локації за раз.

Ключові рішення (узгоджені з користувачем)
------------------------------------------
* Окрема модель `DeepFaune 1.4.1 @ MDR` (is_active=False) — не чіпає
  серверні прогнози DF+MDS; обидва набори співіснують (розрізняються
  `ai_models.level_id`).
* Матчинг per-фото: `basename(filename)` + `captured_at` (до секунди) у межах
  обраної локації. `captured_at` у БД без субсекунд, колізій нема — ключ точний.
* У `ai_predictions` пишемо per-фото `base_label/base_score` (+ top1, counts) —
  серій-незалежне джерело істини.
* Серієвий `prediction_*` НЕ беремо з CSV (там серії по 10с), а рахуємо
  самостійно по поточних `observations` у БД (правило: тварина > людина >
  empty; серед тварин — максимальний base_score). Перезапускний при зміні
  групування.
* Мапінг label→species — з довідника `ai_label_map` (єдине джерело правди,
  спільне з worker).
"""

import csv
import io
import os
from datetime import datetime

from sqlalchemy import text, literal_column
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .models import AIModel, AIModelLevel, AIPrediction

IMPORT_MODEL_NAME = 'DeepFaune'
IMPORT_MODEL_VERSION = '1.4.1'
# Рівень детектора користувач обирає на сторінці (обов'язково) — раніше був
# жорстко зашитий MDR. CSV рівень не містить, тож визначити його автоматично
# неможливо.

# Спецкласи, що НЕ є твариною (для агрегації серії).
NON_ANIMAL_LABELS = {'empty', 'human', 'vehicle'}

CSV_DATE_FMT = '%Y:%m:%d %H:%M:%S'
# Лише справді необхідні колонки. Решта (top1, count, humancount) — опційні:
# різні експорти/версії DeepFaune дають різний набір (напр. без `count`).
REQUIRED_COLUMNS = {'filename', 'date', 'predictionbase', 'scorebase'}


# ──────────────────────────────────────────────────────────────────────────
# Парсинг CSV
# ──────────────────────────────────────────────────────────────────────────
def _to_float(v):
    try:
        return float(v) if v not in (None, '') else None
    except (TypeError, ValueError):
        return None


def _to_int(v):
    try:
        return int(float(v)) if v not in (None, '') else None
    except (TypeError, ValueError):
        return None


def parse_deepfaune_csv(file_obj):
    """Парсить CSV-файл DeepFaune.

    Args:
        file_obj: file-like (binary або text) або bytes/str.

    Returns:
        (rows, errors): rows — список dict з нормалізованими полями:
            original_filename (basename), captured_at (datetime до секунди),
            base_label, base_score, top1_label, animal_count, human_count.
        errors — список рядків-повідомлень про проблемні рядки.
    """
    if isinstance(file_obj, bytes):
        text_stream = io.StringIO(file_obj.decode('utf-8-sig'))
    elif isinstance(file_obj, str):
        text_stream = io.StringIO(file_obj)
    else:
        raw = file_obj.read()
        if isinstance(raw, bytes):
            raw = raw.decode('utf-8-sig')
        text_stream = io.StringIO(raw)

    reader = csv.DictReader(text_stream)
    if reader.fieldnames is None:
        return [], ['Порожній або нечитабельний CSV']
    missing = REQUIRED_COLUMNS - {c.strip() for c in reader.fieldnames}
    if missing:
        return [], [f'Бракує колонок: {", ".join(sorted(missing))}']

    rows, errors = [], []
    for i, raw in enumerate(reader, start=2):  # рядок 1 — заголовок
        fn = (raw.get('filename') or '').strip()
        if not fn:
            continue
        date_str = (raw.get('date') or '').strip()
        try:
            captured = datetime.strptime(date_str, CSV_DATE_FMT).replace(microsecond=0)
        except ValueError:
            errors.append(f'рядок {i}: некоректна дата {date_str!r}')
            continue
        base_label = (raw.get('predictionbase') or '').strip().lower()
        rows.append({
            'original_filename': os.path.basename(fn.replace('\\', '/')),
            'captured_at': captured,
            'base_label': base_label or None,
            'base_score': _to_float(raw.get('scorebase')),
            'top1_label': (raw.get('top1') or '').strip().lower() or None,
            'animal_count': _to_int(raw.get('count')),
            'human_count': _to_int(raw.get('humancount')),
        })
    return rows, errors


# ──────────────────────────────────────────────────────────────────────────
# Допоміжне
# ──────────────────────────────────────────────────────────────────────────
def get_import_levels(session):
    """Рівні, доступні для ІМПОРТУ — усі з ai_model_levels, КРІМ тих, які вже
    використовує активна (серверна) модель. Так імпорт ніколи не пише у
    серверний набір (напр. DF+MDS), а пропонує лише DF / MDS / MDR.

    Повертає список AIModelLevel за зростанням accuracy_rank."""
    active_level_ids = [
        lid for (lid,) in session.query(AIModel.level_id).filter(AIModel.is_active.is_(True)).all()
        if lid is not None
    ]
    q = session.query(AIModelLevel)
    if active_level_ids:
        q = q.filter(~AIModelLevel.id.in_(active_level_ids))
    return q.order_by(AIModelLevel.accuracy_rank).all()


def get_or_create_import_model(session, level_id):
    """Повертає (створюючи за потреби) рядок AIModel для обраного рівня.

    Рівень обов'язковий. Модель розрізняється за (name, version, level_id),
    тож для кожного рівня — свій рядок ai_models, усі is_active=False
    (щоб не чіпати worker / серверну активну модель)."""
    if not level_id:
        raise ValueError('Не вказано рівень моделі')
    level = session.query(AIModelLevel).get(level_id)
    if level is None:
        raise ValueError('Невідомий рівень моделі')

    model = (session.query(AIModel)
             .filter_by(name=IMPORT_MODEL_NAME, version=IMPORT_MODEL_VERSION, level_id=level.id)
             .one_or_none())
    if model is not None and model.is_active:
        # Запобіжник: ніколи не писати в активну (серверну) модель.
        raise ValueError('Цей рівень належить активній серверній моделі — імпорт заборонено')
    if model is None:
        model = AIModel(
            name=IMPORT_MODEL_NAME,
            version=IMPORT_MODEL_VERSION,
            level_id=level.id,
            is_active=False,
            config_json={'detector': level.detector, 'source': 'local-import', 'device': 'cuda'},
        )
        session.add(model)
        session.flush()
    return model


def load_label_map(session):
    """{label(lower) -> species_id|None} із довідника ai_label_map."""
    rows = session.execute(text("SELECT label, species_id FROM ai_label_map")).fetchall()
    return {(lbl or '').strip().lower(): sid for lbl, sid in rows}


def _load_location_photo_index(session, location_id):
    """{(filename_lower, captured_naive_sec) -> (photo_id, observation_id)}.

    Лише згруповані фото (observation_id NOT NULL), бо ai_predictions
    вимагає observation_id.
    """
    rows = session.execute(text("""
        SELECT p.id, p.original_filename, p.captured_at, p.observation_id
        FROM photos p
        JOIN observations o ON o.id = p.observation_id
        WHERE o.location_id = :loc
    """), {'loc': location_id}).fetchall()
    index = {}
    for pid, fn, captured, obs_id in rows:
        if not fn or captured is None:
            continue
        key = ((fn).strip().lower(), captured.replace(tzinfo=None, microsecond=0))
        index[key] = (pid, obs_id)
    return index


def _match(rows, photo_index):
    """Повертає (matched, csv_unmatched, db_keys_without_csv)."""
    matched, csv_unmatched = [], []
    seen = set()
    for r in rows:
        key = (r['original_filename'].strip().lower(), r['captured_at'])
        hit = photo_index.get(key)
        if hit:
            matched.append((r, hit[0], hit[1]))
            seen.add(key)
        else:
            csv_unmatched.append(r)
    db_without = [k for k in photo_index if k not in seen]
    return matched, csv_unmatched, db_without


def _dedupe_matched(matched):
    """Прибирає повтори за photo_id (можливі, якщо в CSV є кілька рядків з тим
    самим (filename, captured_at) — напр. burst із однаковою EXIF-секундою:
    у БД лишилось одне фото, бо дублікати відхиляються при завантаженні).

    Без цього один INSERT...ON CONFLICT впав би з «cannot affect row a second
    time». Перемагає ОСТАННІЙ рядок. Повертає (unique_list, n_duplicates)."""
    by_pid = {}
    for item in matched:
        by_pid[item[1]] = item          # item[1] == photo_id
    return list(by_pid.values()), len(matched) - len(by_pid)


# ──────────────────────────────────────────────────────────────────────────
# Прев'ю (dry-run)
# ──────────────────────────────────────────────────────────────────────────
def preview_import(session, location_id, rows):
    index = _load_location_photo_index(session, location_id)
    matched, csv_unmatched, db_without = _match(rows, index)
    unique_matched, n_dup = _dedupe_matched(matched)
    # Placeholder-фото (без EXIF-часу): captured_at = 1900-01-01 + offset.
    # Такі НІКОЛИ не зматчаться з CSV — попереджаємо, якщо вони є на локації.
    placeholder_photos = sum(1 for _fn, dt in index if dt.year == 1900)
    return {
        'csv_rows': len(rows),
        'location_photos': len(index),
        'matched': len(unique_matched),
        'csv_unmatched': len(csv_unmatched),
        'csv_duplicate_keys': n_dup,
        'db_without_prediction': len(db_without),
        'location_placeholder_photos': placeholder_photos,
        'sample_unmatched': [
            {'filename': r['original_filename'],
             'captured_at': r['captured_at'].strftime('%Y-%m-%d %H:%M:%S')}
            for r in csv_unmatched[:10]
        ],
    }


# ──────────────────────────────────────────────────────────────────────────
# Імпорт
# ──────────────────────────────────────────────────────────────────────────
def run_import(session, location_id, rows, level_id, user_id=None, chunk_size=5000):
    """Записує per-фото base-прогнози і перераховує серієвий prediction.

    level_id — обов'язковий рівень детектора (обирає користувач на сторінці).
    Викликати В МЕЖАХ транзакції; commit робить викликач (route).
    """
    model = get_or_create_import_model(session, level_id)
    index = _load_location_photo_index(session, location_id)
    matched, csv_unmatched, _ = _match(rows, index)
    matched, n_dup = _dedupe_matched(matched)

    payload = [{
        'photo_id': pid,
        'observation_id': obs_id,
        'model_id': model.id,
        'base_label': r['base_label'],
        'base_score': r['base_score'],
        'top1_label': r['top1_label'],
        'top1_score': None,           # CSV не містить окремого top1-score
        'animal_count': r['animal_count'],
        'human_count': r['human_count'],
    } for (r, pid, obs_id) in matched]

    tbl = AIPrediction.__table__
    added = updated = skipped = 0
    for start in range(0, len(payload), chunk_size):
        chunk = payload[start:start + chunk_size]
        stmt = pg_insert(tbl).values(chunk)
        stmt = stmt.on_conflict_do_update(
            constraint='uq_ai_predictions_photo_model',
            set_={
                'observation_id': stmt.excluded.observation_id,
                'base_label': stmt.excluded.base_label,
                'base_score': stmt.excluded.base_score,
                'top1_label': stmt.excluded.top1_label,
                'top1_score': stmt.excluded.top1_score,
                'animal_count': stmt.excluded.animal_count,
                'human_count': stmt.excluded.human_count,
                'processed_at': text('now()'),
            },
            # Не знижувати впевненість: при тому самому рівні (model_id)
            # перезаписуємо лише коли новий base_score ВИЩИЙ (або наявного
            # ще нема). Інакше рядок-конфлікт пропускається (ідемпотентно:
            # повторний залив того ж/гіршого CSV нічого не псує).
            where=(tbl.c.base_score.is_(None)) | (stmt.excluded.base_score > tbl.c.base_score),
        ).returning(literal_column('(xmax = 0)').label('inserted'))
        # RETURNING повертає лише реально вставлені/оновлені рядки; пропущені
        # (WHERE=false) не повертаються. xmax=0 → щойно вставлений.
        res = session.execute(stmt).fetchall()
        chunk_added = sum(1 for r in res if r.inserted)
        added += chunk_added
        updated += len(res) - chunk_added
        skipped += len(chunk) - len(res)

    affected_obs = {obs_id for (_, _, obs_id) in matched}
    recomputed = recompute_observation_predictions(session, model.id, affected_obs)

    return {
        'model_id': model.id,
        'matched': len(matched),
        'added': added,
        'updated': updated,
        'skipped_lower_score': skipped,
        'csv_unmatched': len(csv_unmatched),
        'csv_duplicate_keys': n_dup,
        'observations_recomputed': recomputed,
    }


def _aggregate_series(pairs):
    """Правило агрегації серії: тварина > людина > empty.

    pairs — список (base_label, base_score) для observation.
    Повертає (label, score).
    """
    animals = [(l, s if s is not None else 0.0) for l, s in pairs if l and l not in NON_ANIMAL_LABELS]
    if animals:
        return max(animals, key=lambda t: t[1])
    if any(l == 'human' for l, _ in pairs):
        humans = [s for l, s in pairs if l == 'human' and s is not None]
        return 'human', (max(humans) if humans else 1.0)
    if any(l == 'vehicle' for l, _ in pairs):
        return 'vehicle', 1.0
    return 'empty', 1.0


def recompute_observation_predictions(session, model_id, observation_ids=None):
    """Перераховує серієвий prediction_* по поточних observations у БД.

    Читає per-фото base_label/base_score (джерело істини), агрегує за правилом
    тварина>людина>empty, мапить label→species_id через ai_label_map і пише
    однаковий prediction_* на всі рядки ai_predictions цієї серії.

    observation_ids=None → перерахувати ВСІ серії цієї моделі.
    Повертає к-сть оброблених observations.
    """
    label_map = load_label_map(session)

    params = {'mid': model_id}
    where = "model_id = :mid"
    if observation_ids is not None:
        ids = list(observation_ids)
        if not ids:
            return 0
        where += " AND observation_id = ANY(:ids)"
        params['ids'] = ids

    rows = session.execute(
        text(f"SELECT observation_id, base_label, base_score FROM ai_predictions WHERE {where}"),
        params,
    ).fetchall()

    by_obs = {}
    for obs_id, base_label, base_score in rows:
        by_obs.setdefault(obs_id, []).append((base_label, base_score))

    updates = []
    for obs_id, pairs in by_obs.items():
        label, score = _aggregate_series(pairs)
        updates.append({
            'obs': obs_id,
            'mid': model_id,
            'label': label,
            'score': score,
            'sid': label_map.get((label or '').strip().lower()),
        })

    if updates:
        session.execute(text("""
            UPDATE ai_predictions
               SET prediction_label = :label,
                   prediction_score = :score,
                   prediction_species_id = :sid
             WHERE observation_id = :obs AND model_id = :mid
        """), updates)

    return len(updates)

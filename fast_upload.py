# myproject/app/camera_traps/fast_upload.py
"""
Швидкий шлях завантаження великих наборів фото (10k–100k+).

Існує паралельно зі старим `utils.group_batch_into_series` —
жодних змін у старому шляху, спільний лише `process_single_photo`.

Архітектура:
    /upload-fast (HTML)
        ├── /api/create-batch           (старий, переюзаний)
        ├── /upload/process-single      (старий, переюзаний)   ← N паралельних
        ├── /api/finalize-batch-async   (202 → start_async_grouping)
        ├── /api/batch-status           (старий, переюзаний для polling)
        └── /api/batch/<id>/uploaded-files (resumable)

Інваріант:
    Групування викликається лише після того, як усі фото batchʼа
    зафіксовані у БД зі status='uploaded'. CTE-запит робить single-pass
    LAG над повним набором, тож межі серій будуються глобально.

Заміна threading на Celery надалі — точкова: підмінити тіло
`start_async_grouping` на `finalize_batch_task.delay(batch_id)`,
решта роутів/JS не змінюється.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Optional

from flask import current_app
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, DBAPIError

from .database import get_ct_engine, get_ct_session, close_ct_session
from .models import UploadBatch


# ─────────────────────────────────────────────────────────────────────────────
# 1. ГРУПУВАННЯ ОДНИМ SQL-ЗАПИТОМ (single-pass над усім batchʼем)
# ─────────────────────────────────────────────────────────────────────────────

def group_batch_into_series_sql(batch_id: str) -> int:
    """
    Групує всі фото batchʼа у серії одним CTE-запитом.

    Повертає кількість фото, які отримали observation_id.

    Алгоритм:
        1. WITH ordered: сортуємо photos batchʼа за (captured_at, id),
           обчислюємо LAG(captured_at) — час попереднього фото.
        2. WITH marked: фіксуємо межу серії там, де
           captured_at - prev_captured > SERIES_TIME_WINDOW
           (або де prev IS NULL — перше фото). Кумулятивний SUM(flag)
           дає series_idx — номер серії в межах batchʼа.
        3. INSERT INTO observations по одному рядку на кожен series_idx:
           MIN/MAX(captured_at), COUNT(*). RETURNING id для маппінгу.
        4. UPDATE photos: проставляємо observation_id, sequence_number
           (ROW_NUMBER в межах серії), status='pending'.
        5. Окремий короткий UPDATE locations.photo_count.

    Ідемпотентність:
        - Якщо batch уже 'completed' — return 0 без дій.
        - Якщо є partial Observations від попередньої невдалої спроби
          (батч у 'failed' / 'grouping') — спершу їх чистимо:
          DELETE FROM observations WHERE id IN (...) — повʼязані photos
          автоматично втрачають observation_id через nullable FK.

    Транзакційність:
        Усе тіло — одна транзакція через engine.begin(). Жодних
        per-photo flush, жодних ORM-обʼєктів у памʼять. На 50k фото
        обчислюється секундами, не хвилинами.
    """
    config = current_app.config['CAMERA_TRAP_CONFIG']
    series_window_seconds = int(config['SERIES_TIME_WINDOW'])

    engine = get_ct_engine()

    with engine.begin() as conn:
        # ─── 0. Перевірка стану batchʼа + читання context ────────────────
        row = conn.execute(
            text("""
                SELECT location_id, uploaded_by_id, status
                  FROM upload_batches
                 WHERE id = :b
                 FOR UPDATE
            """),
            {"b": batch_id},
        ).first()

        if row is None:
            raise ValueError(f"Batch {batch_id} not found")

        if row.status == 'completed':
            current_app.logger.info(
                f"[fast-upload] Batch {batch_id} already completed; skip grouping"
            )
            return 0

        location_id = row.location_id
        uploaded_by_id = row.uploaded_by_id

        # ─── 1. Чистка часткових результатів попередньої спроби ──────────
        # ПОРЯДОК ВАЖЛИВИЙ: спершу занулюємо FK у photos цього batchʼа
        # (щоб не порушити foreign-key constraint), потім видаляємо
        # observations, які тепер лишилися без жодного photo.

        # 1а. Зберігаємо у tmp ті observation_id, які раніше мали фото
        # цього batchʼа — кандидати на видалення.
        conn.execute(text("DROP TABLE IF EXISTS tmp_partial_obs"))
        conn.execute(
            text("""
                CREATE TEMP TABLE tmp_partial_obs ON COMMIT DROP AS
                SELECT DISTINCT observation_id AS id
                  FROM photos
                 WHERE upload_batch_id = :b
                   AND observation_id IS NOT NULL
            """),
            {"b": batch_id},
        )

        # 1б. Скидаємо стан photos цього batchʼа до 'uploaded' —
        # FK observation_id зануляється, FK більше не блокує DELETE.
        conn.execute(
            text("""
                UPDATE photos
                   SET observation_id = NULL,
                       sequence_number = NULL,
                       status = 'uploaded'
                 WHERE upload_batch_id = :b
            """),
            {"b": batch_id},
        )

        # 1в. Видаляємо ті кандидати, які тепер не мають жодного photo
        # (тобто всі їхні фото належали саме цьому batchʼу — orphan
        # observations попередньої невдалої спроби).
        conn.execute(
            text("""
                DELETE FROM observations o
                 WHERE o.id IN (SELECT id FROM tmp_partial_obs)
                   AND NOT EXISTS (
                       SELECT 1 FROM photos p
                        WHERE p.observation_id = o.id
                   )
            """),
        )

        # ─── 2. Window-функцією рахуємо series_idx, агрегуємо у tmp ──────
        # Створюємо тимчасову таблицю з series_idx для кожного фото —
        # потрібно бо INSERT...RETURNING обʼєднати з UPDATE photos
        # одним statement-ом важко без unique key.
        conn.execute(text("DROP TABLE IF EXISTS tmp_batch_groups"))
        conn.execute(
            text("""
                CREATE TEMP TABLE tmp_batch_groups
                ON COMMIT DROP
                AS
                WITH ordered AS (
                    SELECT
                        p.id          AS photo_id,
                        p.captured_at AS captured_at,
                        LAG(p.captured_at) OVER (
                            ORDER BY p.captured_at, p.id
                        ) AS prev_t
                      FROM photos p
                     WHERE p.upload_batch_id = :b
                       AND p.status = 'uploaded'
                ),
                marked AS (
                    SELECT
                        photo_id,
                        captured_at,
                        CASE
                            WHEN prev_t IS NULL
                              OR captured_at - prev_t
                                 > make_interval(secs => :win)
                            THEN 1 ELSE 0
                        END AS new_series_flag
                      FROM ordered
                ),
                numbered AS (
                    -- Кумулятивна сума межевих прапорів = індекс серії
                    SELECT
                        photo_id,
                        captured_at,
                        SUM(new_series_flag) OVER (
                            ORDER BY captured_at, photo_id
                            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                        ) AS series_idx
                      FROM marked
                )
                -- ROW_NUMBER PARTITION BY series_idx — окремим кроком, бо
                -- PostgreSQL не дозволяє window-функцію всередині
                -- PARTITION BY іншої window-функції.
                SELECT
                    photo_id,
                    captured_at,
                    series_idx,
                    ROW_NUMBER() OVER (
                        PARTITION BY series_idx
                        ORDER BY captured_at, photo_id
                    ) AS seq_in_series
                  FROM numbered
            """),
            {"b": batch_id, "win": series_window_seconds},
        )

        # Якщо tmp порожня — фото немає.
        photo_count = conn.execute(
            text("SELECT COUNT(*) FROM tmp_batch_groups")
        ).scalar() or 0

        if photo_count == 0:
            raise ValueError(
                f"No 'uploaded' photos found in batch {batch_id}"
            )

        # ─── 3. INSERT серій у observations + RETURNING для маппінгу ─────
        # Створюємо ще одну tmp з мапою series_idx -> observation_id.
        conn.execute(text("DROP TABLE IF EXISTS tmp_series_obs"))
        conn.execute(
            text("""
                CREATE TEMP TABLE tmp_series_obs
                ON COMMIT DROP
                AS
                WITH agg AS (
                    SELECT
                        series_idx,
                        MIN(captured_at) AS series_start,
                        MAX(captured_at) AS series_end,
                        COUNT(*)         AS cnt
                      FROM tmp_batch_groups
                     GROUP BY series_idx
                ),
                ins AS (
                    INSERT INTO observations (
                        location_id, series_start_time, series_end_time,
                        uploaded_by_id, photo_count, status, created_at
                    )
                    SELECT
                        :loc, series_start, series_end,
                        :uid, cnt, 'pending', NOW()
                      FROM agg
                     ORDER BY series_idx
                    RETURNING
                        id, series_start_time, series_end_time
                )
                SELECT
                    a.series_idx,
                    ins.id AS observation_id
                  FROM agg a
                  JOIN ins
                    ON ins.series_start_time = a.series_start
                   AND ins.series_end_time   = a.series_end
            """),
            {"loc": location_id, "uid": uploaded_by_id},
        )

        # ─── 4. UPDATE photos: observation_id + sequence_number ──────────
        result = conn.execute(
            text("""
                UPDATE photos AS p
                   SET observation_id  = so.observation_id,
                       sequence_number = bg.seq_in_series,
                       status          = 'pending'
                  FROM tmp_batch_groups bg
                  JOIN tmp_series_obs   so ON so.series_idx = bg.series_idx
                 WHERE p.id = bg.photo_id
            """),
        )
        grouped_photos = result.rowcount or 0

        # ─── 5. Перерахунок photo_count у локації одним short UPDATE ─────
        conn.execute(
            text("""
                UPDATE locations
                   SET photo_count = (
                       SELECT COUNT(*) FROM photos p
                         JOIN observations o ON o.id = p.observation_id
                        WHERE o.location_id = :loc
                          AND p.status IN ('pending', 'completed',
                                           'needs_review', 'grouped')
                   )
                 WHERE id = :loc
            """),
            {"loc": location_id},
        )

    current_app.logger.info(
        f"[fast-upload] Batch {batch_id}: grouped {grouped_photos} photos"
    )
    return grouped_photos


# ─────────────────────────────────────────────────────────────────────────────
# 2. АСИНХРОННИЙ ЗАПУСК (threading; точка заміни на Celery у майбутньому)
# ─────────────────────────────────────────────────────────────────────────────

# Стани UploadBatch.status, додані цим модулем:
#   'ready_to_group' — клієнт надіслав finalize, таск ще не стартував
#   'grouping'       — фоновий потік виконує group_batch_into_series_sql
# Решта ('uploading' / 'completed' / 'failed') — як у старому шляху.


def _set_batch_status(batch_id: str, status: str,
                      error_message: Optional[str] = None) -> None:
    """Короткий окремий commit зі зміною статусу batchʼа."""
    engine = get_ct_engine()
    with engine.begin() as conn:
        conn.execute(
            text("""
                UPDATE upload_batches
                   SET status = :s,
                       error_message = COALESCE(:err, error_message),
                       completed_at = CASE
                           WHEN :s IN ('completed', 'failed') THEN NOW()
                           ELSE completed_at
                       END
                 WHERE id = :b
            """),
            {"b": batch_id, "s": status,
             "err": (error_message[:500] if error_message else None)},
        )


def _run_grouping_in_thread(app, batch_id: str) -> None:
    """Тіло фонового потоку. Виконується поза HTTP-контекстом."""
    with app.app_context():
        try:
            _set_batch_status(batch_id, 'grouping')
            group_batch_into_series_sql(batch_id)
            _set_batch_status(batch_id, 'completed')
        except (OperationalError, DBAPIError) as e:
            # Транзієнтні помилки БД — простий single retry
            current_app.logger.warning(
                f"[fast-upload] Batch {batch_id} hit DB error, retry once: {e}"
            )
            try:
                group_batch_into_series_sql(batch_id)
                _set_batch_status(batch_id, 'completed')
            except Exception as e2:
                current_app.logger.exception(
                    f"[fast-upload] Batch {batch_id} failed after retry"
                )
                _set_batch_status(batch_id, 'failed', str(e2))
        except Exception as e:
            current_app.logger.exception(
                f"[fast-upload] Batch {batch_id} failed during grouping"
            )
            _set_batch_status(batch_id, 'failed', str(e))


def start_async_grouping(batch_id: str) -> None:
    """
    Стартує фонове групування. Викликається з HTTP-роуту після
    переведення batch у стан 'ready_to_group'. Повертається миттєво.

    NB: цей шар спеціально тонкий — щоб у майбутньому замінити на
    `finalize_batch_task.delay(batch_id)` без зміни роутів і JS.
    """
    app = current_app._get_current_object()  # type: ignore[attr-defined]
    thread = threading.Thread(
        target=_run_grouping_in_thread,
        args=(app, batch_id),
        name=f"ct-fast-group-{batch_id[:8]}",
        daemon=True,
    )
    thread.start()
    current_app.logger.info(
        f"[fast-upload] Started async grouping thread for batch {batch_id}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3. RECOVERY: при рестарті worker'а — підібрати «застряглі» батчі
# ─────────────────────────────────────────────────────────────────────────────

def recover_stale_grouping_batches() -> int:
    """
    Викликається з app factory при старті. Якщо worker був убитий під
    час 'grouping' — батч ніхто не перезапустить. Цей хелпер переводить
    застряглі батчі у 'failed', щоб клієнт побачив помилку й міг
    запустити повторно (group_batch_into_series_sql ідемпотентний).

    Поріг — 30 хвилин: реалістична верхня межа для 100k фото на SQL.
    """
    try:
        engine = get_ct_engine()
        cutoff = datetime.utcnow() - timedelta(minutes=30)
        with engine.begin() as conn:
            result = conn.execute(
                text("""
                    UPDATE upload_batches
                       SET status = 'failed',
                           error_message = COALESCE(error_message,
                               'Worker restarted while grouping; please retry'),
                           completed_at = NOW()
                     WHERE status = 'grouping'
                       AND created_at < :cutoff
                """),
                {"cutoff": cutoff},
            )
            n = result.rowcount or 0
            if n:
                current_app.logger.warning(
                    f"[fast-upload] Recovered {n} stale 'grouping' batches"
                )
            return n
    except Exception as e:
        # На рестарті не валимо весь застосунок — лише лог.
        try:
            current_app.logger.error(
                f"[fast-upload] recover_stale_grouping_batches failed: {e}"
            )
        except Exception:
            pass
        return 0
    finally:
        close_ct_session()

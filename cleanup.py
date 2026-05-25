# myproject/app/camera_traps/cleanup.py
"""
Очистка сиріт і невдалих batchʼів — заміна старої cleanup_stale_batches.

Процес із трьох категорій:
  A. Stale batches — застряглі/невдалі завантаження
     (status IN uploading/processing/ready_to_group/grouping/failed)
  B. Stranded photos — фото зі status='uploaded' AND observation_id IS NULL,
     що не повʼязані з активним batchʼем
  C. Orphan files — файли в raw/ + thumbnails/, для яких немає Photo-рядка

Архітектура:
  • Двофазний запуск: analyze (dry-run) → execute (видалення).
  • Захист активних batchʼів через probe (10с спостереження за
    processed_files). Виключає з обох фаз.
  • Жорстко зашиті захисти:
      photo.is_favorite=TRUE         → НІКОЛИ не видаляється
      photo.observation_id IS NOT NULL → НІКОЛИ не видаляється
      photo.status != 'uploaded'     → НІКОЛИ не видаляється (вже в роботі)
      file.mtime > NOW() - 5 хв      → НІКОЛИ не видаляється (race-захист)
  • Виконання — у фоновому потоці (threading.Thread), як group_batch.

НЕ конфліктує з `background_tasks.cleanup_old_photos`:
  той працює на множині (observation_id NOT NULL, status='archived') —
  ця функція працює на множині (observation_id IS NULL, status='uploaded').
  Перетин — порожній.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Optional

from flask import current_app
from sqlalchemy import text

from .database import get_ct_engine, get_ct_session, close_ct_session
from .models import CleanupLog


# ─── Константи безпеки (захардкожено, без можливості перевизначення) ────
ACTIVE_BATCH_STATES = ('uploading', 'processing', 'ready_to_group', 'grouping')
CLEANUP_BATCH_STATES = ACTIVE_BATCH_STATES + ('failed',)  # без 'completed'!
DISK_MTIME_SAFETY_SECONDS = 300        # 5 хв — race-захист file.save → commit
TRANSIENT_STATE_MAX_AGE_MIN = 60       # ready_to_group/grouping старші — stale
REPORT_TTL_SECONDS = 600               # звіт analyze живий 10 хв
EXECUTE_STUCK_HOURS = 1                # 'executing' старший — вважаємо crashed
DELETE_CHUNK_SIZE = 500                # пакетне видалення (photos / files)


# ════════════════════════════════════════════════════════════════════════
# Probe: виявляємо активні batch без зміни схеми
# ════════════════════════════════════════════════════════════════════════

def _probe_active_batches(probe_seconds: int) -> set:
    """
    Знімає два снапшоти upload_batches.processed_files з заданим інтервалом.
    Повертає множину id-в, чий processed_files змінився → активні.

    Також додає до активних усі ready_to_group/grouping молодші за
    TRANSIENT_STATE_MAX_AGE_MIN (короткі стани, які можуть швидко
    переходити, тож probe може не зловити їх).
    """
    engine = get_ct_engine()

    def _snapshot():
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT id, COALESCE(processed_files, 0) AS pf, status, created_at
                  FROM upload_batches
                 WHERE status = ANY(:states)
            """), {"states": list(ACTIVE_BATCH_STATES)}).fetchall()
        return {r.id: (r.pf, r.status, r.created_at) for r in rows}

    snap1 = _snapshot()
    if not snap1:
        return set()

    time.sleep(probe_seconds)
    snap2 = _snapshot()

    active = set()
    # 1) processed_files змінився → точно активний
    for bid, (pf1, _, _) in snap1.items():
        pf2 = snap2.get(bid, (None,))[0]
        if pf2 is not None and pf2 != pf1:
            active.add(bid)

    # 2) ready_to_group/grouping молодший за поріг — захист на випадок
    # короткої фази, що probe пропустив
    cutoff = datetime.utcnow() - timedelta(minutes=TRANSIENT_STATE_MAX_AGE_MIN)
    for bid, (_, status, created_at) in snap2.items():
        if status in ('ready_to_group', 'grouping') and created_at and created_at > cutoff:
            active.add(bid)

    return active


# ════════════════════════════════════════════════════════════════════════
# ANALYZE: dry-run з повним звітом
# ════════════════════════════════════════════════════════════════════════

def analyze_cleanup(triggered_by: int, threshold_hours: int = 0,
                    probe_seconds: int = 10) -> str:
    """
    Стартує асинхронний analyze. Повертає report_id для polling.

    Створює CleanupLog зі status='analyzing' одразу (видно у polling),
    далі фоновий потік збирає звіт і переводить у 'analyzed'.
    """
    report_id = str(uuid.uuid4())
    engine = get_ct_engine()
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO cleanup_log
                (id, kind, status, triggered_by, started_at, threshold_hours)
            VALUES
                (:id, 'analysis', 'analyzing', :uid, NOW(), :th)
        """), {"id": report_id, "uid": triggered_by, "th": threshold_hours})

    # Purge старих логів (retention) — fire-and-forget
    try:
        purge_old_logs()
    except Exception as e:
        current_app.logger.warning(f"[cleanup] purge_old_logs failed: {e}")

    app = current_app._get_current_object()  # type: ignore[attr-defined]
    threading.Thread(
        target=_run_analyze_in_thread,
        args=(app, report_id, threshold_hours, probe_seconds),
        name=f"cleanup-analyze-{report_id[:8]}",
        daemon=True,
    ).start()

    return report_id


def _run_analyze_in_thread(app, report_id: str, threshold_hours: int,
                           probe_seconds: int) -> None:
    with app.app_context():
        try:
            report = _collect_cleanup_report(threshold_hours, probe_seconds)
            engine = get_ct_engine()
            with engine.begin() as conn:
                conn.execute(text("""
                    UPDATE cleanup_log
                       SET status='analyzed',
                           report_json = CAST(:rpt AS JSONB),
                           finished_at = NOW()
                     WHERE id = :id
                """), {"id": report_id, "rpt": json.dumps(report)})
        except Exception as e:
            current_app.logger.exception(f"[cleanup] analyze failed for {report_id}")
            _set_log_status(report_id, 'failed', str(e)[:500])


def _collect_cleanup_report(threshold_hours: int, probe_seconds: int) -> dict:
    """Серце dry-run-у. Повертає JSON-серіалізовний dict."""
    config = current_app.config['CAMERA_TRAP_CONFIG']
    upload_path = config['UPLOAD_PATH']
    raw_dir = os.path.join(upload_path, 'pending_photos', 'raw')
    thumb_dir = os.path.join(upload_path, 'pending_photos', 'thumbnails')

    engine = get_ct_engine()

    # 1) Probe активних batchʼів
    active_ids = _probe_active_batches(probe_seconds)

    threshold_cutoff = datetime.utcnow() - timedelta(hours=threshold_hours)

    # 2) Stale batches (категорія A) — кандидати на маркування 'failed'
    with engine.connect() as conn:
        stale_rows = conn.execute(text("""
            SELECT id, status, created_at, processed_files, total_files
              FROM upload_batches
             WHERE status = ANY(:states)
               AND created_at <= :cutoff
        """), {
            "states": list(CLEANUP_BATCH_STATES),
            "cutoff": threshold_cutoff,
        }).fetchall()
    stale_batches = [
        {
            "id": r.id,
            "status": r.status,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "processed_files": r.processed_files,
            "total_files": r.total_files,
        }
        for r in stale_rows if r.id not in active_ids
    ]
    stale_batch_ids = [b["id"] for b in stale_batches]

    # 3) Stranded photos (категорії A + B обʼєднано) — однією вибіркою.
    # Критерій SAFE-deletion:
    #   status='uploaded' AND observation_id IS NULL AND is_favorite=FALSE
    # І batch або у списку stale, або None, або 'completed'/'failed'.
    # Тобто фото-сирота без активного звʼязку.
    with engine.connect() as conn:
        photos_rows = conn.execute(text("""
            SELECT p.id, p.system_filename, p.upload_batch_id
              FROM photos p
              LEFT JOIN upload_batches b ON b.id = p.upload_batch_id
             WHERE p.status = 'uploaded'
               AND p.observation_id IS NULL
               AND p.is_favorite = FALSE
               AND (
                   p.upload_batch_id IS NULL
                   OR b.status IN ('completed', 'failed')
                   OR p.upload_batch_id = ANY(:stale_ids)
               )
               AND (
                   p.upload_batch_id IS NULL
                   OR NOT (p.upload_batch_id = ANY(:active_ids))
               )
        """), {
            "stale_ids": stale_batch_ids if stale_batch_ids else [''],
            "active_ids": list(active_ids) if active_ids else [''],
        }).fetchall()
    stranded_photos = [
        {"id": r.id, "system_filename": r.system_filename,
         "batch_id": r.upload_batch_id}
        for r in photos_rows
    ]
    stranded_filenames = {p["system_filename"] for p in stranded_photos
                          if p["system_filename"]}

    # 4) Orphan files на диску (категорія C)
    with engine.connect() as conn:
        all_known = {row[0] for row in conn.execute(
            text("SELECT system_filename FROM photos")
        )}

    orphan_files = []
    now = time.time()
    for d in (raw_dir, thumb_dir):
        if not os.path.isdir(d):
            continue
        for entry in os.scandir(d):
            if not entry.is_file():
                continue
            if entry.name in all_known:
                continue
            try:
                st = entry.stat()
            except OSError:
                continue
            if now - st.st_mtime < DISK_MTIME_SAFETY_SECONDS:
                continue  # race-захист
            orphan_files.append({
                "path": entry.path,
                "name": entry.name,
                "size": st.st_size,
                "mtime": st.st_mtime,
            })

    # 5) Файли stranded-photos для видалення — теж рахуємо як disk-кандидати
    stranded_file_paths = []
    stranded_files_bytes = 0
    for fn in stranded_filenames:
        for d in (raw_dir, thumb_dir):
            p = os.path.join(d, fn)
            if os.path.isfile(p):
                try:
                    sz = os.path.getsize(p)
                except OSError:
                    sz = 0
                stranded_file_paths.append({"path": p, "size": sz})
                stranded_files_bytes += sz

    orphan_bytes = sum(f["size"] for f in orphan_files)
    total_bytes = stranded_files_bytes + orphan_bytes

    return {
        "probe_seconds": probe_seconds,
        "threshold_hours": threshold_hours,
        "active_protected_count": len(active_ids),
        "active_protected_ids": sorted(active_ids),
        "stale_batches": stale_batches,
        "stale_batches_count": len(stale_batches),
        "stranded_photos_count": len(stranded_photos),
        "stranded_photos_sample": stranded_photos[:100],
        "stranded_files_count": len(stranded_file_paths),
        "stranded_files_bytes": stranded_files_bytes,
        "orphan_files_count": len(orphan_files),
        "orphan_files_bytes": orphan_bytes,
        "orphan_files_sample": orphan_files[:100],
        "total_bytes_freed_estimate": total_bytes,
        "generated_at": datetime.utcnow().isoformat(),
    }


# ════════════════════════════════════════════════════════════════════════
# EXECUTE: реальне видалення
# ════════════════════════════════════════════════════════════════════════

def start_execute(report_id: str, probe_seconds: int = 10) -> None:
    """
    Стартує фонове виконання для готового analyze-звіту.
    Перевіряє: status='analyzed', report < REPORT_TTL_SECONDS.
    Робить ПОВТОРНИЙ probe — захист від нових uploadʼів між analyze й execute.
    """
    engine = get_ct_engine()
    with engine.begin() as conn:
        row = conn.execute(text("""
            SELECT status, started_at, report_json
              FROM cleanup_log WHERE id = :id
             FOR UPDATE
        """), {"id": report_id}).first()
        if row is None:
            raise ValueError("Report not found")
        if row.status != 'analyzed':
            raise ValueError(f"Report status is '{row.status}', expected 'analyzed'")
        age = (datetime.utcnow() - row.started_at).total_seconds()
        if age > REPORT_TTL_SECONDS:
            raise ValueError(f"Report expired ({int(age)}s > {REPORT_TTL_SECONDS}s)")
        # Lock-in: переходимо в executing — другий клік побачить це і отримає 409
        conn.execute(text("""
            UPDATE cleanup_log SET status='executing', finished_at=NULL
             WHERE id = :id
        """), {"id": report_id})

    app = current_app._get_current_object()  # type: ignore[attr-defined]
    threading.Thread(
        target=_run_execute_in_thread,
        args=(app, report_id, probe_seconds),
        name=f"cleanup-execute-{report_id[:8]}",
        daemon=True,
    ).start()


def _run_execute_in_thread(app, report_id: str, probe_seconds: int) -> None:
    with app.app_context():
        try:
            stats = _execute_cleanup(report_id, probe_seconds)
            engine = get_ct_engine()
            with engine.begin() as conn:
                conn.execute(text("""
                    UPDATE cleanup_log
                       SET status='completed', finished_at=NOW(),
                           batches_examined = :be, batches_marked_failed = :bmf,
                           photos_deleted = :pd, files_deleted = :fd,
                           bytes_freed = :bf
                     WHERE id = :id
                """), {"id": report_id, **stats})
        except Exception as e:
            current_app.logger.exception(f"[cleanup] execute failed for {report_id}")
            _set_log_status(report_id, 'failed', str(e)[:500])


def _execute_cleanup(report_id: str, probe_seconds: int) -> dict:
    """Виконує видалення на основі звіту. Перевіряє безпеку на кожному кроці."""
    engine = get_ct_engine()
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT report_json FROM cleanup_log WHERE id = :id"
        ), {"id": report_id}).first()
    if row is None or row.report_json is None:
        raise ValueError("Report data missing")
    report = row.report_json if isinstance(row.report_json, dict) else json.loads(row.report_json)

    # ── ПОВТОРНИЙ PROBE — критичний захист ─────────────────────────────
    active_now = _probe_active_batches(probe_seconds)

    stats = {"be": 0, "bmf": 0, "pd": 0, "fd": 0, "bf": 0}

    # 1) Маркування batchʼів 'failed' (тих, що пройшли свіжий probe)
    stale_ids = [b["id"] for b in report.get("stale_batches", [])
                 if b["id"] not in active_now]
    stats["be"] = len(report.get("stale_batches", []))
    if stale_ids:
        with engine.begin() as conn:
            r = conn.execute(text("""
                UPDATE upload_batches
                   SET status = 'failed',
                       error_message = COALESCE(error_message,
                           'Marked failed by cleanup ' || :rid),
                       completed_at = COALESCE(completed_at, NOW())
                 WHERE id = ANY(:ids)
                   AND status != 'completed'
            """), {"ids": stale_ids, "rid": report_id})
            stats["bmf"] = r.rowcount or 0

    # 2) Видалення stranded-photos (БД + файли) пакетами по DELETE_CHUNK_SIZE.
    # ПЕРЕВІРКА: ще раз фільтруємо інваріантами (захист на випадок зміни стану).
    stranded_ids_all = [p["id"] for p in report.get("stranded_photos_sample", [])]
    # Звіт містив лише sample (100). Беремо повний поточний набір за тими ж
    # критеріями + виключаємо active.
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT p.id, p.system_filename
              FROM photos p
              LEFT JOIN upload_batches b ON b.id = p.upload_batch_id
             WHERE p.status = 'uploaded'
               AND p.observation_id IS NULL
               AND p.is_favorite = FALSE
               AND (
                   p.upload_batch_id IS NULL
                   OR b.status IN ('completed', 'failed')
               )
               AND (
                   p.upload_batch_id IS NULL
                   OR NOT (p.upload_batch_id = ANY(:active_ids))
               )
        """), {"active_ids": list(active_now) if active_now else ['']}).fetchall()
    safe_photo_ids = [r.id for r in rows]
    safe_filenames = [r.system_filename for r in rows if r.system_filename]

    # Видалення файлів stranded-photos
    config = current_app.config['CAMERA_TRAP_CONFIG']
    upload_path = config['UPLOAD_PATH']
    raw_dir = os.path.join(upload_path, 'pending_photos', 'raw')
    thumb_dir = os.path.join(upload_path, 'pending_photos', 'thumbnails')
    for fn in safe_filenames:
        for d in (raw_dir, thumb_dir):
            p = os.path.join(d, fn)
            if os.path.isfile(p):
                try:
                    sz = os.path.getsize(p)
                    os.remove(p)
                    stats["fd"] += 1
                    stats["bf"] += sz
                except OSError as e:
                    current_app.logger.warning(f"[cleanup] failed to rm {p}: {e}")

    # Видалення photo-рядків пакетами
    for i in range(0, len(safe_photo_ids), DELETE_CHUNK_SIZE):
        chunk = safe_photo_ids[i:i + DELETE_CHUNK_SIZE]
        with engine.begin() as conn:
            r = conn.execute(text("""
                DELETE FROM photos
                 WHERE id = ANY(:ids)
                   AND status = 'uploaded'
                   AND observation_id IS NULL
                   AND is_favorite = FALSE
            """), {"ids": chunk})
            stats["pd"] += r.rowcount or 0

    # 3) Orphan files на диску — звіряємо знов перед видаленням.
    # Звіт містив sample, але паралельно мають бути всі. Повторно скануємо.
    with engine.connect() as conn:
        all_known = {row[0] for row in conn.execute(
            text("SELECT system_filename FROM photos")
        )}
    now = time.time()
    for d in (raw_dir, thumb_dir):
        if not os.path.isdir(d):
            continue
        for entry in os.scandir(d):
            if not entry.is_file():
                continue
            if entry.name in all_known:
                continue
            try:
                st = entry.stat()
            except OSError:
                continue
            if now - st.st_mtime < DISK_MTIME_SAFETY_SECONDS:
                continue
            try:
                os.remove(entry.path)
                stats["fd"] += 1
                stats["bf"] += st.st_size
            except OSError as e:
                current_app.logger.warning(f"[cleanup] failed to rm {entry.path}: {e}")

    return stats


# ════════════════════════════════════════════════════════════════════════
# Recovery / retention / helpers
# ════════════════════════════════════════════════════════════════════════

def recover_stuck_cleanup() -> int:
    """Викликати при старті app. Перекидає executing > 1 год у failed."""
    try:
        engine = get_ct_engine()
        cutoff = datetime.utcnow() - timedelta(hours=EXECUTE_STUCK_HOURS)
        with engine.begin() as conn:
            r = conn.execute(text("""
                UPDATE cleanup_log
                   SET status = 'failed',
                       error_message = COALESCE(error_message,
                           'Worker restarted while executing'),
                       finished_at = NOW()
                 WHERE status IN ('analyzing', 'executing')
                   AND started_at < :cutoff
            """), {"cutoff": cutoff})
            n = r.rowcount or 0
            if n:
                current_app.logger.warning(
                    f"[cleanup] recovered {n} stuck cleanup_log rows"
                )
            return n
    except Exception as e:
        try:
            current_app.logger.error(f"[cleanup] recover_stuck failed: {e}")
        except Exception:
            pass
        return 0
    finally:
        close_ct_session()


def purge_old_logs() -> int:
    """Видаляє рядки старші CLEANUP_LOG_RETENTION_DAYS. Викликається при analyze."""
    config = current_app.config.get('CAMERA_TRAP_CONFIG', {})
    days = int(config.get('CLEANUP_LOG_RETENTION_DAYS', 90))
    cutoff = datetime.utcnow() - timedelta(days=days)
    engine = get_ct_engine()
    with engine.begin() as conn:
        r = conn.execute(text("""
            DELETE FROM cleanup_log
             WHERE started_at < :cutoff
        """), {"cutoff": cutoff})
        return r.rowcount or 0


def _set_log_status(report_id: str, status: str,
                    error_message: Optional[str] = None) -> None:
    engine = get_ct_engine()
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE cleanup_log
               SET status = :s,
                   error_message = COALESCE(:err, error_message),
                   finished_at = NOW()
             WHERE id = :id
        """), {"id": report_id, "s": status, "err": error_message})


def get_log(report_id: str) -> Optional[dict]:
    """Повертає поточний стан cleanup_log для polling."""
    engine = get_ct_engine()
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT id, kind, status, triggered_by, started_at, finished_at,
                   threshold_hours, report_json,
                   batches_examined, batches_marked_failed,
                   photos_deleted, files_deleted, bytes_freed,
                   error_message
              FROM cleanup_log WHERE id = :id
        """), {"id": report_id}).first()
    if row is None:
        return None
    return {
        "id": row.id,
        "kind": row.kind,
        "status": row.status,
        "triggered_by": row.triggered_by,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "threshold_hours": row.threshold_hours,
        "report": row.report_json if isinstance(row.report_json, dict)
                  else (json.loads(row.report_json) if row.report_json else None),
        "batches_examined": row.batches_examined,
        "batches_marked_failed": row.batches_marked_failed,
        "photos_deleted": row.photos_deleted,
        "files_deleted": row.files_deleted,
        "bytes_freed": row.bytes_freed,
        "error_message": row.error_message,
    }

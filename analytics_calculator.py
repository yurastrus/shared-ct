# myproject/app/camera_traps/analytics_calculator.py

import logging
import random
from collections import defaultdict
import numpy as np
from datetime import datetime
from sqlalchemy import func, extract, select, distinct

# Важливо: Імпортуємо моделі та функції для роботи з сесією
# з існуючих файлів вашого проєкту.
from .database import get_ct_session, close_ct_session
from .models import (
    Observation, Photo, Identification, Species, Location,
    LocationMonthlyActivity, CalculationLog, SpeciesYearlyTrend,
    location_institutions
)

# Налаштовуємо логування, щоб бачити, що відбувається під час виконання
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def _calculate_monthly_activity():
    """
    Виконує основний розрахунок і заповнює таблицю location_monthly_activity.
    Ця версія правильно обробляє нульові детекції.
    """
    session = get_ct_session()
    try:
        logging.info("Starting calculation of monthly activity...")

        # Крок 1: Очищення таблиці
        logging.info("Truncating LocationMonthlyActivity table...")
        session.query(LocationMonthlyActivity).delete()
        session.commit()
        
        # Крок 2: РОЗРАХУНОК ПАСТКОДНІВ (TRAP DAYS) для всіх активних локацій
        logging.info("Calculating trap days for ALL active locations per month...")
        trap_days_query = session.query(
            Observation.location_id,
            extract('year', Photo.captured_at).label('year'),
            extract('month', Photo.captured_at).label('month'),
            func.count(func.distinct(func.date(Photo.captured_at))).label('trap_days')
        ).join(Photo, Observation.id == Photo.observation_id)\
        .filter(Observation.status.in_(['completed', 'archived']))\
        .group_by(Observation.location_id, 'year', 'month')\
        .all()

        # Створюємо словник: {(loc, year, month): trap_days}
        trap_days_map = {
            (r.location_id, r.year, r.month): r.trap_days 
            for r in trap_days_query
        }
        logging.info(f"Calculated trap days for {len(trap_days_map)} unique location-months.")

        # Крок 3: РОЗРАХУНОК ДНІВ ДЕТЕКЦІЇ (DETECTION COUNT)
        logging.info("Calculating detection counts where species were present...")
        detection_query = session.query(
            Identification.species_id,
            Observation.location_id,
            extract('year', Photo.captured_at).label('year'),
            extract('month', Photo.captured_at).label('month'),
            func.count(func.distinct(func.date(Photo.captured_at))).label('detection_count')
        ).join(Photo, Identification.photo_id == Photo.id)\
        .join(Observation, Photo.observation_id == Observation.id)\
        .filter(
            Observation.status.in_(['completed', 'archived']),
            Identification.species_id.isnot(None),
            Identification.species_id > 0
        )\
        .group_by(Identification.species_id, Observation.location_id, 'year', 'month')\
        .all()
        
        # Створюємо словник: {(species, loc, year, month): detection_count}
        detection_map = {
            (r.species_id, r.location_id, r.year, r.month): r.detection_count
            for r in detection_query
        }
        logging.info(f"Found {len(detection_map)} non-zero detection records.")

        # Крок 4: ФОРМУВАННЯ РЕЗУЛЬТАТІВ, включаючи нульові
        logging.info("Assembling final records, including zero-detection entries...")
        all_species_ids = [row[0] for row in session.query(Species.id).filter(Species.id > 0).all()]
        new_activity_records = []

        # Ітеруємо по всіх комбінаціях (активна локація/місяць) * (всі види)
        for (location_id, year, month), trap_days in trap_days_map.items():
            for species_id in all_species_ids:
                key = (species_id, location_id, year, month)
                detection_count = detection_map.get(key, 0) # Беремо к-ть детекцій, або 0, якщо їх не було

                record = LocationMonthlyActivity(
                    species_id=species_id,
                    location_id=location_id,
                    year=year,
                    month=month,
                    detection_count=detection_count,
                    trap_days=trap_days
                )
                new_activity_records.append(record)

        # Крок 5: Збереження в БД
        if new_activity_records:
            logging.info(f"Adding {len(new_activity_records)} records to the database (this may take a moment)...")
            session.bulk_save_objects(new_activity_records)
            session.commit()
            logging.info("Successfully saved all monthly activity records.")
        else:
            logging.info("No activity records to save.")
            
        return True

    except Exception as e:
        logging.error(f"An error occurred during monthly activity calculation: {e}", exc_info=True)
        session.rollback()
        return False
    finally:
        close_ct_session()

def _run_bootstrap(species_id, location_data, scope_locations, all_years, scope_type, scope_id, N_ITERATIONS):
    """
    Bootstrap для одного виду і одного скоупу локацій.
    Повертає список об'єктів SpeciesYearlyTrend.
    """
    if not scope_locations or not all_years:
        return []

    n = len(scope_locations)
    bootstrap_results = defaultdict(list)

    for _ in range(N_ITERATIONS):
        sampled = random.choices(scope_locations, k=n)
        for year in all_years:
            total_det = total_trap = 0
            for loc_id in sampled:
                det, trap = location_data.get(loc_id, {}).get(year, (0, 0))
                total_det += det
                total_trap += trap
            if total_trap > 0:
                bootstrap_results[year].append((total_det * 100) / total_trap)

    return [
        SpeciesYearlyTrend(
            species_id=species_id, year=year,
            scope_type=scope_type, scope_id=scope_id,
            mean_dr_index=float(np.mean(results)),
            lower_ci=float(np.percentile(results, 2.5)),
            upper_ci=float(np.percentile(results, 97.5))
        )
        for year, results in bootstrap_results.items() if results
    ]


def _calculate_yearly_trends_with_bootstrap():
    """
    Розраховує річні тренди з bootstrap для трьох скоупів:
      - global (всі локації)
      - institution (окремо для кожної установи)
      - ecoregion (для кожного екорегіону)
    """
    session = get_ct_session()
    N_ITERATIONS = 10000

    try:
        logging.info("Starting yearly trend calculation with bootstrap...")

        session.query(SpeciesYearlyTrend).delete()
        session.commit()

        # Завантажуємо mapping локація → установа з ct_db
        loc_inst_rows = session.execute(
            select(location_institutions.c.location_id, location_institutions.c.institution_id)
        ).fetchall()

        from collections import defaultdict as _dd
        inst_locations = _dd(set)   # {institution_id: {location_id, ...}}
        for loc_id, inst_id in loc_inst_rows:
            inst_locations[inst_id].add(loc_id)

        # Завантажуємо екорегіони установ з головної БД
        from app.models import Institution
        institutions = Institution.query.filter(Institution.ecoregion_uk.isnot(None)).all()
        eco_locations = _dd(set)    # {ecoregion_uk: {location_id, ...}}
        for inst in institutions:
            eco_locations[inst.ecoregion_uk].update(inst_locations.get(inst.id, set()))

        # Всі види
        species_ids = [s[0] for s in session.query(LocationMonthlyActivity.species_id).distinct().all()]
        logging.info(f"Found {len(species_ids)} species, "
                     f"{len(inst_locations)} institutions, "
                     f"{len(eco_locations)} ecoregions.")

        final_trends = []

        for species_id in species_ids:
            logging.info(f"  Processing species ID: {species_id}...")

            yearly_rows = session.query(
                LocationMonthlyActivity.location_id,
                LocationMonthlyActivity.year,
                func.sum(LocationMonthlyActivity.detection_count).label('total_detections'),
                func.sum(LocationMonthlyActivity.trap_days).label('total_trap_days')
            ).filter(LocationMonthlyActivity.species_id == species_id)\
             .group_by(LocationMonthlyActivity.location_id, LocationMonthlyActivity.year)\
             .all()

            if not yearly_rows:
                continue

            location_data = _dd(dict)
            for row in yearly_rows:
                location_data[row.location_id][row.year] = (row.total_detections, row.total_trap_days)

            available_locs = set(location_data.keys())
            all_years = sorted(set(r.year for r in yearly_rows))

            # 1. Global
            final_trends.extend(_run_bootstrap(
                species_id, location_data, list(available_locs),
                all_years, 'global', '', N_ITERATIONS))

            # 2. Per institution
            for inst_id, locs in inst_locations.items():
                scope_locs = list(available_locs & locs)
                if scope_locs:
                    final_trends.extend(_run_bootstrap(
                        species_id, location_data, scope_locs,
                        all_years, 'institution', str(inst_id), N_ITERATIONS))

            # 3. Per ecoregion
            for eco_uk, locs in eco_locations.items():
                scope_locs = list(available_locs & locs)
                if scope_locs:
                    final_trends.extend(_run_bootstrap(
                        species_id, location_data, scope_locs,
                        all_years, 'ecoregion', eco_uk, N_ITERATIONS))

        if final_trends:
            logging.info(f"Saving {len(final_trends)} trend records...")
            session.bulk_save_objects(final_trends)
            session.commit()

        return True
    except Exception as e:
        logging.error(f"Error in bootstrap calculation: {e}", exc_info=True)
        session.rollback()
        return False
    finally:
        close_ct_session()

def update_analytics_tables(force_run=False):
    """
    Головна функція. Перевіряє, чи потрібен перерахунок, і запускає обидва етапи.
    """
    session = get_ct_session()
    try:
        source_name = 'completed_observations'
        current_count = session.query(func.count(Observation.id))\
            .filter(Observation.status.in_(['completed', 'archived'])).scalar()
        log_entry = session.query(CalculationLog).filter_by(source_name=source_name).first()
        last_count = log_entry.last_count if log_entry else -1

        logging.info(f"Checking for updates. Current completed observations: {current_count}. Last recorded: {last_count}.")

        if not force_run and current_count == last_count:
            logging.info("No changes detected. Skipping calculation.")
            return

        logging.info("Changes detected or force_run=True. Starting analytics calculation...")
        
        # Етап 1: Розрахунок щомісячної активності
        success_monthly = _calculate_monthly_activity()
        if not success_monthly:
            logging.error("Monthly activity calculation failed. Aborting further calculations.")
            return

        # Етап 2: Розрахунок річних трендів
        success_yearly = _calculate_yearly_trends_with_bootstrap()
        if not success_yearly:
            logging.error("Yearly trend calculation failed. Log will not be updated.")
            return

        # Етап 3: Якщо все успішно, оновлюємо лог
        logging.info("All calculations successful. Updating log.")
        if log_entry:
            log_entry.last_count = current_count
            log_entry.last_calculated_at = datetime.utcnow()
        else:
            new_log_entry = CalculationLog(
                source_name=source_name,
                last_count=current_count,
                last_calculated_at=datetime.utcnow()
            )
            session.add(new_log_entry)
        
        session.commit()
        logging.info("Log updated successfully.")

    except Exception as e:
        logging.error(f"An error occurred in the main update function: {e}", exc_info=True)
        session.rollback()
    finally:
        close_ct_session()

if __name__ == '__main__':
    # Цей блок дозволяє запускати цей файл напряму з командного рядка для тестування
    # Потрібно, щоб ваше середовище Flask було правильно налаштоване для доступу до БД
    
    # Спершу треба створити Flask app context, щоб SQLAlchemy знав, до якої БД підключатись
    from app import create_app
    app = create_app()
    with app.app_context():
        # Запускаємо оновлення з параметром force_run=True для першого разу
        update_analytics_tables(force_run=True)
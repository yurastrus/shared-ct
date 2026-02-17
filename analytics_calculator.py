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
    LocationMonthlyActivity, CalculationLog, SpeciesYearlyTrend
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

def _calculate_yearly_trends_with_bootstrap():
    """
    Розраховує річні тренди та довірчі інтервали, використовуючи дані
    з location_monthly_activity, і зберігає їх у species_yearly_trends.
    """
    session = get_ct_session()
    # Для тестування ставимо менше ітерацій, для продакшену - 5000-10000
    N_ITERATIONS = 10000

    try:
        logging.info("Starting yearly trend calculation with bootstrap...")

        # Крок 1: Очищуємо цільову таблицю
        logging.info("Truncating SpeciesYearlyTrend table...")
        session.query(SpeciesYearlyTrend).delete()
        session.commit()

        # Крок 2: Знаходимо всі види, для яких є дані в проміжній таблиці
        species_to_process = session.query(LocationMonthlyActivity.species_id).distinct().all()
        species_ids = [s[0] for s in species_to_process]
        logging.info(f"Found {len(species_ids)} species to process.")

        final_trends = []
        for species_id in species_ids:
            logging.info(f"  Processing species ID: {species_id}...")

            # Крок 3: Агрегуємо щомісячні дані до річних для кожної локації
            yearly_data_by_location = session.query(
                LocationMonthlyActivity.location_id,
                LocationMonthlyActivity.year,
                func.sum(LocationMonthlyActivity.detection_count).label('total_detections'),
                func.sum(LocationMonthlyActivity.trap_days).label('total_trap_days')
            ).filter(LocationMonthlyActivity.species_id == species_id)\
            .group_by(LocationMonthlyActivity.location_id, LocationMonthlyActivity.year)\
            .all()

            if not yearly_data_by_location:
                logging.warning(f"  No yearly data for species {species_id}, skipping.")
                continue

            # Крок 4: Готуємо дані для бутстрепу
            location_data = defaultdict(dict)
            unique_locations = set()
            for row in yearly_data_by_location:
                location_data[row.location_id][row.year] = (row.total_detections, row.total_trap_days)
                unique_locations.add(row.location_id)

            unique_locations = list(unique_locations)
            n_locations = len(unique_locations)
            all_years = sorted(list(set(r.year for r in yearly_data_by_location)))
            
            # Словник для зберігання результатів: {рік: [список_індексів_DR]}
            bootstrap_results = defaultdict(list)

            # Крок 5: Запускаємо цикл бутстрепу
            for _ in range(N_ITERATIONS):
                # Випадково вибираємо локації (з поверненням)
                sampled_locations = random.choices(unique_locations, k=n_locations)
                
                for year in all_years:
                    yearly_total_detections = 0
                    yearly_total_trap_days = 0
                    for loc_id in sampled_locations:
                        detections, trap_days = location_data.get(loc_id, {}).get(year, (0, 0))
                        yearly_total_detections += detections
                        yearly_total_trap_days += trap_days
                    
                    if yearly_total_trap_days > 0:
                        dr_index = (yearly_total_detections * 100) / yearly_total_trap_days
                        bootstrap_results[year].append(dr_index)
            
            # Крок 6: Обраховуємо фінальну статистику (середнє, 95% ДІ)
            for year, results_for_year in bootstrap_results.items():
                # --- ВИПРАВЛЕННЯ ТУТ ---
                # Явно конвертуємо кожен результат у стандартний float
                mean_dr = float(np.mean(results_for_year))
                lower_ci, upper_ci = map(float, np.percentile(results_for_year, [2.5, 97.5]))

                trend = SpeciesYearlyTrend(
                    species_id=species_id, year=year,
                    mean_dr_index=mean_dr, lower_ci=lower_ci, upper_ci=upper_ci
                )
                final_trends.append(trend)

        # Крок 7: Зберігаємо всі результати в БД
        if final_trends:
            logging.info(f"Adding {len(final_trends)} yearly trend records to the database...")
            session.bulk_save_objects(final_trends)
            session.commit()
        
        return True
    except Exception as e:
        logging.error(f"An error occurred during bootstrap calculation: {e}", exc_info=True)
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
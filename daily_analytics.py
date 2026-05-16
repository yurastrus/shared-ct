# myproject/app/camera_traps/daily_analytics.py

import numpy as np
from scipy.stats import gaussian_kde
from sqlalchemy import text
import io
import csv

def fetch_raw_daily_data(session, start_date, end_date, species_ids, location_ids=None):
    """
    Отримує точний час спостережень (у десяткових годинах) для кожної локації.
    Повертає словник: { species_id: { location_id: [12.5, 14.2, ...], ... } }

    Args:
        location_ids: опційний список Location.id для обмеження результату
                      конкретними локаціями (для фільтру по установі/екорегіону).
                      None означає "всі локації".
    """
    # SQL запит для отримання десяткових годин (напр. 13:30 -> 13.5)
    # Використовуємо Consensus CTE
    location_clause = "AND o.location_id IN :location_ids" if location_ids else ""

    query_sql = f"""
        WITH ObservationConsensus AS (
            SELECT
                p.observation_id, i.species_id,
                COUNT(DISTINCT i.user_id) as vote_count,
                MAX(i.quantity) as max_quantity
            FROM identifications i JOIN photos p ON i.photo_id = p.id
            GROUP BY p.observation_id, i.species_id
        ),
        RankedConsensus AS (
            SELECT
                observation_id, species_id,
                ROW_NUMBER() OVER(PARTITION BY observation_id ORDER BY vote_count DESC, max_quantity DESC) as rn
            FROM ObservationConsensus
        )
        SELECT
            rc.species_id,
            o.location_id,
            EXTRACT(EPOCH FROM o.series_start_time::time)/3600.0 as decimal_hour
        FROM observations o
        JOIN RankedConsensus rc ON o.id = rc.observation_id AND rc.rn = 1
        WHERE
            rc.species_id IN :species_ids
            AND o.status IN ('completed', 'archived')
            AND DATE(o.series_start_time) BETWEEN :start_date AND :end_date
            {location_clause}
    """

    params = {
        'species_ids': tuple(species_ids),
        'start_date': start_date,
        'end_date': end_date
    }
    if location_ids:
        params['location_ids'] = tuple(location_ids)

    # Виконуємо запит
    rows = session.execute(text(query_sql), params).fetchall()
    
    # Групуємо дані: Species -> Location -> [Hours]
    data = {sp_id: {} for sp_id in species_ids}
    
    for row in rows:
        sp_id, loc_id, dec_hour = row
        if sp_id not in data:
            data[sp_id] = {}
        if loc_id not in data[sp_id]:
            data[sp_id][loc_id] = []
        data[sp_id][loc_id].append(float(dec_hour))
        
    return data

def calculate_activity_curve(raw_data_by_loc, total_effort, mode='rai', n_boot=1000, compute_ci=False, bw_adjust=0.1):
    """
    Універсальна функція.
    Якщо bw_adjust > 0: використовує KDE (згладжування).
    Якщо bw_adjust == 0: використовує Binning (сирі гістограми).
    """
    locations_with_detections = list(raw_data_by_loc.keys())
    if not locations_with_detections:
        return None

    # Конвертуємо дані в numpy array
    loc_arrays = [np.array(raw_data_by_loc[lid]) for lid in locations_with_detections]
    num_locs = len(loc_arrays)

    # === ВАРІАНТ 1: BINNING (СИРІ ДАНІ) ===
    if bw_adjust <= 0.001:
        # Сітка X - просто години від 0 до 23
        x_grid = np.arange(24) 
        
        # 1. Створюємо гістограму для КОЖНОЇ локації окремо
        # Це потрібно для коректного бутстрепу по локаціях
        loc_histograms = []
        for times in loc_arrays:
            # bins=24, range=(0,24) розбиває на години [0,1), [1,2)...
            hist, _ = np.histogram(times, bins=24, range=(0, 24))
            loc_histograms.append(hist)
        
        loc_histograms = np.array(loc_histograms) # shape: (num_locs, 24)

        # Функція для агрегації та нормалізації однієї вибірки
        def process_sample_binning(hist_matrix):
            # Сумуємо активність по всіх локаціях у вибірці
            total_hist = np.sum(hist_matrix, axis=0) # shape: (24,)
            
            if mode == 'rai':
                # RAI = (Count / Effort) * 100
                # total_effort - загальний еффорт за період
                return (total_hist * 100.0) / max(1, total_effort)
            else:
                # Percent = (Count / Total Counts) * 100
                total_counts = np.sum(total_hist)
                return (total_hist * 100.0) / max(1, total_counts)

        # Якщо CI не треба - просто рахуємо по всіх наявних даних
        if not compute_ci:
            mean_curve = process_sample_binning(loc_histograms)
            return {
                'hours': x_grid.tolist(),
                'mean': mean_curve.tolist(),
                'ci_lower': None, 'ci_upper': None
            }

        # Якщо треба CI - Бутстреп
        boot_curves = []
        for _ in range(n_boot):
            # Вибираємо індекси локацій з поверненням
            indices = np.random.randint(0, num_locs, num_locs)
            sample_hists = loc_histograms[indices]
            
            curve = process_sample_binning(sample_hists)
            boot_curves.append(curve)
        
        boot_matrix = np.array(boot_curves)
        
    # === ВАРІАНТ 2: KDE (ЗГЛАДЖУВАННЯ) ===
    else:
        # Сітка X детальна (128 точок)
        x_grid = np.linspace(0, 24, 128)
        boot_curves = []

        # Функція для KDE однієї вибірки
        def process_sample_kde(times_array):
             # Циклічність
            extended_data = np.concatenate([times_array - 24, times_array, times_array + 24])
            try:
                kde = gaussian_kde(extended_data, bw_method=bw_adjust)
                y_curve = kde(x_grid) * 3 # *3 через потроєння даних
                
                if mode == 'rai':
                    scale = (len(times_array) * 100.0) / max(1, total_effort)
                    return y_curve * scale
                else:
                    return y_curve * 100.0
            except:
                return np.zeros_like(x_grid)

        if not compute_ci:
            all_times = np.concatenate(loc_arrays)
            if len(all_times) < 2: return None
            mean_curve = process_sample_kde(all_times)
            return {
                'hours': x_grid.tolist(),
                'mean': mean_curve.tolist(),
                'ci_lower': None, 'ci_upper': None
            }

        # Бутстреп
        for _ in range(n_boot):
            indices = np.random.randint(0, num_locs, num_locs)
            sample_times = np.concatenate([loc_arrays[i] for i in indices])
            
            if len(sample_times) < 2: continue
            
            curve = process_sample_kde(sample_times)
            boot_curves.append(curve)
        
        boot_matrix = np.array(boot_curves)

    # === ФІНАЛЬНА СТАТИСТИКА (спільна для обох методів) ===
    if len(boot_curves) == 0: return None
    
    # Конвертуємо в np.array, якщо це ще не зроблено
    boot_matrix = np.array(boot_curves)
    
    mean_curve = np.mean(boot_matrix, axis=0)
    
    # Якщо compute_ci=True, рахуємо перцентилі
    if compute_ci:
        ci_lower = np.percentile(boot_matrix, 2.5, axis=0)
        ci_upper = np.percentile(boot_matrix, 97.5, axis=0)
    else:
        ci_lower = None
        ci_upper = None
    
    return {
        'hours': x_grid.tolist(),
        'mean': mean_curve.tolist(),
        'ci_lower': ci_lower.tolist() if ci_lower is not None else None,
        'ci_upper': ci_upper.tolist() if ci_upper is not None else None,
        # !!! ДОДАЄМО ЦЕЙ РЯДОК:
        # Повертаємо сиру матрицю (1000 рядків), вона потрібна для розрахунку CI перекриття
        'boot_matrix': boot_matrix  
    }

def calculate_overlap_coefficient(curve_a, curve_b):
    """
    Рахує коефіцієнт перекриття (Ridout & Linkie Δ).
    Вхід: масиви значень Y.
    Вихід: число 0..1 (або 0..100 у %)
    """
    a = np.array(curve_a)
    b = np.array(curve_b)
    
    # Захист від порожніх масивів
    if len(a) == 0 or len(b) == 0 or np.sum(a) == 0 or np.sum(b) == 0:
        return 0.0
        
    # Нормалізація: площа під кривою має дорівнювати 1 (щоб це стали ймовірності)
    prob_a = a / np.sum(a)
    prob_b = b / np.sum(b)
    
    # Δ = сума мінімумів у кожній точці
    mins = np.minimum(prob_a, prob_b)
    overlap = np.sum(mins)
    
    return float(overlap) # Повертаємо як float (0.0 - 1.0)

def calculate_overlap_matrix(species_data):
    """
    Рахує матрицю перекриття.
    Якщо є 'boot_matrix' (було включено CI), рахує CI для перекриття.
    """
    ids = sorted(list(species_data.keys()))
    matrix = {}

    for id_a in ids:
        matrix[id_a] = {}
        for id_b in ids:
            if id_a == id_b:
                # Сам з собою = 1.0 (без CI)
                matrix[id_a][id_b] = {'mean': 1.0, 'lower': None, 'upper': None}
                continue

            # Дані виду A і B (використовуємо гілку 'percent', хоча 'rai' теж ок після нормалізації)
            data_a = species_data[id_a]['percent']
            data_b = species_data[id_b]['percent']
            
            # --- ВАРІАНТ 1: Є бутстреп-матриці (Рахуємо CI перекриття) ---
            if 'boot_matrix' in data_a and 'boot_matrix' in data_b and \
               data_a['boot_matrix'] is not None and data_b['boot_matrix'] is not None:
                
                mat_a = data_a['boot_matrix']
                mat_b = data_b['boot_matrix']
                
                # Перевірка, чи однакова кількість ітерацій (наприклад, 1000)
                n_boot = min(len(mat_a), len(mat_b))
                
                overlaps = []
                # Проходимо по кожній ітерації бутстрепу
                for i in range(n_boot):
                    # Рахуємо перекриття між i-ю варіантом кривої A та i-ю варіантом кривої B
                    ov = calculate_overlap_coefficient(mat_a[i], mat_b[i])
                    overlaps.append(ov)
                
                overlaps = np.array(overlaps)
                
                # Рахуємо статистику по 1000 коефіцієнтам
                mean_ov = np.mean(overlaps)
                lower_ov = np.percentile(overlaps, 2.5)
                upper_ov = np.percentile(overlaps, 97.5)
                
                matrix[id_a][id_b] = {
                    'mean': float(mean_ov),
                    'lower': float(lower_ov),
                    'upper': float(upper_ov)
                }
                
            # --- ВАРІАНТ 2: Немає бутстрепу (тільки середнє) ---
            else:
                score = calculate_overlap_coefficient(data_a['mean'], data_b['mean'])
                matrix[id_a][id_b] = {
                    'mean': float(score),
                    'lower': None, 
                    'upper': None
                }

    return {
        'order': ids,
        'data': matrix
    }

def generate_csv_export(species_data, species_names):
    """Генерує CSV файл. Коректно обробляє відсутність CI."""
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Заголовки
    header = ['Hour']
    sp_ids = sorted(species_data.keys())
    
    for sp_id in sp_ids:
        name = species_names.get(sp_id, f"Species {sp_id}")
        header.extend([f"{name} (Mean)", f"{name} (Lower CI)", f"{name} (Upper CI)"])
    
    writer.writerow(header)
    
    # Перевірка на порожні дані
    if not sp_ids:
        return ""

    # Беремо години з першого виду
    first_sp_data = species_data[sp_ids[0]]['rai']
    if not first_sp_data: return ""
    
    hours = first_sp_data['hours']
    
    for i, hour in enumerate(hours):
        # Форматуємо час: якщо це ціле число (0, 1...), пишемо як ціле, інакше з дробною частиною
        val = float(hour) # Примусово перетворюємо в float
        time_str = f"{val:.0f}" if val.is_integer() else f"{val:.2f}"
        row = [time_str]
        
        for sp_id in sp_ids:
            data = species_data[sp_id]['rai']
            if data:
                mean = f"{data['mean'][i]:.4f}"
                
                # Перевіряємо, чи існують інтервали
                lower = f"{data['ci_lower'][i]:.4f}" if data['ci_lower'] is not None else ""
                upper = f"{data['ci_upper'][i]:.4f}" if data['ci_upper'] is not None else ""
                
                row.extend([mean, lower, upper])
            else:
                row.extend(["", "", ""])
        writer.writerow(row)
        
    output.seek(0)
    return output.getvalue()



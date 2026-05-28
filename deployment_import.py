"""
Імпорт деплойментів з ARD-Екселю (CT_LocationARD_Dataset.xlsx) у таблицю deployments.

Особливості:
  • Ідемпотентний — ключ дедуплікації = deployment_id (колонка name). Повторний
    запуск оновлює наявні рядки, не плодить дублікати. Можна доімпортовувати
    після поповнення Екселю.
  • Імпортує ТІЛЬКИ для локацій, координати яких (округлені до 5 знаків) уже є
    в таблиці locations. Решта — у звіт як 'skipped_no_location'.
  • Терпимий до «брудних» даних: різні назви колонок між листами, інверсна
    семантика (data_usable ↔ qc_data_not_usable), помилкові типи. Усе, що не
    вдалось привести до типу — лишається NULL і потрапляє у діагностичний звіт.

Публічне API:
    import_deployments(session, xlsx_path, sheets=None, dry_run=False) -> dict (звіт)
    format_report(report) -> str
"""
import os
import math
from collections import defaultdict

import pandas as pd

from .models import Deployment, Location


# Листи з даними деплойментів (решта — ReadMe/Progress/Template/lookup — ігноруємо).
DATA_SHEETS = [
    'SMM_2023', 'Data 2023-2024', 'WLCM_2023-24', 'SMM_2024',
    'WLCM_2024-2025', 'SMM_2025', 'WLCM_2025-26',
]

# Колонки, які свідомо НЕ імпортуємо (є в інших таблицях БД через location).
IGNORED_COLS = {
    'study_area_id', 'study_area_name_en', 'region_name_en',
}

TEXT_FIELDS = {'qc_local_datetime_issue', 'qc_comment'}
INT_FIELDS = {'study_year', 'n_days_working', 'n_photos'}
DATE_FIELDS = {'start_date', 'end_date'}
TIME_FIELDS = {'start_time', 'end_time'}
STR_FIELDS = {'name', 'study_season', 'study_design', 'camera_model', 'serial_number'}
BOOL_FIELDS = {
    'qc_non_functional', 'qc_stolen', 'qc_hardware_issue', 'qc_firmware_issue',
    'qc_settings_issue', 'qc_battery_issue', 'qc_sd_issue', 'qc_no_data_uploaded_by_pa',
    'qc_uploaded_data_is_not_raw', 'qc_no_gps_coordinates', 'qc_no_species_captured',
    'qc_placement_incorrect', 'qc_poor_placement', 'qc_feeding_location',
    'qc_installation_incorrect', 'qc_lapse_photos_missed', 'qc_installation_photos_missed',
    'qc_deinstallation_photos_missed', 'qc_distance_reference_photos_missed',
    'qc_datetime_photos_missed', 'qc_local_datetime_not_set', 'qc_data_not_usable',
    'qc_used_brf',
}

# Нормалізована назва колонки Екселю -> атрибут моделі.
ALIAS_MAP = {
    'study_year': 'study_year', 'study_season': 'study_season', 'study_design': 'study_design',
    'deployment_id': 'name', 'camera_id': 'camera_id', 'camera_model': 'camera_model',
    'serial_number': 'serial_number',
    'start_date': 'start_date', 'start_time': 'start_time',
    'end_date': 'end_date', 'end_time': 'end_time',
    'n_days_working': 'n_days_working', 'n_photos': 'n_photos',
    'latitude': '__lat', 'longitude': '__lon',
    # QC прямі + варіанти назв зі старих листів
    'qc_non_functional': 'qc_non_functional',
    'qc_stolen': 'qc_stolen', 'qc_camera_stolen': 'qc_stolen',
    'qc_hardware_issue': 'qc_hardware_issue',
    'qc_firmware_issue': 'qc_firmware_issue', 'qc_camera_firmware_issue': 'qc_firmware_issue',
    'qc_settings_issue': 'qc_settings_issue', 'qc_camera_settings_issue': 'qc_settings_issue',
    'qc_battery_issue': 'qc_battery_issue', 'qc_camera_battery_depleted': 'qc_battery_issue',
    'qc_sd_issue': 'qc_sd_issue', 'qc_sd_ damaged': 'qc_sd_issue',
    'qc_sd_damaged': 'qc_sd_issue', 'qc_camera_sd_damaged': 'qc_sd_issue',
    'qc_no_data_uploaded': 'qc_no_data_uploaded_by_pa',
    'qc_no_data_uploaded_by_pa': 'qc_no_data_uploaded_by_pa',
    'qc_uploaded_data_is_not_raw': 'qc_uploaded_data_is_not_raw',
    'qc_no_gps_coordinates': 'qc_no_gps_coordinates',
    'qc_no_gps_coordinate': 'qc_no_gps_coordinates',
    'qc_no_species_captured': 'qc_no_species_captured',
    'qc_no_data_captured': 'qc_no_species_captured',
    'qc_placement_incorrect': 'qc_placement_incorrect',
    'qc_placement_is_incorrect': 'qc_placement_incorrect',
    'qc_poor_placement': 'qc_poor_placement', 'qc_placement_is_poor': 'qc_poor_placement',
    'qc_feeding_location': 'qc_feeding_location',
    'qc_placement_at_feeding_location': 'qc_feeding_location',
    'qc_installation_incorrect': 'qc_installation_incorrect',
    'qc_installation_is_incorrect': 'qc_installation_incorrect',
    'qc_lapse_photos_missed': 'qc_lapse_photos_missed',
    'qc_installation_photos_missed': 'qc_installation_photos_missed',
    'qc_deinstallation_photos_missed': 'qc_deinstallation_photos_missed',
    'qc_distance_reference_photos_missed': 'qc_distance_reference_photos_missed',
    'qc_distance_reference_photos_are_missed': 'qc_distance_reference_photos_missed',
    'qc_datetime_photos_missed': 'qc_datetime_photos_missed',
    'qc_local_datetime_photos_missed': 'qc_datetime_photos_missed',
    'qc_local_datetime_photos_are_missed': 'qc_datetime_photos_missed',
    'qc_local_datetime_not_set': 'qc_local_datetime_not_set',
    'qc_data_not_usable': 'qc_data_not_usable',
    'qc_used_brf': 'qc_used_brf', 'used_brf': 'qc_used_brf',
    'used_basic_reporting_form': 'qc_used_brf',
    'qc_local_datetime_issue': 'qc_local_datetime_issue',
    'qc_set_local_time_issue': 'qc_local_datetime_issue',
    'qc_comment': 'qc_comment', 'comment': 'qc_comment', 'problem_comment': 'qc_comment',
}

# Джерела з інверсною семантикою: TRUE означає «все добре» -> зберігаємо як NOT.
INVERT_SOURCES = {
    'camera_functioning': 'qc_non_functional',
    'qc_functional': 'qc_non_functional',
    'qc_camera_functioning': 'qc_non_functional',
    'data_usable': 'qc_data_not_usable',
    'qc_data_usable': 'qc_data_not_usable',
    'set_local_time': 'qc_local_datetime_not_set',
    'qc_set_local_time': 'qc_local_datetime_not_set',
}

_TRUE = {'1', '1.0', 'true', 'yes', 'y', 'x', 'так', '+', 'так.'}
_FALSE = {'0', '0.0', 'false', 'no', 'n', 'ні', '-', 'none'}


def normalize_header(col):
    return ' '.join(str(col).strip().lower().replace('\n', ' ').split())


def _is_na(v):
    try:
        if pd.isna(v):
            return True
    except (TypeError, ValueError):
        if v is None:
            return True
    # Порожні рядки та нерозривні пробіли (\xa0) трактуємо як відсутнє значення
    if isinstance(v, str) and v.replace('\xa0', '').strip() == '':
        return True
    return False


def coerce_bool(v):
    if _is_na(v):
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        if v in (0, 1):
            return bool(v)
        raise ValueError(v)
    s = str(v).strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    raise ValueError(v)


def coerce_int(v):
    if _is_na(v):
        return None
    if isinstance(v, bool):
        raise ValueError(v)
    if isinstance(v, (int, float)):
        if float(v).is_integer():
            return int(v)
        raise ValueError(v)
    s = str(v).strip().replace(',', '')
    return int(float(s))  # кине ValueError на сміття


def coerce_year(v):
    """Рік. 'YYYY-YYYY' (зимова кампанія через 2 роки) -> стартовий рік."""
    if _is_na(v):
        return None
    if isinstance(v, str):
        import re
        m = re.search(r'\d{4}', v)
        if m:
            return int(m.group())
        raise ValueError(v)
    return coerce_int(v)


def coerce_date(v):
    if _is_na(v):
        return None
    ts = pd.to_datetime(v, errors='raise')
    return ts.date()


def coerce_time(v):
    if _is_na(v):
        return None
    import datetime as _dt
    if isinstance(v, _dt.time):
        return v
    if isinstance(v, _dt.datetime):
        return v.time()
    ts = pd.to_datetime(v, errors='raise')
    return ts.time()


def coerce_camera_id(v):
    """Текст; короткі чисті цифри доповнюємо нулями до 4 (NNNN). 5-знач. валідні. Обрізаємо до 10."""
    if _is_na(v):
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v).strip()
    if s.isdigit():
        s = s.zfill(4)
    return s[:10]


def coerce_str(v, maxlen=None):
    if _is_na(v):
        return None
    s = str(v).strip()
    if not s:
        return None
    return s[:maxlen] if maxlen else s


def _round5(x):
    v = round(float(x), 5)
    if not math.isfinite(v):  # NaN/inf (порожній GPS у Екселі) -> трактуємо як биті координати
        raise ValueError('non-finite coordinate')
    return v


def build_location_index(session):
    """{(round5(lat), round5(lon)): location_id}. Дублі координат -> перший + прапор."""
    index = {}
    ambiguous = set()
    for loc in session.query(Location.id, Location.latitude, Location.longitude).all():
        try:
            key = (_round5(loc.latitude), _round5(loc.longitude))
        except (TypeError, ValueError):
            continue  # пропускаємо локації з битими координатами (NaN тощо)
        if key in index:
            ambiguous.add(key)
        else:
            index[key] = loc.id
    return index, ambiguous


def import_deployments(session, xlsx_path, sheets=None, dry_run=False,
                       create_missing_locations=False, park_institution_map=None):
    """Імпорт/оновлення деплойментів з Екселю. Повертає звіт (dict).

    create_missing_locations: якщо True — для рядків без наявної локації
        створюється нова локація (name=deployment_id, координати з рядка) і,
        якщо park_institution_map дає установу за назвою парку, додається
        зв'язок location_institutions.
    park_institution_map: {normalize_header(study_area_name_EN): institution_id}.
    """
    from .models import Location, location_institutions

    if not os.path.exists(xlsx_path):
        raise FileNotFoundError(xlsx_path)

    park_institution_map = park_institution_map or {}
    sheets = sheets or DATA_SHEETS
    xl = pd.ExcelFile(xlsx_path)
    loc_index, ambiguous = build_location_index(session)

    report = {
        'xlsx': xlsx_path,
        'dry_run': dry_run,
        'inserted': 0,
        'updated': 0,
        'locations_created': 0,
        'locations_without_institution': 0,
        'no_coords_deployments': 0,
        'skipped_no_location': 0,
        'skipped_bad_coords': 0,
        'skipped_no_name': 0,
        'ambiguous_location': 0,
        'per_sheet': defaultdict(lambda: defaultdict(int)),
        'unmapped_columns': {},
        'coercion_warnings': [],   # (sheet, excel_row, field, raw_value)
        'unmapped_parks': set(),
        'sheets_missing': [],
    }

    # name -> Deployment (кеш у межах запуску, щоб дублі deployment_id між листами оновлювали один рядок)
    seen = {}

    for sheet in sheets:
        if sheet not in xl.sheet_names:
            report['sheets_missing'].append(sheet)
            continue
        df = pd.read_excel(xl, sheet_name=sheet)

        # Карта: оригінальна колонка -> (атрибут моделі, invert?)
        colmap = {}
        unmapped = []
        for col in df.columns:
            norm = normalize_header(col)
            if norm in IGNORED_COLS:
                continue
            if norm in INVERT_SOURCES:
                colmap[col] = (INVERT_SOURCES[norm], True)
            elif norm in ALIAS_MAP:
                colmap[col] = (ALIAS_MAP[norm], False)
            else:
                unmapped.append(str(col))
        if unmapped:
            report['unmapped_columns'][sheet] = unmapped

        norm_cols = {normalize_header(c): c for c in df.columns}
        name_col = next((c for c, (a, _i) in colmap.items() if a == 'name'), None)
        park_col = norm_cols.get('study_area_name_en')

        for idx, row in df.iterrows():
            excel_row = idx + 2  # +1 заголовок, +1 1-індекс
            ps = report['per_sheet'][sheet]
            ps['rows'] += 1

            # координати -> локація. NaN/відсутні -> деплоймент без локації (для QC-аналізу).
            lat = row.get('latitude'); lon = row.get('longitude')
            coords_finite = False
            key = None
            if not _is_na(lat) and not _is_na(lon):
                try:
                    lat_f = float(lat); lon_f = float(lon)
                    if math.isfinite(lat_f) and math.isfinite(lon_f):
                        coords_finite = True
                        key = (round(lat_f, 5), round(lon_f, 5))
                except (TypeError, ValueError):
                    # Реально нечитані координати (текст) -> bad_coords
                    report['skipped_bad_coords'] += 1
                    ps['skipped_bad_coords'] += 1
                    continue

            if not coords_finite:
                # Деплоймент без GPS — імпортуємо без локації; це qc_no_gps_coordinates у R-аналізі.
                location_id = None
                report['no_coords_deployments'] = report.get('no_coords_deployments', 0) + 1
                ps['no_coords'] = ps.get('no_coords', 0) + 1
            else:
                if key in ambiguous:
                    report['ambiguous_location'] += 1
                    ps['ambiguous_location'] += 1
                    continue
                location_id = loc_index.get(key)
                if location_id is None:
                    if not create_missing_locations:
                        report['skipped_no_location'] += 1
                        ps['skipped_no_location'] += 1
                        continue
                    # Створюємо нову локацію (name = deployment_id) + прив'язка установи
                    dep_name = coerce_str(row.get(name_col), 200) if name_col else None
                    if not dep_name:
                        report['skipped_no_name'] += 1
                        ps['skipped_no_name'] += 1
                        continue
                    inst_id = None
                    if park_col is not None:
                        park = normalize_header(row.get(park_col)) if not _is_na(row.get(park_col)) else None
                        inst_id = park_institution_map.get(park) if park else None
                        if park and inst_id is None:
                            report['unmapped_parks'].add(str(row.get(park_col)).strip())
                    new_loc = Location(name=dep_name, latitude=_round5(lat), longitude=_round5(lon))
                    session.add(new_loc)
                    session.flush()  # отримати id
                    location_id = new_loc.id
                    loc_index[key] = location_id
                    report['locations_created'] += 1
                    ps['locations_created'] += 1
                    if inst_id is not None:
                        session.execute(location_institutions.insert().values(
                            location_id=location_id, institution_id=inst_id))
                    else:
                        report['locations_without_institution'] += 1

            # збір значень полів
            values = {'location_id': location_id}
            comment_parts = []
            for col, (attr, invert) in colmap.items():
                if attr.startswith('__'):
                    continue
                raw = row.get(col)
                try:
                    if attr in BOOL_FIELDS:
                        val = coerce_bool(raw)
                        if invert and val is not None:
                            val = not val
                    elif attr == 'study_year':
                        val = coerce_year(raw)
                    elif attr in INT_FIELDS:
                        val = coerce_int(raw)
                    elif attr in DATE_FIELDS:
                        val = coerce_date(raw)
                    elif attr in TIME_FIELDS:
                        val = coerce_time(raw)
                    elif attr == 'camera_id':
                        val = coerce_camera_id(raw)
                    elif attr == 'qc_comment':
                        c = coerce_str(raw)
                        if c:
                            comment_parts.append(c)
                        continue
                    elif attr in TEXT_FIELDS:
                        val = coerce_str(raw)
                    elif attr in STR_FIELDS:
                        maxlen = 200 if attr == 'name' else 100
                        val = coerce_str(raw, maxlen)
                    else:
                        continue
                except (ValueError, TypeError):
                    report['coercion_warnings'].append((sheet, excel_row, attr, repr(raw)))
                    continue
                if val is not None:
                    values[attr] = val
            if comment_parts:
                values['qc_comment'] = '; '.join(comment_parts)[:100000]

            name = values.get('name')
            if not name:
                report['skipped_no_name'] += 1
                ps['skipped_no_name'] += 1
                continue

            # upsert за name (deployment_id)
            dep = seen.get(name)
            if dep is None:
                dep = session.query(Deployment).filter_by(name=name).first()
            if dep is None:
                dep = Deployment(**values)
                session.add(dep)
                seen[name] = dep
                report['inserted'] += 1
                ps['inserted'] += 1
            else:
                for k, v in values.items():
                    setattr(dep, k, v)
                seen[name] = dep
                report['updated'] += 1
                ps['updated'] += 1

    if dry_run:
        session.rollback()
    else:
        session.commit()

    report['per_sheet'] = {k: dict(v) for k, v in report['per_sheet'].items()}
    report['unmapped_parks'] = sorted(report['unmapped_parks'])
    return report


def format_report(report):
    lines = []
    lines.append('=' * 60)
    lines.append(f"Імпорт деплойментів: {report['xlsx']}")
    lines.append(f"Режим: {'DRY-RUN (без запису)' if report['dry_run'] else 'ЗАПИС'}")
    lines.append('-' * 60)
    lines.append(f"  Вставлено:               {report['inserted']}")
    lines.append(f"  Оновлено:                {report['updated']}")
    lines.append(f"  Створено локацій:        {report.get('locations_created', 0)}")
    lines.append(f"  Локацій без установи:    {report.get('locations_without_institution', 0)}")
    lines.append(f"  Деплойменти без GPS:     {report.get('no_coords_deployments', 0)}")
    lines.append(f"  Пропущено (нема локації):{report['skipped_no_location']}")
    lines.append(f"  Пропущено (биті коорд.): {report['skipped_bad_coords']}")
    lines.append(f"  Пропущено (нема назви):  {report['skipped_no_name']}")
    lines.append(f"  Неоднозначна локація:    {report['ambiguous_location']}")
    if report['sheets_missing']:
        lines.append(f"  Відсутні листи: {', '.join(report['sheets_missing'])}")
    lines.append('-' * 60)
    lines.append('По листах (rows / inserted / updated / no_location):')
    for sheet, st in report['per_sheet'].items():
        lines.append(f"  {sheet:18} {st.get('rows',0):4} / {st.get('inserted',0):4} / "
                     f"{st.get('updated',0):4} / {st.get('skipped_no_location',0):4}")
    if report['unmapped_columns']:
        lines.append('-' * 60)
        lines.append('Незмаплені (проігноровані) колонки:')
        for sheet, cols in report['unmapped_columns'].items():
            lines.append(f"  {sheet}: {', '.join(cols)}")
    if report.get('unmapped_parks'):
        lines.append('-' * 60)
        lines.append('Парки без зіставленої установи: ' + ', '.join(report['unmapped_parks']))
    warns = report['coercion_warnings']
    lines.append('-' * 60)
    lines.append(f"Помилки приведення типів: {len(warns)}")
    for w in warns[:40]:
        lines.append(f"  [{w[0]} рядок {w[1]}] {w[2]} = {w[3]}")
    if len(warns) > 40:
        lines.append(f"  ... ще {len(warns) - 40}")
    lines.append('=' * 60)
    return '\n'.join(lines)

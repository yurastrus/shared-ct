from flask import current_app
from sqlalchemy import text
from .database import get_ct_engine
from app.models import User

def get_ct_occurrence_data(filters, limit=None):
    """Fetch camera-trap occurrence rows (Darwin Core-style) plus a total count.

    Returns:
        dict: ``{'data': [...], 'total_count': N}``.

    Args:
        filters: may include ``institution_ids`` (list[int]) to restrict to those
            institutions' locations; None/absent = no restriction (admin only).
        limit: optional cap on returned rows (the count is computed separately).
    """
    engine = get_ct_engine()
    try:
        with engine.connect() as conn:
            params = {
                'start_date': filters.get('start_date'),
                'end_date': filters.get('end_date')
            }

            taxo_conditions = []
            if filters.get('species_ids'):
                taxo_conditions.append("s.id IN :species_ids")
                params['species_ids'] = tuple(filters['species_ids'])
            elif filters.get('genus'):
                taxo_conditions.append("s.genus = :genus")
                params['genus'] = filters['genus']
            elif filters.get('family'):
                taxo_conditions.append("s.family = :family")
                params['family'] = filters['family']
            elif filters.get('order'):
                taxo_conditions.append("s.order_rank = :order")
                params['order'] = filters['order']
            elif filters.get('class'):
                taxo_conditions.append('s."class" = :class')
                params['class'] = filters['class']

            taxo_where = " AND " + " AND ".join(taxo_conditions) if taxo_conditions else ""

            filter_type = filters.get('filter_type', 'species_only')
            species_filter_condition = "AND c.species_id > 0" if filter_type == 'species_only' else ""

            # --- Institution filter ---
            institution_ids = filters.get('institution_ids')
            if institution_ids:
                inst_cond = """
                    AND EXISTS (
                        SELECT 1 FROM location_institutions li_exp
                        WHERE li_exp.location_id = l.id
                          AND li_exp.institution_id = ANY(:export_inst_ids)
                    )"""
                params['export_inst_ids'] = list(institution_ids)
            else:
                inst_cond = ""

            # --- Main SQL query (CTE pipeline) ---

            base_query_cte = f"""
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
                        observation_id, species_id, max_quantity,
                        ROW_NUMBER() OVER(
                            PARTITION BY observation_id ORDER BY vote_count DESC, max_quantity DESC
                        ) as rn
                    FROM ObservationConsensus
                ),
                -- --- НОВИЙ БЛОК: Збираємо ID користувачів, які проголосували за вид-переможець ---
                WinningIdentifiers AS (
                    SELECT
                        p.observation_id,
                        i.species_id,
                        STRING_AGG(DISTINCT i.user_id::text, '|') as identifier_user_ids
                    FROM identifications i
                    JOIN photos p ON i.photo_id = p.id
                    GROUP BY p.observation_id, i.species_id
                ),
                -- --- КІНЕЦЬ НОВОГО БЛОКУ ---
                BaseData AS (
                    SELECT
                        o.id as observation_id,
                        c.species_id as winning_species_id,
                        c.max_quantity,
                        s.scientific_name, s.kingdom, s.phylum, s."class", s.order_rank,
                        s.family, s.genus, s.establishment_means,
                        o.series_start_time,
                        l.latitude as lat, l.longitude as lon, l.name as location_name, l.state_province,
                        wi.identifier_user_ids -- <-- ДОДАНО: Отримуємо рядок з ID
                    FROM observations o
                    JOIN RankedConsensus c ON o.id = c.observation_id AND c.rn = 1
                    JOIN locations l ON o.location_id = l.id
                    JOIN species s ON s.id = c.species_id
                    -- <-- ЗМІНЕНО: Приєднуємо новий CTE, щоб отримати правильних користувачів -->
                    LEFT JOIN WinningIdentifiers wi ON o.id = wi.observation_id AND c.species_id = wi.species_id
                    WHERE
                        o.status IN ('completed', 'archived')
                        AND DATE(o.series_start_time) BETWEEN :start_date AND :end_date
                        {species_filter_condition}
                        {taxo_where}
                        {inst_cond}
                )
            """

            aggregation = filters.get('aggregation')
            sql_query = ""
            count_query_str = ""

            if aggregation == 'location_day':
                aggregation_cte = f"""
                    , RankedAggregatedData AS (
                        SELECT
                            observation_id,
                            ROW_NUMBER() OVER(
                                PARTITION BY scientific_name, location_name, DATE(series_start_time)
                                ORDER BY max_quantity DESC, series_start_time ASC
                            ) as agg_rn
                        FROM BaseData
                    )
                    SELECT bd.*
                    FROM BaseData bd
                    JOIN RankedAggregatedData rad ON bd.observation_id = rad.observation_id
                    WHERE rad.agg_rn = 1
                """
                final_query_base = f"{base_query_cte} {aggregation_cte}"
                sql_query = f"{final_query_base} ORDER BY series_start_time"
                count_query_str = f"SELECT COUNT(*) FROM ({final_query_base}) as aggregated_data"

            elif aggregation == 'location_timewindow':
                # #36: «інтервал незалежності» — нова подія, коли проміжок між
                # сусідніми серіями того ж виду+локації > вікно (стандарт фото-
                # пасткової екології). Не ріже подію на межах сітки годинника.
                try:
                    agg_minutes = int(filters.get('aggregation_minutes', 5))
                except (ValueError, TypeError):
                    agg_minutes = 5
                max_win = current_app.config['CAMERA_TRAP_CONFIG'].get('EXPORT_MAX_AGG_MINUTES', 60)
                agg_minutes = max(1, min(agg_minutes, max_win))
                params['agg_seconds'] = agg_minutes * 60
                aggregation_cte = """
                    , EventTagged AS (
                        SELECT observation_id, scientific_name, location_name,
                               series_start_time, max_quantity,
                               CASE
                                 WHEN LAG(series_start_time) OVER w IS NULL THEN 1
                                 WHEN EXTRACT(EPOCH FROM (series_start_time
                                      - LAG(series_start_time) OVER w)) > :agg_seconds THEN 1
                                 ELSE 0
                               END AS is_new_event
                        FROM BaseData
                        WINDOW w AS (PARTITION BY scientific_name, location_name
                                     ORDER BY series_start_time)
                    ),
                    EventGrouped AS (
                        SELECT observation_id, scientific_name, location_name,
                               series_start_time, max_quantity,
                               SUM(is_new_event) OVER (
                                   PARTITION BY scientific_name, location_name
                                   ORDER BY series_start_time
                                   ROWS UNBOUNDED PRECEDING
                               ) AS event_id
                        FROM EventTagged
                    ),
                    RankedAggregatedData AS (
                        SELECT observation_id,
                               ROW_NUMBER() OVER(
                                   PARTITION BY scientific_name, location_name, event_id
                                   ORDER BY max_quantity DESC, series_start_time ASC
                               ) as agg_rn
                        FROM EventGrouped
                    )
                    SELECT bd.*
                    FROM BaseData bd
                    JOIN RankedAggregatedData rad ON bd.observation_id = rad.observation_id
                    WHERE rad.agg_rn = 1
                """
                final_query_base = f"{base_query_cte} {aggregation_cte}"
                sql_query = f"{final_query_base} ORDER BY series_start_time"
                count_query_str = f"SELECT COUNT(*) FROM ({final_query_base}) as aggregated_data"

            else:  # 'none' — no aggregation
                final_query_base = "SELECT * FROM BaseData"
                sql_query = f"{base_query_cte} {final_query_base} ORDER BY series_start_time"
                count_query_str = f"{base_query_cte} SELECT COUNT(*) FROM BaseData"

            total_count = conn.execute(text(count_query_str), params).scalar() or 0

            if limit:
                sql_query += " LIMIT :limit"
                params['limit'] = limit

            db_result = conn.execute(text(sql_query), params).mappings().fetchall()

            all_user_ids = set()
            for row in db_result:
                if row['identifier_user_ids']:
                    ids = [int(uid) for uid in row['identifier_user_ids'].split('|')]
                    all_user_ids.update(ids)

            user_map = {}
            if all_user_ids:
                # Query the main database for the identifiers' display names.
                users = User.query.filter(User.id.in_(list(all_user_ids))).all()
                user_map = {u.id: u.full_name for u in users}

            institution_code = filters.get('institution_code', 'WNBO-CT')
            occurrence_data = []
            for row in db_result:
                try:
                    specific_epithet = row['scientific_name'].split(' ', 1)[1]
                except IndexError:
                    specific_epithet = None

                identifiedBy = 'Human Expert'  # fallback
                user_ids_str = row['identifier_user_ids']
                if user_ids_str:
                    ids = [int(uid) for uid in user_ids_str.split('|')]
                    names = [user_map.get(uid, f'User #{uid}') for uid in ids]
                    identifiedBy = " | ".join(names)

                basisOfRecord = 'MachineObservation'  # always, per requirement
                identificationVerificationStatus = 'verified by human'
                identificationRemarks = 'Verified by expert consensus'

                occurrence_data.append({
                    'occurrenceID': f"URN:ctmon:{institution_code}:observation:{row['observation_id']}",
                    'basisOfRecord': basisOfRecord,
                    'identificationVerificationStatus': identificationVerificationStatus,
                    'identifiedBy': identifiedBy,
                    'identificationRemarks': identificationRemarks,
                    'institutionCode': institution_code,
                    'scientificName': row['scientific_name'], 'kingdom': row['kingdom'], 'phylum': row['phylum'],
                    'class': row['class'], 'order': row['order_rank'], 'family': row['family'], 'genus': row['genus'],
                    'specificEpithet': specific_epithet, 'establishmentMeans': row['establishment_means'],
                    'occurrenceStatus': 'present',
                    'eventDate': row['series_start_time'].strftime('%Y-%m-%d'),
                    'individualCount': row['max_quantity'],
                    'eventTime': row['series_start_time'].strftime('%H:%M:%S'),
                    'countryCode': 'UA',
                    'stateProvince': row['state_province'],
                    'locality': row['location_name'],
                    'decimalLatitude': float(row['lat']), 'decimalLongitude': float(row['lon']),
                    'geodeticDatum': 'WGS84',
                    'coordinateUncertaintyInMeters': 20,
                    'georeferenceSources': 'GPS (smartphone)',
                    'recordedBy': 'Automated camera trap'
                })

            return {'data': occurrence_data, 'total_count': total_count}

    except Exception as e:
        current_app.logger.error(f"Error getting camera trap occurrence data: {e}", exc_info=True)
        raise

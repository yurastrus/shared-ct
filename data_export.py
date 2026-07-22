# SPDX-License-Identifier: AGPL-3.0-only
from flask import current_app
from sqlalchemy import text
from .database import get_ct_engine
from app.models import User

# Whitelist of valid QC flag names — prevents SQL injection when building
# the dynamic NOT EXISTS clause.
_VALID_QC_BOOL_FLAGS = frozenset({
    'qc_non_functional', 'qc_stolen', 'qc_hardware_issue', 'qc_firmware_issue',
    'qc_settings_issue', 'qc_battery_issue', 'qc_sd_issue',
    'qc_no_data_uploaded_by_pa', 'qc_uploaded_data_is_not_raw',
    'qc_no_gps_coordinates', 'qc_no_species_captured', 'qc_placement_incorrect',
    'qc_poor_placement', 'qc_feeding_location', 'qc_installation_incorrect',
    'qc_lapse_photos_missed', 'qc_installation_photos_missed',
    'qc_deinstallation_photos_missed', 'qc_distance_reference_photos_missed',
    'qc_datetime_photos_missed', 'qc_local_datetime_not_set',
    'qc_data_not_usable', 'qc_used_brf',
})
# Text field: treated as "problem present" when the column is not NULL and not empty.
_QC_TEXT_FLAG = 'qc_local_datetime_issue'
_VALID_QC_FLAGS = _VALID_QC_BOOL_FLAGS | {_QC_TEXT_FLAG}


def _build_qc_exclusion_cond(qc_exclude, obs_alias='o'):
    """Return a bare ``NOT EXISTS (...)`` SQL predicate for QC exclusion, or "".

    The returned fragment carries NO leading `` AND `` so it can be dropped
    equally into a raw-SQL ``conditions`` list (AND-joined) or into an ORM
    ``.filter(text(...))`` clause. Callers add `` AND `` themselves when needed.

    Semantics (identical to #30):
        * boolean flag  → ``d_qc.<flag> = TRUE``
        * text flag     → ``IS NOT NULL AND <> ''``
        * flags OR-ed together; one matching deployment excludes the observation
        * observations with no overlapping deployment are NOT excluded

    Args:
        qc_exclude: list of flag names (validated here against _VALID_QC_FLAGS).
        obs_alias: SQL alias of the observations table in the outer query, used
            to correlate the subquery (``<alias>.location_id`` /
            ``<alias>.series_start_time``). Pass ``'observations'`` for ORM
            queries that join the ``Observation`` model.
    """
    safe = [f for f in qc_exclude if f in _VALID_QC_FLAGS]
    if not safe:
        return ""
    or_parts = []
    for flag in safe:
        if flag == _QC_TEXT_FLAG:
            or_parts.append(
                "(d_qc.qc_local_datetime_issue IS NOT NULL"
                " AND d_qc.qc_local_datetime_issue <> '')"
            )
        else:
            or_parts.append(f"d_qc.{flag} = TRUE")
    return f"""NOT EXISTS (
            SELECT 1 FROM deployments d_qc
            WHERE d_qc.location_id = {obs_alias}.location_id
              AND DATE({obs_alias}.series_start_time) BETWEEN d_qc.start_date AND d_qc.end_date
              AND ({' OR '.join(or_parts)})
        )"""


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

            qc_pred = _build_qc_exclusion_cond(filters.get('qc_exclude', []))
            qc_cond = (' AND ' + qc_pred) if qc_pred else ''

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
            # Standardised on the joined species alias ``s`` (was ``c.species_id``):
            # all record producers below join ``species s``, so ``s.id`` works
            # uniformly for consensus / conflict / AI rows.
            species_filter_condition = "AND s.id > 0" if filter_type == 'species_only' else ""

            # --- Export completeness mode (#new filter) ---
            #   consensus  — only series that reached expert consensus (default; legacy behaviour)
            #   human_any  — every series with >=1 human identification: consensus → 1 row,
            #                unresolved/conflicting → one row per competing species, all sharing
            #                the same ``observationID`` so R can group them
            #   human_ai   — human_any PLUS series with no human input at all, exported with the
            #                AI prediction (best model by accuracy_rank), flagged as AI in the
            #                identification columns + real confidence value
            export_mode = filters.get('export_mode', 'consensus')
            if export_mode not in ('consensus', 'human_any', 'human_ai'):
                export_mode = 'consensus'

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

            # Filter fragment shared by every record producer (date + taxon +
            # institution + QC + validity). Each producer joins ``o``/``l``/``s``,
            # so the same fragment drops into all of them.
            common_conditions = f"""
                        l.is_valid IS NOT FALSE  -- exclude admin-invalidated locations
                        AND DATE(o.series_start_time) BETWEEN :start_date AND :end_date
                        {species_filter_condition}
                        {taxo_where}
                        {inst_cond}
                        {qc_cond}"""

            # --- Shared CTEs (built from the identifications table) ---
            shared_ctes = """
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
                -- IDs of users who voted for each species in a series (per species,
                -- so it serves both the winning row and each competing conflict row).
                WinningIdentifiers AS (
                    SELECT
                        p.observation_id,
                        i.species_id,
                        STRING_AGG(DISTINCT i.user_id::text, '|') as identifier_user_ids
                    FROM identifications i
                    JOIN photos p ON i.photo_id = p.id
                    GROUP BY p.observation_id, i.species_id
                )
            """

            # Producer C — consensus rows (status completed/archived), one per series.
            producer_consensus = f"""
                    SELECT
                        o.id as observation_id,
                        s.id as species_id,
                        s.scientific_name, s.kingdom, s.phylum, s."class", s.order_rank,
                        s.family, s.genus, s.establishment_means,
                        o.series_start_time,
                        l.latitude as lat, l.longitude as lon, l.name as location_name, l.state_province,
                        rc.max_quantity,
                        wi.identifier_user_ids,
                        'human_consensus'::text as row_kind,
                        NULL::double precision as ai_confidence,
                        NULL::text as ai_model_name
                    FROM observations o
                    JOIN RankedConsensus rc ON o.id = rc.observation_id AND rc.rn = 1
                    JOIN locations l ON o.location_id = l.id
                    JOIN species s ON s.id = rc.species_id
                    LEFT JOIN WinningIdentifiers wi ON o.id = wi.observation_id AND rc.species_id = wi.species_id
                    WHERE o.status IN ('completed', 'archived')
                        AND {common_conditions}
            """

            # Producer H — unresolved/conflicting human series (status pending with
            # >=1 identification): one row per competing species.
            producer_conflict = f"""
                    SELECT
                        o.id as observation_id,
                        s.id as species_id,
                        s.scientific_name, s.kingdom, s.phylum, s."class", s.order_rank,
                        s.family, s.genus, s.establishment_means,
                        o.series_start_time,
                        l.latitude as lat, l.longitude as lon, l.name as location_name, l.state_province,
                        oc.max_quantity,
                        wi.identifier_user_ids,
                        'human_conflict'::text as row_kind,
                        NULL::double precision as ai_confidence,
                        NULL::text as ai_model_name
                    FROM observations o
                    JOIN ObservationConsensus oc ON o.id = oc.observation_id
                    JOIN locations l ON o.location_id = l.id
                    JOIN species s ON s.id = oc.species_id
                    LEFT JOIN WinningIdentifiers wi ON o.id = wi.observation_id AND oc.species_id = wi.species_id
                    WHERE o.status = 'pending'
                        AND {common_conditions}
            """

            # Producer A — AI-only series (no human identification at all), exported
            # with the prediction from the highest-ranked model that resolved a species.
            ai_pick_cte = """
                , AIPick AS (
                    SELECT
                        ap.observation_id,
                        ap.prediction_species_id,
                        ap.prediction_score,
                        COALESCE(ap.animal_count, 1) as animal_count,
                        am.name as model_name, am.version as model_version,
                        lvl.code as level_code,
                        ROW_NUMBER() OVER(
                            PARTITION BY ap.observation_id
                            ORDER BY COALESCE(lvl.accuracy_rank, 0) DESC,
                                     ap.prediction_score DESC NULLS LAST
                        ) as rn
                    FROM ai_predictions ap
                    JOIN ai_models am ON ap.model_id = am.id
                    LEFT JOIN ai_model_levels lvl ON am.level_id = lvl.id
                    WHERE ap.prediction_species_id IS NOT NULL
                )
            """
            producer_ai = f"""
                    SELECT
                        o.id as observation_id,
                        s.id as species_id,
                        s.scientific_name, s.kingdom, s.phylum, s."class", s.order_rank,
                        s.family, s.genus, s.establishment_means,
                        o.series_start_time,
                        l.latitude as lat, l.longitude as lon, l.name as location_name, l.state_province,
                        aip.animal_count as max_quantity,
                        NULL::text as identifier_user_ids,
                        'ai'::text as row_kind,
                        aip.prediction_score as ai_confidence,
                        (aip.model_name || ' ' || aip.model_version
                            || COALESCE(' (' || aip.level_code || ')', '')) as ai_model_name
                    FROM observations o
                    JOIN AIPick aip ON o.id = aip.observation_id AND aip.rn = 1
                    JOIN locations l ON o.location_id = l.id
                    JOIN species s ON s.id = aip.prediction_species_id
                    WHERE NOT EXISTS (
                            SELECT 1 FROM identifications i2
                            JOIN photos p2 ON i2.photo_id = p2.id
                            WHERE p2.observation_id = o.id
                          )
                        AND {common_conditions}
            """

            # Assemble the CTE header + UNION of the producers required by the mode.
            cte_header = shared_ctes
            producers = [producer_consensus]
            if export_mode in ('human_any', 'human_ai'):
                producers.append(producer_conflict)
            if export_mode == 'human_ai':
                cte_header = shared_ctes + ai_pick_cte
                producers.append(producer_ai)

            base_query_cte = f"""
                {cte_header},
                BaseData AS (
                    {' UNION ALL '.join(producers)}
                )
            """

            # Variant A: aggregation is meaningful only when there is exactly one
            # species per series. In human_any/human_ai a conflicting series expands
            # into several rows sharing an observationID, which independence-interval
            # reduction would tear apart — so aggregation is forced off there.
            aggregation = filters.get('aggregation') if export_mode == 'consensus' else 'none'
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
                # "Independence interval": a new event starts when the gap between
                # consecutive series of the same species+location exceeds the window
                # (a camera-trap ecology standard). Avoids splitting an event at
                # clock-grid boundaries.
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

                # Resolve the human identifiers' display names (if any).
                identifiedBy_human = ''
                user_ids_str = row['identifier_user_ids']
                if user_ids_str:
                    ids = [int(uid) for uid in user_ids_str.split('|')]
                    names = [user_map.get(uid, f'User #{uid}') for uid in ids]
                    identifiedBy_human = " | ".join(names)

                basisOfRecord = 'MachineObservation'  # always, per requirement
                row_kind = row.get('row_kind', 'human_consensus')
                ai_conf = row.get('ai_confidence')
                ai_model_name = row.get('ai_model_name')

                # Machine-readable confidence column: filled for AI rows only.
                identification_confidence = ''

                if row_kind == 'ai':
                    identifiedBy = ai_model_name or 'AI model'
                    identificationVerificationStatus = 'unverified (AI)'
                    if ai_conf is not None:
                        identification_confidence = round(float(ai_conf), 3)
                        identificationRemarks = (
                            f"Automatic detection by {identifiedBy}, "
                            f"confidence: {round(float(ai_conf), 2)}"
                        )
                    else:
                        identificationRemarks = f"Automatic detection by {identifiedBy}"
                elif row_kind == 'human_conflict':
                    identifiedBy = identifiedBy_human or 'Human Expert'
                    identificationVerificationStatus = 'competing human identifications (unresolved)'
                    identificationRemarks = (
                        'Competing identification — series has not reached expert '
                        'consensus. Group rows by observationID to see all candidates.'
                    )
                else:  # human_consensus (legacy default)
                    identifiedBy = identifiedBy_human or 'Human Expert'
                    identificationVerificationStatus = 'verified by human'
                    identificationRemarks = 'Verified by expert consensus'

                # Per-row occurrenceID must stay unique; conflict rows share a series
                # so disambiguate them by taxon. observationID is the shared grouping key.
                occurrence_id = f"URN:ctmon:{institution_code}:observation:{row['observation_id']}"
                if row_kind == 'human_conflict':
                    occurrence_id += f":taxon:{row['species_id']}"

                occurrence_data.append({
                    'occurrenceID': occurrence_id,
                    'observationID': row['observation_id'],
                    'basisOfRecord': basisOfRecord,
                    'identificationVerificationStatus': identificationVerificationStatus,
                    'identifiedBy': identifiedBy,
                    'identificationConfidence': identification_confidence,
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

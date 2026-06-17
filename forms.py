# SPDX-License-Identifier: AGPL-3.0-only
"""WTForms form definitions for the camera-traps module."""

from flask_wtf import FlaskForm
from wtforms import StringField, DecimalField, SelectField, SubmitField, HiddenField, IntegerField, BooleanField, RadioField, SelectMultipleField, TextAreaField, widgets
from wtforms.validators import DataRequired, NumberRange, Optional, Length
from app.camera_traps.domain import _l

class UploadForm(FlaskForm):
    """Photo-upload form; also supports creating a new location inline."""
    location = SelectField(_l('Виберіть місце'), coerce=int, validators=[Optional()])

    new_location_name = StringField(
        _l('Назва місця'), 
        validators=[Optional(), Length(min=3, max=200)]
    )
    description = StringField(
        _l('Опис місця'),
        validators=[Optional(), Length(max=500)]
    )
    latitude = DecimalField(
        _l('Широта (Lat)'), 
        places=5, 
        validators=[Optional(), NumberRange(min=-90.0, max=90.0, message=_l("Широта має бути між -90 та 90."))]
    )
    longitude = DecimalField(
        _l('Довгота (Lng)'), 
        places=5, 
        validators=[Optional(), NumberRange(min=-180.0, max=180.0, message=_l("Довгота має бути між -180 та 180."))]
    )

    create_location_submit = SubmitField(_l('Створити місце'))
    upload_submit = SubmitField(_l('Завантажити фотографії'))

class IdentificationForm(FlaskForm):
    """Series-level species identification (one vote applied to the whole observation)."""
    observation_id = HiddenField(_l("Observation ID"))

    # No DataRequired: species is validated client-side in JS; the field is declared
    # only so its name is available in the template.
    species = RadioField(_l('Вид тварини'), coerce=int, validators=[Optional()])
    
    quantity = IntegerField(_l('Особин'), default=1, validators=[DataRequired(), NumberRange(min=1, max=100)])
    
    behaviors = SelectMultipleField(
        _l('Додаткові теги'),
        coerce=int,
        validators=[Optional()],
        widget=widgets.ListWidget(prefix_label=False),
        option_widget=widgets.CheckboxInput()
    )
    
    is_favorite = BooleanField(_l('У вибране'))

    # Optional free-text note, capped at 200 chars.
    comment = TextAreaField(
        _l('Коментар'),
        validators=[Optional(), Length(max=200)],
        render_kw={'maxlength': 200, 'rows': 2}
    )
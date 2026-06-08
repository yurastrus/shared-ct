# myproject/app/camera_traps/forms.py

from flask_wtf import FlaskForm
# ВИПРАВЛЕННЯ 1: Додано HiddenField, IntegerField, BooleanField до імпортів
from wtforms import StringField, DecimalField, SelectField, SubmitField, HiddenField, IntegerField, BooleanField, RadioField, SelectMultipleField, TextAreaField, widgets
from wtforms.validators import DataRequired, NumberRange, Optional, Length
from flask_babel import lazy_gettext as _l # <-- Імпортуємо lazy_gettext

class UploadForm(FlaskForm):
    # Всі текстові мітки обгортаємо в _l()
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
    # ЗАМІНА: Використовуємо observation_id замість photo_id
    observation_id = HiddenField(_l("Observation ID")) 
    
    # ВИПРАВЛЕННЯ: Валідатор DataRequired тут не потрібен, бо перевірка робиться в JS.
    # Залишаємо поле для того, щоб можна було легко отримати його ім'я в шаблоні.
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

    # #47: необов'язковий вільний коментар до ідентифікації, ≤200 символів.
    comment = TextAreaField(
        _l('Коментар'),
        validators=[Optional(), Length(max=200)],
        render_kw={'maxlength': 200, 'rows': 2}
    )
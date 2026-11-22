import pytest
from pydantic import ValidationError
from jarvis.intelligence import DeskSettings

def test_desk_settings_ui_scale_default():
    settings = DeskSettings()
    assert settings.ui_scale == 100

def test_desk_settings_ui_scale_valid_values():
    for val in [50, 85, 90, 100, 110, 120, 150, 200]:
        settings = DeskSettings(ui_scale=val)
        assert settings.ui_scale == val

def test_desk_settings_ui_scale_bounds():
    with pytest.raises(ValidationError):
        DeskSettings(ui_scale=49)
    
    with pytest.raises(ValidationError):
        DeskSettings(ui_scale=201)

def test_desk_settings_ui_scale_serialization():
    settings = DeskSettings(ui_scale=110)
    data = settings.model_dump()
    assert data["ui_scale"] == 110
    restored = DeskSettings.model_validate(data)
    assert restored.ui_scale == 110

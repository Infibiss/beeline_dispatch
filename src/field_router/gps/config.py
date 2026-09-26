"""Bounded, replaceable GPS policy; durations are seconds, distances metres."""
from dataclasses import dataclass, asdict
import json
from pathlib import Path


@dataclass(frozen=True)
class GpsConfig:
    max_accuracy_m: float = 100
    stale_seconds: float = 90
    offline_seconds: float = 120
    lease_seconds: float = 120
    max_sample_age_seconds: float = 600
    stationary_seconds: float = 120
    moving_away_m: float = 300
    near_target_m: float = 80
    worksite_distance_m: float = 200
    worksite_span_seconds: float = 30
    reacquire_after_seconds: float = 90
    reacquire_span_seconds: float = 30
    reacquire_points: int = 3
    source_conflict_seconds: float = 60
    shift_grace_minutes: int = 10
    utc_offset_minutes: int = 180
    model_timeout_seconds: float = 1.0
    startup_timeout_seconds: float = 8.0
    failure_threshold: int = 3
    cooldown_seconds: float = 20
    max_points: int = 120
    max_engineers: int = 100
    history_seconds: float = 1800
    retention_seconds: int = 604800
    max_history_rows: int = 100000

    def __post_init__(self):
        import math
        for key, value in asdict(self).items():
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError('GPS: все настройки должны быть конечными числами')
            if key != 'utc_offset_minutes' and value <= 0:
                raise ValueError('GPS: настройки должны быть положительными')
        for key in ('max_points', 'max_engineers', 'reacquire_points', 'failure_threshold', 'shift_grace_minutes', 'utc_offset_minutes', 'retention_seconds', 'max_history_rows'):
            if type(getattr(self, key)) is not int:
                raise ValueError('GPS: ' + key + ' должен быть целым числом')
        if not 3 <= self.reacquire_points <= 6:
            raise ValueError('GPS: восстановление требует от 3 до 6 измерений')
        if not 2 <= self.max_points <= 500 or not 1 <= self.max_engineers <= 100:
            raise ValueError('GPS: превышен лимит буфера')
        if not .05 <= self.model_timeout_seconds <= 5 or not .1 <= self.startup_timeout_seconds <= 20:
            raise ValueError('GPS: таймаут вне допустимых границ')
        if not -720 <= self.utc_offset_minutes <= 840:
            raise ValueError('GPS: недопустимое смещение часового пояса')
        if not 3600 <= self.retention_seconds <= 2592000 or not 100 <= self.max_history_rows <= 1000000:
            raise ValueError('GPS: архив от 1 часа до 30 дней и от 100 до 1000000 точек')

    @classmethod
    def load(cls, path=None):
        return cls(**json.loads(Path(path).read_text(encoding='utf-8'))) if path else cls()

    def public(self):
        return asdict(self)

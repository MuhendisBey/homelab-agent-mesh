"""Pure thermodynamic and psychrometric calculations for indoor comfort & mold analysis."""

from __future__ import annotations

import math

_MAGNUS_A = 6.1078
_MAGNUS_B = 17.27
_MAGNUS_C = 237.3
_GAS_CONST_WATER_FACTOR = 216.679
_KELVIN_OFFSET = 273.15
_MIN_VALID_TEMP_C = -237.0


def is_valid_humidity(rel_humidity: float | None) -> bool:
    """Return True iff ``rel_humidity`` is finite and in ``[0.0, 100.0]``.

    Rejects ``None``, NaN/Inf, booleans, negative values, and sensor sentinel values
    such as Bosch eTRV ``255.0``.
    """
    if rel_humidity is None or isinstance(rel_humidity, bool):
        return False
    try:
        val = float(rel_humidity)
    except (TypeError, ValueError):
        return False
    return math.isfinite(val) and 0.0 <= val <= 100.0


def sanitize_humidity(rel_humidity: float | None) -> float | None:
    """Return ``float(rel_humidity)`` when valid, otherwise ``None``."""
    if not is_valid_humidity(rel_humidity):
        return None
    assert rel_humidity is not None
    return float(rel_humidity)


def _validate_temperature_c(temp_c: float) -> float:
    val = float(temp_c)
    if not math.isfinite(val) or val <= _MIN_VALID_TEMP_C:
        raise ValueError(f"Invalid temperature in Celsius: {temp_c!r}")
    return val


def _validate_humidity_pct(rel_humidity: float) -> float:
    if not is_valid_humidity(rel_humidity):
        raise ValueError(f"Relative humidity must be within [0, 100]%: {rel_humidity!r}")
    return float(rel_humidity)


def calculate_saturation_vapor_pressure_hpa(temp_c: float) -> float:
    """Compute saturation vapor pressure in hPa via the Magnus-Tetens formula."""
    t_c = _validate_temperature_c(temp_c)
    exponent = (_MAGNUS_B * t_c) / (t_c + _MAGNUS_C)
    return _MAGNUS_A * math.exp(exponent)


def calculate_absolute_humidity(temp_c: float, rel_humidity: float) -> float:
    """Compute absolute humidity in ``g/m³`` from temperature (°C) and RH (%)."""
    t_c = _validate_temperature_c(temp_c)
    rh = _validate_humidity_pct(rel_humidity)
    p_sat = calculate_saturation_vapor_pressure_hpa(t_c)
    p_v = p_sat * (rh / 100.0)
    return (_GAS_CONST_WATER_FACTOR * p_v) / (t_c + _KELVIN_OFFSET)


def calculate_dew_point(temp_c: float, rel_humidity: float) -> float:
    """Compute dew-point temperature in °C using Magnus-Tetens approximation."""
    t_c = _validate_temperature_c(temp_c)
    rh = _validate_humidity_pct(rel_humidity)
    rh_eff = max(rh, 0.1)
    gamma = math.log(rh_eff / 100.0) + (_MAGNUS_B * t_c) / (t_c + _MAGNUS_C)
    denom = _MAGNUS_B - gamma
    if abs(denom) <= 1e-9:
        raise ValueError(f"Dew point singularity for temp_c={temp_c!r}, rh={rel_humidity!r}")
    return (_MAGNUS_C * gamma) / denom


def calculate_enthalpy(
    temp_c: float,
    rel_humidity: float,
    pressure_hpa: float = 1013.25,
) -> float:
    """Compute moist-air specific enthalpy in ``kJ/kg`` dry air."""
    t_c = _validate_temperature_c(temp_c)
    rh = _validate_humidity_pct(rel_humidity)
    p_atm = float(pressure_hpa)
    if not math.isfinite(p_atm) or p_atm <= 0.0:
        raise ValueError(f"Atmospheric pressure must be positive hPa: {pressure_hpa!r}")
    p_sat = calculate_saturation_vapor_pressure_hpa(t_c)
    p_v = p_sat * (rh / 100.0)
    specific_humidity = 0.62198 * p_v / max(p_atm - p_v, 1.0)
    return 1.006 * t_c + specific_humidity * (2501.0 + 1.86 * t_c)

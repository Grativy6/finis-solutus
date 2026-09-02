#!/usr/bin/env python3
"""Exact arithmetic helpers for the Finis Solutus v0.16 ledgers.

The module intentionally contains no campaign I/O. Its functions accept values,
return immutable results, and leave narrative judgment (for example, how much EXP
an activity deserves) to the DM. Decimal values are serialized as JSON strings by
the CLI so a caller never loses precision through an implicit binary float.
"""

from __future__ import annotations

import argparse
import json
import math
from contextlib import contextmanager
from dataclasses import asdict, dataclass, is_dataclass
from decimal import Decimal, Inexact, InvalidOperation, ROUND_FLOOR, Rounded, localcontext
from typing import Any, Iterator, Sequence


DecimalInput = Decimal | int | str | float

ZERO = Decimal("0")
ONE = Decimal("1")
TWO = Decimal("2")
THREE = Decimal("3")
FOUR = Decimal("4")
TEN = Decimal("10")
TWENTY = Decimal("20")
THIRTY = Decimal("30")
FORTY = Decimal("40")
HALF = Decimal("0.5")
SECONDS_PER_MINUTE = Decimal("60")
SECONDS_PER_HOUR = Decimal("3600")
SECONDS_PER_DAY = Decimal("86400")

# Defensive implementation bounds, not Finis Solutus gameplay caps. They keep
# malformed model/tool input from allocating unbounded memory or making one
# receipt enumerate an absurd number of threshold crossings. The accepted range
# is far beyond any plausible campaign value.
MAX_INPUT_DIGITS = 128
MAX_ABS_INPUT_EXPONENT = 128
MAX_DECIMAL_LITERAL_CHARS = 512
MAX_LEVEL_DIGITS = 128
MAX_LEVEL_VALUE = 10**MAX_LEVEL_DIGITS - 1
MAX_CROSSINGS_PER_OPERATION = 10_000
EXACT_CONTEXT_PRECISION = 1024


class ImplementationLimitError(ValueError):
    """A tooling safety limit was reached; this is not a gameplay rule."""


def to_decimal(value: DecimalInput, *, name: str = "value") -> Decimal:
    """Convert a library or CLI value to a bounded, finite Decimal.

    Library callers must use strings, integers, or ``Decimal`` instances. Binary
    floats and booleans are rejected so an apparently exact ledger mutation cannot
    arrive already rounded or be confused with 0/1.
    """

    if isinstance(value, bool):
        raise TypeError(f"{name} must not be a boolean")
    if isinstance(value, float):
        raise TypeError(
            f"{name} must not be a float; pass a Decimal, integer, or decimal string"
        )
    if not isinstance(value, (Decimal, int, str)):
        raise TypeError(f"{name} must be a Decimal, integer, or decimal string")
    if isinstance(value, str) and len(value.strip()) > MAX_DECIMAL_LITERAL_CHARS:
        raise ImplementationLimitError(
            f"{name} exceeds the decimal literal implementation limit"
        )
    if isinstance(value, int) and abs(value) >= 10**MAX_INPUT_DIGITS:
        raise ImplementationLimitError(
            f"{name} exceeds the {MAX_INPUT_DIGITS}-digit implementation limit"
        )
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a decimal number") from exc
    if not number.is_finite():
        raise ValueError(f"{name} must be finite")
    parts = number.as_tuple()
    if len(parts.digits) > MAX_INPUT_DIGITS:
        raise ImplementationLimitError(
            f"{name} exceeds the {MAX_INPUT_DIGITS}-digit implementation limit"
        )
    if abs(parts.exponent) > MAX_ABS_INPUT_EXPONENT:
        raise ImplementationLimitError(
            f"{name} exponent exceeds the +/-{MAX_ABS_INPUT_EXPONENT} "
            "implementation limit"
        )
    return number


def _nonnegative(value: DecimalInput, *, name: str) -> Decimal:
    number = to_decimal(value, name=name)
    if number < ZERO:
        raise ValueError(f"{name} must be nonnegative")
    return number


def _level(value: int | str) -> int:
    if isinstance(value, bool):
        raise ValueError("level must be a positive integer")
    if isinstance(value, int):
        level = value
    elif isinstance(value, str):
        raw = value.strip()
        if raw.startswith("+"):
            raw = raw[1:]
        if not raw or not raw.isascii() or not raw.isdigit():
            raise ValueError("level must be a positive integer")
        significant = raw.lstrip("0") or "0"
        if len(significant) > MAX_LEVEL_DIGITS:
            raise ImplementationLimitError(
                f"level exceeds the {MAX_LEVEL_DIGITS}-digit implementation limit; "
                "this is not a gameplay Level cap"
            )
        level = int(significant)
    else:
        raise TypeError("level must be an integer or integer string")
    if level < 1:
        raise ValueError("level must be at least 1")
    if level > MAX_LEVEL_VALUE:
        raise ImplementationLimitError(
            f"level exceeds the {MAX_LEVEL_DIGITS}-digit implementation limit; "
            "this is not a gameplay Level cap"
        )
    return level


@contextmanager
def _exact_context() -> Iterator[object]:
    """Use enough precision for all bounded inputs and trap accidental rounding."""

    with localcontext() as context:
        context.prec = EXACT_CONTEXT_PRECISION
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        yield context


@dataclass(frozen=True)
class DerivedMaxima:
    maximum_hp: Decimal
    maximum_rp: Decimal
    maximum_fatigue: Decimal


def calculate_maxima(
    strength: DecimalInput,
    agility: DecimalInput,
    stamina: DecimalInput,
    wisdom: DecimalInput,
) -> DerivedMaxima:
    """Return the v0.16 default derived-resource maxima."""

    strength_d = _nonnegative(strength, name="strength")
    agility_d = _nonnegative(agility, name="agility")
    stamina_d = _nonnegative(stamina, name="stamina")
    wisdom_d = _nonnegative(wisdom, name="wisdom")
    with _exact_context():
        return DerivedMaxima(
            maximum_hp=THREE * strength_d + TWO * agility_d + FOUR * stamina_d,
            maximum_rp=TEN * wisdom_d,
            maximum_fatigue=strength_d + TWO * agility_d + FOUR * stamina_d,
        )


def maximum_hp(
    strength: DecimalInput, agility: DecimalInput, stamina: DecimalInput
) -> Decimal:
    """Return maximum HP: ``3*STR + 2*AGI + 4*STA``."""

    strength_d = _nonnegative(strength, name="strength")
    agility_d = _nonnegative(agility, name="agility")
    stamina_d = _nonnegative(stamina, name="stamina")
    with _exact_context():
        return THREE * strength_d + TWO * agility_d + FOUR * stamina_d


def maximum_rp(wisdom: DecimalInput) -> Decimal:
    """Return maximum RP: ``10*WIS``."""

    wisdom_d = _nonnegative(wisdom, name="wisdom")
    with _exact_context():
        return TEN * wisdom_d


def maximum_fatigue(
    strength: DecimalInput, agility: DecimalInput, stamina: DecimalInput
) -> Decimal:
    """Return maximum Fatigue: ``STR + 2*AGI + 4*STA``."""

    strength_d = _nonnegative(strength, name="strength")
    agility_d = _nonnegative(agility, name="agility")
    stamina_d = _nonnegative(stamina, name="stamina")
    with _exact_context():
        return strength_d + TWO * agility_d + FOUR * stamina_d


def level_threshold(level: int | str) -> Decimal:
    """Return EXP needed to leave ``level``: ``100 + 10*(L-1)``."""

    level_i = _level(level)
    return Decimal(100 + 10 * (level_i - 1))


@dataclass(frozen=True)
class AdvancementResult:
    prior_level: int
    prior_exp: Decimal
    award: Decimal
    level: int
    exp: Decimal
    current_threshold: Decimal
    levels_gained: int
    player_points_granted: int
    earned_points_granted: int
    thresholds_crossed: tuple[Decimal, ...]


def advance_exp(
    level: int | str, current_exp: DecimalInput, award: DecimalInput
) -> AdvancementResult:
    """Apply one EXP award and all crossed thresholds atomically.

    The function tallies awards; it does not decide whether the award is deserved
    or assign the resulting Earned Points to particular stats.
    """

    prior_level = _level(level)
    prior_exp = _nonnegative(current_exp, name="current_exp")
    award_d = _nonnegative(award, name="award")
    starting_threshold = level_threshold(prior_level)
    if prior_exp >= starting_threshold:
        raise ValueError(
            "current_exp must be less than the current Level threshold; "
            "reconcile the earlier advancement before applying a new award"
        )

    with _exact_context():
        total_exp = prior_exp + award_d

    # Thresholds form an arithmetic sequence T, T+10, ... . The exact cost of n
    # crossings is n*T + 5*n*(n-1). Solve the integer quadratic with isqrt so even
    # rejected extreme input takes bounded work and no Decimal approximation.
    available_integer_exp = int(total_exp.to_integral_value(rounding=ROUND_FLOOR))
    first_threshold = int(starting_threshold)
    linear_term = first_threshold - 5
    discriminant = linear_term * linear_term + 20 * available_integer_exp
    levels_gained = max(0, (math.isqrt(discriminant) - linear_term) // 10)

    def crossing_cost(count: int) -> int:
        return count * first_threshold + 5 * count * (count - 1)

    while crossing_cost(levels_gained + 1) <= available_integer_exp:
        levels_gained += 1
    while crossing_cost(levels_gained) > available_integer_exp:
        levels_gained -= 1

    if levels_gained > MAX_CROSSINGS_PER_OPERATION:
        raise ImplementationLimitError(
            f"EXP operation crosses {levels_gained} Levels, exceeding the "
            f"per-operation implementation limit of {MAX_CROSSINGS_PER_OPERATION}; "
            "this is not a gameplay Level cap"
        )
    new_level = prior_level + levels_gained
    if new_level > MAX_LEVEL_VALUE:
        raise ImplementationLimitError(
            f"resulting Level exceeds the {MAX_LEVEL_DIGITS}-digit implementation "
            "limit; this is not a gameplay Level cap"
        )

    with _exact_context():
        exp = total_exp - Decimal(crossing_cost(levels_gained))
    crossed = tuple(
        Decimal(first_threshold + 10 * index) for index in range(levels_gained)
    )
    return AdvancementResult(
        prior_level=prior_level,
        prior_exp=prior_exp,
        award=award_d,
        level=new_level,
        exp=exp,
        current_threshold=level_threshold(new_level),
        levels_gained=levels_gained,
        player_points_granted=2 * levels_gained,
        earned_points_granted=levels_gained,
        thresholds_crossed=crossed,
    )


@dataclass(frozen=True)
class FatigueCostResult:
    prior_fatigue: Decimal
    maximum_fatigue: Decimal
    assessed_cost: Decimal
    available_headroom: Decimal
    overflow: Decimal
    fatigue: Decimal
    prior_remainder: Decimal
    assessed_hp_loss: Decimal
    applied_hp_loss: Decimal | None
    remainder: Decimal
    prior_hp: Decimal | None
    hp: Decimal | None
    over_cap: bool

    @property
    def hp_lost(self) -> Decimal:
        """Backward-compatible alias for the formula's assessed HP loss."""

        return self.assessed_hp_loss


def _remainder(value: DecimalInput) -> Decimal:
    remainder = _nonnegative(value, name="overexertion_remainder")
    if remainder >= FOUR:
        raise ValueError("overexertion_remainder must be less than 4")
    return remainder


def apply_fatigue_cost(
    current_fatigue: DecimalInput,
    maximum: DecimalInput,
    assessed_cost: DecimalInput,
    overexertion_remainder: DecimalInput = ZERO,
    current_hp: DecimalInput | None = None,
) -> FatigueCostResult:
    """Apply v0.16 Fatigue headroom, overflow, and 4:1 HP strain.

    ``assessed_hp_loss`` reports the formula's full strain amount;
    ``applied_hp_loss`` reports the amount actually removed from supplied HP. A
    host resolves any zero-HP consequence separately.
    """

    fatigue_d = _nonnegative(current_fatigue, name="current_fatigue")
    maximum_d = _nonnegative(maximum, name="maximum_fatigue")
    cost_d = _nonnegative(assessed_cost, name="assessed_cost")
    remainder_d = _remainder(overexertion_remainder)

    with _exact_context():
        headroom = max(ZERO, maximum_d - fatigue_d)
        overflow = max(ZERO, cost_d - headroom)
        if fatigue_d > maximum_d:
            new_fatigue = fatigue_d
        else:
            new_fatigue = min(maximum_d, fatigue_d + cost_d)

        overexertion_total = remainder_d + overflow
        assessed_hp_loss = (overexertion_total / FOUR).to_integral_value(
            rounding=ROUND_FLOOR
        )
        new_remainder = overexertion_total % FOUR

    hp_before: Decimal | None = None
    hp_after: Decimal | None = None
    applied_hp_loss: Decimal | None = None
    if current_hp is not None:
        hp_before = _nonnegative(current_hp, name="current_hp")
        with _exact_context():
            applied_hp_loss = min(hp_before, assessed_hp_loss)
            hp_after = hp_before - applied_hp_loss

    return FatigueCostResult(
        prior_fatigue=fatigue_d,
        maximum_fatigue=maximum_d,
        assessed_cost=cost_d,
        available_headroom=headroom,
        overflow=overflow,
        fatigue=new_fatigue,
        prior_remainder=remainder_d,
        assessed_hp_loss=assessed_hp_loss,
        applied_hp_loss=applied_hp_loss,
        remainder=new_remainder,
        prior_hp=hp_before,
        hp=hp_after,
        over_cap=new_fatigue > maximum_d,
    )


@dataclass(frozen=True)
class FatigueRecoveryResult:
    prior_fatigue: Decimal
    maximum_fatigue: Decimal
    requested_recovery: Decimal
    prior_remainder: Decimal
    remainder_recovered: Decimal
    remainder: Decimal
    fatigue_recovered: Decimal
    fatigue: Decimal
    unused_recovery: Decimal
    over_cap: bool


def recover_fatigue(
    current_fatigue: DecimalInput,
    maximum: DecimalInput,
    recovery: DecimalInput,
    overexertion_remainder: DecimalInput = ZERO,
) -> FatigueRecoveryResult:
    """Apply genuine Fatigue recovery to remainder first, then Fatigue."""

    fatigue_d = _nonnegative(current_fatigue, name="current_fatigue")
    maximum_d = _nonnegative(maximum, name="maximum_fatigue")
    recovery_d = _nonnegative(recovery, name="recovery")
    remainder_d = _remainder(overexertion_remainder)

    with _exact_context():
        remainder_recovered = min(recovery_d, remainder_d)
        new_remainder = remainder_d - remainder_recovered
        available_for_fatigue = recovery_d - remainder_recovered
        fatigue_recovered = min(available_for_fatigue, fatigue_d)
        new_fatigue = fatigue_d - fatigue_recovered
        unused = available_for_fatigue - fatigue_recovered

    return FatigueRecoveryResult(
        prior_fatigue=fatigue_d,
        maximum_fatigue=maximum_d,
        requested_recovery=recovery_d,
        prior_remainder=remainder_d,
        remainder_recovered=remainder_recovered,
        remainder=new_remainder,
        fatigue_recovered=fatigue_recovered,
        fatigue=new_fatigue,
        unused_recovery=unused,
        over_cap=new_fatigue > maximum_d,
    )


def advance_time(
    current_seconds: DecimalInput, elapsed_seconds: DecimalInput
) -> Decimal:
    """Advance a monotonic campaign clock represented as seconds from Day 1."""

    current = _nonnegative(current_seconds, name="current_seconds")
    elapsed = _nonnegative(elapsed_seconds, name="elapsed_seconds")
    with _exact_context():
        return current + elapsed


def _format_second(second: Decimal) -> str:
    raw = format(second, "f")
    if "." in raw:
        raw = raw.rstrip("0").rstrip(".")
    whole, dot, fraction = raw.partition(".")
    padded = whole.zfill(2)
    return f"{padded}{dot}{fraction}" if dot else padded


def format_campaign_time(
    total_seconds: DecimalInput, *, include_seconds: bool | None = None
) -> str:
    """Format seconds from Day 1 as ``Day N · h:mm[:ss] AM/PM``.

    With ``include_seconds=None``, seconds appear exactly when stored time is not
    on a whole-minute boundary. Passing ``True`` always shows them; passing
    ``False`` produces a minute-scale display without changing state.
    """

    total = _nonnegative(total_seconds, name="total_seconds")
    with _exact_context():
        day_index = int(total // SECONDS_PER_DAY)
        within_day = total % SECONDS_PER_DAY
        hour_24 = int(within_day // SECONDS_PER_HOUR)
        within_hour = within_day % SECONDS_PER_HOUR
        minute = int(within_hour // SECONDS_PER_MINUTE)
        second = within_hour % SECONDS_PER_MINUTE

    show_seconds = second != ZERO if include_seconds is None else include_seconds
    suffix = "AM" if hour_24 < 12 else "PM"
    hour_12 = hour_24 % 12 or 12
    clock = f"{hour_12}:{minute:02d}"
    if show_seconds:
        clock += f":{_format_second(second)}"
    return f"Day {day_index + 1} · {clock} {suffix}"


def base_load_limit(strength: DecimalInput) -> Decimal:
    """Return default human-scale Base Load Limit B from STR."""

    strength_d = _nonnegative(strength, name="strength")
    with _exact_context():
        if strength_d <= TWENTY:
            return strength_d
        if strength_d <= FORTY:
            return TWENTY + HALF * (strength_d - TWENTY)
        return THIRTY


def practical_load_limit(
    strength: DecimalInput,
    body_trait_factor: DecimalInput = ONE,
    current_capability_factor: DecimalInput = ONE,
) -> Decimal:
    """Apply established body/trait and current-capability factors to B."""

    body_factor = _nonnegative(body_trait_factor, name="body_trait_factor")
    capability_factor = _nonnegative(
        current_capability_factor, name="current_capability_factor"
    )
    base = base_load_limit(strength)
    with _exact_context():
        return base * body_factor * capability_factor


# Compact, stable imports for ledger integrations.
max_hp = maximum_hp
max_rp = maximum_rp
max_fatigue = maximum_fatigue
exp_threshold = level_threshold
apply_exp = advance_exp


def decimal_string(value: Decimal) -> str:
    """Return a canonical, non-exponent JSON representation of a Decimal."""

    if not value.is_finite():
        raise ValueError("cannot serialize a nonfinite Decimal")
    if value.is_zero():
        return "0"
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return decimal_string(value)
    if is_dataclass(value):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Exact Finis Solutus v0.16 ledger arithmetic"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    maxima = commands.add_parser("maxima", help="calculate HP/RP/Fatigue maxima")
    maxima.add_argument("--strength", required=True)
    maxima.add_argument("--agility", required=True)
    maxima.add_argument("--stamina", required=True)
    maxima.add_argument("--wisdom", required=True)

    exp = commands.add_parser("exp", help="apply an EXP award atomically")
    exp.add_argument("--level", required=True)
    exp.add_argument("--current-exp", required=True)
    exp.add_argument("--award", required=True)

    fatigue_cost = commands.add_parser(
        "fatigue-cost", help="apply Fatigue cost and overexertion"
    )
    fatigue_cost.add_argument("--current", required=True)
    fatigue_cost.add_argument("--maximum", required=True)
    fatigue_cost.add_argument("--cost", required=True)
    fatigue_cost.add_argument("--remainder", default="0")
    fatigue_cost.add_argument("--hp")

    fatigue_recover = commands.add_parser(
        "fatigue-recover", help="apply recovery to remainder then Fatigue"
    )
    fatigue_recover.add_argument("--current", required=True)
    fatigue_recover.add_argument("--maximum", required=True)
    fatigue_recover.add_argument("--amount", required=True)
    fatigue_recover.add_argument("--remainder", default="0")

    time_advance = commands.add_parser(
        "time-advance", help="advance seconds from Day 1"
    )
    time_advance.add_argument("--current-seconds", required=True)
    time_advance.add_argument("--elapsed-seconds", required=True)
    time_advance.add_argument(
        "--show-seconds", action="store_true", help="always include seconds"
    )

    time_format = commands.add_parser("time-format", help="format seconds from Day 1")
    time_format.add_argument("--total-seconds", required=True)
    time_format.add_argument(
        "--show-seconds", action="store_true", help="always include seconds"
    )

    load = commands.add_parser("load-limit", help="calculate default load limits")
    load.add_argument("--strength", required=True)
    load.add_argument("--body-factor", default="1")
    load.add_argument("--capability-factor", default="1")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "maxima":
            result: Any = calculate_maxima(
                args.strength, args.agility, args.stamina, args.wisdom
            )
        elif args.command == "exp":
            result = advance_exp(args.level, args.current_exp, args.award)
        elif args.command == "fatigue-cost":
            result = apply_fatigue_cost(
                args.current, args.maximum, args.cost, args.remainder, args.hp
            )
        elif args.command == "fatigue-recover":
            result = recover_fatigue(
                args.current, args.maximum, args.amount, args.remainder
            )
        elif args.command == "time-advance":
            total = advance_time(args.current_seconds, args.elapsed_seconds)
            result = {
                "total_seconds": total,
                "formatted": format_campaign_time(
                    total, include_seconds=True if args.show_seconds else None
                ),
            }
        elif args.command == "time-format":
            total = _nonnegative(args.total_seconds, name="total_seconds")
            result = {
                "total_seconds": total,
                "formatted": format_campaign_time(
                    total, include_seconds=True if args.show_seconds else None
                ),
            }
        elif args.command == "load-limit":
            result = {
                "base_load_limit": base_load_limit(args.strength),
                "practical_load_limit": practical_load_limit(
                    args.strength, args.body_factor, args.capability_factor
                ),
            }
        else:  # pragma: no cover
            parser.error(f"unknown command: {args.command}")
    except (TypeError, ValueError) as exc:
        parser.error(str(exc))

    print(json.dumps(_jsonable(result), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

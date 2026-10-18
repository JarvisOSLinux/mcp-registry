#!/usr/bin/env python3
"""selftest_property_types.py — prove the typed-setting checks fire (#115).

A setting's `type` tells forms what to draw (a switch, a choice, a number) and
`dmcp config set` what to accept, so a wrong declaration fails quietly at the
moment a user saves. Each rule is checked against the real validator entry
point on a throwaway registry, including the cases that must stay silent.

  1. Typed settings with valid defaults pass, silently.
  2. An unknown type, or a field another type owns, ERRORS.
  3. A default the type does not accept ERRORS (boolean spelling, choice,
     each item of a multiple choice, integer range).
  4. A secret that is not a string ERRORS.
  5. Malformed choices, values, separators and bounds ERROR.
  6. A setting declared twice, or with an unknown field, ERRORS.

Offline, stdlib only, writes nothing outside its temp directory. Reuses the
throwaway-registry fixture of selftest_tool_declarations.py.

Usage:
  python3 scripts/selftest_property_types.py
"""
import sys

import selftest_tool_declarations as harness

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def settings(*props):
    def edit(m):
        m["configurableProperties"] = [dict(label=p["key"], description="d", sensitive=False, required=False) | p for p in props]

    return edit


@case
def typed_settings_pass_silently():
    good = settings(
        {"key": "READ_ONLY", "type": "boolean", "default": "false"},
        {"key": "TELEMETRY", "type": "boolean", "values": {"true": "1", "false": "0"}, "default": "1"},
        {"key": "SECURITY", "type": "choice", "choices": ["ssl", "starttls"], "default": "ssl"},
        {"key": "PRESET", "type": "choice", "multiple": True, "separator": ",", "choices": ["mail", "calendar"],
         "default": "mail,calendar"},
        {"key": "PORT", "type": "integer", "min": 1, "max": 65535, "default": "993"},
        {"key": "API_KEY", "type": "string", "sensitive": True, "required": True},
        {"key": "URL"},
    )
    with harness.fixture(good):
        code, out = harness.validate()
        harness.check(code == 0, "every type with a valid default passes")
        harness.check("::error::" not in out and "::warning::" not in out, "silently")


@case
def an_unknown_type_or_a_misplaced_field_is_an_error():
    harness.expect_error(settings({"key": "X", "type": "list"}), needle="type 'list' is not one of",
                         label="an unknown type")
    harness.expect_error(settings({"key": "X", "type": "integer", "choices": ["a"]}),
                         needle="'choices' applies only to type 'choice'", label="choices on an integer")
    harness.expect_error(settings({"key": "X", "min": 1}), needle="'min' applies only to type 'integer'",
                         label="min on a string")


@case
def a_default_the_type_rejects_is_an_error():
    cases = [
        ({"key": "X", "type": "boolean", "default": "yes"}, "default 'yes' must be 'true' or 'false'"),
        ({"key": "X", "type": "boolean", "values": {"true": "1", "false": "0"}, "default": "true"},
         "default 'true' must be '1' or '0'"),
        ({"key": "X", "type": "choice", "choices": ["a", "b"], "default": "c"}, "must be one of a, b"),
        ({"key": "X", "type": "choice", "multiple": True, "choices": ["a", "b"], "default": "a,c"},
         "each of its items must be one of a, b"),
        ({"key": "X", "type": "integer", "min": 1, "max": 10, "default": "11"}, "must be at most 10"),
        ({"key": "X", "type": "integer", "default": "3.5"}, "must be a whole number"),
    ]
    for prop, needle in cases:
        harness.expect_error(settings(prop), needle=needle, label=f"default {prop['default']!r}")


@case
def a_secret_is_always_a_string():
    harness.expect_error(settings({"key": "X", "type": "choice", "choices": ["a"], "sensitive": True}),
                         needle="a secret must be of type 'string'", label="a secret choice")


@case
def malformed_declarations_are_errors():
    cases = [
        ({"key": "X", "type": "choice", "choices": []}, "choices must be a non-empty list of distinct strings"),
        ({"key": "X", "type": "choice", "choices": ["a", "a"]}, "choices must be a non-empty list of distinct strings"),
        ({"key": "X", "type": "boolean", "values": {"true": "1", "false": "1"}}, "two different non-empty strings"),
        ({"key": "X", "type": "choice", "choices": ["a"], "separator": ";"}, "separator applies only with"),
        ({"key": "X", "type": "choice", "multiple": True, "choices": ["a,b"]}, "a choice contains the separator"),
        ({"key": "X", "type": "integer", "min": 5, "max": 1}, "min is above max"),
        ({"key": "X", "type": "integer", "min": "1"}, "min and max must be whole numbers"),
    ]
    for prop, needle in cases:
        harness.expect_error(settings(prop), needle=needle, label=needle)


@case
def duplicate_and_unknown_fields_are_errors():
    harness.expect_error(settings({"key": "X"}, {"key": "X"}), needle="setting 'X' is declared twice",
                         label="a setting declared twice")
    harness.expect_error(settings({"key": "X", "placeholder": "e.g."}), needle="has unknown field(s) ['placeholder']",
                         label="an unknown field")


def main() -> int:
    for fn in CASES:
        harness.running = fn.__name__
        print(f"  {harness.running}")
        fn()
    if harness.FAILURES:
        print(f"\nFAIL: {len(harness.FAILURES)} self-test assertion(s) failed.")
        for failure in harness.FAILURES:
            print(f"  - {failure}")
        return 1
    print("\nOK: property type self-test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

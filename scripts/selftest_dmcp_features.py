#!/usr/bin/env python3
"""selftest_dmcp_features.py — prove the `execution` hold and the
`requiredFeatures` check fire (dmcp#63).

dmcp 0.2.0 and older skip a transport's `execution` block and run the launch
line on the host, so the registry refuses the block until those versions are
out of use. `requiredFeatures` names dmcp features a manifest cannot run
without; a name dmcp doesn't know makes the server uninstallable everywhere.
Each rule runs against the real validator on a throwaway registry, including
the cases that must stay silent.

  1. A manifest with neither passes, silently; so does a known feature name.
  2. An `execution` block on any transport ERRORS, naming the transport.
  3. An unknown feature name ERRORS.
  4. A `requiredFeatures` that is not an array of names ERRORS.

Offline, stdlib only, writes nothing outside its temp directory. Reuses the
throwaway-registry fixture of selftest_tool_declarations.py.

Usage:
  python3 scripts/selftest_dmcp_features.py
"""
import sys

import selftest_tool_declarations as harness

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def passes_silently(edit, label):
    with harness.fixture(edit):
        code, out = harness.validate()
        harness.check(code == 0, f"{label} passes")
        harness.check("::error::" not in out and "::warning::" not in out, f"{label}: silently")


@case
def no_backend_and_known_features_pass():
    passes_silently(None, "a manifest with neither field")

    def known(m):
        m["requiredFeatures"] = ["execution"]

    passes_silently(known, "a known feature name")

    def empty(m):
        m["requiredFeatures"] = []

    passes_silently(empty, "an empty list")


@case
def an_execution_block_is_held_back():
    def docker(m):
        m["transports"][0]["execution"] = {"type": "docker", "image": "python:3-slim"}

    harness.expect_error(docker, needle="transports[0]: 'execution' is not accepted",
                         label="a docker backend")

    def wrapper_on_second(m):
        m["transports"].append(dict(m["transports"][0], execution={"wrapper": ["ssh", "h", "--"]}))
        m["transports"][0]["platforms"] = ["windows"]

    harness.expect_error(wrapper_on_second, needle="transports[1]: 'execution' is not accepted",
                         label="a wrapper on a later transport")

    def with_marker(m):
        m["requiredFeatures"] = ["execution"]
        m["transports"][0]["execution"] = {"type": "docker", "image": "python:3-slim"}

    harness.expect_error(with_marker, needle="dmcp 0.2.0 and older ignore it",
                         label="a backend even when the manifest lists the feature")


@case
def an_unknown_feature_is_an_error():
    def unknown(m):
        m["requiredFeatures"] = ["execution", "teleport"]

    harness.expect_error(unknown, needle="requiredFeatures lists ['teleport']",
                         label="an unknown name")


@case
def a_malformed_list_is_an_error():
    for value in ("execution", {"execution": True}, ["execution", 5]):
        def malformed(m, value=value):
            m["requiredFeatures"] = value

        harness.expect_error(malformed, needle="'requiredFeatures' must be an array of feature names",
                             label=f"requiredFeatures = {value!r}")


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
    print("\nOK: dmcp features self-test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

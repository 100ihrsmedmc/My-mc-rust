#!/usr/bin/env python3
"""Regression tests for the resource-preparation tools.

Run with the standard library only:

    python3 -m unittest discover -s tools/tests -v

Every test builds its own inputs in a temporary directory and calls the real
functions; nothing is mocked and no network is used.
"""

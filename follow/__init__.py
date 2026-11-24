"""Microduck follow / interaction module.

A client of robotd: it only sends intents over /run/robotd.sock and never touches motors,
HOME, fall handling or policies directly. See README.md for the safety contract.
"""

__all__ = ["robotd_client", "safety", "motion"]

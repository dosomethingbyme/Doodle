#!/usr/bin/env python3
"""Compatibility entry point for the booking application."""

from booking_app.app import *  # noqa: F401,F403
from booking_app.app import main


if __name__ == "__main__":
    main()

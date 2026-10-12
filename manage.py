#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""
import os
import sys


def main():
    """Run administrative tasks."""
    # `manage.py test` uses isolated settings (fake keys, in-memory DB, no network).
    default_settings = 'algo_tracker_backend.test_settings' if sys.argv[1:2] == ['test']         else 'algo_tracker_backend.settings'
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', default_settings)
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == '__main__':
    main()

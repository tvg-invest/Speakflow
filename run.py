#!/usr/bin/env python3
"""SpeakFlow - Voice to text, effortlessly."""
import sys
import os
import logging

def main():
    # Activate the venv site-packages so the embedded Python binary
    # (which lives outside the venv) can find all dependencies.
    venv_site = os.path.join(
        os.path.expanduser('~/.speakflow'), 'venv', 'lib',
        f'python{sys.version_info.major}.{sys.version_info.minor}',
        'site-packages',
    )
    if os.path.isdir(venv_site) and venv_site not in sys.path:
        sys.path.insert(0, venv_site)

    # Ensure config directory exists before setting up file logging
    os.makedirs(os.path.expanduser('~/.speakflow'), exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(os.path.expanduser('~/.speakflow/speakflow.log')),
            logging.StreamHandler()
        ]
    )

    # Check Python version
    if sys.version_info < (3, 9):
        print("SpeakFlow requires Python 3.9 or later")
        sys.exit(1)

    # Import and run
    from speakflow.app import SpeakFlowApp
    app = SpeakFlowApp()
    app.run()

if __name__ == '__main__':
    main()

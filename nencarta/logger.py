import logging

LOG = logging.getLogger('nencarta')
LOG.setLevel(logging.INFO)

if not LOG.hasHandlers():
    handler = logging.StreamHandler()
    formatter = logging.Formatter(
        '%(asctime)s [%(name)s] %(levelname)s: %(message)s'
    )
    handler.setFormatter(formatter)
    LOG.addHandler(handler)

def set_log_level(level: str):
    """
    Set the logging level.

    Parameters:
    - level (str): The logging level to set ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL').
    """
    numeric_level = getattr(logging, level.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError(f'Invalid log level: {level}')
    LOG.setLevel(numeric_level)
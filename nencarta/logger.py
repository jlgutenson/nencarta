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
    Set how much NenCarta logs.

    Parameters
    ----------
    level : str
        ``DEBUG``, ``INFO`` (the default), ``WARNING``, ``ERROR`` or ``CRITICAL``, in any case.

    Raises
    ------
    TypeError
        When ``level`` isn't a logging level.
    """
    numeric_level = getattr(logging, level.upper(), None)
    if not isinstance(numeric_level, int):
        raise TypeError(f'Invalid log level: {level}')
    
    LOG.setLevel(numeric_level)
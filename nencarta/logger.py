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

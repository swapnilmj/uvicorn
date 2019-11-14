import os
import pytest
import sys
from pathlib import Path

from uvicorn.config import Config
from uvicorn.supervisors import StatReload


def test_statreload():
    """
    A basic sanity check.

    Simply run the reloader against a no-op server, and signal for it to
    quit immediately.
    """
    config = Config(app=None, reload=True)
    reloader = StatReload(config, sockets=[])
    reloader.startup()
    reloader.shutdown(hard=True)


@pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="Skipping reload test on Windows, due to low mtime resolution.",
)
def test_should_reload(tmpdir):
    update_file = Path(os.path.join(str(tmpdir), "example.py"))
    update_file.touch()

    working_dir = os.getcwd()
    os.chdir(str(tmpdir))
    try:
        config = Config(app=None, reload=True)
        reloader = StatReload(config, sockets=[])
        reloader.startup()

        assert not reloader.should_restart()
        update_file.touch()
        assert reloader.should_restart()

        reloader.restart()
        reloader.shutdown(hard=True)
    finally:
        os.chdir(working_dir)

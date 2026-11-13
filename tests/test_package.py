import linuxcru


def test_version_exists() -> None:
    assert linuxcru.__version__ == "0.1.0"

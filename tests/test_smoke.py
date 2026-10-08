import naga


def test_package_importable():
    assert naga.__name__ == "naga"
    assert naga.__version__

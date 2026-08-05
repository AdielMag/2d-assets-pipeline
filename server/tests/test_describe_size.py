from app.storage import describe_size


def test_describe_size_labels_by_aspect():
    assert describe_size(800, 224) == "wide 800x224"
    assert describe_size(224, 800) == "tall 224x800"
    assert describe_size(256, 256) == "square-ish 256x256"


def test_describe_size_rejects_degenerate_boxes():
    assert describe_size(0, 100) == "empty"
    assert describe_size(100, -1) == "empty"

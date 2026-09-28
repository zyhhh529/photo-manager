from photoman import config


def test_remember_library_moves_to_front_and_caps_list():
    cfg = {"library_root": None, "recent_libraries": []}
    for i in range(config.MAX_RECENT_LIBRARIES + 2):
        config.remember_library(cfg, f"/Volumes/D{i}/Photos/")
    config.remember_library(cfg, "/Volumes/D3/Photos")
    assert cfg["library_root"] == "/Volumes/D3/Photos"
    assert cfg["recent_libraries"][0] == "/Volumes/D3/Photos"
    assert cfg["recent_libraries"].count("/Volumes/D3/Photos") == 1
    assert len(cfg["recent_libraries"]) == config.MAX_RECENT_LIBRARIES


def test_known_libraries_keeps_every_destination():
    cfg = {"library_root": None, "recent_libraries": [], "known_libraries": []}
    for i in range(config.MAX_RECENT_LIBRARIES + 3):
        config.remember_library(cfg, f"/Volumes/D{i}/Photos")
    assert len(cfg["recent_libraries"]) == config.MAX_RECENT_LIBRARIES
    assert len(cfg["known_libraries"]) == config.MAX_RECENT_LIBRARIES + 3
    # configs written before known_libraries existed still work
    assert config.known_libraries({"library_root": "/a", "recent_libraries": ["/a", "/b"]}) == ["/a", "/b"]

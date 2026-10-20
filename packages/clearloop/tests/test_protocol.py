from clearloop.printer.state import HmsEvent, PrinterState
from clearloop.protocol import Marker, MarkerEdgeDetector, decode, encode


def test_encode_decode_roundtrip():
    g = encode(Marker.EJECT)
    assert "M73 L9003" in g and "G4 S2" in g
    assert decode(9003) == Marker.EJECT
    assert decode(42) is None
    assert decode(None) is None


def test_edge_detector_only_fires_on_change():
    d = MarkerEdgeDetector()
    seq = [1, 2, 9001, 9001, 3, 9003, 9003, 9004, 1, 9001]
    assert [m for m in map(d.feed, seq) if m] == [
        Marker.JOB_START,
        Marker.EJECT,
        Marker.SWEEP_DONE,
        Marker.JOB_START,
    ]


def test_state_merges_partial_reports():
    s = PrinterState()
    s.merge(
        {
            "print": {
                "gcode_state": "RUNNING",
                "mc_percent": 10,
                "layer_num": 3,
                "ams": {"ams": [{"tray": [{"tray_type": "PLA"}, {"tray_type": "PETG"}]}]},
            }
        }
    )
    s.merge({"print": {"mc_percent": 11}})
    s.merge({"print": {"ams": {"tray_now": "1"}}})
    assert s.gcode_state == "RUNNING" and s.percent == 11 and s.layer_num == 3
    assert s.ams_types == ["PETG", "PLA"]
    s.merge({"print": {"hms": [{"attr": 0x07002000, "code": 0x00020001}]}})
    assert s.blocking_errors == ["HMS 0700_2000_0002_0001 (serious)"]
    s.merge({"info": {"module": [{"name": "ota", "sw_ver": "01.08.02.00"}]}})
    assert s.firmware == "01.08.02.00"


def test_hms_info_is_not_blocking():
    assert not HmsEvent(0x0C000300, 0x00040001).blocking

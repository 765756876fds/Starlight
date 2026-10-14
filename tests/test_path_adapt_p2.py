"""P2 路径自适应收尾：events.log_dir / capture_dir 默认值基于 PROJECT_ROOT。"""
import inspect

from app.core.path_resolve import PROJECT_ROOT
from app.core.event_log import EventLog
from app.lol.ongoing.diagnostic import ChampSelectDiagnostic


def _signature_default(func, param_name):
    sig = inspect.signature(func)
    return sig.parameters[param_name].default


def test_diagnostic_capture_dir_absolute():
    """capture_dir 默认值是绝对路径（不依赖 CWD）。"""
    default = _signature_default(ChampSelectDiagnostic.__init__, "capture_dir")
    assert isinstance(default, str)
    assert default.startswith(str(PROJECT_ROOT))


def test_diagnostic_capture_dir_points_to_runtime():
    """capture_dir 默认值 == PROJECT_ROOT/runtime。"""
    default = _signature_default(ChampSelectDiagnostic.__init__, "capture_dir")
    assert default == str(PROJECT_ROOT / "runtime")


def test_event_log_default_log_dir_absolute():
    """EventLog 类默认 log_dir 是绝对路径。"""
    default = _signature_default(EventLog.__init__, "log_dir")
    assert isinstance(default, str)
    assert default.startswith(str(PROJECT_ROOT))


def test_event_log_default_points_to_data_events():
    """EventLog 默认 log_dir == PROJECT_ROOT/data/events。"""
    default = _signature_default(EventLog.__init__, "log_dir")
    assert default == str(PROJECT_ROOT / "data" / "events")

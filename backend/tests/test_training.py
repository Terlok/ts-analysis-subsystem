import numpy as np
import pytest

from app.services.training import TrainingError, train_forecaster
from tsa_core.pipeline import ChannelProcessor

SEC = 1_000_000


def test_train_forecaster_beats_naive_on_predictable_signal():
    n = 6000
    ts = 1_760_000_000 * SEC + np.arange(n) * SEC
    rng = np.random.default_rng(0)
    x = np.sin(np.arange(n) / 15) * 5 + 0.05 * rng.standard_normal(n)  # smooth: trend is predictable
    model, metrics, params = train_forecaster(ts, x, 15, 3.0, 30, trees=50, depth=4)
    assert metrics["mae_test"] < metrics["mae_test_naive"]
    assert metrics["sigma_r"] > 0 and params["target"] == "increment"
    assert metrics["split"]["gap"] == 30

    # the trained model plugs into the streaming pipeline and flags an injected impulse
    x2 = x.copy()
    x2[5500] += 3
    res = ChannelProcessor("c", forecaster=model).process(ts, x2)
    assert res.anomaly[5500]


def test_train_forecaster_rejects_short_series():
    ts = np.arange(100) * SEC
    with pytest.raises(TrainingError):
        train_forecaster(ts, np.zeros(100), 15, 3.0, 30)


def test_auto_activation_requires_gain():
    from app.services.training import should_activate

    assert should_activate("auto", {"mae_gain": 0.2})
    assert not should_activate("auto", {"mae_gain": -0.1})
    assert should_activate("always", {"mae_gain": -0.1})
    assert not should_activate("never", {"mae_gain": 0.5})

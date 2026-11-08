"""trader-backtest: the account replay and the time-machine filter, on the re-timed TSLA fixture
(59 NSE-timed sessions of real prices, see test_timemachine.py)."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout

from _load import FIX, load

bt = load("trader-backtest", "backtest")
ts = load("trader-backtest", "tm_signals")
NOW = "2026-10-19 10:35"


def run(mod, args):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = mod.main(args)
    return code, buf.getvalue()


class Backtest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp()
        cls.data = os.path.join(cls.d, "data")
        os.makedirs(cls.data)
        shutil.copy(os.path.join(FIX, "trader-timemachine", "TSLA-60d5m.json.gz"), os.path.join(cls.data, "TSLA.json.gz"))
        cls.cache = os.path.join(cls.d, "tm.json")
        code, _ = run(ts, ["--data", cls.data, "--out", cls.cache, "--replays", "300", "--workers", "1"])
        assert code == 0

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.d)

    def bt(self, *extra):
        code, out = run(bt, ["--data", self.data, "--capital", "15000", "--now", NOW, "--json"] + list(extra))
        self.assertEqual(code, 0)
        return json.loads(out)

    def test_every_signal_matches_the_time_machine_history(self):
        # one stock, one position at a time: the account takes every first close above the range,
        # the same 33 triggers the time machine's own history replay finds (test_timemachine.py)
        s = self.bt()["summary"]
        self.assertEqual(s["trades"], 33)
        self.assertEqual(s["sessions"], 59)

    def test_the_cache_is_walk_forward(self):
        with open(self.cache) as fh:
            doc = json.load(fh)
        rows = doc["signals"]["TSLA"]
        self.assertEqual((doc["replays"], doc["until"], doc["lookback"]), (300, "15:15", 59))
        self.assertTrue(rows)
        # the pool only ever holds sessions before the signal's day, at least the time machine's 20
        days = sorted(r["date"] for r in rows)
        self.assertTrue(all(20 <= r["pool"] <= 59 for r in rows))
        self.assertEqual([r["pool"] for r in sorted(rows, key=lambda r: r["date"])],
                         sorted(r["pool"] for r in rows))           # the pool grows day by day, never peeks ahead
        self.assertGreater(days[0], "2026-08-20")            # the 21st session of the fixture or later
        for r in rows:
            self.assertGreaterEqual(r["slot"], 3)
            self.assertLessEqual(r["slot"], 65)
            self.assertAlmostEqual(r["p_target"] + r["p_stop"] + r["p_neither"], 1.0, places=3)

    def test_the_filter_only_takes_signals_the_time_machine_passes(self):
        with open(self.cache) as fh:
            rows = json.load(fh)["signals"]["TSLA"]
        allow = self.bt("--tm-cache", self.cache)                       # no threshold: any verdict passes
        self.assertEqual(allow["summary"]["trades"], len(rows))
        strict = self.bt("--tm-cache", self.cache, "--tm-min-lean", "0")
        want = {r["date"] for r in rows if r["lean"] is not None and r["lean"] >= 0}
        self.assertEqual({t["date"] for t in strict["trades"]}, want)
        none = self.bt("--tm-cache", self.cache, "--tm-min-avg-r", "99")
        self.assertEqual(none["summary"]["trades"], 0)

    def test_a_cache_for_another_plan_is_refused(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            import contextlib
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = bt.main(["--data", self.data, "--now", NOW, "--tm-cache", self.cache, "--stop", "or-low"])
        self.assertEqual(code, 1)
        self.assertIn("built with stop=or-mid", err.getvalue())

    def test_charges(self):
        cfg = {"brokerage_pct": 0.03, "brokerage_cap": 20.0, "stt_pct": 0.025, "txn_pct": 0.00307, "stamp_pct": 0.003}
        # Rs 10,000 bought and sold: brokerage 3 + 3, STT 2.5, NSE 0.307 x 2, SEBI 0.01 x 2, stamp 0.3, GST 18 %
        total = bt.charges(10000, "buy", cfg) + bt.charges(10000, "sell", cfg)
        self.assertAlmostEqual(total, 6 + 2.5 + 0.614 + 0.02 + 0.3 + 0.18 * (6 + 0.614 + 0.02), places=3)
        self.assertAlmostEqual(bt.charges(1e7, "buy", cfg) - bt.charges(1e7 - 1, "buy", cfg),
                               (0.0000307 + 1e-6 + 0.00003) * 1.0 + 0.18 * (0.0000307 + 1e-6), places=6)


if __name__ == "__main__":
    unittest.main()

import copy
import math
import unittest
from processing.quality import QualityProcessor, trend_forecast


def sample(seq=0, value=20):
    return dict(device='discovery01', seq=seq, uptime_ms=seq*1000,
                valid=True, temperature_c=value, humidity_percent=50,
                pressure_hpa=1016, error_count=0)


class QualityTests(unittest.TestCase):
    def test_duplicate_does_not_refresh(self):
        p = QualityProcessor(); p.process(sample(), 1000)
        self.assertEqual(p.process(sample(), 5000)['quality_flags'], ['duplicate'])
        self.assertEqual(p.snapshot(7000)['status'], 'stale')
        self.assertEqual(p.snapshot(7000)['raw_decision'], 'withheld')

    def test_conflicting_duplicate_invalidates(self):
        p = QualityProcessor(); p.process(sample(), 0)
        self.assertEqual(p.process(sample(value=25), 1000)['quality_flags'], ['conflicting_duplicate'])
        self.assertEqual(p.snapshot(1000)['status'], 'unavailable')

    def test_old_reading_cannot_replace_latest(self):
        p = QualityProcessor(); p.process(sample(10), 0)
        rejected = p.process(sample(9, 30), 1000)
        self.assertFalse(rejected['accepted'])
        self.assertEqual(p.snapshot(1000)['latest_accepted']['seq'], 10)

    def test_missing_gap_resets_filter_and_forecast(self):
        p = QualityProcessor()
        for i in range(25): p.process(sample(i), i*1000)
        self.assertEqual(p.snapshot(24000)['forecast_status'], 'ready')
        result = p.process(sample(30, 24), 30000)
        self.assertEqual(result['filtered_temperature_c'], 24)
        self.assertEqual(result['missing_sequence_numbers'], 5)
        self.assertEqual(p.snapshot(30000)['forecast_status'], 'warming_up')

    def test_invalid_blocks_decisions_and_recovery_starts_fresh(self):
        for change in [dict(valid=False), dict(temperature_c=None), dict(humidity_percent=True),
                       dict(pressure_hpa=101600), dict(seq=True), dict(error_count=-1), dict(device='other')]:
            with self.subTest(change=change):
                p = QualityProcessor(); p.process(sample(), 0)
                bad = sample(1); bad.update(change)
                self.assertFalse(p.process(bad, 1000)['accepted'])
                self.assertEqual(p.snapshot(1000)['raw_decision'], 'withheld')
                self.assertEqual(p.process(sample(2, 23), 2000)['filtered_temperature_c'], 23)

    def test_parse_and_clock_errors(self):
        p = QualityProcessor(); p.process(sample(), 1000)
        self.assertFalse(p.process(None, 2000, 'bad JSON')['accepted'])
        p.process(sample(1), 3000)
        self.assertEqual(p.process(sample(2), 2500)['quality_flags'], ['reception_clock_backward'])
        self.assertEqual(p.snapshot(3000)['status'], 'unavailable')

    def test_source_gap_even_with_fast_reception(self):
        p = QualityProcessor(); p.process(sample(), 0)
        r = sample(1, 24); r['uptime_ms'] = 6001
        self.assertTrue(p.process(r, 100)['filter_reset'])

    def test_stale_boundary_and_future_clock(self):
        p = QualityProcessor(); p.process(sample(value=23), 1000)
        self.assertEqual(p.snapshot(6000)['raw_decision'], 'warning')
        self.assertEqual(p.snapshot(6001)['raw_decision'], 'withheld')
        self.assertEqual(p.snapshot(999)['status'], 'time_check_needed')

    def test_filter_causality_and_no_mutation(self):
        records = [sample(i, 28 if i == 10 else 20) for i in range(35)]
        original = copy.deepcopy(records)
        p = QualityProcessor(); full = [p.process(r) for r in records]
        for length in range(1, 36):
            p = QualityProcessor()
            self.assertEqual([p.process(r) for r in records[:length]], full[:length])
        self.assertEqual(full[10]['filtered_temperature_c'], 20)
        self.assertEqual(records, original)

    def test_same_predictor_and_no_future_history(self):
        rows = [dict(uptime_ms=i*1000, x=20+i*.01) for i in range(30)]
        self.assertAlmostEqual(trend_forecast(rows, 'x', 29000), 20.59)
        self.assertEqual(trend_forecast(rows, 'x', 19000), trend_forecast(rows[:20], 'x', 19000))
        self.assertIsNone(trend_forecast(rows[:19], 'x', 18000))

    def test_restart_is_not_silently_accepted(self):
        p = QualityProcessor(); p.process(sample(100), 0)
        self.assertFalse(p.process(sample(0), 10000)['accepted'])
        self.assertEqual(p.snapshot(10000)['status'], 'stale')
        # A caller can deliberately create a new instance for a confirmed new epoch.
        self.assertTrue(QualityProcessor().process(sample(0), 10000)['accepted'])

    def test_nonfinite_and_boolean_rejected(self):
        for value in [float('inf'),float('nan'),True]:
            p=QualityProcessor(); self.assertFalse(p.process(sample(value=value))['accepted'])

if __name__ == '__main__':
    unittest.main()

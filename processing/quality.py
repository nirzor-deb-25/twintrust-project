"""Causal quality gates for the single-station TwinTrust prototype.

Arrival timestamps are reception times, not source measurement timestamps.
No labels, future references or MQTT duplicate bits are used as truth.
"""
import math
import statistics
from collections import OrderedDict, deque


def numeric(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def integer(value):
    return type(value) is int and 0 <= value <= 9007199254740991


def trend_forecast(history, field, now):
    points = [((row['uptime_ms'] - now) / 1000, row.get(field))
              for row in history
              if numeric(row.get('uptime_ms'))
              and 0 <= now - row['uptime_ms'] <= 30000
              and numeric(row.get(field))]
    if len(points) < 20 or points[-1][0] - points[0][0] < 19:
        return None
    mx = statistics.mean(x for x, _ in points)
    my = statistics.mean(y for _, y in points)
    denominator = sum((x - mx)**2 for x, _ in points)
    if denominator <= 0:
        return None
    slope = sum((x - mx)*(y - my) for x, y in points) / denominator
    prediction = my - slope * mx + slope * 30
    return prediction if math.isfinite(prediction) else None


class QualityProcessor:
    """One device and source-counter epoch per instance; reset explicitly after reboot.

    Backward counters are rejected conservatively. Without a boot ID they cannot
    reliably distinguish a restarted device from a delayed older message.
    """
    def __init__(self, device='discovery01', median_window=5, alpha=0.25):
        if type(median_window) is not int or median_window < 1 or median_window > 101:
            raise ValueError('median_window must be an integer from 1 to 101')
        if not numeric(alpha) or not 0 < alpha <= 1:
            raise ValueError('alpha must be in (0, 1]')
        self.device = device
        self.window = deque(maxlen=median_window)
        self.alpha = alpha
        self.ema = None
        self.previous = None
        self.last_accepted_arrival = None
        self.last_arrival = None
        self.latest = None
        self.latest_issue = None
        self.history = deque()
        self.seen = OrderedDict()
        self.missing = 0
        self.segment = 0

    def clear_signal(self):
        self.window.clear()
        self.history.clear()
        self.ema = None
        self.latest = None
        self.last_accepted_arrival = None
        self.segment += 1

    def process(self, record, arrival_ms=None, parse_error=None):
        row = {key: record.get(key) if isinstance(record, dict) else None
               for key in ('device', 'seq', 'uptime_ms')}
        raw = record.get('temperature_c') if isinstance(record, dict) else None
        row.update(raw_temperature_c=raw if numeric(raw) else None,
                   filtered_temperature_c=None, accepted=False,
                   filter_reset=False, quality_flags=[], missing_sequence_numbers=0,
                   segment=self.segment)

        def reject(flag, invalidate=False):
            row['quality_flags'].append(flag)
            self.latest_issue = flag
            if invalidate:
                self.clear_signal()
                row['filter_reset'] = True
            row['segment'] = self.segment
            return row

        if arrival_ms is not None:
            if not numeric(arrival_ms):
                return reject('invalid_reception_time', True)
            if self.last_arrival is not None and arrival_ms < self.last_arrival:
                return reject('reception_clock_backward', True)
            self.last_arrival = arrival_ms
        if parse_error or not isinstance(record, dict):
            return reject('parse_error', True)
        if (record.get('device') != self.device
                or not all(integer(record.get(k)) for k in ('seq', 'uptime_ms', 'error_count'))
                or type(record.get('valid')) is not bool):
            return reject('invalid_metadata', True)

        identity = (record['device'], record['seq'], record['uptime_ms'])
        signature = tuple((type(record.get(k)).__name__, repr(record.get(k)))
                          for k in ('valid', 'temperature_c', 'humidity_percent', 'pressure_hpa', 'error_count'))
        if identity in self.seen:
            return reject('duplicate' if self.seen[identity] == signature
                          else 'conflicting_duplicate', self.seen[identity] != signature)
        # Remember rejected readings too: retransmission cannot make them usable.
        self.seen[identity] = signature
        if len(self.seen) > 4096:
            self.seen.popitem(last=False)

        if self.previous is not None:
            if record['seq'] <= self.previous['seq'] or record['uptime_ms'] <= self.previous['uptime_ms']:
                return reject('backward_or_nonincreasing_counters')
            step = record['seq'] - self.previous['seq']
            source_gap = record['uptime_ms'] - self.previous['uptime_ms']
            reception_gap = (arrival_ms is not None and self.last_accepted_arrival is not None
                             and arrival_ms - self.last_accepted_arrival > 5000)
            if step > 1 or source_gap > 5000 or reception_gap:
                self.clear_signal()
                row['filter_reset'] = True
                if step > 1:
                    row['missing_sequence_numbers'] = step - 1
                    self.missing += step - 1
                    row['quality_flags'].append('sequence_gap')
                if source_gap > 5000:
                    row['quality_flags'].append('source_time_gap')
                if reception_gap:
                    row['quality_flags'].append('reception_gap')
        # Track in-order metadata even on invalid readings to avoid counting them as missing.
        self.previous = {k: record[k] for k in ('seq', 'uptime_ms')}
        if record['valid'] is not True:
            return reject('source_invalid', True)
        bounds = {'temperature_c': (-40, 85), 'humidity_percent': (0, 100), 'pressure_hpa': (300, 1100)}
        invalid_fields = [field for field, (low, high) in bounds.items()
                          if not numeric(record.get(field)) or not low <= record[field] <= high]
        if invalid_fields:
            row['invalid_fields'] = invalid_fields
            return reject('invalid_measurement', True)

        self.window.append(raw)
        median = statistics.median(self.window)
        self.ema = median if self.ema is None else self.alpha * median + (1 - self.alpha) * self.ema
        row.update(accepted=True, filtered_temperature_c=self.ema, segment=self.segment)
        self.latest = row.copy()
        self.latest_issue = None
        self.last_accepted_arrival = arrival_ms
        self.history.append(row.copy())
        while self.history and record['uptime_ms'] - self.history[0]['uptime_ms'] > 30000:
            self.history.popleft()
        return row

    def snapshot(self, now_ms, threshold=22.0):
        if not numeric(threshold):
            raise ValueError('threshold must be finite')
        age = ((now_ms - self.last_accepted_arrival) / 1000
               if numeric(now_ms) and self.last_accepted_arrival is not None else None)
        if not self.latest:
            status = 'unavailable'
        elif age is None or age < 0:
            status = 'time_check_needed'
        elif age > 5:
            status = 'stale'
        else:
            status = 'ready'
        usable = status == 'ready'
        def decision(field):
            if not usable:
                return 'withheld'
            return 'warning' if self.latest[field] >= threshold else 'below_threshold'
        forecast = {field: trend_forecast(self.history, field, self.latest['uptime_ms'])
                    if usable else None
                    for field in ('raw_temperature_c', 'filtered_temperature_c')}
        return {'status': status, 'age_seconds': age, 'latest_accepted': self.latest,
                'latest_issue': self.latest_issue, 'threshold_c': threshold,
                'raw_decision': decision('raw_temperature_c'),
                'filtered_decision': decision('filtered_temperature_c'),
                'forecast_30s_c': forecast,
                'forecast_status': 'ready' if usable and all(v is not None for v in forecast.values())
                                   else 'warming_up' if usable else 'withheld',
                'freshness_basis': 'last accepted backend reception, not sensor measurement age'}

"""Offline, causal temperature features. Quality decisions remain authoritative."""
import copy
import hashlib
import json
import statistics
from collections import deque
from pathlib import Path
from processing.quality import QualityProcessor

FEATURE_NAMES = ['absolute_step_c', 'absolute_previous20_median_residual_c',
                 'trailing20_std_c', 'trailing20_range_c']
WINDOW = 20


def load_session(path):
    path = Path(path)
    events = []
    for line_number, line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(), 1):
        if not line.strip():
            continue
        event = json.loads(line)
        if not isinstance(event, dict) or event.get('accepted') is not True:
            raise ValueError(f'{path.name}:{line_number}: not an accepted gateway sensor event')
        if not isinstance(event.get('record'), dict) or event.get('event_type') == 'command_ack':
            raise ValueError('Use a sensor-only recording, not a command event')
        tick = event.get('received_monotonic_ns')
        if type(tick) is not int or tick < 0:
            raise ValueError('Missing nonnegative gateway monotonic reception timestamp')
        if events and tick < events[-1]['received_monotonic_ns']:
            raise ValueError('Gateway monotonic reception clock moved backward')
        events.append(event)
    sessions = {e.get('session_id') for e in events}
    if len(sessions) != 1 or None in sessions:
        raise ValueError('Exactly one identified session is required')
    return events


def audit(events):
    rows = feature_rows(events)
    records = [e['record'] for e in events]
    result = dict(rows=len(rows), accepted=sum(r['accepted'] for r in rows),
                  flagged=sum(bool(r['quality_flags']) for r in rows),
                  session_id=events[0]['session_id'],
                  seq_first=records[0]['seq'], seq_last=records[-1]['seq'])
    result['ranges'] = {k: [min(r[k] for r in records), max(r[k] for r in records)]
                        for k in ('temperature_c', 'humidity_percent', 'pressure_hpa')}
    result['unique_temperature_values'] = len({r['temperature_c'] for r in records})
    result['error_counter_increase'] = records[-1]['error_count'] - records[0]['error_count']
    return result


def feature_rows(events):
    processor = QualityProcessor()
    history = deque(maxlen=WINDOW)
    rows = []
    origin = events[0]['received_monotonic_ns'] if events else 0
    for index, event in enumerate(events):
        record = event['record']
        arrival = (event['received_monotonic_ns'] - origin) / 1e6
        row = processor.process(record, arrival_ms=arrival)
        if row['filter_reset']:
            history.clear()
        features = None
        if row['accepted']:
            value = row['raw_temperature_c']
            if len(history) == WINDOW:
                trailing = list(history)[1:] + [value]
                features = [abs(value-history[-1]),
                            abs(value-statistics.median(history)),
                            statistics.pstdev(trailing), max(trailing)-min(trailing)]
            history.append(value)
        row.update(index=index, features=features,
                   # Additional comparator, not an existing production quality rule.
                   simple_rule=(features[1] > 0.5 or features[2] > 0.2 or features[3] <= 0.005)
                   if features is not None else None)
        rows.append(row)
    return rows


def scenarios(events):
    """Fixed simulations on evaluation session only. Labels are stored separately."""
    n = len(events)
    if n < 300:
        raise ValueError('At least 300 evaluation readings are needed')
    specs = [
        ('recorded_control', 'Unmodified separate recording; physical accuracy unverified.'),
        ('noise', 'Temperature Gaussian noise sigma 0.30 C; seed 20261110.'),
        ('spikes', 'Temperature +8 C at zero-based indices 60, 150, 240.'),
        ('stuck', 'Temperature held at index99 value for indices100 through159.'),
        ('bias_drift', 'Temperature bias ramps from index60 to +2 C by index180, then holds.'),
        ('sustained_rise', 'Intended +4 C rise from index60 through120, then holds; not labelled a fault.'),
        ('dropout', 'Remove indices100 through109; no reconstruction.'),
        ('unit_mismatch', 'Pressure multiplied by100 for indices100 through129.'),
    ]
    import random
    for name, description in specs:
        variant = copy.deepcopy(events)
        modified = [False] * n
        rng = random.Random(20261110)
        for i, event in enumerate(variant):
            record = event['record']
            if name == 'noise':
                record['temperature_c'] += rng.gauss(0, 0.3); modified[i] = True
            elif name == 'spikes' and i in (60, 150, 240):
                record['temperature_c'] += 8; modified[i] = True
            elif name == 'stuck' and 100 <= i < 160:
                record['temperature_c'] = events[99]['record']['temperature_c']; modified[i] = True
            elif name in ('bias_drift', 'sustained_rise') and i > 60:
                amplitude, duration = (2, 120) if name == 'bias_drift' else (4, 60)
                record['temperature_c'] += amplitude * min((i-60)/duration, 1)
                modified[i] = True
            elif name == 'unit_mismatch' and 100 <= i < 130:
                record['pressure_hpa'] *= 100; modified[i] = True
        if name == 'dropout':
            variant = [event for i, event in enumerate(variant) if not 100 <= i < 110]
            modified = [False] * len(variant)
        yield name, description, variant, modified


def identity(path, events):
    return dict(name=Path(path).name, sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                session_id=events[0]['session_id'])

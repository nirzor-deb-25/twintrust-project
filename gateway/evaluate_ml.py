"""Reproducible offline ML-versus-rules experiment. Never publishes or controls devices."""
import argparse
import csv
import hashlib
import html
import json
import platform
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ml_advisory import FEATURE_NAMES, audit, feature_rows, identity, load_session, scenarios
import processing.quality as quality_module
import numpy as np
import sklearn
from sklearn.ensemble import IsolationForest

SEED = 20261110


def fit_model(events):
    # Chronological train-session partition: fit [0:180], guard [180:200],
    # calibration [200:]. Fresh feature histories prevent shared input windows.
    fit = [r['features'] for r in feature_rows(events[:180]) if r['features'] is not None]
    calibration = [r['features'] for r in feature_rows(events[200:]) if r['features'] is not None]
    if len(fit) < 100 or len(calibration) < 50:
        raise ValueError('Insufficient fit/calibration features; provide a clean >=300-reading training session')
    model = IsolationForest(n_estimators=200, max_samples='auto', contamination='auto',
                            random_state=SEED, n_jobs=1)
    model.fit(fit)
    # Lower score_samples means more abnormal. Equality is not flagged.
    threshold = float(np.quantile(model.score_samples(calibration), 0.02, method='linear'))
    return model, threshold, dict(fit_features=len(fit), calibration_features=len(calibration),
                                 threshold=threshold, quantile=0.02, comparison='score < threshold',
                                 fit_indices='0:180', unused_guard_indices='180:200',
                                 calibration_indices='200:end', feature_history_reset_at_calibration=True)


def summarize(rows):
    usable = [r for r in rows if r['ml_flag'] is not None]
    changed = [r for r in usable if r['modified_by_simulation']]
    unchanged = [r for r in usable if not r['modified_by_simulation']]
    return dict(rows=len(rows), quality_flagged=sum(bool(r['quality_flags']) for r in rows),
                quality_excluded=sum(not r['accepted'] for r in rows),
                scorable=len(usable), ml_flags=sum(r['ml_flag'] for r in usable),
                simple_flags=sum(r['simple_rule'] for r in usable),
                modified_scorable=len(changed), modified_ml_flags=sum(r['ml_flag'] for r in changed),
                modified_simple_flags=sum(r['simple_rule'] for r in changed),
                unmodified_scorable=len(unchanged), unmodified_ml_flags=sum(r['ml_flag'] for r in unchanged),
                unmodified_simple_flags=sum(r['simple_rule'] for r in unchanged))


def write_html(report, folder):
    table = ''.join('<tr>' + ''.join(f'<td>{html.escape(str(x))}</td>' for x in (
        name, row['rows'], row['quality_excluded'], row['quality_flagged'], row['scorable'],
        row['ml_flags'], row['simple_flags'], f"{row['modified_ml_flags']}/{row['modified_scorable']}")) + '</tr>'
        for name, row in report['scenarios'].items())
    notes = ''.join('<li>'+html.escape(x)+'</li>' for x in report['limitations'])
    body = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>TwinTrust advisory ML comparison</title><style>body{{font:16px system-ui;margin:32px auto;padding:0 20px;max-width:1100px;background:#111827;color:#e5e7eb}}table{{border-collapse:collapse;width:100%}}td,th{{padding:10px;border:1px solid #4b5563;text-align:left}}.scroll{{overflow:auto}}p,li{{line-height:1.6}}code{{overflow-wrap:anywhere}}</style>
<h1>TwinTrust: advisory ML comparison</h1><p>Offline experiment. No device control or change to live quality decisions.</p>
<p>Training: <code>{html.escape(report['training']['name'])}</code><br>Evaluation: <code>{html.escape(report['evaluation']['name'])}</code></p>
<p>Model fits {report['calibration']['fit_features']} feature rows. Threshold uses {report['calibration']['calibration_features']} later training-session rows, separated by a guard and fresh history. The evaluation recording never sets the threshold.</p>
<div class="scroll"><table><tr><th>Scenario</th><th>Rows</th><th>Quality excluded</th><th>Quality flagged</th><th>Common ML/rule coverage</th><th>ML flags</th><th>Simple rule flags</th><th>ML flags / modified scorable</th></tr>{table}</table></div>
<p>Counts are per reading, not independent incidents. Quality gates check schema, validity, order, gaps and broad ranges. ML and the additional simple rule inspect temperature behaviour on identical scorable rows. They address different problems. A rise can be a genuine change.</p>
<h2>Method and limits</h2><ul>{notes}</ul><p>Details: report.json; per-reading features, labels, scores and flags: per_reading.csv. See scenario JSONL files for exact simulation inputs.</p></html>'''
    (folder/'report.html').write_text(body, encoding='utf-8')


def run(train_path, eval_path, output):
    train, evaluation = load_session(train_path), load_session(eval_path)
    train_id, eval_id = identity(train_path, train), identity(eval_path, evaluation)
    if train_id['sha256'] == eval_id['sha256'] or train_id['session_id'] == eval_id['session_id']:
        raise ValueError('Training and evaluation must be distinct sessions')
    for name, events in [('training', train), ('evaluation', evaluation)]:
        info = audit(events)
        if info['rows'] < 300 or info['accepted'] != info['rows'] or info['flagged']:
            raise ValueError(f'{name} must contain >=300 clean in-order readings without quality flags')
        if info['unique_temperature_values'] < 2:
            raise ValueError(f'{name} temperature is constant; do not assume it is healthy training data')
    model, threshold, calibration = fit_model(train)
    folder = Path(output)/('ml_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'_'+uuid.uuid4().hex[:8])
    folder.mkdir(parents=True, exist_ok=False)
    report = dict(processing_version='ml-advisory-v1', training=train_id, evaluation=eval_id,
                  training_audit=audit(train), evaluation_audit=audit(evaluation),
                  calibration=calibration, features=FEATURE_NAMES,
                  model=dict(name='IsolationForest', n_estimators=200, max_samples='auto',
                             contamination='auto', random_state=SEED, n_jobs=1),
                  versions=dict(python=platform.python_version(), numpy=np.__version__, sklearn=sklearn.__version__),
                  source_sha256={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (
                      Path(__file__), Path(__file__).with_name('ml_advisory.py'), Path(quality_module.__file__))},
                  scenarios={}, scenario_descriptions={}, scenario_sha256={}, limitations=[
                      'Exploratory demonstration: one five-minute training recording and one five-minute evaluation recording.',
                      'These recordings have already been inspected during project development; this is not a pristine blind final test.',
                      'Fixed seed, features, split, model settings and 2nd-percentile threshold; no search over evaluation outcomes.',
                      'Features use only current/past accepted temperature readings. No scenario, label, session ID, absolute time or future value is a feature.',
                      'Features: absolute step, residual from previous20 median, trailing20 standard deviation and range. Absolute temperature level is deliberately omitted.',
                      'The additional simple comparator flags median residual >0.5 C, standard deviation >0.2 C, or range <=0.005 C. These fixed demonstration rules are not installed in live processing.',
                      'Existing shared quality gates run first; both behavioural detectors are unavailable on excluded readings and during 20-reading warmup/reset.',
                      'A new chronological calibration block starts with fresh history; its first20 readings are warmup. Calibration windows and evaluation windows overlap within their own blocks.',
                      '2% calibration quantile is a threshold design choice, not a guaranteed future false-alarm rate. Tied scores may yield fewer calibration flags.',
                      'Modified labels describe synthetic interventions, not confirmed physical sensor faults. Sustained rise is an intended-change challenge.',
                      'Counts during modified intervals do not measure incident recall; a behavioural effect may persist after an injected spike ends.',
                      'Dropout has no point labels for absent readings. Quality flags on the next received event identify the gap; ML never reconstructs it.',
                      'Temperature-only ML may miss pressure problems, drift or stuck values. A normal-looking result does not establish physical accuracy.',
                      'The recorded control is assumed quiet, not independently calibrated. Flags can reflect legitimate environmental changes.',
                      'No live integration, automatic action, saved executable model, MQTT publication, or original data changes.',
                      'Library versions are recorded; results can differ between scikit-learn versions.',
                  ])
    all_rows=[]
    for name, description, events, modified in scenarios(evaluation):
        rows=feature_rows(events)
        eligible=[r for r in rows if r['features'] is not None]
        scores=iter(model.score_samples([r['features'] for r in eligible]))
        for row, label in zip(rows, modified):
            score=float(next(scores)) if row['features'] is not None else None
            row.update(scenario=name, modified_by_simulation=label, ml_score=score,
                       ml_flag=(score < threshold) if score is not None else None)
        report['scenarios'][name]=summarize(rows)
        report['scenario_descriptions'][name]=description
        scenario_file=folder/(name+'.events.jsonl')
        scenario_file.write_text(''.join(json.dumps(e,allow_nan=False)+'\n' for e in events),encoding='utf-8')
        report['scenario_sha256'][name]=hashlib.sha256(scenario_file.read_bytes()).hexdigest()
        all_rows.extend(rows)
    (folder/'report.json').write_text(json.dumps(report,indent=2,allow_nan=False),encoding='utf-8')
    fields=['scenario','index','seq','uptime_ms','raw_temperature_c','accepted','quality_flags','features',
            'modified_by_simulation','ml_score','ml_flag','simple_rule']
    with (folder/'per_reading.csv').open('w',newline='',encoding='utf-8') as file:
        writer=csv.DictWriter(file,fieldnames=fields,extrasaction='ignore');writer.writeheader()
        for row in all_rows:
            row=dict(row,quality_flags=json.dumps(row['quality_flags']),features=json.dumps(row['features']))
            writer.writerow(row)
    write_html(report,folder)
    print('Scenario           Rows  Quality flags  Scorable  ML flags  Simple flags')
    for name,r in report['scenarios'].items():
        print(f"{name:18} {r['rows']:4} {r['quality_flagged']:14} {r['scorable']:9} {r['ml_flags']:9} {r['simple_flags']:13}")
    print('Saved:',folder)
    print('Advisory only. Synthetic modifications are not physical fault diagnoses.')
    return report,folder


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--train',type=Path,default=ROOT/'data/original/gateway_20261107T222348Z_663fc098.events.jsonl')
    parser.add_argument('--evaluate',type=Path,default=ROOT/'data/original/gateway_20261109T123542Z_02a3a5d2.events.jsonl')
    parser.add_argument('--output',type=Path,default=ROOT/'data/results')
    args=parser.parse_args()
    run(args.train,args.evaluate,args.output)

"""Fixed-filter and forecast comparison on a separate recorded session."""
import argparse
import hashlib
import html
import json
import statistics
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from processing.quality import QualityProcessor, trend_forecast
import processing.quality as quality_module
from ml_advisory import load_session, audit, scenarios
from evaluate_forecasts import reference_at
import ml_advisory
import evaluate_forecasts

SETTINGS=[(5,0.25),(9,0.10)]


def predict_rows(events,window,alpha):
    processor=QualityProcessor(median_window=window,alpha=alpha)
    origin=events[0]['received_monotonic_ns']
    rows=[]
    for event in events:
        row=processor.process(event['record'],arrival_ms=(event['received_monotonic_ns']-origin)/1e6)
        row['raw_trend_c']=None;row['filtered_trend_c']=None
        if row['accepted']:
            row['raw_trend_c']=trend_forecast(processor.history,'raw_temperature_c',row['uptime_ms'])
            row['filtered_trend_c']=trend_forecast(processor.history,'filtered_temperature_c',row['uptime_ms'])
        rows.append(row)
    return rows,processor.missing


def score(rows,reference):
    lookup={(r['device'],r['seq'],r['uptime_ms']):r['raw_temperature_c'] for r in reference}
    errors={'raw':[],'filtered':[]}
    future_errors={'raw_trend':[],'filtered_trend':[],'hold_raw':[]}
    eligible=0;predictions=[]
    for row in rows:
        key=(row['device'],row['seq'],row['uptime_ms'])
        actual=lookup.get(key)
        if row['accepted'] and actual is not None:
            errors['raw'].append(abs(row['raw_temperature_c']-actual))
            errors['filtered'].append(abs(row['filtered_temperature_c']-actual))
        now=row.get('uptime_ms')
        if type(now) is not int or now+30000>reference[-1]['uptime_ms']:
            continue
        eligible+=1
        # Predictions above were already made from prefix history only.
        target=reference_at(reference,now+30000)
        if not row['accepted'] or target is None or row['raw_trend_c'] is None or row['filtered_trend_c'] is None:
            continue
        predictions.append(dict(origin_seq=row['seq'],origin_uptime_ms=now,target_uptime_ms=now+30000,
                                raw_trend_c=row['raw_trend_c'],filtered_trend_c=row['filtered_trend_c'],
                                hold_raw_c=row['raw_temperature_c'],reference_target_c=target))
        for name,value in [('raw_trend',row['raw_trend_c']),('filtered_trend',row['filtered_trend_c']),('hold_raw',row['raw_temperature_c'])]:
            future_errors[name].append(abs(value-target))
    mean=lambda values:statistics.mean(values) if values else None
    result=dict(rows=len(rows),compared_readings=len(errors['raw']),
                filter_mae_c={k:mean(v) for k,v in errors.items()},eligible_origins=eligible,
                scored_origins=len(predictions),unavailable_origins=eligible-len(predictions),
                coverage_fraction=len(predictions)/eligible if eligible else None,
                forecast_mae_c={k:mean(v) for k,v in future_errors.items()})
    return result,predictions


def crossing_delay(rows):
    def first(field):
        return next((r['uptime_ms'] for r in rows if r['accepted'] and r[field]>=22),None)
    raw=first('raw_temperature_c');filtered=first('filtered_temperature_c')
    return (filtered-raw)/1000 if raw is not None and filtered is not None else None


def run(source,output):
    events=load_session(source);info=audit(events)
    if info['rows']<300 or info['accepted']!=info['rows'] or info['flagged']:
        raise ValueError('Use a >=300-reading recording without baseline quality flags')
    if info['unique_temperature_values']<2:raise ValueError('Constant recording; investigate before validation')
    folder=Path(output)/('session_validation_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'_'+uuid.uuid4().hex[:8])
    folder.mkdir(parents=True,exist_ok=False)
    baseline=[dict(e['record'],raw_temperature_c=e['record']['temperature_c']) for e in events]
    report=dict(source_name=Path(source).name,source_sha256=hashlib.sha256(Path(source).read_bytes()).hexdigest(),
                audit=info,settings=[dict(median_window=w,ema_alpha=a) for w,a in SETTINGS],
                forecast=dict(horizon_seconds=30,history_seconds=30,minimum_readings=20,minimum_span_seconds=19),
                scenario_seed=20261110,results=[],source_code_sha256={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in
                [Path(__file__),Path(quality_module.__file__),Path(ml_advisory.__file__),Path(evaluate_forecasts.__file__)]},
                limitations=[
                    'This is an exploratory separate-session check, not a blind test or calibration against physical truth.',
                    'Both fixed settings were selected before this run; no parameter search or fitting on this session.',
                    'Raw baseline and raw intended-rise MAE are zero by construction, not proof of measurement accuracy.',
                    'Noise and spikes are scored against the separate recorded control; intended rise against its synthetic reference.',
                    'Simulations reuse the ML experiment seed/definitions, not the original Nov7 fault manifest; do not conflate their scores.',
                    'Forecast algorithms are identical; all three methods use matching origins within each scenario/setting.',
                    'Future reference interpolation is for scoring only, with adjacent reference samples no more than2s apart.',
                    'Unavailable forecasts are excluded from MAE and counted; overlapping windows are not independent trials.',
                    'Gap handling resets history. Missing measurements are not reconstructed.',
                    'Gateway reception timestamps are not sensor measurement age or end-to-end network delay.',
                    '22C is a demonstration threshold, not a safety limit. ML and live dashboard processing are unchanged.',
                ])
    for name,description,variant,_ in scenarios(events):
        if name not in ('recorded_control','noise','spikes','dropout','sustained_rise'):continue
        reference=[dict(e['record'],raw_temperature_c=e['record']['temperature_c']) for e in variant] if name=='sustained_rise' else baseline
        with (folder/(name+'.input.events.jsonl')).open('x',encoding='utf-8') as f:
            for e in variant:f.write(json.dumps(e,allow_nan=False)+'\n')
        for window,alpha in SETTINGS:
            rows,missing=predict_rows(variant,window,alpha)
            result,predictions=score(rows,reference)
            result.update(scenario=name,median_window=window,ema_alpha=alpha,missing_sequence_numbers=missing,
                          description=description,additional_22c_crossing_delay_seconds=crossing_delay(rows) if name=='sustained_rise' else None)
            report['results'].append(result)
            stem=f'{name}_median{window}_alpha{alpha:.2f}'
            for suffix,data in [('processed',rows),('predictions',predictions)]:
                with (folder/(stem+'.'+suffix+'.jsonl')).open('x',encoding='utf-8') as f:
                    for row in data:f.write(json.dumps(row,allow_nan=False)+'\n')
    (folder/'report.json').write_text(json.dumps(report,indent=2,allow_nan=False),encoding='utf-8')
    def fmt(x):return f'{x:.5f}' if x is not None else 'n/a'
    body=[]
    print('Scenario          Median Alpha Filter MAE  Scored/eligible Raw trend  Filt trend Hold raw')
    for r in report['results']:
        m=r['forecast_mae_c']
        print(f"{r['scenario']:17} {r['median_window']:6} {r['ema_alpha']:.2f} {fmt(r['filter_mae_c']['filtered']):>10} {r['scored_origins']:4}/{r['eligible_origins']:<4}      {fmt(m['raw_trend']):>9} {fmt(m['filtered_trend']):>10} {fmt(m['hold_raw']):>8}")
        values=[r['scenario'],r['median_window'],r['ema_alpha'],fmt(r['filter_mae_c']['filtered']),f"{r['scored_origins']}/{r['eligible_origins']}",fmt(m['raw_trend']),fmt(m['filtered_trend']),fmt(m['hold_raw'])]
        body.append('<tr>'+''.join('<td>'+html.escape(str(v))+'</td>' for v in values)+'</tr>')
    notes=''.join('<li>'+html.escape(s)+'</li>' for s in report['limitations'])
    page='''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>TwinTrust separate-session validation</title><style>body{font:16px system-ui;max-width:1100px;margin:30px auto;padding:0 20px;color:#172b40}td,th{padding:10px;border:1px solid #bbc9d3;text-align:left}table{border-collapse:collapse}li,p{line-height:1.6}.scroll{overflow:auto}</style><h1>Separate-session filter and forecast check</h1><p>All MAE values are in degrees C. Lower is better. No parameter tuning on this session.</p>'''
    page+='<p>Input: '+html.escape(Path(source).name)+'</p><div class="scroll"><table><tr>'+''.join('<th>'+s+'</th>' for s in ['Scenario','Median','Alpha','Filtered MAE','Scored/eligible','Raw trend','Filtered trend','Hold raw'])+'</tr>'+''.join(body)+'</table></div><h2>Limitations</h2><ul>'+notes+'</ul></html>'
    (folder/'report.html').write_text(page,encoding='utf-8')
    print('Saved:',folder);print('Original data unchanged. Nothing published to MQTT.')
    return report,folder


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=ROOT/'data/original/gateway_20261109T123542Z_02a3a5d2.events.jsonl')
    parser.add_argument('--output',type=Path,default=ROOT/'data/results')
    args=parser.parse_args();run(args.source,args.output)

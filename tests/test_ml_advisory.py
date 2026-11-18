import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'gateway'))
from ml_advisory import feature_rows, scenarios, load_session
from evaluate_ml import fit_model, run


def events(n=300):
    return [dict(session_id='test',accepted=True,received_monotonic_ns=i*1001000000,
                 record=dict(device='discovery01',seq=i,uptime_ms=i*1001,valid=True,
                             temperature_c=20+(i%7)*0.01,humidity_percent=50,pressure_hpa=1010,error_count=0))
            for i in range(n)]


class MLAdvisoryTests(unittest.TestCase):
    def test_features_are_causal_and_do_not_mutate(self):
        data=events(); original=copy.deepcopy(data)
        a=feature_rows(data[:70]); b=feature_rows(data)
        self.assertEqual(a,b[:70]); self.assertEqual(data,original)
        changed=copy.deepcopy(data);changed[100]['record']['temperature_c']=50
        self.assertEqual(feature_rows(changed)[:100],b[:100])
    def test_levels_and_labels_are_not_features(self):
        data=events(); shifted=copy.deepcopy(data)
        for row in shifted:
            row['record']['temperature_c']+=3
            row['scenario']='spikes';row['label']=True
        a,b=feature_rows(data),feature_rows(shifted)
        for x,y in zip(a[20:],b[20:]):
            for u,v in zip(x['features'],y['features']): self.assertAlmostEqual(u,v,places=10)
    def test_invalid_resets_warmup(self):
        data=events();data[50]['record']['valid']=False
        rows=feature_rows(data)
        self.assertIsNone(rows[50]['features']);self.assertIsNone(rows[70]['features'])
        self.assertIsNotNone(rows[71]['features'])
    def test_duplicate_does_not_change_history(self):
        data=events(70); duplicate=copy.deepcopy(data[40]);duplicate['received_monotonic_ns']+=100
        data.insert(41,duplicate)
        rows=feature_rows(data)
        self.assertIsNone(rows[41]['features']);self.assertIn('duplicate',rows[41]['quality_flags'])
        self.assertEqual(rows[42]['features'],feature_rows(events(70))[41]['features'])
    def test_calibration_does_not_fit_model(self):
        data=events();model,_,info=fit_model(data)
        changed=copy.deepcopy(data)
        for e in changed[200:]:e['record']['temperature_c']+= (e['record']['seq']%2)*2
        other,_,_=fit_model(changed)
        self.assertEqual(model.score_samples([[0.01,0.01,0.01,0.02]]).tolist(),
                         other.score_samples([[0.01,0.01,0.01,0.02]]).tolist())
        self.assertEqual(info['fit_features'],160);self.assertEqual(info['calibration_features'],80)
    def test_simulations_separate_and_original_unchanged(self):
        data=events();old=copy.deepcopy(data)
        result={name:(variant,labels) for name,_,variant,labels in scenarios(data)}
        self.assertEqual(data,old)
        self.assertEqual(sum(result['spikes'][1]),3)
        self.assertEqual(len(result['dropout'][0]),290)
        self.assertEqual(sum(result['stuck'][1]),60)
    def test_same_session_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'events.jsonl'
            path.write_text(''.join(json.dumps(e)+'\n' for e in events()))
            with self.assertRaisesRegex(ValueError,'distinct sessions'):run(path,path,Path(td)/'out')
    def test_loader_rejects_backward_reception_clock(self):
        data=events(3);data[2]['received_monotonic_ns']=0
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'events.jsonl';path.write_text(''.join(json.dumps(e)+'\n' for e in data))
            with self.assertRaisesRegex(ValueError,'backward'):load_session(path)

if __name__=='__main__':unittest.main()

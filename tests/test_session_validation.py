import copy
from pathlib import Path
import sys
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'gateway'))
from evaluate_session import predict_rows,score


def events(n=90):
    return [dict(received_monotonic_ns=i*1001000000,record=dict(device='discovery01',seq=i,
                 uptime_ms=i*1001,valid=True,temperature_c=20,humidity_percent=50,pressure_hpa=1010,error_count=0)) for i in range(n)]


class SessionValidationTests(unittest.TestCase):
    def test_constant_reference_common_origins(self):
        data=events();rows,_=predict_rows(data,5,.25)
        reference=[dict(e['record'],raw_temperature_c=20) for e in data]
        result,predictions=score(rows,reference)
        self.assertEqual(result['eligible_origins'],60)
        self.assertEqual(result['scored_origins'],41)
        self.assertEqual(result['unavailable_origins'],19)
        self.assertTrue(all(v==0 for v in result['forecast_mae_c'].values()))
        self.assertTrue(all(p['raw_trend_c']==p['filtered_trend_c']==p['hold_raw_c']==20 for p in predictions))
    def test_predictions_causal_and_no_input_mutation(self):
        data=events();old=copy.deepcopy(data)
        prefix,_=predict_rows(data[:40],9,.1);full,_=predict_rows(data,9,.1)
        self.assertEqual(prefix,full[:40]);self.assertEqual(old,data)
        data[60]['record']['temperature_c']=28
        changed,_=predict_rows(data,9,.1)
        self.assertEqual(changed[:60],full[:60])
    def test_dropout_resets_both_forecasts(self):
        data=events();data=data[:30]+data[40:]
        rows,missing=predict_rows(data,5,.25)
        self.assertEqual(missing,10)
        self.assertTrue(rows[30]['filter_reset'])
        self.assertIsNone(rows[30]['raw_trend_c']);self.assertIsNone(rows[30]['filtered_trend_c'])
        self.assertIsNone(rows[48]['raw_trend_c']);self.assertIsNotNone(rows[49]['raw_trend_c'])

if __name__=='__main__':unittest.main()

"""Exercise both saved evaluation scripts using synthetic fixture files only."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from test_quality import sample


class EvaluationTests(unittest.TestCase):
    def test_filter_and_forecast_end_to_end(self):
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(source / 'processing', root / 'processing')
            shutil.copytree(source / 'gateway', root / 'gateway')
            base = root / 'data/scenarios/baseline_20261107T224640Z_fea32582'
            faults = base / 'faults_20261107T224747Z_b17ff2fa'
            faults.mkdir(parents=True)
            rows = [sample(i, 20+i*.0002) for i in range(300)]
            def write(path, items):
                raw = ''.join(json.dumps(r)+'\n' for r in items).encode()
                path.write_bytes(raw)
                return hashlib.sha256(raw).hexdigest()
            baseline_hash = write(base / 'baseline.jsonl', rows)
            scenarios = []
            for name in ('noise', 'spikes', 'dropout'):
                altered = [dict(r) for r in rows if not (name == 'dropout' and 100 <= r['seq'] < 110)]
                if name == 'spikes': altered[60]['temperature_c'] += 8
                digest = write(faults / (name+'.jsonl'), altered)
                scenarios.append(dict(name=name, data_file=name+'.jsonl', data_sha256=digest))
            (faults / 'manifest.json').write_text(json.dumps(dict(baseline_sha256=baseline_hash, scenarios=scenarios)))
            for script in ('evaluate_filters.py','evaluate_forecasts.py'):
                run = subprocess.run([sys.executable, str(root/'gateway'/script)], capture_output=True, text=True)
                self.assertEqual(run.returncode, 0, run.stdout+run.stderr)
            evaluation = next((root/'data/results').glob('evaluation_*'))
            report = json.loads((evaluation/'report.json').read_text())
            self.assertEqual(report['processing_version'], 'quality-v1')
            self.assertEqual(next(r for r in report['results'] if r['dataset']=='dropout')['missing_sequence_numbers'], 10)
            forecast = next((root/'data/results').glob('forecast_*'))
            results = json.loads((forecast/'report.json').read_text())
            self.assertTrue(all(r['scored_forecasts'] > 0 for r in results['results']))
            self.assertTrue(all(0 <= r['coverage_fraction'] <= 1 for r in results['results']))

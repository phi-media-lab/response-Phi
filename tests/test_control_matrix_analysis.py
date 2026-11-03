"""Matrix pairing, warmup exclusion and recorded artifact negative tests."""
import copy
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from analyze_control_matrix import audit, check_schedule, expected_cells, summarize_trials

MATRIX=Path(os.environ.get('CONTROL_MATRIX_FIXTURE',ROOT/'results/batch4/control-matrix'))
BUNDLE=Path(os.environ['CONTROL_MATRIX_BUNDLE']) if os.environ.get('CONTROL_MATRIX_BUNDLE') else None


class MatrixAnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report=audit(MATRIX,BUNDLE)
        cls.schedule=json.loads((MATRIX/'schedule.json').read_text())

    def test_real_matrix_complete(self):
        self.assertEqual((self.report['cells'],self.report['measured_trials'],self.report['warmup_trials']),(64,64,64))
        self.assertEqual(set(self.report['main']),{'16','64'})

    def test_warmups_are_separate(self):
        self.assertTrue(all(t['trial_id']==1 for t in self.report['measured']))
        self.assertTrue(all(t['trial_id']==0 for t in self.report['warmups']))

    def test_schedule_mode_swap_rejected(self):
        schedule=copy.deepcopy(self.schedule)
        schedule['cells'][0],schedule['cells'][1]=schedule['cells'][1],schedule['cells'][0]
        with self.assertRaises(ValueError):check_schedule(schedule)

    def test_schedule_seed_change_rejected(self):
        schedule=copy.deepcopy(self.schedule);schedule['shuffle_seed']=1
        with self.assertRaises(ValueError):check_schedule(schedule)

    def test_schedule_metadata_wrong_count(self):
        schedule=copy.deepcopy(self.schedule);schedule['warmups_total']=0
        with self.assertRaises(ValueError):check_schedule(schedule)

    def test_each_mode_each_position_twice(self):
        cells=expected_cells()
        for rounds in (16,64):
            for symbol in 'HGPF':
                for position in range(4):
                    self.assertEqual(sum(c['T']==rounds and c['mode_symbol']==symbol and c['mode_position']==position for c in cells),2)

    def test_duplicate_pair_member_rejected(self):
        rows=copy.deepcopy(self.report['measured']);rows[1]=copy.deepcopy(rows[0])
        with self.assertRaises(ValueError):summarize_trials(rows)

    def test_missing_measured_row_rejected(self):
        with self.assertRaises(ValueError):summarize_trials(self.report['measured'][:-1])

    def test_pair_difference_not_difference_of_medians(self):
        rows=copy.deepcopy(self.report['measured'])
        # Three exceptional pairs make these two statistics intentionally differ.
        g=[1,10,100,1,10,100,1,10];p=[2,110,101,2,110,101,2,110]
        for row in rows:
            block=int(row['block_id'].split('-')[1])
            if row['mode_symbol'] in ('G','P'):
                row['metrics']['work_ms']=(g if row['mode_symbol']=='G' else p)[block]
        out=summarize_trials(rows)['16']
        self.assertEqual(out['P_minus_G_ms']['median'],1)
        self.assertNotEqual(out['P_minus_G_ms']['median'],out['modes']['P']['work_ms']['median']-out['modes']['G']['work_ms']['median'])

    def test_ratio_is_per_pair(self):
        group=self.report['main']['16']
        for pair in group['G_P_pairs']:
            self.assertEqual(pair['P_over_G'],pair['P_work_ms']/pair['G_work_ms'])

    def test_no_speed_requirement(self):
        rows=copy.deepcopy(self.report['measured'])
        for row in rows:
            row['metrics']['work_ms']=10 if row['mode_symbol']=='P' else 1
        out=summarize_trials(rows)
        self.assertTrue(all(g['P_faster_pairs']==0 for g in out.values()))

    def copied_matrix(self):
        temporary=tempfile.TemporaryDirectory()
        target=Path(temporary.name)/'matrix'
        shutil.copytree(MATRIX,target)
        self.addCleanup(temporary.cleanup)
        return target

    def test_progress_result_disagreement(self):
        target=self.copied_matrix();path=target/'progress.jsonl'
        rows=path.read_text().splitlines();row=json.loads(rows[0]);row['main_trial_id']=0
        rows[0]=json.dumps(row);path.write_text('\n'.join(rows)+'\n')
        with self.assertRaisesRegex(ValueError,'progress/result'):audit(target,BUNDLE)

    def test_unrecorded_cell_rejected(self):
        target=self.copied_matrix();(target/'extra-cell').mkdir()
        with self.assertRaisesRegex(ValueError,'extra cell'):audit(target,BUNDLE)

    def test_failure_cannot_be_hidden_by_top_pass(self):
        target=self.copied_matrix();path=target/'matrix-result.json';d=json.loads(path.read_text())
        d['completed_cells']=63;path.write_text(json.dumps(d))
        with self.assertRaisesRegex(ValueError,'incomplete'):audit(target,BUNDLE)

    def test_warmup_and_main_labels_cannot_swap(self):
        target=self.copied_matrix();path=target/expected_cells()[0]['cell_id']/'stdout.json';d=json.loads(path.read_text())
        d['warmups'],d['trials']=d['trials'],d['warmups'];path.write_text(json.dumps(d))
        with self.assertRaises(ValueError):audit(target,BUNDLE)

    def test_recorded_cli_mode_tamper(self):
        target=self.copied_matrix();path=target/expected_cells()[0]['cell_id']/'run.json';d=json.loads(path.read_text())
        index=d['command'].index('--rounds')+1;d['command'][index]='64' if d['command'][index]=='16' else '16'
        path.write_text(json.dumps(d))
        with self.assertRaisesRegex(ValueError,'executed command'):audit(target,BUNDLE)


if __name__=='__main__':unittest.main()

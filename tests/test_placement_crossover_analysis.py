"""Mutations of saved real traces test checker rejection, not performance."""
import copy
import json
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))
import pair_placement_crossover as p


class CrossoverAnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw=json.loads((ROOT/"results/batch3/placement-crossover-smoke/stdout.json").read_text())

    def test_real_smoke_validates(self):
        result=p.pair(copy.deepcopy(self.raw));self.assertTrue(result["passed"]);self.assertEqual(result["total_trials"],12)

    def test_swapped_executed_order_rejected(self):
        d=copy.deepcopy(self.raw);d["trials"][0],d["trials"][1]=d["trials"][1],d["trials"][0]
        with self.assertRaises(ValueError):p.pair(d)

    def test_actual_order_forgery_rejected(self):
        d=copy.deepcopy(self.raw);d["actual_order"][-1]["position"]=0
        with self.assertRaises(ValueError):p.pair(d)

    def test_wrong_seed_rejected(self):
        d=copy.deepcopy(self.raw);d["config"]["order_seed"]+=99
        with self.assertRaises(ValueError):p.pair(d)

    def test_calibration_value_forgery_rejected(self):
        d=copy.deepcopy(self.raw);d["equivalent_path_calibration"][0]["ready_over_boundary"]=0.1
        with self.assertRaises(ValueError):p.pair(d)

    def test_missing_cell_rejected(self):
        d=copy.deepcopy(self.raw);d["trials"].pop()
        with self.assertRaises(ValueError):p.pair(d)


if __name__=="__main__":unittest.main()

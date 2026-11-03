import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('prepare_cost_batch', ROOT/'scripts/prepare_decode_cost_batch.py')
batch = importlib.util.module_from_spec(spec); spec.loader.exec_module(batch)


class ScheduleTests(unittest.TestCase):
    def test_each_position_contains_each_policy_once(self):
        cells = batch.schedule('matrix')
        self.assertEqual(len(cells),36)
        self.assertEqual(len({c['cell_id'] for c in cells}),36)
        for position in range(6):
            self.assertEqual({c['policy'] for c in cells if c['position']==position},set(batch.POLICIES))
        self.assertTrue(all(c['trials']==1 and c['warmup']==1 for c in cells))

    def test_every_ordered_within_block_pair_appears_once(self):
        cells = batch.schedule('matrix');pairs=[]
        for block in range(6):
            row=[c['policy'] for c in cells if c['block']==block]
            self.assertEqual(set(row),set(batch.POLICIES))
            pairs.extend(zip(row,row[1:]))
        wanted={(a,b) for a in batch.POLICIES for b in batch.POLICIES if a!=b}
        self.assertEqual(len(pairs),30);self.assertEqual(set(pairs),wanted)

    def test_repeated_preparation_preserves_schedule(self):
        self.assertEqual(batch.schedule('matrix'),batch.schedule('matrix'))

    def test_smoke_has_six_trials_and_no_warmup(self):
        cells=batch.schedule('smoke')
        self.assertEqual([c['policy'] for c in cells],list(batch.POLICIES))
        self.assertTrue(all(c['trials']==1 and c['warmup']==0 for c in cells))

    def test_shape_limits_and_anchor_are_enforced(self):
        fixture=dict(a_token_ids=[0]*1024,b_token_ids=[0]*4096)
        valid=dict(a_prompt_length=64,b_prompt_length=1024,a_decode_steps=40,arrival_after_a_decode=4)
        batch.validate_shape(valid,fixture)
        for key,value in [('a_prompt_length',True),('a_prompt_length',1025),('b_prompt_length',4097),
                          ('a_decode_steps',41),('arrival_after_a_decode',40),('arrival_after_a_decode',0)]:
            with self.subTest(key=key,value=value),self.assertRaises(ValueError):
                batch.validate_shape(dict(valid,**{key:value}),fixture)
        with self.assertRaises(ValueError):batch.validate_shape(valid,dict(a_token_ids=[],b_token_ids=[0]*4096))


class CloneTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.template=self.root/'template';payload=self.template/'payload'
        source=payload/'experiments/tiny-decode-cost';source.mkdir(parents=True)
        for name in ('attempt.py','run.py','protocol.py','generated_runtime.py'):
            (source/name).write_text('# CPU-only fixture; never executed\n')
        (source/'fixture.json').write_text(json.dumps(dict(a_token_ids=list(range(64)),b_token_ids=list(range(1024)))))
        files={str(p.relative_to(payload)):dict(sha256=batch.sha(p),bytes=p.stat().st_size) for p in payload.rglob('*') if p.is_file()}
        self.external=self.root/'must-not-be-read-external';self.external.write_text('captured elsewhere')
        manifest=dict(kind='tiny-decode-cost-attempt',schema_version=1,created_at='fixture-time',
            runtime=dict(executable=sys.executable),scope=[],payload_files=files,
            external_dependencies=[dict(path=str(self.external),sha256=batch.sha(self.external))],
            generated_runtime_templates=[dict(module_name='CPU-test-only')],run_spec=dict(model='/not-loaded/model'))
        batch.save(self.template/'manifest.json',manifest)
        batch.save(self.template/'prepared.json',dict(manifest_sha256=batch.sha(self.template/'manifest.json'),model_or_gpu_loaded=False))
        self.shape=dict(a_prompt_length=64,b_prompt_length=1024,a_decode_steps=40,arrival_after_a_decode=4)

    def tearDown(self):
        for p in self.root.rglob('*'):
            if not p.is_symlink():p.chmod(0o755 if p.is_dir() else 0o644)
        self.temp.cleanup()

    def test_clone_has_same_source_independent_files_and_frozen_specs(self):
        result=batch.prepare(self.template,self.root/'out','smoke',self.shape)
        self.assertTrue(result['passed']);self.assertFalse(result['external_full_hashes_checked'])
        self.assertEqual((self.root/'out/template-manifest.json').read_bytes(),(self.template/'manifest.json').read_bytes())
        self.assertEqual((self.root/'out/template-prepared.json').read_bytes(),(self.template/'prepared.json').read_bytes())
        original=json.loads((self.template/'manifest.json').read_text())
        for cell in result['cells']:
            dest=Path(cell['path']);m=json.loads((dest/'manifest.json').read_text())
            self.assertEqual(m['payload_files'],original['payload_files'])
            self.assertEqual(m['external_dependencies'],original['external_dependencies'])
            self.assertEqual(m['run_spec'],dict(self.shape,policy=cell['policy'],trials=1,warmup=0,model='/not-loaded/model'))
            self.assertEqual(batch.sha(dest/'manifest.json'),cell['manifest_sha256'])
            self.assertFalse((dest/'execution').exists())
            for relative in m['payload_files']:
                self.assertNotEqual((dest/'payload'/relative).stat().st_ino,(self.template/'payload'/relative).stat().st_ino)

    def test_external_bytes_are_not_silently_reread_or_claimed(self):
        original_open=Path.open
        def guarded(path,*args,**kwargs):
            if path.resolve()==self.external:raise AssertionError('external full-byte read during clone')
            return original_open(path,*args,**kwargs)
        with patch.object(Path,'open',guarded):result=batch.prepare(self.template,self.root/'out','smoke',self.shape)
        self.assertFalse(result['external_full_hashes_checked'])

    def test_changed_source_is_rejected_before_destination(self):
        (self.template/'payload/experiments/tiny-decode-cost/run.py').write_text('changed')
        with self.assertRaises(ValueError):batch.prepare(self.template,self.root/'out','smoke',self.shape)
        self.assertFalse((self.root/'out').exists())

    def test_existing_destination_is_not_reused(self):
        dest=self.root/'out';dest.mkdir();(dest/'keep').write_text('unchanged')
        with self.assertRaises(FileExistsError):batch.prepare(self.template,dest,'smoke',self.shape)
        self.assertEqual((dest/'keep').read_text(),'unchanged')

    def test_unlisted_directory_symlink_is_rejected(self):
        outside=self.root/'outside';outside.mkdir();(outside/'unrelated').write_text('not part of source capture')
        (self.template/'payload/extra').symlink_to(outside,target_is_directory=True)
        with self.assertRaises(ValueError):batch.prepare(self.template,self.root/'out','smoke',self.shape)
        self.assertFalse((self.root/'out').exists())

    def test_partial_copy_failure_stays_failed(self):
        original=batch.shutil.copyfile
        def corrupt(source,target,*args,**kwargs):
            result=original(source,target,*args,**kwargs)
            if Path(target).name=='protocol.py':Path(target).write_text('corrupted clone')
            return result
        dest=self.root/'out'
        with patch.object(batch.shutil,'copyfile',corrupt),self.assertRaises(ValueError):
            batch.prepare(self.template,dest,'smoke',self.shape)
        self.assertFalse((dest/'prepared-index.json').exists())
        self.assertFalse(json.loads((dest/'preparation-error.json').read_text())['passed'])
        batch.verify_source(self.template)


if __name__=='__main__':unittest.main()

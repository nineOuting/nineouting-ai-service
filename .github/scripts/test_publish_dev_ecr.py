"""Offline failure-path tests: no registry, AWS authentication, or image pushes."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).with_name('publish-dev-ecr.sh').resolve()
MOCK = r'''#!/usr/bin/env python3
import json, os, sys
a=sys.argv[1:]; case=os.environ['CASE']
with open(os.environ['MOCK_LOG'],'a') as f:f.write(json.dumps(a)+'\n')
digest=lambda c:'sha256:'+c*64
manifest={'config':{'digest':digest('c')},'layers':[{'digest':digest('d')}]}
if a[:3]==['buildx','imagetools','create']:pass
elif a[:3]==['buildx','imagetools','inspect']:
 if '--raw' not in a:print('Digest: '+digest('e'))
 elif a[-1].endswith('@'+digest('a')):
  entries=[{'platform':{'os':'linux','architecture':'amd64'},'digest':digest('b')}]
  if case=='no-amd64':entries[0]['platform']['architecture']='arm64'
  if case=='duplicate-amd64':entries*=2
  print(json.dumps({'manifests':entries}))
 elif 'amazonaws.com/' in a[-1]:
  if case=='wrong-config':manifest['config']['digest']=digest('f')
  if case=='wrong-layers':manifest['layers'][0]['digest']=digest('f')
  print(json.dumps(manifest))
 else:print(json.dumps(manifest))
elif a[:2]==['image','inspect']:print('linux/arm64' if case=='wrong-platform' else 'linux/amd64')
elif a[0]=='pull':pass
else:sys.exit(1)
'''


class Publish(unittest.TestCase):
    def exercise(self, case, overrides=None):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake = root / 'docker'
            fake.write_text(MOCK)
            fake.chmod(0o755)
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'],
                       CASE=case, MOCK_LOG=str(root / 'calls'),
                       GITHUB_REPOSITORY='nineOuting/nineouting-ai-service', GITHUB_REF='refs/heads/develop',
                       GITHUB_EVENT_NAME='push', EXPECTED_ACCOUNT='443845329525', AWS_REGION='ap-northeast-2',
                       SOURCE_IMAGE='ghcr.io/nineouting/nineouting-ai-service', SOURCE_DIGEST='sha256:'+'a'*64,
                       TARGET_IMAGE='443845329525.dkr.ecr.ap-northeast-2.amazonaws.com/nineouting-dev-ai-service',
                       GITHUB_SHA='1'*40, GITHUB_RUN_ID='42', GITHUB_RUN_ATTEMPT='1',
                       GITHUB_OUTPUT=str(root/'output'), GITHUB_STEP_SUMMARY=str(root/'summary'))
            env.update(overrides or {})
            run = subprocess.run(['bash', str(SCRIPT)], cwd=root, env=env, capture_output=True, text=True)
            calls = [json.loads(x) for x in (root/'calls').read_text().splitlines()] if (root/'calls').exists() else []
            artifact = (root/'image-result.txt').read_text() if (root/'image-result.txt').exists() else ''
            outputs = (root/'output').read_text() if (root/'output').exists() else ''
            return run, calls, artifact, outputs

    def test_verified_image_outputs(self):
        run, calls, artifact, outputs = self.exercise('pass')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('image.digest=sha256:'+'e'*64, artifact)
        self.assertIn('image_digest=sha256:'+'e'*64, outputs)
        copy = next(c for c in calls if c[:3] == ['buildx','imagetools','create'])
        self.assertTrue(copy[-1].endswith('@sha256:'+'b'*64))
        self.assertIn('--prefer-index=false', copy)

    def test_no_or_ambiguous_amd64_never_pushes(self):
        for case in ['no-amd64','duplicate-amd64']:
            with self.subTest(case=case):
                run, calls, artifact, outputs = self.exercise(case)
                self.assertNotEqual(run.returncode, 0)
                self.assertFalse(any(c[:3] == ['buildx','imagetools','create'] for c in calls))
                self.assertEqual((artifact,outputs), ('',''))

    def test_wrong_image_never_emits_verified_result(self):
        for case in ['wrong-config','wrong-layers','wrong-platform']:
            with self.subTest(case=case):
                run, _, artifact, outputs = self.exercise(case)
                self.assertNotEqual(run.returncode, 0)
                self.assertEqual((artifact,outputs), ('',''))

    def test_wrong_context_never_contacts_registry(self):
        for overrides in [{'GITHUB_REF':'refs/heads/main'}, {'GITHUB_REPOSITORY':'nineOuting/other'},
                          {'SOURCE_DIGEST':'latest'}, {'EXPECTED_ACCOUNT':'000000000000'},
                          {'GITHUB_EVENT_NAME':'pull_request'}]:
            with self.subTest(overrides=overrides):
                run, calls, _, _ = self.exercise('pass', overrides)
                self.assertNotEqual(run.returncode, 0)
                self.assertEqual(calls, [])

    def test_manual_develop_run(self):
        run, _, _, outputs = self.exercise('pass', {'GITHUB_EVENT_NAME':'workflow_dispatch'})
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertTrue(outputs)


if __name__ == '__main__':
    unittest.main()

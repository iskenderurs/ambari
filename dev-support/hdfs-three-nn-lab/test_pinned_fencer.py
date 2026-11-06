#!/usr/bin/env python3
"""
Licensed to the Apache Software Foundation (ASF) under one
or more contributor license agreements.  See the NOTICE file
distributed with this work for additional information
regarding copyright ownership.  The ASF licenses this file
to you under the Apache License, Version 2.0 (the
"License"); you may not use this file except in compliance
with the License.  You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

"""
"""Target guards must reject bad identities before opening an SSH connection."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import pinned_fencer as f

PROFILE={'nameservice':'warehouse','targets':{'primary':{'host':'nn1.geo.test','rpc_port':8020}},
         'ssh_user':'hdfs','ssh_port':2200,'known_hosts':'/managed/known_hosts','identity_file':'/managed/key'}
TARGET={'target_nameserviceid':'warehouse','target_namenodeid':'primary','target_host':'nn1.geo.test','target_port':'8020'}

class PinnedFencerTests(unittest.TestCase):
    def test_invalid_targets_never_reach_ssh(self):
        invalid=[('target_nameserviceid','legacy'),('target_namenodeid','third'),
                 ('target_host','nn2.geo.test'),('target_port','22'),
                 ('target_host','nn1.geo.test;touch /tmp/unsafe'),('target_nameserviceid','')]
        for key,value in invalid:
            with self.subTest(key=key,value=value),patch.dict(os.environ,{**TARGET,key:value},clear=True),patch.object(f,'config',return_value=PROFILE),patch.object(f.subprocess,'run') as ssh:
                with self.assertRaises((RuntimeError,ValueError)):f.client('/managed/profile')
                ssh.assert_not_called()

    def test_ssh_failure_never_becomes_success(self):
        with patch.dict(os.environ,TARGET,clear=True),patch.object(f,'config',return_value=PROFILE),patch.object(f,'trusted'),patch.object(f.subprocess,'run') as ssh:
            ssh.return_value.returncode=255
            with self.assertRaisesRegex(RuntimeError,'remote fencing failed'):f.client('/managed/profile')
            argv=ssh.call_args.args[0]
            for required in ['StrictHostKeyChecking=yes','BatchMode=yes','IdentityAgent=none','ProxyCommand=none','UserKnownHostsFile=/managed/known_hosts']:
                self.assertIn(required,argv)
            self.assertEqual(ssh.call_args.kwargs['timeout'],12)

    def test_unmanaged_config_or_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            file=Path(directory)/'config';file.write_text('{}');file.chmod(0o666)
            link=Path(directory)/'link';link.symlink_to(file)
            for path in [file,link]:
                with self.assertRaises(RuntimeError):f.trusted(path)

if __name__=='__main__':unittest.main()

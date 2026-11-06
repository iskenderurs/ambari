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
"""Candidate SSH fencing profile. Qualified only in the synthetic lab."""
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import select
import shlex
import signal
import socket
import stat
import subprocess
import sys

CLASS=b'org.apache.hadoop.hdfs.server.namenode.NameNode'

def require(condition, reason):
    if not condition:
        raise RuntimeError(reason)

def trusted(path):
    path=Path(path)
    require(path.is_absolute(),'absolute managed path required')
    for p in [path,*path.parents]:
        s=p.lstat()
        require(s.st_uid==0 and not s.st_mode & 0o022 and not stat.S_ISLNK(s.st_mode),'untrusted managed path')
    return path

def config(path):
    return json.loads(trusted(path).read_text())

def label(value):
    require(isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',value),'invalid identity')
    return value

def client(path):
    c=config(path)
    ns=label(os.environ.get('target_nameserviceid',''))
    ident=label(os.environ.get('target_namenodeid',''))
    host=label(os.environ.get('target_host',''))
    port=int(os.environ.get('target_port','0'))
    require(ns==c['nameservice'],'wrong nameservice')
    target=c['targets'].get(ident)
    require(target is not None and (host,port)==(target['host'],target['rpc_port']),'target mismatch')
    trusted(c['known_hosts'])
    user=label(c['ssh_user'])
    command=shlex.join(['fence',ns,ident,str(port)])
    argv=['/usr/bin/ssh','-F','/dev/null','-n','-T','-p',str(c['ssh_port']),
          '-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
          '-o','GlobalKnownHostsFile=/dev/null','-o','UserKnownHostsFile='+c['known_hosts'],
          '-o','IdentitiesOnly=yes','-o','IdentityAgent=none',
          '-o','PasswordAuthentication=no','-o','KbdInteractiveAuthentication=no',
          '-o','ConnectionAttempts=1','-o','ConnectTimeout=3',
          '-o','ServerAliveInterval=2','-o','ServerAliveCountMax=2',
          '-o','ClearAllForwardings=yes','-o','PermitLocalCommand=no','-o','ProxyCommand=none',
          '-i',c['identity_file'],user+'@'+host,command]
    p=subprocess.run(argv,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL,timeout=12)
    require(p.returncode==0,'remote fencing failed')

def listening_inodes(port):
    result=set()
    for name in ('tcp','tcp6'):
        for line in (Path('/proc/net')/name).read_text().splitlines()[1:]:
            fields=line.split()
            if fields[3]=='0A' and int(fields[1].rsplit(':',1)[1],16)==port:
                result.add(fields[9])
    return result

def owner_sockets(pid):
    result=set()
    for fd in (Path('/proc')/str(pid)/'fd').iterdir():
        try:
            m=re.fullmatch(r'socket:\[(\d+)\]',os.readlink(fd))
            if m:result.add(m.group(1))
        except FileNotFoundError:
            pass
    return result

def absent(uid,port):
    require(not listening_inodes(port),'rpc listener without expected NameNode')
    for p in Path('/proc').iterdir():
        if not p.name.isdigit():continue
        try:
            require(not (p.stat().st_uid==uid and CLASS in (p/'cmdline').read_bytes().split(b'\0')),'NameNode still alive')
        except FileNotFoundError:
            pass

def remote(path):
    c=config(path)
    expected=['fence',c['nameservice'],c['namenode_id'],str(c['rpc_port'])]
    require(shlex.split(os.environ.get('SSH_ORIGINAL_COMMAND',''))==expected,'forced-command target mismatch')
    require(socket.getfqdn()==c['host'],'wrong local host')
    uid=pwd.getpwnam(c['user']).pw_uid
    require(os.getuid()==uid,'wrong remote uid')
    require(hashlib.sha256(Path(c['version_file']).read_bytes()).hexdigest()==c['version_sha256'],'namespace identity mismatch')
    pidfile=Path(c['pid_file'])
    port=c['rpc_port']
    if not pidfile.exists():
        # A process may be alive before binding RPC. Never certify it absent.
        absent(uid,port)
        return
    pid=int(pidfile.read_text().strip())
    require(pid>1,'invalid pid')
    # pidfd pins the exact process before identity validation and signal delivery.
    try:
        fd=os.pidfd_open(pid,0)
    except ProcessLookupError:
        absent(uid,port)
        return
    try:
        proc=Path('/proc')/str(pid)
        require(proc.stat().st_uid==uid,'wrong process owner')
        command=(proc/'cmdline').read_bytes()
        if not command:
            absent(uid,port)
            return
        require(CLASS in command.split(b'\0'),'wrong process class')
        listeners=listening_inodes(port)
        require(bool(listeners) and listeners <= owner_sockets(pid),'rpc listener belongs to another process')
        signal.pidfd_send_signal(fd,signal.SIGKILL,None,0)
        poll=select.poll();poll.register(fd,select.POLLIN)
        require(bool(poll.poll(5000)),'NameNode termination timeout')
        require(not listening_inodes(port),'rpc listener remains')
    finally:
        os.close(fd)

if __name__=='__main__':
    try:
        require(len(sys.argv)==3 and sys.argv[1] in ('client','remote'),'usage: pinned_fencer.py client|remote CONFIG')
        {'client':client,'remote':remote}[sys.argv[1]](sys.argv[2])
    except Exception as error:
        # No command, key material or credential-bearing stderr is emitted.
        print('fencing refused: '+str(error) if isinstance(error,RuntimeError) else 'fencing refused: '+type(error).__name__,file=sys.stderr)
        sys.exit(1)

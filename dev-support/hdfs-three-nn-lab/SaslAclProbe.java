/*
 * Licensed to the Apache Software Foundation (ASF) under one
 * or more contributor license agreements. See the NOTICE file
 * distributed with this work for additional information
 * regarding copyright ownership. The ASF licenses this file
 * to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance
 * with the License. You may obtain a copy of the License at
 *
 * http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing,
 * software distributed under the License is distributed on an
 * "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
 * KIND, either express or implied. See the License for the
 * specific language governing permissions and limitations
 * under the License.
 */

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import org.apache.zookeeper.CreateMode;
import org.apache.zookeeper.KeeperException;
import org.apache.zookeeper.Watcher.Event.KeeperState;
import org.apache.zookeeper.ZooDefs;
import org.apache.zookeeper.ZooKeeper;
import org.apache.zookeeper.data.ACL;
import org.apache.zookeeper.data.Id;
import org.apache.zookeeper.data.Stat;

/** Synthetic-only SASL identity, versioned ACL migration and denial checks. */
public final class SaslAclProbe {
  private interface Operation { void run() throws Exception; }
  private static void denied(Operation op) throws Exception {
    try { op.run(); throw new IllegalStateException("unexpected permission granted"); }
    catch (KeeperException.NoAuthException expected) { }
  }
  private static void visit(ZooKeeper zk, String path, List<String> nodes, boolean ephemeral) throws Exception {
    Stat s = zk.exists(path, false);
    if (s == null || (!ephemeral && s.getEphemeralOwner() != 0)) {
      throw new IllegalStateException("missing or ephemeral node in migration");
    }
    for (String child : zk.getChildren(path, false)) visit(zk, path + "/" + child, nodes, ephemeral);
    nodes.add(path);
  }
  public static void main(String[] args) throws Exception {
    String servers=args[0], mode=args[1], path=args[2];
    if (!servers.matches("nn[123]\\.geo\\.test:2181(,nn[123]\\.geo\\.test:2181)*")) {
      throw new IllegalArgumentException("synthetic endpoints only");
    }
    CountDownLatch connected=new CountDownLatch(1), authenticated=new CountDownLatch(1);
    try (ZooKeeper zk=new ZooKeeper(servers, 15000, event -> {
      if (event.getState()==KeeperState.SyncConnected) connected.countDown();
      if (event.getState()==KeeperState.SaslAuthenticated) authenticated.countDown();
    })) {
      if (!connected.await(15,TimeUnit.SECONDS)) throw new IllegalStateException("no connection");
      boolean sasl=Boolean.parseBoolean(System.getProperty("zookeeper.sasl.client","true"));
      if (sasl && !authenticated.await(15,TimeUnit.SECONDS)) {
        throw new IllegalStateException("SASL authentication not confirmed");
      }
      if (mode.equals("identity")) {
        zk.create(path,new byte[0],ZooDefs.Ids.CREATOR_ALL_ACL,CreateMode.PERSISTENT);
        List<ACL> acl=zk.getACL(path,new Stat());
        if (acl.size()!=1 || !acl.get(0).getId().equals(new Id("sasl","nn"))) {
          throw new IllegalStateException("unexpected normalized identity: "+acl);
        }
        zk.delete(path,-1);
        System.out.println("{\"normalized_identity\":\"sasl:nn\",\"sasl_authenticated\":true}");
      } else if (mode.equals("migrate")) {
        if (!path.equals("/hadoop-ha")) throw new IllegalArgumentException("wrong subtree");
        List<String> nodes=new ArrayList<>(); visit(zk,path,nodes,false);
        List<ACL> desired=Arrays.asList(new ACL(ZooDefs.Perms.ALL,new Id("sasl","nn")));
        for (String node : nodes) {
          Stat before=new Stat(); zk.getACL(node,before);
          zk.setACL(node,desired,before.getAversion());
          if (!zk.getACL(node,new Stat()).equals(desired)) throw new IllegalStateException("ACL mismatch");
        }
        System.out.println("{\"nodes_migrated\":"+nodes.size()+",\"version_guarded\":true}");
      } else if (mode.equals("deny")) {
        denied(() -> zk.getData(path,false,new Stat()));
        denied(() -> zk.setData(path,new byte[0],-1));
        denied(() -> zk.getChildren(path,false));
        denied(() -> zk.create(path+"/forbidden",new byte[0],ZooDefs.Ids.OPEN_ACL_UNSAFE,CreateMode.PERSISTENT));
        denied(() -> zk.getACL(path,new Stat()));
        denied(() -> zk.setACL(path,ZooDefs.Ids.OPEN_ACL_UNSAFE,-1));
        System.out.println("{\"denied_operations\":6,\"sasl_enabled\":"+sasl+"}");
      } else if (mode.equals("check")) {
        List<String> nodes=new ArrayList<>(); visit(zk,path,nodes,true);
        for (String node : nodes) {
          List<ACL> acl=zk.getACL(node,new Stat());
          if (acl.size()!=1 || acl.get(0).getPerms()!=31 || !acl.get(0).getId().equals(new Id("sasl","nn"))) {
            throw new IllegalStateException("unprotected node");
          }
        }
        System.out.println("{\"nodes_verified\":"+nodes.size()+"}");
      } else if (mode.equals("cas")) {
        Stat stat=new Stat(); List<ACL> acl=zk.getACL(path,stat);
        zk.setACL(path,acl,stat.getAversion());
        try { zk.setACL(path,ZooDefs.Ids.OPEN_ACL_UNSAFE,stat.getAversion());
          throw new IllegalStateException("stale ACL version was accepted");
        } catch (KeeperException.BadVersionException expected) { }
        if (!zk.getACL(path,new Stat()).equals(acl)) throw new IllegalStateException("ACL changed");
        System.out.println("{\"stale_acl_update_rejected\":true}");
      } else throw new IllegalArgumentException("unknown probe");
    }
  }
}

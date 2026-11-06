/*
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

*/

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.nio.file.StandardCopyOption;
import java.security.MessageDigest;
import org.apache.hadoop.conf.Configuration;
import org.apache.hadoop.fs.FileSystem;
import org.apache.hadoop.fs.FSDataInputStream;
import org.apache.hadoop.fs.FSDataOutputStream;
import org.apache.hadoop.fs.Path;
import org.apache.hadoop.security.UserGroupInformation;

/** A single authenticated FileSystem instance retained across HA transitions. */
public final class RetainedClient {
  public static void main(String[] args) throws Exception {
    Configuration conf = new Configuration();
    if (!"hdfs://warehouse".equals(conf.get("fs.defaultFS")) ||
        !"kerberos".equals(conf.get("hadoop.security.authentication"))) {
      throw new IllegalArgumentException("synthetic namespace guard failed");
    }
    UserGroupInformation.setConfiguration(conf);
    UserGroupInformation.loginUserFromKeytab("hdfs@GEO.TEST", "/etc/security/keytabs/hdfs.headless.keytab");
    int successes = 0;
    int failures = 0;
    java.nio.file.Path dir = Paths.get("/var/tmp/geo-client");
    Files.createDirectories(dir);
    String expected = args[0];
    try (FileSystem fs = FileSystem.get(conf)) {
      for (int i = 0; i < 3600 && !Files.exists(dir.resolve("stop")); i++) {
        try {
          MessageDigest digest = MessageDigest.getInstance("SHA-256");
          try (FSDataInputStream in = fs.open(new Path("/lab/control.bin"))) {
            byte[] buf = new byte[8192];
            for (int n; (n = in.read(buf)) != -1;) digest.update(buf, 0, n);
          }
          StringBuilder hash = new StringBuilder();
          for (byte b : digest.digest()) hash.append(String.format("%02x", b & 255));
          if (!expected.equals(hash.toString())) throw new IllegalStateException("control data mismatch");
          Path test = new Path("/lab/client-" + System.currentTimeMillis() + "-" + i);
          byte[] data = new byte[65536];
          java.util.Arrays.fill(data, (byte) (i & 127));
          try (FSDataOutputStream out = fs.create(test, false)) { out.write(data); }
          byte[] actual = new byte[data.length];
          try (FSDataInputStream in = fs.open(test)) { in.readFully(actual); }
          if (!java.util.Arrays.equals(data, actual)) throw new IllegalStateException("new block mismatch");
          successes++;
        } catch (java.io.IOException e) {
          failures++;
          System.err.println("I/O failure class: " + e.getClass().getSimpleName());
        }
        String status = "{\"successes\":" + successes + ",\"failures\":" + failures +
          ",\"last_update_ms\":" + System.currentTimeMillis() + ",\"single_filesystem_instance\":true}";
        Files.write(dir.resolve("status.tmp"), status.getBytes(StandardCharsets.UTF_8));
        Files.move(dir.resolve("status.tmp"), dir.resolve("status.json"), StandardCopyOption.REPLACE_EXISTING);
        Thread.sleep(2000);
      }
    }
  }
}

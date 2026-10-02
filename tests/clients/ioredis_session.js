// A short session with ioredis: it waits for `ready` (which makes it send INFO), then a handful of commands, a pipeline and a transaction-free multi-key read.
const Redis = require("ioredis");
const port = parseInt(process.argv[2], 10);
const r = new Redis({ port, maxRetriesPerRequest: 1, retryStrategy: () => null, connectionName: "ioredis-test" });
const fail = (m) => { console.log("FAIL", m); process.exit(1); };
const timer = setTimeout(() => fail("not ready in 4 seconds"), 4000);
r.on("error", (e) => fail("error event: " + e.message));
r.on("ready", async () => {
  clearTimeout(timer);
  const eq = (a, b, what) => { if (JSON.stringify(a) !== JSON.stringify(b)) fail(what + ": " + JSON.stringify(a) + " != " + JSON.stringify(b)); };
  eq(await r.set("io:a", "1"), "OK", "set");
  eq(await r.get("io:a"), "1", "get");
  eq(await r.incr("io:a"), 2, "incr");
  eq(await r.set("io:b", "x", "EX", 100), "OK", "set EX");
  eq(await r.mget("io:a", "io:b", "io:none"), ["2", "x", null], "mget");
  const res = await r.pipeline().set("io:c", "y").get("io:c").expire("io:c", 100).ttl("io:c").exec();
  eq(res.map((x) => x[0]), [null, null, null, null], "pipeline errors");
  eq(res.map((x) => x[1]), ["OK", "y", 1, 100], "pipeline results");
  eq(await r.client("GETNAME"), "ioredis-test", "connectionName was set on connect");
  eq(await r.del("io:a", "io:b", "io:c"), 3, "del");
  console.log("ioredis ok");
  r.disconnect();
});
